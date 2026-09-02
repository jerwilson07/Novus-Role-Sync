import discord
from discord import app_commands
from discord.ext import commands
import sqlite3
import logging
from typing import Optional
from cogs.general_cog import PlayerListView
import asyncio

logger = logging.getLogger('WOMBot')

class BroadcastConfirmationView(discord.ui.View):
    def __init__(self, author, message_content, bot):
        super().__init__(timeout=60.0)
        self.author = author
        self.message_content = message_content
        self.bot = bot
        self.value = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author.id

    @discord.ui.button(label="Confirm Broadcast", style=discord.ButtonStyle.green, emoji="🚀")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        
        # Disable buttons
        for item in self.children:
            item.disabled = True
        await interaction.edit_original_response(content="⏳ Broadcasting message to all servers...", view=self)

        conn = sqlite3.connect('wom_multi.db')
        c = conn.cursor()
        c.execute("SELECT log_channel_id FROM guild_configs WHERE log_channel_id IS NOT NULL AND inactive_since IS NULL")
        log_channel_ids = c.fetchall()
        conn.close()

        sent_count = 0
        failed_count = 0

        for (channel_id,) in log_channel_ids:
            channel = self.bot.get_channel(channel_id)
            if channel:
                try:
                    await channel.send(self.message_content)
                    sent_count += 1
                except discord.Forbidden:
                    logger.warning(f"Failed to send broadcast to channel {channel_id}. Missing permissions.")
                    failed_count += 1
                except Exception as e:
                    logger.error(f"Failed to send broadcast to channel {channel_id}: {e}")
                    failed_count += 1
            else:
                logger.warning(f"Could not find log channel with ID {channel_id} for broadcast.")
                failed_count += 1
            await asyncio.sleep(0.5)

        self.value = True
        self.stop()
        
        await interaction.edit_original_response(
            content=f"✅ Broadcast complete.\nSent to **{sent_count}** servers.\nFailed for **{failed_count}** servers."
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.value = False
        self.stop()
        
        for item in self.children:
            item.disabled = True
        await interaction.edit_original_response(content="❌ Broadcast cancelled.", view=self)

class OwnerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Only listen to DMs from the bot owner
        if message.guild is not None or not await self.bot.is_owner(message.author) or message.author.bot:
            return
        
        # Ignore commands
        if message.content.startswith(self.bot.command_prefix):
            return

        embed = discord.Embed(
            title="Confirm Broadcast",
            description="Are you sure you want to broadcast the following message to ALL configured log channels?",
            color=discord.Color.orange()
        )
        embed.add_field(name="Message Content", value=f"```\n{message.content}\n```", inline=False)
        embed.set_footer(text="This action is irreversible.")

        view = BroadcastConfirmationView(message.author, message.content, self.bot)
        
        confirm_msg = await message.channel.send(embed=embed, view=view)

        await view.wait()

        # Handle timeout
        if view.value is None:
            for item in view.children:
                item.disabled = True
            await confirm_msg.edit(content="Timed out. Broadcast cancelled.", embed=None, view=view)

    @app_commands.command(name="playerlist", description="Get a list of all linked players for this server")
    @app_commands.checks.has_permissions(administrator=True)
    async def playerlist(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        conn = sqlite3.connect('wom_multi.db')
        c = conn.cursor()
        c.execute("SELECT group_id FROM guild_configs WHERE guild_id = ?", (interaction.guild_id,))
        config = c.fetchone()

        if not config or not config[0]:
            conn.close()
            await interaction.followup.send("No WOM Group ID configured for this server. Use `/groupid` to set it.")
            return

        group_id = config[0]
        
        c.execute("SELECT discord_id, rsn FROM links WHERE guild_id = ?", (interaction.guild_id,))
        links = c.fetchall()
        conn.close()

        if not links:
            await interaction.followup.send("No players have been linked in this server yet. Use `/linkuser` to add one.")
            return

        view = PlayerListView(interaction, links, interaction.guild.name, group_id)
        embed = await view.get_embed()
        view.update_buttons()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="sync", description="Force sync (Developer Only)")
    @app_commands.describe(group_id="Optional: The Group ID to force sync.")
    @app_commands.checks.has_permissions(administrator=True)
    async def manual_sync(self, interaction: discord.Interaction, group_id: Optional[int] = None):
        if interaction.user.id != self.bot.owner_id:
            return await interaction.response.send_message("Restricted to Bot Developer.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        tasks_cog = self.bot.get_cog('TasksCog')
        if not tasks_cog:
            return await interaction.followup.send("Tasks cog is not loaded.")

        if group_id:
            conn = sqlite3.connect('wom_multi.db')
            c = conn.cursor()
            c.execute("SELECT guild_id, log_channel_id, nickname_enforcement, dm_notifications_on FROM guild_configs WHERE group_id = ?", (group_id,))
            config = c.fetchone()
            conn.close()

            if config:
                target_guild_id, log_channel_id, nickname_enforcement, dm_notifications_on = config[0], config[1], config[2], config[3]
                guild = self.bot.get_guild(target_guild_id)
                if guild:
                    synced, failed, checked = await tasks_cog.sync_guild(guild, group_id, log_channel_id, nickname_enforcement, dm_notifications_on)
                    logger.info(f"Manual sync finished for Group ID {group_id}. {checked} members checked, {synced} updated, {failed} failed.")
                    await interaction.followup.send(f"Sync finished for Group ID **{group_id}** in guild **{guild.name}**.\n"
                                                      f"Checked: `{checked}`\n"
                                                      f"Updated: `{synced}`\n"
                                                      f"Failed: `{failed}`")
                else:
                    await interaction.followup.send(f"Bot is not in the guild associated with Group ID **{group_id}** (Guild ID: {target_guild_id}). Sync cannot be performed.")
            else:
                await interaction.followup.send(f"No guild found configured for Group ID **{group_id}**.")
        else:
            conn = sqlite3.connect('wom_multi.db')
            c = conn.cursor()
            c.execute("SELECT group_id, log_channel_id, nickname_enforcement, dm_notifications_on FROM guild_configs WHERE guild_id = ?", (interaction.guild_id,))
            res = c.fetchone()
            conn.close()

            if res:
                group_id, log_channel_id, nickname_enforcement, dm_notifications_on = res[0], res[1], res[2], res[3]
                synced, failed, checked = await tasks_cog.sync_guild(interaction.guild, group_id, log_channel_id, nickname_enforcement, dm_notifications_on)
                logger.info(f"Manual sync finished for guild {interaction.guild.name}. {checked} members checked, {synced} updated, {failed} failed.")
                await interaction.followup.send(f"Sync finished for **{interaction.guild.name}**.\n"
                                                  f"Checked: `{checked}`\n"
                                                  f"Updated: `{synced}`\n"
                                                  f"Failed: `{failed}`")
            else:
                await interaction.followup.send("No Group ID set for this server.")

async def setup(bot: commands.Bot):
    await bot.add_cog(OwnerCog(bot))
