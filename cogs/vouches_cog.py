"""Trial-member PvM references from full Novus members and staff."""
import asyncio
import datetime
import logging
import os
import sqlite3

import discord
from discord import app_commands
from discord.ext import commands

from common import has_non_clan_role, has_novus_member_role


logger = logging.getLogger("WOMBot")

COUNCIL_ROLE_ID = 1532830356875513906
MODERATOR_ROLE_ID = 1532830960062693396
TRIAL_MOD_ROLE_ID = 1533587986204069908
STAFF_ROLE_IDS = (COUNCIL_ROLE_ID, MODERATOR_ROLE_ID, TRIAL_MOD_ROLE_ID)
NOVUS_MOD_CHAT_CHANNEL_ID = 1532835276567613531
DB_FILE = "wom_multi.db"
REQUIRED_VOUCHES = 3


def is_staff(member: discord.Member) -> bool:
    return any(role.id in STAFF_ROLE_IDS for role in getattr(member, "roles", ()))


def is_trial(member: discord.Member) -> bool:
    return (
        not member.bot
        and any(role.name.strip().casefold() == "trial member" for role in member.roles)
    )


def is_eligible_voucher(member: discord.Member) -> bool:
    # Clan Friends, guests, and trial members cannot vouch. Full members
    # and staff submit under their own Discord account.
    return (
        not member.bot
        and not has_non_clan_role(member)
        and not is_trial(member)
        and (has_novus_member_role(member) or is_staff(member))
    )


def init_vouch_tables() -> None:
    conn = sqlite3.connect(DB_FILE)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trial_vouches (
                guild_id INTEGER NOT NULL,
                trial_member_id INTEGER NOT NULL,
                voucher_id INTEGER NOT NULL,
                submitted_by_id INTEGER NOT NULL,
                comment TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, trial_member_id, voucher_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trial_vouch_notifications (
                guild_id INTEGER NOT NULL,
                trial_member_id INTEGER NOT NULL,
                notified_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, trial_member_id)
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def get_vouches(guild_id: int, trial_id: int) -> list[tuple]:
    conn = sqlite3.connect(DB_FILE)
    try:
        return conn.execute(
            """
            SELECT voucher_id, submitted_by_id, comment, created_at
            FROM trial_vouches
            WHERE guild_id = ? AND trial_member_id = ?
            ORDER BY created_at ASC, voucher_id ASC
            """,
            (guild_id, trial_id),
        ).fetchall()
    finally:
        conn.close()


def record_vouch(guild_id: int, trial_id: int, voucher_id: int,
                 submitter_id: int, comment: str) -> bool:
    """Return False for an already recorded voucher (even after bot restarts)."""
    conn = sqlite3.connect(DB_FILE)
    try:
        try:
            conn.execute(
                """
                INSERT INTO trial_vouches (
                    guild_id, trial_member_id, voucher_id,
                    submitted_by_id, comment, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id, trial_id, voucher_id, submitter_id, comment,
                    datetime.datetime.now(datetime.timezone.utc).isoformat(),
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return False
        return True
    finally:
        conn.close()


def has_been_notified(guild_id: int, trial_id: int) -> bool:
    conn = sqlite3.connect(DB_FILE)
    try:
        return conn.execute(
            """
            SELECT 1 FROM trial_vouch_notifications
            WHERE guild_id = ? AND trial_member_id = ?
            """,
            (guild_id, trial_id),
        ).fetchone() is not None
    finally:
        conn.close()


def mark_notified(guild_id: int, trial_id: int) -> None:
    conn = sqlite3.connect(DB_FILE)
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO trial_vouch_notifications (
                guild_id, trial_member_id, notified_at
            ) VALUES (?, ?, ?)
            """,
            (
                guild_id, trial_id,
                datetime.datetime.now(datetime.timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def formatted_vouches(rows: list[tuple], limit: int = 10) -> str:
    lines = []
    for i, (voucher_id, _submitted_by, comment, _created) in enumerate(
        rows[:limit], start=1
    ):
        line = f"**{i}.** <@{voucher_id}>"
        if comment:
            line += f" — {discord.utils.escape_mentions(comment)}"
        lines.append(line)
    if len(rows) > limit:
        lines.append(f"…and {len(rows) - limit} more.")
    return "\n".join(lines) or "No vouches recorded yet."


async def trial_member_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Suggest only current Trial Member role holders in the guild."""
    guild = interaction.guild
    if guild is None or not is_staff(interaction.user):
        return []

    search = current.strip().casefold()
    members = sorted(
        (member for member in guild.members if is_trial(member)),
        key=lambda member: (member.display_name.casefold(), member.id),
    )
    choices = []
    for member in members:
        searchable = (member.display_name, member.name, member.global_name or "", str(member.id))
        if search and not any(search in value.casefold() for value in searchable):
            continue
        label = f"{member.display_name} (@{member.name})"
        choices.append(
            app_commands.Choice(name=label[:100], value=str(member.id))
        )
        if len(choices) == 25:
            break
    return choices


class VouchesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._notify_lock = asyncio.Lock()
        init_vouch_tables()

    @staticmethod
    def staff_channel(guild: discord.Guild):
        channel_id = os.getenv("NOVUS_VOUCH_STAFF_CHANNEL_ID", "").strip()
        if channel_id.isdecimal():
            channel = guild.get_channel(int(channel_id))
            if isinstance(channel, discord.TextChannel):
                return channel
        return guild.get_channel(NOVUS_MOD_CHAT_CHANNEL_ID) or discord.utils.get(
            guild.text_channels, name="mod-chat"
        )

    async def notify_new_vouch(
        self, guild: discord.Guild, trial: discord.Member,
        voucher: discord.Member, total: int
    ) -> bool:
        """Quiet staff-side activity note; no role or member pings."""
        channel = self.staff_channel(guild)
        if channel is None:
            logger.warning(
                "Could not post new trial vouch for %s: #mod-chat unavailable.",
                trial.id,
            )
            return False
        try:
            await channel.send(
                f"**New Trial Vouch**\n"
                f"{voucher.mention} vouched for {trial.mention} "
                f"(**{total}/{REQUIRED_VOUCHES} vouches**).",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception(
                "Could not post new trial vouch for %s from %s.",
                trial.id, voucher.id,
            )
            return False
        return True

    async def notify_if_ready(self, guild: discord.Guild, trial: discord.Member) -> str:
        """Send exactly one staff review alert once 3 distinct vouches exist."""
        async with self._notify_lock:
            rows = get_vouches(guild.id, trial.id)
            if len(rows) < REQUIRED_VOUCHES or has_been_notified(guild.id, trial.id):
                return "already_notified" if len(rows) >= REQUIRED_VOUCHES else "pending"

            channel = self.staff_channel(guild)
            if channel is None:
                logger.warning(
                    "Trial vouch threshold reached for %s in guild %s, "
                    "but #mod-chat / NOVUS_VOUCH_STAFF_CHANNEL_ID was not found.",
                    trial.id, guild.id,
                )
                return "missing_channel"

            embed = discord.Embed(
                title="Trial Member Ready for Staff Review",
                description=(
                    f"{trial.mention} has received **{len(rows)} unique vouches** "
                    "and has met the 3-vouch reference requirement.\n\n"
                    "**Staff review is required before approving membership.**"
                ),
                color=discord.Color.green(),
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            )
            embed.add_field(
                name="Member Feedback",
                value=formatted_vouches(rows, limit=3)[:1024],
                inline=False,
            )
            roles = " ".join(f"<@&{role_id}>" for role_id in STAFF_ROLE_IDS)
            try:
                await channel.send(
                    content=roles,
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions(
                        everyone=False, users=False, roles=True
                    ),
                )
            except discord.HTTPException:
                logger.exception(
                    "Could not notify staff that trial member %s reached 3 vouches.",
                    trial.id,
                )
                return "send_failed"

            mark_notified(guild.id, trial.id)
            return "notified"

    @app_commands.command(
        name="vouch",
        description="Vouch for a Novus trial member after raiding together",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        trial_member="Trial member receiving your vouch",
        comments="Optional notes on PvM skill, mechanics, or teamwork",
    )
    async def vouch(
        self,
        interaction: discord.Interaction,
        trial_member: discord.Member,
        comments: str = "",
    ):
        if not is_eligible_voucher(interaction.user):
            await interaction.response.send_message(
                "Only full Novus Members and staff can submit vouches.",
                ephemeral=True,
            )
            return
        if not is_trial(trial_member):
            await interaction.response.send_message(
                "The first selection must have the Trial Member role.",
                ephemeral=True,
            )
            return
        if trial_member.id == interaction.user.id:
            await interaction.response.send_message(
                "You cannot vouch for yourself.", ephemeral=True
            )
            return
        comments = comments.strip()
        if len(comments) > 500:
            await interaction.response.send_message(
                "Comments must be 500 characters or fewer.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        inserted = record_vouch(
            interaction.guild_id, trial_member.id, interaction.user.id,
            interaction.user.id, comments,
        )
        rows = get_vouches(interaction.guild_id, trial_member.id)
        if not inserted:
            await interaction.followup.send(
                f"You have already vouched for {trial_member.mention}. "
                "Each member can only vouch once per trial member.",
                ephemeral=True,
            )
            return

        activity_posted = await self.notify_new_vouch(
            interaction.guild, trial_member, interaction.user, len(rows)
        )
        status = await self.notify_if_ready(interaction.guild, trial_member)
        message = (
            f"Recorded your vouch for {trial_member.mention}.\n"
            f"**Progress: {len(rows)}/{REQUIRED_VOUCHES} unique vouches**"
        )
        if not activity_posted:
            message += "\nThe staff activity notification could not be sent; your vouch is still saved."
        if status == "notified":
            message += "\nStaff have been notified in the review channel."
        elif status in {"missing_channel", "send_failed"}:
            message += (
                "\n**Staff alert not sent.** Please check #mod-chat or configure "
                "`NOVUS_VOUCH_STAFF_CHANNEL_ID` and notify staff manually."
            )
        elif len(rows) >= REQUIRED_VOUCHES:
            message += "\nThe 3-vouch staff notification was already sent."
        await interaction.followup.send(message, ephemeral=True)

    @app_commands.command(
        name="vouches",
        description="Review a trial member's recorded vouches and comments",
    )
    @app_commands.guild_only()
    @app_commands.describe(trial_member="Select a member with the Trial Member role")
    @app_commands.autocomplete(trial_member=trial_member_autocomplete)
    async def vouches(
        self, interaction: discord.Interaction, trial_member: str
    ):
        if not is_staff(interaction.user):
            await interaction.response.send_message(
                "Only Trial Moderators, Moderators, and Council may view vouches.",
                ephemeral=True,
            )
            return

        # Autocomplete values are Discord IDs, not mutable display names.
        # Validate again on execution since an API user can submit arbitrary text.
        if not trial_member.isdecimal():
            await interaction.response.send_message(
                "Please select a trial member from the suggestions.",
                ephemeral=True,
            )
            return
        member = interaction.guild.get_member(int(trial_member))
        if member is None:
            try:
                member = await interaction.guild.fetch_member(int(trial_member))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                member = None
        if member is None or not is_trial(member):
            await interaction.response.send_message(
                "This person is not a current Trial Member. Select a trial member from the suggestions.",
                ephemeral=True,
            )
            return

        rows = get_vouches(interaction.guild_id, member.id)
        embed = discord.Embed(
            title=f"Trial Vouches • {member.display_name}",
            description=formatted_vouches(rows, limit=10)[:3900],
            color=discord.Color.green() if len(rows) >= REQUIRED_VOUCHES
            else discord.Color.blurple(),
        )
        embed.add_field(
            name="Progress",
            value=f"**{len(rows)}/{REQUIRED_VOUCHES}** unique vouches",
            inline=False,
        )
        if len(rows) >= REQUIRED_VOUCHES:
            embed.add_field(
                name="Staff notification",
                value="Sent" if has_been_notified(interaction.guild_id, member.id)
                else "Pending — check the staff notification channel",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(VouchesCog(bot))
