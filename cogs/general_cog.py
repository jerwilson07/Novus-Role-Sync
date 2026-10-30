import discord
from discord import app_commands
from discord.ext import commands
import sqlite3
import datetime
from typing import List

from common import (
    WOM_API_KEY, has_novus_member_role, is_active_novus_member_link
)


async def get_current_wom_memberships(bot, group_id):
    """Return current WOM group memberships or None on an API error."""
    headers = {"User-Agent": "NovusRoleSync/1.0"}
    if WOM_API_KEY:
        headers["x-api-key"] = WOM_API_KEY
    try:
        async with bot.http_session.get(
            f"https://api.wiseoldman.net/v2/groups/{group_id}",
            headers=headers, timeout=30
        ) as response:
            if response.status != 200:
                return None
            data = await response.json()
        if not isinstance(data, dict) or not isinstance(data.get("memberships"), list):
            return None
        return data["memberships"]
    except Exception:
        return None



# Novus staff command access
COUNCIL_ROLE_ID = 1532830356875513906
MODERATOR_ROLE_ID = 1532830960062693396
TRIAL_MOD_ROLE_ID = 1533587986204069908

NOVUS_COMMAND_ROLE_IDS = {
    COUNCIL_ROLE_ID,
    MODERATOR_ROLE_ID,
    TRIAL_MOD_ROLE_ID,
}


def has_novus_command_permission(member: discord.Member) -> bool:
    return any(
        role.id in NOVUS_COMMAND_ROLE_IDS
        for role in member.roles
    )

class PlayerListView(discord.ui.View):
    def __init__(self, interaction: discord.Interaction, links: List[tuple], guild_name: str, group_id: int):
        super().__init__(timeout=180)
        self.interaction = interaction
        self.links = links
        self.guild_name = guild_name
        self.group_id = group_id
        self.current_page = 0
        self.items_per_page = 10

    async def get_embed(self) -> discord.Embed:
        start_index = self.current_page * self.items_per_page
        end_index = start_index + self.items_per_page
        page_links = self.links[start_index:end_index]

        embed = discord.Embed(
            title=f"Linked Players for {self.guild_name} (Group {self.group_id})",
            color=discord.Color.blue()
        )

        description = ""
        for discord_id, rsn in page_links:
            user = self.interaction.guild.get_member(discord_id)
            user_name = str(user) if user else "Unknown User"
            description += f"• {user_name} ({discord_id}) - **{rsn}**\n"
        
        if not description:
            description = "No linked players on this page."

        embed.description = description
        embed.set_footer(text=f"Page {self.current_page + 1} of {self.get_max_pages()}")
        return embed

    def get_max_pages(self) -> int:
        return (len(self.links) + self.items_per_page - 1) // self.items_per_page

    async def update_message(self, interaction: discord.Interaction):
        embed = await self.get_embed()
        self.update_buttons()
        await interaction.response.edit_message(embed=embed, view=self)

    def update_buttons(self):
        max_pages = self.get_max_pages()
        self.children[0].disabled = self.current_page == 0
        self.children[1].disabled = self.current_page == max_pages - 1

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.grey)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
        await self.update_message(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.grey)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page < self.get_max_pages() - 1:
            self.current_page += 1
        await self.update_message(interaction)

class InfoView(discord.ui.View):
    def __init__(self, interaction: discord.Interaction):
        super().__init__(timeout=180)
        self.interaction = interaction

    @discord.ui.button(label="View Linked Users", style=discord.ButtonStyle.primary)
    async def view_linked_users(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        conn = sqlite3.connect('wom_multi.db')
        c = conn.cursor()
        c.execute("SELECT group_id FROM guild_configs WHERE guild_id = ?", (interaction.guild_id,))
        config = c.fetchone()

        if not config or not config[0]:
            conn.close()
            await interaction.followup.send("No WOM Group ID configured for this server. Use `/groupid` to set it.", ephemeral=True)
            return

        group_id = config[0]
        
        c.execute(
            "SELECT discord_id, rsn, wom_id FROM links WHERE guild_id = ?",
            (interaction.guild_id,)
        )
        stored_links = c.fetchall()
        conn.close()

        memberships = await get_current_wom_memberships(
            interaction.client, group_id
        )
        if memberships is None:
            await interaction.followup.send(
                "Could not verify current Novus membership with WOM. "
                "No cached member list was shown.", ephemeral=True
            )
            return

        links = [
            (discord_id, rsn)
            for discord_id, rsn, wom_id in stored_links
            if is_active_novus_member_link(
                interaction.guild.get_member(discord_id),
                rsn, wom_id, memberships
            )
        ]

        if not links:
            await interaction.followup.send(
                "No actively linked Novus clan members were found. "
                "Members must appear in WOM and have the Discord Member role.",
                ephemeral=True
            )
            return

        view = PlayerListView(interaction, links, interaction.guild.name, group_id)
        embed = await view.get_embed()
        view.update_buttons()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class GeneralCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="How to set up the bot")
    async def help_cmd(self, interaction: discord.Interaction):
        embed = discord.Embed(title="Setup Guide", color=discord.Color.green())
        embed.add_field(name="1. Wise Old Man Setup", value="Your clan must use the **[WOM Runelite Plugin](https://runelite.net/plugin-hub/show/wom-utils)**. After configuring the plugin, open the Clan Chat tab in-game and click \"Sync WOM Group\" to ensure ranks are up to date on the website.", inline=False)
        embed.add_field(name="2. Find Group ID", value="Go to your group page on https://wiseoldman.net/. The ID is the numbers at the end of the URL (e.g., `.../groups/1234`; your Group ID would be 1234).", inline=False)
        embed.add_field(name="3. Organise Roles", value="Ensure that this bot's role is above the Discord roles you wish to assign.", inline=False)
        embed.add_field(name="4. Commands", value="`/groupid [id]` - Set your clan ID\n"
                                                  "`/logchannel #channel` - Set a channel to log role changes\n"
                                                  "`/linkuser @user [rsn]` - Link/Update a member\n"
                                                  "`/unlinkuser @user` - Unlink a member\n"
                                                  "`/linkrole [wom_role] [discord_role]` - Map WOM Group Role to Discord Role\n"
                                                  "`/unlinkrole [wom_role]` - Remove a role mapping\n"
                                                  "`/nickname on/off` - Toggle forcing member nicknames to their RSN\n"
                                                  "`/reminder off/3d/..` - Set inactivity reminder timer\n"
                                                  "`/notifyplayers on/off` - Toggle role change DMs for all players\n"
                                                  "`/notifyme on/off` - Toggle personal role change DMs\n"
                                                  "`/playerlist` - View linked players\n"
                                                  "`/checkuser @user` - Check a user's linked RSN\n"
                                                  "`/info` - View configuration and sync status", inline=False)
        embed.add_field(name="Need further help?", value="[Join the support Discord](https://discord.gg/T6j59QC2kh)\n[List of WOM Group Roles](https://docs.wiseoldman.net/api/groups/group-type-definitions#object-membership)", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="checkuser", description="Check the RSN linked to a Discord user")
    @app_commands.describe(user="The user to check")
    async def checkuser(self, interaction: discord.Interaction, user: discord.Member):
        if not has_novus_command_permission(interaction.user):
            await interaction.response.send_message(
                "You must be a Trial Moderator, Moderator, or Council member to use this command.",
                ephemeral=True
            )
            return

        if not has_novus_member_role(user):
            await interaction.response.send_message(
                f"{user.mention} is not an active Novus member. "
                "The Discord Member role is required.",
                ephemeral=True
            )
            return

        conn = sqlite3.connect("wom_multi.db")
        try:
            c = conn.cursor()
            c.execute(
                "SELECT group_id FROM guild_configs WHERE guild_id = ?",
                (interaction.guild_id,)
            )
            row = c.fetchone()
            group_id = row[0] if row else None
            c.execute(
                "SELECT rsn, wom_id FROM links WHERE guild_id = ? AND discord_id = ?",
                (interaction.guild_id, user.id)
            )
            link = c.fetchone()
        finally:
            conn.close()

        if not group_id:
            await interaction.response.send_message(
                "No WOM group is configured.", ephemeral=True
            )
            return
        if not link:
            await interaction.response.send_message(
                f"{user.mention} does not have an active Novus account link.",
                ephemeral=True
            )
            return

        memberships = await get_current_wom_memberships(
            interaction.client, group_id
        )
        if memberships is None:
            await interaction.response.send_message(
                "Could not verify the current Novus WOM roster.",
                ephemeral=True
            )
            return
        if not is_active_novus_member_link(user, link[0], link[1], memberships):
            await interaction.response.send_message(
                f"{user.mention} is not in the current Novus WOM group "
                "and is excluded from Novus member reports.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"{user.mention} is linked to the Novus RSN: **{link[0]}**",
            ephemeral=True
        )

    @app_commands.command(name="info", description="View configuration and sync status")
    async def info(self, interaction: discord.Interaction):
        if not has_novus_command_permission(interaction.user):
            await interaction.response.send_message(
                "You must be a Trial Moderator, Moderator, or Council member to use this command.",
                ephemeral=True
            )
            return

        conn = sqlite3.connect('wom_multi.db')
        c = conn.cursor()
        c.execute("SELECT group_id, last_sync FROM guild_configs WHERE guild_id = ?", (interaction.guild_id,))
        config = c.fetchone()

        c.execute("SELECT wom_role, discord_role_id FROM role_mappings WHERE guild_id = ?", (interaction.guild_id,))
        mappings = c.fetchall()
        conn.close()

        gid = config[0] if config else "None"
        
        if config and config[1]:
            try:
                sync_time = datetime.datetime.fromisoformat(config[1])
                lsync = f"<t:{int(sync_time.timestamp())}:F>"
            except (ValueError, TypeError):
                lsync = "Invalid format ([Report Here](https://discord.gg/T6j59QC2kh))"
        else:
            lsync = "Never"

        mapping_text = "\n".join([f"• {w}: <@&{r}>" for w, r in mappings]) if mappings else "No roles mapped yet."

        embed = discord.Embed(title="Server Sync Status", color=discord.Color.blue())
        embed.add_field(name="Group ID", value=gid, inline=True)
        embed.add_field(name="Last Sync", value=lsync, inline=True)
        embed.add_field(name="Role Mappings", value=mapping_text, inline=False)
        
        view = InfoView(interaction)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="notifyme", description="Turn on/off personal DM notifications for role changes.")
    @app_commands.describe(state="Your preferred state for DM notifications")
    @app_commands.choices(
        state=[
            app_commands.Choice(name="on", value="on"),
            app_commands.Choice(name="off", value="off"),
        ]
    )
    async def notifyme(self, interaction: discord.Interaction, state: app_commands.Choice[str]):
        """Toggles personal role change DM notifications for the user."""
        guild_id = interaction.guild_id
        user_id = interaction.user.id
        new_state = 1 if state.value == "on" else 0

        conn = sqlite3.connect('wom_multi.db')
        c = conn.cursor()

        # Check if user is linked
        c.execute("SELECT 1 FROM links WHERE guild_id = ? AND discord_id = ?", (guild_id, user_id))
        if c.fetchone() is None:
            conn.close()
            await interaction.response.send_message("You are not linked to an RSN in this server. An admin must link you with `/linkuser` first.", ephemeral=True)
            return

        c.execute("UPDATE links SET dm_notifications_on = ? WHERE guild_id = ? AND discord_id = ?", (new_state, guild_id, user_id))
        conn.commit()
        conn.close()

        if new_state == 1:
            await interaction.response.send_message("✅ You will now receive DMs when your roles change in this server.", ephemeral=True)
        else:
            await interaction.response.send_message("✅ You will no longer receive DMs about role changes in this server.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(GeneralCog(bot))
