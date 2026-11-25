"""Replacement Novus Verify commands; no direct tenure or WOM-link writes."""
import asyncio
import datetime
import logging
import re
import sqlite3

import discord
from novus_logging import check, notice, bind_novus_ui, _dismiss_private, start_logged_task
from discord import app_commands
from discord.ext import commands

from common import GUEST_ROLE_ID, CLAN_FRIEND_ROLE_ID, sanitize_rsn
from cogs.tasks_cog import (
    NOVUS_GUILD_ID, NOVUS_COMMAND_ROLE_IDS, NOVUS_TENURE_ROLES,
    NOVUS_MOD_CHAT_CHANNEL_ID,
)

logger = logging.getLogger("WOMBot")


def unique_role(guild, name):
    matches = [r for r in guild.roles if r.name.casefold() == name.casefold()]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one Discord role named {name}.")
    return matches[0]


def parse_rsn(value):
    parts = [p.strip() for p in re.split(r"[|/]", value)]
    if any(not re.fullmatch(r"[A-Za-z0-9 _-]{1,12}", p) or not p.strip(" _-")
           for p in parts):
        raise ValueError("Each RSN must be 1–12 valid OSRS characters.")
    keys = [" ".join(p.replace("_", " ").replace("-", " ").split()).casefold()
            for p in parts]
    if len(set(keys)) != len(keys):
        raise ValueError("Please enter each account only once.")
    nickname = re.sub(r"\s*([|/])\s*", r" \1 ", value.strip())
    if len(nickname) > 32:
        raise ValueError("The combined nickname must be 32 characters or fewer.")
    return nickname


class VerifyModal(discord.ui.Modal, title="Novus Account Verification"):
    username = discord.ui.TextInput(
        label="OSRS username(s) - primary first",
        placeholder="Minor Mohi | Echo Mohi",
        min_length=1,
        max_length=32,
        required=True,
    )

    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.verify_rsn(interaction, str(self.username.value))


class VerifyView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog
        self.verify_button.emoji = check()

    @discord.ui.button(
        label="Click Here to Verify Your RSN",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="novus:verify:rsn:v1",
    )
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild_id != NOVUS_GUILD_ID:
            await notice(interaction, False, "This button is only available in Novus.", ephemeral=True)
            return
        await interaction.response.send_modal(VerifyModal(self.cog))


PANEL_TEXT = None


class VerifyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.locks = {}
        self.panel_lock = asyncio.Lock()
        self.panel_checked = False
        self.panel_message_id = None
        self.panel_channel_id = None
        self.restore_task = None
        self.restore_generation = 0

    def lock(self, member):
        return self.locks.setdefault(member.id, asyncio.Lock())

    async def log_approval(self, guild, actor, member, already, *, was_trial=False, pathway=None):
        channel = guild.get_channel(NOVUS_MOD_CHAT_CHANNEL_ID)
        if channel is None:
            logger.warning("Approval log channel unavailable")
            return False
        try:
            reviewed = already and not was_trial
            embed = discord.Embed(
                title="Membership Reviewed" if reviewed else f"{check()} Membership Approved",
                color=discord.Color.blurple() if reviewed else discord.Color.green(),
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            )
            embed.add_field(name="Member", value=member.mention+"\n\u200b", inline=False)
            if not reviewed:
                embed.add_field(name="Pathway", value=(pathway or "Manual staff approval (/approve)")+"\n\u200b", inline=False)
            embed.add_field(name="Reviewed by" if reviewed else "Approved by", value=actor.mention+"\n\u200b", inline=False)
            status = "Existing member • No promotion required" if reviewed else (f"{check()} Trial Member → Member" if was_trial else f"{check()} Member access granted")
            embed.add_field(name="Status", value=status, inline=False)
            embed.set_footer(text="Novus Membership")
            logo = guild.icon or self.bot.user.display_avatar
            embed.set_thumbnail(url=logo.url)
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            return True
        except discord.HTTPException:
            logger.exception("Could not log approval for %s", member.id)
            return False

    @app_commands.command(name="rsn", description="Set your server RSN and receive the Verified role.")
    @app_commands.describe(username="Your exact OSRS username; separate additional accounts with | or /")
    @app_commands.guild_only()
    async def rsn(self, interaction: discord.Interaction, username: str):
        await self.verify_rsn(interaction, username)

    async def verify_rsn(self, interaction: discord.Interaction, username: str):
        guild = interaction.guild
        if guild is None or guild.id != NOVUS_GUILD_ID:
            await notice(interaction, False, "This command is only available in Novus.", ephemeral=True)
            return
        try:
            nickname = parse_rsn(username)
            verified = unique_role(guild, "Verified")
            guest = guild.get_role(GUEST_ROLE_ID)
            if guest is None:
                raise ValueError("Guest role is not configured.")
        except ValueError as exc:
            await notice(interaction, False, str(exc), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            async with self.lock(interaction.user):
                member = await guild.fetch_member(interaction.user.id)
                submitted = [sanitize_rsn(part).casefold() for part in re.split(r"[|/]", nickname)]
                with sqlite3.connect("wom_multi.db") as conn:
                    rows = conn.execute(
                        "SELECT rsn FROM links WHERE guild_id=? AND discord_id!=? "
                        "UNION ALL SELECT rsn FROM alt_links WHERE guild_id=? AND discord_id!=?",
                        (guild.id, member.id, guild.id, member.id),
                    ).fetchall()
                    own = conn.execute(
                        "SELECT rsn FROM links WHERE guild_id=? AND discord_id=?",
                        (guild.id, member.id),
                    ).fetchone()
                claimed = {sanitize_rsn(row[0]).casefold() for row in rows if row[0]}
                if claimed.intersection(submitted):
                    await notice(interaction, True, "An entered RSN is already linked to another Discord account. Please contact staff.", ephemeral=True)
                    return
                if own and own[0] and sanitize_rsn(own[0]).casefold() != submitted[0]:
                    await notice(interaction, True, "Your saved primary account differs from the submitted RSN. If you changed your in-game name, allow WOM to update your link first. To switch primary accounts, contact staff.", ephemeral=True)
                    return
                was_verified = verified in member.roles
                role_names = {role.name.strip().casefold() for role in member.roles}
                if member.nick != nickname:
                    await member.edit(nick=nickname, reason="Novus /rsn")
                if verified not in member.roles:
                    await member.add_roles(verified, reason="Novus /rsn")
                if guest in member.roles:
                    await member.remove_roles(guest, reason="Novus /rsn")
        except discord.HTTPException:
            logger.exception("RSN verification failed for %s", interaction.user.id)
            await notice(interaction, True, "Could not complete verification. Please contact staff or retry; some changes may already have applied.", ephemeral=True)
            return
        view = discord.ui.View()
        rsn = discord.utils.escape_markdown(nickname)
        if "member" in role_names:
            title = "RSN Updated"
            description = f"**RSN:** {rsn}\n\nYour nickname is synchronized and your existing clan roles and tenure are preserved."
        elif "trial member" in role_names:
            title = "RSN Updated"
            description = f"**RSN:** {rsn}\n\nYour Trial Member status is unchanged. Visit the trial hub for your next steps."
            view.add_item(discord.ui.Button(
                label="Trial Member Hub",
                url=f"https://discord.com/channels/{guild.id}/1544747251983843349",
            ))
        else:
            title = "RSN Updated" if was_verified else "Verification Successful!"
            description = (
                f"**RSN:** {rsn}\n**Server access:** Verified\n\n"
                "Ready to join Novus? Review our requirements and submit an application."
            )
            view.add_item(discord.ui.Button(
                label="Info & Apply",
                emoji="📋",
                url=f"https://discord.com/channels/{guild.id}/1537889319220551753",
            ))
        embed = discord.Embed(title=title, description=description, color=discord.Color.green())
        embed.set_footer(text="Novus Membership • Account Verification")
        logo = guild.icon or self.bot.user.display_avatar
        embed.set_thumbnail(url=logo.url)
        kwargs = {"view": view} if view.children else {}
        confirmation = await notice(interaction, True, embed=embed, ephemeral=True, wait=True, **kwargs)
        if confirmation is not None and confirmation.flags.ephemeral:
            start_logged_task(_dismiss_private(confirmation), name="novus-rsn-confirmation-dismiss")
        application_cog = self.bot.get_cog("ApplicationCog")
        if application_cog is not None:
            try:
                await application_cog.cache_verified(guild.id, member.id, nickname)
            except Exception:
                logger.exception("Could not cache verified applicant KC")

    @commands.Cog.listener()
    async def on_ready(self):
        await self.ensure_panel(force=True)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload):
        if payload.guild_id == NOVUS_GUILD_ID and payload.message_id == self.panel_message_id:
            self.schedule_restore()

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload):
        if payload.guild_id == NOVUS_GUILD_ID and self.panel_message_id in payload.message_ids:
            self.schedule_restore()

    @staticmethod
    def is_welcome_post(message):
        if not (message.author.bot or message.webhook_id):
            return False
        for embed in message.embeds:
            title = (embed.title or "").casefold()
            if any(text in title for text in ("welcome to novus", "verify your rsn", "spread the love")):
                return True
        return False

    @commands.Cog.listener()
    async def on_message(self, message):
        if (
            message.guild is not None
            and message.guild.id == NOVUS_GUILD_ID
            and message.channel.id == self.panel_channel_id
            and message.author.id != self.bot.user.id
            and self.is_welcome_post(message)
        ):
            self.schedule_restore()

    def schedule_restore(self):
        self.restore_generation += 1
        if self.restore_task is None or self.restore_task.done():
            self.restore_task = asyncio.create_task(self.restore_panel())

    async def restore_panel(self):
        # Debounce the entire batch of republished welcome embeds.
        while True:
            generation = self.restore_generation
            await asyncio.sleep(20)
            if generation != self.restore_generation:
                continue
            await self.ensure_panel(force=True)
            if generation == self.restore_generation:
                return

    def cog_unload(self):
        if self.restore_task is not None:
            self.restore_task.cancel()

    async def ensure_panel(self, force=False):
        # Reuse the same panel across restarts; never delete existing welcome embeds.
        async with self.panel_lock:
            if self.panel_checked and not force:
                return
            guild = self.bot.get_guild(NOVUS_GUILD_ID)
            if guild is None:
                return
            channels = [c for c in guild.text_channels if c.name == "welcome-and-verify"]
            if len(channels) != 1:
                logger.warning("Verification panel: expected one welcome-and-verify channel, found %s", len(channels))
                return
            channel = channels[0]
            conn = sqlite3.connect("wom_multi.db")
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS verification_panels "
                    "(guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL)"
                )
                row = conn.execute(
                    "SELECT channel_id, message_id FROM verification_panels WHERE guild_id = ?",
                    (guild.id,),
                ).fetchone()
                message = None
                if row and row[0] == channel.id:
                    try:
                        message = await channel.fetch_message(row[1])
                    except discord.NotFound:
                        pass
                if message is None:
                    # Recover if the bot posted but restarted before the database commit.
                    async for candidate in channel.history(limit=100):
                        if candidate.author.id != self.bot.user.id:
                            continue
                        if any(
                            getattr(component, "custom_id", None) == "novus:verify:rsn:v1"
                            for action_row in candidate.components
                            for component in action_row.children
                        ):
                            message = candidate
                            break
                old_panel = None
                if message is not None:
                    if message.author.id != self.bot.user.id:
                        raise RuntimeError("Saved verification panel is not owned by Novus Sync")
                    if not any(
                        getattr(component, "custom_id", None) == "novus:verify:rsn:v1"
                        for action_row in message.components
                        for component in action_row.children
                    ):
                        raise RuntimeError("Saved message is not a verification panel")
                    async for newer in channel.history(limit=100, after=message, oldest_first=False):
                        if newer.author.id != self.bot.user.id and self.is_welcome_post(newer):
                            old_panel = message
                            message = None
                            break
                if message is None:
                    message = await channel.send(view=VerifyView(self))
                else:
                    await message.edit(content=PANEL_TEXT, view=VerifyView(self))
                conn.execute(
                    "INSERT OR REPLACE INTO verification_panels (guild_id, channel_id, message_id) VALUES (?, ?, ?)",
                    (guild.id, channel.id, message.id),
                )
                conn.commit()
                self.panel_message_id = message.id
                self.panel_channel_id = channel.id
                self.panel_checked = True
                if old_panel is not None:
                    try:
                        await old_panel.delete()
                    except discord.NotFound:
                        pass
                logger.info("Novus verification panel ready: %s/%s", channel.id, message.id)
            except Exception:
                logger.exception("Could not publish Novus verification panel")
            finally:
                conn.close()

    @app_commands.command(name="approve", description="Approve a member and update their server roles.")
    @app_commands.describe(member="The applicant to approve")
    @app_commands.guild_only()
    async def approve(self, interaction: discord.Interaction, member: discord.Member):
        guild = interaction.guild
        if guild is None or guild.id != NOVUS_GUILD_ID:
            await notice(interaction, False, "This command is only available in Novus.", ephemeral=True)
            return
        staff_ids = set(NOVUS_COMMAND_ROLE_IDS) | {1532829195657285843}
        if not any(r.id in staff_ids for r in interaction.user.roles):
            await notice(interaction, False, "Only Founder, Council, Moderators and Trial Moderators can approve members.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            member, already, logged = await self.approve_member(guild, interaction.user, member)
        except ValueError as exc:
            await notice(interaction, True, str(exc), ephemeral=True)
            return
        except discord.HTTPException:
            logger.exception("Approval failed for %s", member.id)
            await notice(interaction, True, "Could not complete approval. Please check roles and retry; some changes may already have applied.", ephemeral=True)
            return
        message = f"{member.mention} is approved. Existing ranks and Verified are preserved. WOM linking and tenure are handled by the existing sync."
        if not logged:
            message += " Staff log could not be sent."
        await notice(interaction, True, message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def approve_member(self, guild, actor, member, *, pathway=None):
        """Shared role transition for /approve and ticket approval."""
        staff_ids = set(NOVUS_COMMAND_ROLE_IDS) | {1532829195657285843}
        if guild.id != NOVUS_GUILD_ID or not any(r.id in staff_ids for r in actor.roles):
            raise ValueError("Only authorized Novus staff may approve members.")
        if member.bot:
            raise ValueError("You cannot approve a bot.")
        full = unique_role(guild, "Member")
        squire = unique_role(guild, "Squire")
        trial = unique_role(guild, "Trial Member")
        verified = unique_role(guild, "Verified")
        friend = guild.get_role(CLAN_FRIEND_ROLE_ID)
        guest = guild.get_role(GUEST_ROLE_ID)
        if guest is None or friend is None:
            raise ValueError("Guest or Clan Friend role is not configured.")
        async with self.lock(member):
            member = await guild.fetch_member(member.id)
            if verified not in member.roles:
                raise ValueError("This applicant must complete /rsn first.")
            already = full in member.roles
            was_trial = trial in member.roles
            add = []
            if not already:
                add.append(full)
            if not any(r.name.strip().casefold() in NOVUS_TENURE_ROLES for r in member.roles):
                add.append(squire)
            if add:
                await member.add_roles(*add, reason=f"Novus approval by {actor.id}")
            remove = [r for r in (friend, trial, guest) if r in member.roles]
            if remove:
                await member.remove_roles(*remove, reason=f"Novus approval by {actor.id}")
        logged = await self.log_approval(guild, actor, member, already, was_trial=was_trial, pathway=pathway)
        return member, already, logged


async def setup(bot):
    cog = VerifyCog(bot)
    await bot.add_cog(cog)
    bot.add_view(VerifyView(cog))
