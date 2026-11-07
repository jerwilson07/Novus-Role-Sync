import discord
from discord import app_commands
from discord.ext import tasks, commands
from zoneinfo import ZoneInfo
import sqlite3
import datetime
import calendar
import logging
import asyncio
import os
import shutil

from common import (
    WOM_API_KEY, sanitize_rsn, has_non_clan_role, forget_member_links,
    forget_non_clan_links,
    has_novus_member_role, is_active_novus_member_link,
    find_primary_wom_membership,
)
from tenure_history import (
    cumulative_effective_join, record_roster, refresh_activity_history,
    set_manual_previous_tenure, prior_tenure, get_tenure_diagnostics,
)


logger = logging.getLogger("WOMBot")


# ============================================================
# NOVUS CONFIGURATION
# ============================================================

# Discord staff roles
COUNCIL_ROLE_ID = 1532830356875513906
MODERATOR_ROLE_ID = 1532830960062693396
TRIAL_MOD_ROLE_ID = 1533587986204069908

# Users with these Discord roles may use staff sync commands.
NOVUS_COMMAND_ROLE_IDS = {
    COUNCIL_ROLE_ID,
    MODERATOR_ROLE_ID,
    TRIAL_MOD_ROLE_ID,
}
TENURE_CREDIT_ROLE_IDS = {COUNCIL_ROLE_ID, MODERATOR_ROLE_ID}

# WOM staff rank -> Discord staff role
NOVUS_STAFF_WOM_ROLES = {
    "quester": COUNCIL_ROLE_ID,
    "explorer": MODERATOR_ROLE_ID,
    "gamer": TRIAL_MOD_ROLE_ID,
}

# Standard Novus membership / tenure ranks.
NOVUS_TENURE_ROLES = {
    "squire",
    "duellist",
    "inquisitor",
    "expert",
    "knight",
    "paladin",
}

# Trial Member in WOM/in-game. Trials are not yet on the tenure ladder.
NOVUS_TRIAL_WOM_RANKS = {
    "gnome_child",
}

# Discord role held by active trial members.
NOVUS_TRIAL_DISCORD_ROLE_NAME = "trial member"

# Default staff channel for the scheduled noon Novus report.
NOVUS_MOD_CHAT_CHANNEL_ID = 1532835276567613531

# Promotion thresholds measured from the in-game clan join date.
#
# Squire is the initial Full Member rank.
# Later ranks are based on completed months.
PROMOTION_THRESHOLDS = [
    ("paladin", 12),
    ("knight", 9),
    ("expert", 6),
    ("inquisitor", 3),
    ("duellist", 1),
    ("squire", 0),
]

# Used to compare how far along the normal rank ladder someone is.
TENURE_RANK_ORDER = {
    "squire": 0,
    "duellist": 1,
    "inquisitor": 2,
    "expert": 3,
    "knight": 4,
    "paladin": 5,
}

TRIAL_PERIOD_DAYS = 30
TRIAL_ENDING_SOON_DAYS = 7


# ============================================================
# HELPERS
# ============================================================

def has_novus_command_permission(member: discord.Member) -> bool:
    return any(
        role.id in NOVUS_COMMAND_ROLE_IDS
        for role in member.roles
    )


def build_wom_headers():
    headers = {
        "User-Agent": "NovusRoleSync/1.0"
    }

    if WOM_API_KEY:
        headers["x-api-key"] = WOM_API_KEY

    return headers


def expected_tenure_rank(
    joined_at: datetime.datetime,
    now: datetime.datetime | None = None
) -> str:
    """Return the tenure rank earned on the exact calendar eligibility date."""
    if joined_at is None:
        return "squire"

    now = now or datetime.datetime.now(
        ZoneInfo("America/New_York")
    )

    if now.tzinfo is None:
        now = now.replace(
            tzinfo=ZoneInfo("America/New_York")
        )
    else:
        now = now.astimezone(
            ZoneInfo("America/New_York")
        )

    for role_name, required_months in PROMOTION_THRESHOLDS:
        eligible_at = add_calendar_months(
            joined_at,
            required_months
        )

        if now.date() >= eligible_at.date():
            return role_name

    return "squire"


def parse_wom_datetime(value):
    if not value:
        return None

    try:
        # WOM timestamps generally use ISO-8601 format.
        value = value.replace("Z", "+00:00")

        parsed = datetime.datetime.fromisoformat(value)

        if parsed.tzinfo is None:
            parsed = parsed.replace(
                tzinfo=datetime.timezone.utc
            )

        return parsed

    except Exception:
        return None


def get_membership_joined_at(membership: dict):
    """Return WOM clientSyncJoinedAt from either membership or player data."""
    player = membership.get("player", {})

    joined_value = membership.get("clientSyncJoinedAt")

    if joined_value is None:
        joined_value = player.get("clientSyncJoinedAt")

    return parse_wom_datetime(joined_value)


def add_calendar_months(start: datetime.datetime, months: int) -> datetime.datetime:
    """Add whole calendar months while safely clamping end-of-month dates."""
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    day = min(
        start.day,
        calendar.monthrange(year, month)[1]
    )

    return start.replace(
        year=year,
        month=month,
        day=day
    )


def tenure_eligible_at(joined_at: datetime.datetime, rank: str):
    """Return the date/time a member becomes eligible for a tenure rank."""
    required_months = next(
        (
            months
            for role_name, months in PROMOTION_THRESHOLDS
            if role_name == rank
        ),
        None
    )

    if required_months is None:
        return None

    return add_calendar_months(
        joined_at,
        required_months
    )


def next_tenure_rank(rank: str):
    """Return the next normal tenure rank, or None at Paladin."""
    ordered = [
        "squire",
        "duellist",
        "inquisitor",
        "expert",
        "knight",
        "paladin",
    ]

    try:
        index = ordered.index(rank)
    except ValueError:
        return None

    if index + 1 >= len(ordered):
        return None

    return ordered[index + 1]
def build_wom_server_nickname(
    primary_rsn: str,
    active_alts: list[str] | None = None,
    current_nickname: str | None = None
) -> str:
    """Build a Discord server nickname from the current WOM primary + alts.

    True RSN changes update the primary, while capitalization-only WOM
    differences are ignored. Current in-clan linked alts may remain after the
    primary. Preserve the member's
    existing '/' versus '|' separator style when possible.
    """
    if not primary_rsn:
        return ""

    active_alts = active_alts or []

    if (
        current_nickname
        and "/" in current_nickname
        and "|" not in current_nickname
    ):
        separator = " / "
    else:
        separator = " | "

    parts = [primary_rsn]
    seen = {
        primary_rsn.casefold()
    }

    for alt_rsn in active_alts:
        if not alt_rsn:
            continue

        key = alt_rsn.casefold()
        if key in seen:
            continue

        candidate = separator.join(
            parts + [alt_rsn]
        )

        # Discord server nicknames are limited to 32 characters. Keep only
        # complete RSNs; never truncate an account name to squeeze an alt in.
        if len(candidate) > 32:
            break

        parts.append(
            alt_rsn
        )
        seen.add(
            key
        )

    return separator.join(
        parts
    )




# Sync result categories used by both automatic and manual sync reports.
SKIPPED_SYNC_STATUSES = {
    "not_linked",
    "staff_role_missing",
    "tenure_not_mapped",
    "unsupported_rank",
}

FAILED_SYNC_STATUSES = {
    "forbidden",
    "error",
    "inactive_cleanup_forbidden",
    "inactive_cleanup_error",
}


def format_sync_issue(member: discord.Member, result: dict) -> str:
    """Return a concise, staff-friendly description of a sync issue."""
    status = result.get("status", "unknown")
    rsn = result.get("rsn") or "Unknown RSN"
    wom_role = result.get("wom_role")

    reasons = {
        "not_linked": "Discord member is not linked",
        "staff_role_missing": "Matching Discord staff role was not found",
        "tenure_not_mapped": "WOM tenure rank is not mapped",
        "unsupported_rank": "WOM rank is not managed by Novus Sync",
        "forbidden": "Discord permission or role hierarchy prevented the update",
        "error": "Unexpected role sync error",
        "inactive_cleanup_forbidden": (
            "Could not remove inactive Member/tenure roles due to Discord permissions"
        ),
        "inactive_cleanup_error": (
            "Unexpected error while removing inactive Member/tenure roles"
        ),
    }

    reason = reasons.get(
        status,
        status.replace("_", " ").title()
    )

    line = f"- {member.mention} (`{rsn}`) - {reason}"

    if wom_role:
        line += f" (`{wom_role}`)"

    return line


def classify_sync_issue(result: dict) -> str:
    """Classify a non-success sync result as skipped or failed."""
    status = result.get("status")

    if status in FAILED_SYNC_STATUSES:
        return "failed"

    return "skipped"


# ============================================================
# TASK COG
# ============================================================

class TasksCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        self.sync_roles_loop.start()
        self.history_backfill_loop.start()
        self.cleanup_inactive_guilds.start()
        self.backup_database.start()
        self.update_stats.start()
        self.check_reminders.start()

    def cog_unload(self):
        self.sync_roles_loop.cancel()
        self.history_backfill_loop.cancel()
        self.cleanup_inactive_guilds.cancel()
        self.backup_database.cancel()
        self.update_stats.cancel()
        self.check_reminders.cancel()

    # ========================================================
    # GUEST / CLAN FRIEND EXCLUSION
    # ========================================================

    @commands.Cog.listener()
    async def on_ready(self):
        # Reconcile preexisting Guest/Clan Friend roles on every startup.
        for guild in self.bot.guilds:
            try:
                conn = sqlite3.connect("wom_multi.db")
                try:
                    cursor = conn.cursor()
                    people, accounts = forget_non_clan_links(cursor, guild)

                    migration_key = (
                        f"mod_chat_log_channel_v1:{guild.id}"
                    )
                    cursor.execute(
                        """
                        SELECT value
                        FROM bot_stats
                        WHERE key = ?
                        """,
                        (migration_key,)
                    )
                    migration_done = cursor.fetchone()

                    if not migration_done:
                        target_channel = guild.get_channel(
                            NOVUS_MOD_CHAT_CHANNEL_ID
                        )

                        if target_channel is not None:
                            bot_member = guild.get_member(
                                self.bot.user.id
                            )
                            permissions = target_channel.permissions_for(
                                bot_member
                            )

                            if (
                                permissions.send_messages
                                and permissions.embed_links
                            ):
                                cursor.execute(
                                    """
                                    UPDATE guild_configs
                                    SET log_channel_id = ?
                                    WHERE guild_id = ?
                                    """,
                                    (
                                        NOVUS_MOD_CHAT_CHANNEL_ID,
                                        guild.id
                                    )
                                )

                                if cursor.rowcount:
                                    cursor.execute(
                                        """
                                        INSERT OR REPLACE INTO bot_stats
                                        (key, value)
                                        VALUES (?, ?)
                                        """,
                                        (
                                            migration_key,
                                            str(
                                                NOVUS_MOD_CHAT_CHANNEL_ID
                                            )
                                        )
                                    )
                                    logger.info(
                                        "Moved Novus sync log channel to "
                                        "#mod-chat (%s) for guild %s",
                                        NOVUS_MOD_CHAT_CHANNEL_ID,
                                        guild.id
                                    )

                    notice_key = (
                        f"mod_chat_test_notice_v1:{guild.id}"
                    )
                    cursor.execute(
                        """
                        SELECT value
                        FROM bot_stats
                        WHERE key = ?
                        """,
                        (notice_key,)
                    )
                    notice_sent = cursor.fetchone()

                    if not notice_sent:
                        target_channel = guild.get_channel(
                            NOVUS_MOD_CHAT_CHANNEL_ID
                        )

                        if target_channel is not None:
                            bot_member = guild.get_member(
                                self.bot.user.id
                            )
                            permissions = target_channel.permissions_for(
                                bot_member
                            )

                            if permissions.send_messages:
                                try:
                                    await target_channel.send(
                                        "Testing: WOM Sync logs will be posted here "
                                        "daily at 12 PM EST following Auto-Link and "
                                        "SyncAll runs performed internally."
                                    )
                                    cursor.execute(
                                        """
                                        INSERT OR REPLACE INTO bot_stats
                                        (key, value)
                                        VALUES (?, ?)
                                        """,
                                        (
                                            notice_key,
                                            datetime.datetime.now().isoformat()
                                        )
                                    )
                                    logger.info(
                                        "Sent one-time Novus Sync test notice "
                                        "to #mod-chat (%s) for guild %s",
                                        NOVUS_MOD_CHAT_CHANNEL_ID,
                                        guild.id
                                    )
                                except discord.Forbidden:
                                    logger.warning(
                                        "Could not send one-time Novus Sync "
                                        "test notice to #mod-chat (%s)",
                                        NOVUS_MOD_CHAT_CHANNEL_ID
                                    )
                                except Exception:
                                    logger.exception(
                                        "Failed to send one-time Novus Sync "
                                        "test notice to #mod-chat (%s)",
                                        NOVUS_MOD_CHAT_CHANNEL_ID
                                    )

                    council_notice_key = (
                        f"inactive_member_cleanup_notice_v2:{guild.id}"
                    )
                    cursor.execute(
                        """
                        SELECT value
                        FROM bot_stats
                        WHERE key = ?
                        """,
                        (council_notice_key,)
                    )
                    council_notice_sent = cursor.fetchone()

                    if not council_notice_sent:
                        council_channel = discord.utils.get(
                            guild.text_channels,
                            name="council-chat"
                        )

                        if council_channel is not None:
                            bot_member = guild.get_member(
                                self.bot.user.id
                            )
                            permissions = council_channel.permissions_for(
                                bot_member
                            )

                            if permissions.send_messages:
                                try:
                                    await council_channel.send(
                                        "**Novus Sync Update**\n\n"
                                        "Novus Sync will now automatically remove stale **Member**, "
                                        "tenure, and **Trial Member** Discord roles when the linked "
                                        "account is no longer active in the appropriate WOM clan role. "
                                        "Saved account/tenure history is preserved for future rejoins.\n\n"
                                        "Cleanup actions will be included in the normal sync reports."
                                    )
                                    cursor.execute(
                                        """
                                        INSERT OR REPLACE INTO bot_stats
                                        (key, value)
                                        VALUES (?, ?)
                                        """,
                                        (
                                            council_notice_key,
                                            datetime.datetime.now().isoformat()
                                        )
                                    )
                                    logger.info(
                                        "Sent inactive-member cleanup update "
                                        "to #council-chat for guild %s",
                                        guild.id
                                    )
                                except discord.Forbidden:
                                    logger.warning(
                                        "Could not send inactive-member cleanup "
                                        "update to #council-chat for guild %s",
                                        guild.id
                                    )
                                except Exception:
                                    logger.exception(
                                        "Failed to send inactive-member cleanup "
                                        "update to #council-chat for guild %s",
                                        guild.id
                                    )

                    moderator_recruitment_key = (
                        f"moderator_recruitment_notice_v1:{guild.id}"
                    )
                    cursor.execute(
                        """
                        SELECT value
                        FROM bot_stats
                        WHERE key = ?
                        """,
                        (moderator_recruitment_key,)
                    )
                    moderator_recruitment_sent = cursor.fetchone()

                    if not moderator_recruitment_sent:
                        general_channel = discord.utils.get(
                            guild.text_channels,
                            name="general-chat"
                        )

                        if general_channel is not None:
                            bot_member = guild.get_member(
                                self.bot.user.id
                            )
                            permissions = general_channel.permissions_for(
                                bot_member
                            )

                            if permissions.send_messages:
                                try:
                                    embed = discord.Embed(
                                        title="⚔️ NOVUS STAFF APPLICATIONS OPEN",
                                        description=(
                                            "Want to take a bigger role in Novus?\n\n"
                                            "We’re accepting **Moderator applications** from active, "
                                            "dependable members who want to **foster engagement, "
                                            "strengthen the community, and help Novus continue to grow.**\n\n"
                                            "If you’re someone who shows up, gets involved, and wants "
                                            "to help shape the community, we’d like to hear from you.\n\n"
                                            "**Think you’d be a good fit? Apply now in "
                                            "<#1533481323883401216>.**"
                                        ),
                                        color=0xE79A2D
                                    )
                                    embed.set_footer(
                                        text="NOVUS • Old School RuneScape"
                                    )
                                    await general_channel.send(
                                        embed=embed
                                    )
                                    cursor.execute(
                                        """
                                        INSERT OR REPLACE INTO bot_stats
                                        (key, value)
                                        VALUES (?, ?)
                                        """,
                                        (
                                            moderator_recruitment_key,
                                            datetime.datetime.now().isoformat()
                                        )
                                    )
                                    logger.info(
                                        "Sent moderator recruitment notice "
                                        "to #general-chat for guild %s",
                                        guild.id
                                    )
                                except discord.Forbidden:
                                    logger.warning(
                                        "Could not send moderator recruitment "
                                        "notice to #general-chat for guild %s",
                                        guild.id
                                    )
                                except Exception:
                                    logger.exception(
                                        "Failed to send moderator recruitment "
                                        "notice to #general-chat for guild %s",
                                        guild.id
                                    )

                    conn.commit()
                finally:
                    conn.close()
                if people:
                    logger.info(
                        "Forgot %s Guest/Clan Friend members (%s linked accounts) in guild %s",
                        people, accounts, guild.id
                    )
            except sqlite3.Error:
                logger.exception(
                    "Failed to forget Guest/Clan Friend links for guild %s",
                    guild.id
                )

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        # Forget people as soon as they receive either exclusion role.
        if not has_non_clan_role(after) or has_non_clan_role(before):
            return
        try:
            conn = sqlite3.connect("wom_multi.db")
            try:
                cursor = conn.cursor()
                deleted = forget_member_links(cursor, after.guild.id, after.id)
                conn.commit()
            finally:
                conn.close()
            if deleted:
                logger.info(
                    "Forgot Guest/Clan Friend %s in guild %s: %s account links deleted",
                    after.id, after.guild.id, deleted
                )
        except sqlite3.Error:
            logger.exception(
                "Failed to forget Guest/Clan Friend %s in guild %s",
                after.id, after.guild.id
            )

    # ========================================================
    # WOM API
    # ========================================================

    async def get_wom_group(self, group_id: int):
        url = (
            f"https://api.wiseoldman.net/v2/groups/{group_id}"
        )

        try:
            async with self.bot.http_session.get(
                url,
                headers=build_wom_headers(),
                timeout=30
            ) as response:

                if response.status != 200:
                    logger.error(
                        f"WOM API returned status "
                        f"{response.status} for group {group_id}"
                    )
                    return None, response.status

                data = await response.json()
                return data, 200

        except Exception as e:
            logger.error(
                f"WOM API request failed for group "
                f"{group_id}: {e}"
            )
            return None, None

    # ========================================================
    # HISTORICAL CLAN TENURE
    # ========================================================

    async def prepare_tenure_history(self, cursor, guild, group_data):
        group_id = group_data.get("id")
        if not isinstance(group_id, int):
            return
        memberships = group_data.get("memberships", [])
        record_roster(cursor, guild.id, group_id, memberships)
        await refresh_activity_history(
            self.bot.http_session, build_wom_headers(),
            cursor, guild.id, group_id
        )

    # ========================================================
    # ALT ACCOUNT VERIFICATION
    # ========================================================

    def sync_alt_links_in_database(
        self,
        cursor,
        guild_id: int,
        discord_id: int,
        wom_by_id: dict,
        wom_by_username: dict
    ):
        """
        Resolve and refresh linked alt identities.

        Alts temporarily outside Novus remain linked so their independent
        tenure history can resume if they later rejoin. Only current in-clan
        alts participate in tenure and promotion calculations.
        """

        cursor.execute(
            """
            SELECT rsn, wom_id
            FROM alt_links
            WHERE guild_id = ?
            AND discord_id = ?
            """,
            (
                guild_id,
                discord_id
            )
        )

        alts = cursor.fetchall()
        verified = []
        missing = []

        for alt_rsn, alt_wom_id in alts:
            membership = None

            if alt_wom_id:
                membership = wom_by_id.get(
                    alt_wom_id
                )

            if membership is None and alt_rsn:
                membership = wom_by_username.get(
                    alt_rsn.lower()
                )

            if membership is None:
                missing.append(
                    alt_rsn
                )
                continue

            player = membership.get(
                "player",
                {}
            )
            wom_reported_alt_rsn = player.get(
                "username",
                alt_rsn
            )

            if (
                alt_rsn
                and wom_reported_alt_rsn
                and alt_rsn.casefold()
                    == wom_reported_alt_rsn.casefold()
            ):
                current_alt_rsn = alt_rsn
            else:
                current_alt_rsn = wom_reported_alt_rsn

            current_alt_id = player.get(
                "id",
                alt_wom_id
            )

            if (
                current_alt_id != alt_wom_id
                or (
                    current_alt_rsn
                    and alt_rsn
                    and current_alt_rsn.casefold()
                        != alt_rsn.casefold()
                )
            ):
                cursor.execute(
                    """
                    UPDATE alt_links
                    SET rsn = ?, wom_id = ?
                    WHERE guild_id = ?
                    AND discord_id = ?
                    AND rsn = ?
                    """,
                    (
                        current_alt_rsn,
                        current_alt_id,
                        guild_id,
                        discord_id,
                        alt_rsn
                    )
                )

            verified.append(
                current_alt_rsn
            )

        return verified, missing

    def get_linked_account_tenures(
        self,
        cursor,
        guild_id: int,
        group_id: int,
        discord_id: int,
        memberships: list
    ):
        """Return independent tenure snapshots for every linked in-clan RSN."""

        wom_by_id = {
            membership["player"]["id"]: membership
            for membership in memberships
            if isinstance(membership, dict)
            and isinstance(membership.get("player"), dict)
            and membership["player"].get("id") is not None
        }

        wom_by_username = {
            membership["player"]["username"].lower(): membership
            for membership in memberships
            if isinstance(membership, dict)
            and isinstance(membership.get("player"), dict)
            and membership["player"].get("username")
        }

        cursor.execute(
            """
            SELECT rsn, wom_id
            FROM links
            WHERE guild_id = ?
            AND discord_id = ?
            """,
            (
                guild_id,
                discord_id
            )
        )
        primary = cursor.fetchone()

        cursor.execute(
            """
            SELECT rsn, wom_id
            FROM alt_links
            WHERE guild_id = ?
            AND discord_id = ?
            ORDER BY rowid
            """,
            (
                guild_id,
                discord_id
            )
        )
        alts = cursor.fetchall()

        account_rows = []

        if primary:
            account_rows.append(
                (
                    "primary",
                    primary[0],
                    primary[1]
                )
            )

        account_rows.extend(
            (
                "alt",
                alt_rsn,
                alt_wom_id
            )
            for alt_rsn, alt_wom_id in alts
        )

        snapshots = []
        seen_player_ids = set()

        for account_type, stored_rsn, stored_wom_id in account_rows:
            membership = None

            if stored_wom_id is not None:
                membership = wom_by_id.get(
                    stored_wom_id
                )

            if membership is None and stored_rsn:
                membership = wom_by_username.get(
                    stored_rsn.lower()
                )

            if membership is None:
                continue

            player = membership.get(
                "player",
                {}
            )
            player_id = player.get(
                "id"
            )

            if player_id in seen_player_ids:
                continue

            if player_id is not None:
                seen_player_ids.add(
                    player_id
                )

            wom_reported_rsn = player.get(
                "username",
                stored_rsn
            )

            if (
                stored_rsn
                and wom_reported_rsn
                and stored_rsn.casefold()
                    == wom_reported_rsn.casefold()
            ):
                current_rsn = stored_rsn
            else:
                current_rsn = wom_reported_rsn

            wom_role = (
                membership.get(
                    "role",
                    ""
                ).lower()
            )
            joined_at = get_membership_joined_at(
                membership
            )

            effective_joined_at = None
            prior_service = datetime.timedelta(0)
            expected_rank = None
            eligible_at = None
            next_rank = None
            next_eligible_at = None

            if joined_at is not None:
                (
                    effective_joined_at,
                    prior_service
                ) = cumulative_effective_join(
                    cursor,
                    guild_id,
                    group_id,
                    player_id,
                    joined_at
                )

                if wom_role not in NOVUS_TRIAL_WOM_RANKS:
                    expected_rank = expected_tenure_rank(
                        effective_joined_at
                    )
                    eligible_at = tenure_eligible_at(
                        effective_joined_at,
                        expected_rank
                    )

                    next_rank = next_tenure_rank(
                        expected_rank
                    )

                    if next_rank:
                        next_eligible_at = tenure_eligible_at(
                            effective_joined_at,
                            next_rank
                        )

            snapshots.append(
                {
                    "account_type": account_type,
                    "rsn": current_rsn,
                    "player_id": player_id,
                    "wom_role": wom_role,
                    "joined_at": joined_at,
                    "effective_joined_at": effective_joined_at,
                    "prior_service": prior_service,
                    "expected_rank": expected_rank,
                    "eligible_at": eligible_at,
                    "next_rank": next_rank,
                    "next_eligible_at": next_eligible_at,
                    "membership": membership,
                }
            )

        return snapshots

    def get_highest_discord_tenure_basis(
        self,
        account_tenures: list
    ):
        """Choose the highest independently earned linked-account tenure rank."""

        eligible = [
            account
            for account in account_tenures
            if account.get(
                "expected_rank"
            ) in TENURE_RANK_ORDER
        ]

        if not eligible:
            return None

        return max(
            eligible,
            key=lambda account: TENURE_RANK_ORDER[
                account["expected_rank"]
            ]
        )

    def get_promotion_audit_entries(
        self,
        cursor,
        guild: discord.Guild,
        group_data: dict,
        upcoming_days: int = 14
    ):
        """
        Audit in-game promotions for primary and alt RSNs separately.

        Each account is compared only with its own WOM rank and its own
        cumulative tenure. Discord role state does not determine whether an
        in-game promotion is due.
        """

        memberships = group_data.get(
            "memberships",
            []
        )
        group_id = group_data.get(
            "id"
        )

        cursor.execute(
            """
            SELECT DISTINCT discord_id
            FROM links
            WHERE guild_id = ?
            """,
            (guild.id,)
        )

        discord_ids = [
            row[0]
            for row in cursor.fetchall()
        ]

        today = datetime.datetime.now(
            ZoneInfo("America/New_York")
        ).date()

        due_today = []
        overdue = []
        upcoming = []
        missing_join_dates = []
        active_accounts = 0

        for discord_id in discord_ids:
            member = guild.get_member(
                discord_id
            )

            if (
                member is None
                or has_non_clan_role(member)
                or not has_novus_member_role(member)
            ):
                continue

            accounts = self.get_linked_account_tenures(
                cursor,
                guild.id,
                group_id,
                discord_id,
                memberships
            )

            active_accounts += len(
                accounts
            )

            for account in accounts:
                wom_role = account.get(
                    "wom_role"
                )
                joined_at = account.get(
                    "joined_at"
                )
                expected_rank = account.get(
                    "expected_rank"
                )

                if wom_role in NOVUS_TRIAL_WOM_RANKS:
                    continue

                if joined_at is None:
                    missing_join_dates.append(
                        {
                            "member": member,
                            **account,
                        }
                    )
                    continue

                # Staff/special WOM ranks still contribute to Discord tenure,
                # but they are not waiting for a normal in-game tenure rank.
                if (
                    wom_role not in TENURE_RANK_ORDER
                    or expected_rank not in TENURE_RANK_ORDER
                ):
                    continue

                current_level = TENURE_RANK_ORDER[
                    wom_role
                ]
                expected_level = TENURE_RANK_ORDER[
                    expected_rank
                ]

                if current_level < expected_level:
                    eligible_at = tenure_eligible_at(
                        account["effective_joined_at"],
                        expected_rank
                    )
                    eligible_date = eligible_at.date()

                    entry = {
                        "member": member,
                        **account,
                        "current_rank": wom_role,
                        "target_rank": expected_rank,
                        "eligible_at": eligible_at,
                        "overdue_days": max(
                            (
                                today
                                - eligible_date
                            ).days,
                            0
                        ),
                    }

                    if eligible_date == today:
                        due_today.append(
                            entry
                        )
                    elif eligible_date < today:
                        overdue.append(
                            entry
                        )

                    continue

                next_rank = next_tenure_rank(
                    wom_role
                )

                if not next_rank:
                    continue

                next_eligible_at = tenure_eligible_at(
                    account["effective_joined_at"],
                    next_rank
                )

                days_until = (
                    next_eligible_at.date()
                    - today
                ).days

                if (
                    days_until >= 1
                    and days_until <= upcoming_days
                ):
                    upcoming.append(
                        {
                            "member": member,
                            **account,
                            "current_rank": wom_role,
                            "target_rank": next_rank,
                            "eligible_at": next_eligible_at,
                            "days_until": days_until,
                        }
                    )

        due_today.sort(
            key=lambda entry: entry["rsn"].lower()
        )
        overdue.sort(
            key=lambda entry: entry["overdue_days"],
            reverse=True
        )
        upcoming.sort(
            key=lambda entry: entry["days_until"]
        )

        return {
            "due_today": due_today,
            "overdue": overdue,
            "upcoming": upcoming,
            "missing_join_dates": missing_join_dates,
            "active_accounts": active_accounts,
        }

    def get_link_audit_entries(
        self,
        cursor,
        guild: discord.Guild,
        group_data: dict
    ):
        """Return actionable Discord/WOM linking exceptions for staff."""

        memberships = group_data.get(
            "memberships",
            []
        )

        cursor.execute(
            """
            SELECT discord_id, rsn, wom_id
            FROM links
            WHERE guild_id = ?
            """,
            (guild.id,)
        )
        primary_links = cursor.fetchall()

        cursor.execute(
            """
            SELECT discord_id, rsn, wom_id
            FROM alt_links
            WHERE guild_id = ?
            """,
            (guild.id,)
        )
        alt_links = cursor.fetchall()

        wom_by_id = {
            membership["player"]["id"]: membership
            for membership in memberships
            if isinstance(membership, dict)
            and isinstance(
                membership.get("player"),
                dict
            )
            and membership["player"].get("id") is not None
        }

        wom_by_name = {
            membership["player"]["username"].casefold(): membership
            for membership in memberships
            if isinstance(membership, dict)
            and isinstance(
                membership.get("player"),
                dict
            )
            and membership["player"].get("username")
        }

        linked_wom_ids = set()
        linked_rsn_keys = set()

        for (
            discord_id,
            stored_rsn,
            stored_wom_id
        ) in (
            primary_links
            + alt_links
        ):
            membership = None

            if stored_wom_id is not None:
                membership = wom_by_id.get(
                    stored_wom_id
                )

            if (
                membership is None
                and stored_rsn
            ):
                membership = wom_by_name.get(
                    stored_rsn.casefold()
                )

            if membership is None:
                continue

            player = membership.get(
                "player",
                {}
            )
            player_id = player.get(
                "id"
            )
            rsn = player.get(
                "username"
            )

            if player_id is not None:
                linked_wom_ids.add(
                    player_id
                )

            if rsn:
                linked_rsn_keys.add(
                    rsn.casefold()
                )

        linked_members_without_active_rsn = []

        for (
            discord_id,
            stored_rsn,
            stored_wom_id
        ) in primary_links:
            member = guild.get_member(
                discord_id
            )

            if (
                member is None
                or has_non_clan_role(
                    member
                )
                or not has_novus_member_role(
                    member
                )
            ):
                continue

            accounts = self.get_linked_account_tenures(
                cursor,
                guild.id,
                group_data.get("id"),
                discord_id,
                memberships
            )

            if not accounts:
                linked_members_without_active_rsn.append(
                    f"- {member.mention}: "
                    "no linked RSN is currently active in Novus"
                )

        unlinked_full_wom_accounts = []

        for membership in memberships:
            wom_role = (
                membership.get(
                    "role",
                    ""
                ).lower()
            )

            if wom_role in NOVUS_TRIAL_WOM_RANKS:
                continue

            player = membership.get(
                "player",
                {}
            )
            player_id = player.get(
                "id"
            )
            rsn = player.get(
                "username"
            )

            if (
                player_id in linked_wom_ids
                or (
                    rsn
                    and rsn.casefold()
                        in linked_rsn_keys
                )
            ):
                continue

            if rsn:
                unlinked_full_wom_accounts.append(
                    f"- `{rsn}` "
                    f"({wom_role or 'unknown rank'})"
                )

        unlinked_full_wom_accounts.sort(
            key=str.casefold
        )

        return {
            "unlinked_full_wom_accounts": (
                unlinked_full_wom_accounts
            ),
            "linked_members_without_active_rsn": (
                linked_members_without_active_rsn
            ),
        }

    # ========================================================
    # ROLE SYNC ENGINE
    # ========================================================

    async def remove_inactive_member_roles(
        self,
        cursor,
        guild: discord.Guild,
        member: discord.Member
    ):
        """Remove membership/tenure roles when no linked RSN is active in Novus.

        Saved account links and tenure history are intentionally preserved so
        a future rejoin can restore the correct earned tenure.
        """

        cursor.execute(
            """
            SELECT wom_role, discord_role_id
            FROM role_mappings
            WHERE guild_id = ?
            """,
            (guild.id,)
        )

        mapped_tenure_role_ids = {
            discord_role_id
            for wom_role, discord_role_id
            in cursor.fetchall()
            if wom_role
            and wom_role.casefold()
                in NOVUS_TENURE_ROLES
        }

        roles_to_remove = []

        for role in member.roles:
            role_name = (
                getattr(
                    role,
                    "name",
                    ""
                )
                .strip()
                .casefold()
            )

            if (
                role_name == "member"
                or role.id in mapped_tenure_role_ids
                or role_name in NOVUS_TENURE_ROLES
            ):
                if (
                    not getattr(
                        role,
                        "managed",
                        False
                    )
                    and not role.is_default()
                ):
                    roles_to_remove.append(
                        role
                    )

        if not roles_to_remove:
            return {
                "status": "inactive_member_cleaned",
                "roles_removed": [],
            }

        try:
            await member.remove_roles(
                *roles_to_remove,
                reason=(
                    "No linked RSN is currently active "
                    "in the Novus WOM roster"
                )
            )
        except discord.Forbidden:
            return {
                "status": "inactive_cleanup_forbidden",
                "roles_removed": [],
            }
        except Exception:
            logger.exception(
                "Failed inactive-member role cleanup for %s (%s)",
                member,
                member.id
            )
            return {
                "status": "inactive_cleanup_error",
                "roles_removed": [],
            }

        return {
            "status": "inactive_member_cleaned",
            "roles_removed": [
                role.name
                for role in roles_to_remove
            ],
        }

    async def sync_one_member(
        self,
        guild: discord.Guild,
        member: discord.Member,
        group_data: dict,
        cursor
    ):
        """
        Sync one linked Discord member.

        Primary and alt accounts keep separate cumulative tenure. The Discord
        tenure role is based on whichever current linked in-clan account has
        independently earned the highest tenure rank. Discord tenure never
        moves downward automatically.
        """

        if has_non_clan_role(
            member
        ):
            forget_member_links(
                cursor,
                guild.id,
                member.id
            )
            return {
                "status": "non_clan_ignored"
            }

        if not has_novus_member_role(
            member
        ):
            return {
                "status": "not_current_member"
            }

        cursor.execute(
            """
            SELECT rsn, wom_id, dm_notifications_on
            FROM links
            WHERE guild_id = ?
            AND discord_id = ?
            """,
            (
                guild.id,
                member.id
            )
        )

        link = cursor.fetchone()

        if not link:
            return {
                "status": "not_linked"
            }

        rsn, wom_id, user_dm_on = link

        memberships = group_data.get(
            "memberships",
            []
        )

        wom_by_id = {
            membership["player"]["id"]: membership
            for membership in memberships
            if isinstance(membership, dict)
            and isinstance(
                membership.get("player"),
                dict
            )
            and membership["player"].get("id") is not None
        }

        wom_by_username = {
            membership["player"]["username"].lower(): membership
            for membership in memberships
            if isinstance(membership, dict)
            and isinstance(
                membership.get("player"),
                dict
            )
            and membership["player"].get("username")
        }

        # ----------------------------------------------------
        # PRIMARY ACCOUNT
        # ----------------------------------------------------

        membership = None

        if wom_id:
            membership = wom_by_id.get(
                wom_id
            )

        if (
            membership is None
            and rsn
        ):
            membership = wom_by_username.get(
                rsn.lower()
            )

        current_rsn = rsn
        current_wom_id = wom_id
        current_wom_role = ""
        primary_rsn_changed = False
        previous_primary_rsn = rsn
        nickname_updated_from_wom = False
        nickname_update_failed = False

        if membership is not None:
            player = membership.get(
                "player",
                {}
            )

            wom_reported_rsn = player.get(
                "username",
                rsn
            )

            if (
                rsn
                and wom_reported_rsn
                and rsn.casefold()
                    == wom_reported_rsn.casefold()
            ):
                # Case-only differences are not treated as name changes.
                current_rsn = rsn
            else:
                current_rsn = wom_reported_rsn

            current_wom_id = player.get(
                "id"
            )

            current_wom_role = (
                membership.get(
                    "role",
                    ""
                ).lower()
            )

            primary_rsn_changed = bool(
                current_rsn
                and rsn
                and current_rsn.casefold()
                    != rsn.casefold()
            )

            if (
                current_wom_id != wom_id
                or primary_rsn_changed
            ):
                cursor.execute(
                    """
                    UPDATE links
                    SET rsn = ?, wom_id = ?
                    WHERE guild_id = ?
                    AND discord_id = ?
                    """,
                    (
                        current_rsn,
                        current_wom_id,
                        guild.id,
                        member.id
                    )
                )

        # ----------------------------------------------------
        # ALT ACCOUNTS
        # ----------------------------------------------------

        (
            verified_alts,
            missing_alts
        ) = self.sync_alt_links_in_database(
            cursor,
            guild.id,
            member.id,
            wom_by_id,
            wom_by_username
        )

        # WOM is authoritative for the primary RSN. Preserve any currently
        # linked in-clan alts after the member's existing '|' or '/' separator.
        if (
            primary_rsn_changed
            and current_rsn
        ):
            desired_nickname = build_wom_server_nickname(
                current_rsn,
                verified_alts,
                member.nick
            )

            if (
                desired_nickname
                and member.nick != desired_nickname
            ):
                try:
                    await member.edit(
                        nick=desired_nickname,
                        reason=(
                            "Primary RSN renamed in Wise Old Man"
                        )
                    )
                    nickname_updated_from_wom = True
                except discord.Forbidden:
                    nickname_update_failed = True
                    logger.warning(
                        "Could not update Novus nickname for %s (%s) "
                        "after WOM rename %r -> %r: missing role hierarchy "
                        "or Manage Nicknames permission.",
                        member,
                        member.id,
                        previous_primary_rsn,
                        current_rsn,
                    )
                except Exception as e:
                    nickname_update_failed = True
                    logger.error(
                        "Nickname update failed for %s (%s) after WOM "
                        "rename %r -> %r: %s",
                        member,
                        member.id,
                        previous_primary_rsn,
                        current_rsn,
                        e,
                    )

        account_tenures = (
            self.get_linked_account_tenures(
                cursor,
                guild.id,
                group_data.get("id"),
                member.id,
                memberships
            )
        )

        if not account_tenures:
            cleanup_result = await self.remove_inactive_member_roles(
                cursor,
                guild,
                member
            )
            cleanup_result.update(
                {
                    "rsn": current_rsn,
                    "wom_role": current_wom_role,
                    "verified_alts": verified_alts,
                    "missing_alts": missing_alts,
                }
            )
            return cleanup_result

        # ----------------------------------------------------
        # DISCORD ROLE CONFIG
        # ----------------------------------------------------

        cursor.execute(
            """
            SELECT wom_role, discord_role_id
            FROM role_mappings
            WHERE guild_id = ?
            """,
            (guild.id,)
        )

        configured_map = {
            wom_role.lower(): guild.get_role(
                discord_role_id
            )
            for wom_role, discord_role_id
            in cursor.fetchall()
            if guild.get_role(
                discord_role_id
            )
        }

        tenure_role_by_id = {
            role.id: wom_role
            for wom_role, role
            in configured_map.items()
            if wom_role in NOVUS_TENURE_ROLES
            and role is not None
        }

        staff_roles = {
            guild.get_role(
                role_id
            )
            for role_id
            in NOVUS_STAFF_WOM_ROLES.values()
            if guild.get_role(
                role_id
            )
        }

        member_roles = set(
            member.roles
        )

        tenure_roles_to_add = []
        tenure_roles_to_remove = []
        staff_roles_to_add = []
        staff_roles_to_remove = []

        # ----------------------------------------------------
        # STAFF ROLE SYNC - PRIMARY ACCOUNT ONLY
        # ----------------------------------------------------

        staff_target_role = None

        if (
            membership is not None
            and current_wom_role
                in NOVUS_STAFF_WOM_ROLES
        ):
            staff_role_id = (
                NOVUS_STAFF_WOM_ROLES[
                    current_wom_role
                ]
            )

            staff_target_role = guild.get_role(
                staff_role_id
            )

            if staff_target_role is None:
                return {
                    "status": "staff_role_missing",
                    "rsn": current_rsn,
                    "wom_role": current_wom_role,
                    "role_id": staff_role_id,
                    "verified_alts": verified_alts,
                    "missing_alts": missing_alts,
                }

            if staff_target_role not in member_roles:
                staff_roles_to_add.append(
                    staff_target_role
                )

            for role in staff_roles:
                if (
                    role != staff_target_role
                    and role in member_roles
                ):
                    staff_roles_to_remove.append(
                        role
                    )

        elif (
            membership is not None
            and current_wom_role
                in NOVUS_TENURE_ROLES
        ):
            for role in staff_roles:
                if role in member_roles:
                    staff_roles_to_remove.append(
                        role
                    )

        # All other specialty/status roles are left alone. They may coexist
        # with the underlying tenure role.

        # ----------------------------------------------------
        # TENURE SYNC - HIGHEST LINKED ACCOUNT, NEVER DOWNGRADE
        # ----------------------------------------------------

        basis = (
            self.get_highest_discord_tenure_basis(
                account_tenures
            )
        )

        expected_rank = (
            basis.get(
                "expected_rank"
            )
            if basis
            else None
        )

        expected_role = (
            configured_map.get(
                expected_rank
            )
            if expected_rank
            else None
        )

        current_discord_tenure_rank = None
        tenure_status = "no_tenure_account"

        discord_tenure_ranks = [
            tenure_role_by_id[
                role.id
            ]
            for role in member.roles
            if role.id in tenure_role_by_id
        ]

        if discord_tenure_ranks:
            current_discord_tenure_rank = max(
                discord_tenure_ranks,
                key=lambda rank:
                    TENURE_RANK_ORDER[
                        rank
                    ]
            )

        if expected_rank is not None:
            if expected_role is None:
                tenure_status = "not_mapped"
            else:
                expected_level = (
                    TENURE_RANK_ORDER[
                        expected_rank
                    ]
                )

                current_level = (
                    TENURE_RANK_ORDER[
                        current_discord_tenure_rank
                    ]
                    if current_discord_tenure_rank
                    else -1
                )

                if current_level < expected_level:
                    if expected_role not in member_roles:
                        tenure_roles_to_add.append(
                            expected_role
                        )

                    for role in member.roles:
                        mapped_rank = (
                            tenure_role_by_id.get(
                                role.id
                            )
                        )

                        if (
                            mapped_rank is not None
                            and TENURE_RANK_ORDER[
                                mapped_rank
                            ] < expected_level
                        ):
                            tenure_roles_to_remove.append(
                                role
                            )

                    tenure_status = "promoted"

                else:
                    # Keep the highest existing tenure role even if the
                    # account that originally earned it later leaves Novus.
                    # Only lower duplicate tenure roles are removed.
                    retained_level = max(
                        current_level,
                        expected_level
                    )

                    for role in member.roles:
                        mapped_rank = (
                            tenure_role_by_id.get(
                                role.id
                            )
                        )

                        if (
                            mapped_rank is not None
                            and TENURE_RANK_ORDER[
                                mapped_rank
                            ] < retained_level
                        ):
                            tenure_roles_to_remove.append(
                                role
                            )

                    if tenure_roles_to_remove:
                        tenure_status = (
                            "duplicate_cleanup"
                        )
                    elif current_level > expected_level:
                        tenure_status = (
                            "retained_rank"
                        )
                    else:
                        tenure_status = (
                            "current"
                        )

        # ----------------------------------------------------
        # PER-ACCOUNT IN-GAME TENURE MISMATCHES
        # ----------------------------------------------------

        wom_tenure_mismatches = []

        for account in account_tenures:
            wom_role = account.get(
                "wom_role"
            )
            account_expected = account.get(
                "expected_rank"
            )

            if (
                wom_role in TENURE_RANK_ORDER
                and account_expected
                    in TENURE_RANK_ORDER
                and TENURE_RANK_ORDER[
                    wom_role
                ] < TENURE_RANK_ORDER[
                    account_expected
                ]
            ):
                wom_tenure_mismatches.append(
                    account
                )

        wom_tenure_mismatch = bool(
            wom_tenure_mismatches
        )

        wom_tenure_direction = (
            "behind"
            if wom_tenure_mismatch
            else None
        )

        roles_to_add = list(
            dict.fromkeys(
                tenure_roles_to_add
                + staff_roles_to_add
            )
        )

        roles_to_remove = list(
            dict.fromkeys(
                tenure_roles_to_remove
                + staff_roles_to_remove
            )
        )

        # ----------------------------------------------------
        # APPLY DISCORD CHANGES
        # ----------------------------------------------------

        try:
            if roles_to_add:
                await member.add_roles(
                    *roles_to_add,
                    reason=(
                        "Novus role synchronization"
                    )
                )

            if roles_to_remove:
                await member.remove_roles(
                    *roles_to_remove,
                    reason=(
                        "Novus role synchronization"
                    )
                )

        except discord.Forbidden:
            return {
                "status": "forbidden",
                "rsn": current_rsn,
                "wom_role": current_wom_role,
                "expected_tenure_rank": expected_rank,
            }

        except Exception as e:
            logger.error(
                f"Role update failed for "
                f"{member} ({member.id}): {e}"
            )

            return {
                "status": "error",
                "rsn": current_rsn,
                "wom_role": current_wom_role,
                "expected_tenure_rank": expected_rank,
            }

        return {
            "status": "success",
            "rsn": current_rsn,
            "wom_role": current_wom_role,
            "joined_at": (
                get_membership_joined_at(
                    membership
                )
                if membership is not None
                else None
            ),
            "effective_joined_at": (
                basis.get(
                    "effective_joined_at"
                )
                if basis
                else None
            ),
            "prior_tenure_days": (
                basis.get(
                    "prior_service",
                    datetime.timedelta(0)
                ).total_seconds() / 86400
                if basis
                else 0
            ),
            "expected_tenure_rank": expected_rank,
            "expected_tenure_role": expected_role,
            "current_discord_tenure_rank": (
                current_discord_tenure_rank
            ),
            "tenure_status": tenure_status,
            "tenure_basis_rsn": (
                basis.get(
                    "rsn"
                )
                if basis
                else None
            ),
            "tenure_basis_type": (
                basis.get(
                    "account_type"
                )
                if basis
                else None
            ),
            "account_tenures": account_tenures,
            "wom_tenure_mismatch": (
                wom_tenure_mismatch
            ),
            "wom_tenure_mismatches": (
                wom_tenure_mismatches
            ),
            "wom_tenure_direction": (
                wom_tenure_direction
            ),
            "staff_target_role": staff_target_role,
            "tenure_roles_added": tenure_roles_to_add,
            "tenure_roles_removed": tenure_roles_to_remove,
            "staff_roles_added": staff_roles_to_add,
            "staff_roles_removed": staff_roles_to_remove,
            "roles_added": roles_to_add,
            "roles_removed": roles_to_remove,
            "verified_alts": verified_alts,
            "missing_alts": missing_alts,
            "user_dm_on": user_dm_on,
            "primary_rsn_changed": primary_rsn_changed,
            "previous_primary_rsn": previous_primary_rsn,
            "nickname_updated_from_wom": nickname_updated_from_wom,
            "nickname_update_failed": nickname_update_failed,
        }

    # ========================================================
    # /TENUREHISTORY
    # ========================================================

    @app_commands.command(
        name="tenurehistory",
        description="Inspect WOM join/leave events and credited tenure for a Novus member."
    )
    @app_commands.describe(member="Active Novus member to inspect.")
    async def tenurehistory(
        self,
        interaction: discord.Interaction,
        member: discord.Member
    ):
        if not has_novus_command_permission(interaction.user):
            await interaction.response.send_message(
                "Only Novus staff can inspect historical tenure.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        if guild is None or not has_novus_member_role(member):
            await interaction.followup.send(
                "Select an active Discord Novus Member.",
                ephemeral=True
            )
            return

        conn = sqlite3.connect("wom_multi.db")
        try:
            c = conn.cursor()
            c.execute(
                "SELECT group_id FROM guild_configs WHERE guild_id = ?",
                (guild.id,)
            )
            row = c.fetchone()
            group_id = row[0] if row else None
            c.execute(
                "SELECT rsn, wom_id FROM links WHERE guild_id = ? AND discord_id = ?",
                (guild.id, member.id)
            )
            link = c.fetchone()
            if not group_id or not link:
                await interaction.followup.send(
                    "This member must have a linked primary RSN and a configured WOM group.",
                    ephemeral=True
                )
                return

            group_data, api_status = await self.get_wom_group(group_id)
            if not isinstance(group_data, dict):
                await interaction.followup.send(
                    "WOM group data is unavailable; no tenure records were changed.",
                    ephemeral=True
                )
                return

            memberships = group_data.get("memberships", [])
            if not is_active_novus_member_link(
                member, link[0], link[1], memberships
            ):
                await interaction.followup.send(
                    "This account is not a current Novus WOM member.",
                    ephemeral=True
                )
                return

            membership = find_primary_wom_membership(
                link[0], link[1], memberships
            )
            player_id = membership["player"]["id"]
            joined_at = get_membership_joined_at(membership)
            membership_created = parse_wom_datetime(
                membership.get("createdAt")
            )

            # Import available historical events before showing the evidence.
            await self.prepare_tenure_history(c, guild, group_data)
            diagnostic = get_tenure_diagnostics(
                c, guild.id, group_id, player_id, joined_at
            )
            conn.commit()
        finally:
            conn.close()

        documented = diagnostic["wom_previous_days"]
        manual = diagnostic["manual_previous_days"]
        credited = max(documented, manual)
        event_lines = [
            f"- `{kind}` {stamp[:10]}"
            for kind, stamp in diagnostic["events"][-12:]
        ]
        if not event_lines:
            event_lines = ["No WOM group join/leave activity saved for this RSN."]

        join_text = joined_at.date().isoformat() if joined_at else "Unavailable"
        created_text = (
            membership_created.date().isoformat()
            if membership_created else "Unavailable"
        )
        history_status = (
            "Complete for recorded WOM group activity"
            if diagnostic["backfill_complete"]
            else "Historical import still incomplete or unavailable"
        )
        last_checked = diagnostic["history_last_checked"] or "Not successfully imported"

        response = (
            f"**Novus Tenure History: {member.mention}**\n"
            f"RSN: `{membership['player']['username']}`\n"
            f"Current in-game join date: `{join_text}`\n"
            f"Added to WOM group: `{created_text}` "
            "(not necessarily an in-game join date)\n"
            f"Previous WOM service recovered: **{documented:.1f} days**\n"
            f"Manual previous service recorded: **{manual:.1f} days**\n"
            f"Previous service actually credited: **{credited:.1f} days**\n"
            f"Prior WOM joined/left pairs: **{diagnostic['matched_previous_periods']}**\n"
            f"Previously observed closed stints: "
            f"**{diagnostic['closed_observed_stints']}**\n"
            f"WOM activity imported through offset: "
            f"**{diagnostic['history_offset']}**\n"
            f"Import status: **{history_status}**\n"
            f"Last import: `{last_checked[:19]}`\n\n"
            "**Saved WOM group events (last 12)**\n"
            + "\n".join(event_lines)
        )
        if joined_at and documented == 0 and manual == 0:
            response += (
                "\n\n**Why prior tenure is missing:** No completed "
                "previous Novus membership period has been reconstructed. "
                "An earlier WOM-group creation date alone does not prove "
                "continuous in-game clan membership."
            )
        if len(response) > 1900:
            response = response[:1850] + "\n...additional details omitted."

        await interaction.followup.send(response, ephemeral=True)

    # ========================================================
    # /TENURE
    # ========================================================

    @app_commands.command(
        name="tenure",
        description="Check a Novus member's linked account ranks and tenure."
    )
    @app_commands.describe(
        member="Novus Discord member to inspect."
    )
    async def tenure(
        self,
        interaction: discord.Interaction,
        member: discord.Member
    ):
        if not has_novus_command_permission(
            interaction.user
        ):
            await interaction.response.send_message(
                "You must be a Trial Moderator, Moderator, or Council member to use this command.",
                ephemeral=True
            )
            return

        await interaction.response.defer(
            ephemeral=True,
            thinking=True
        )

        guild = interaction.guild

        if guild is None:
            await interaction.followup.send(
                "This command can only be used inside the Novus server.",
                ephemeral=True
            )
            return

        conn = sqlite3.connect(
            "wom_multi.db"
        )

        try:
            c = conn.cursor()
            c.execute(
                """
                SELECT group_id
                FROM guild_configs
                WHERE guild_id = ?
                """,
                (guild.id,)
            )
            row = c.fetchone()

            if not row or not row[0]:
                await interaction.followup.send(
                    "No Wise Old Man group is configured.",
                    ephemeral=True
                )
                return

            group_data, api_status = await self.get_wom_group(
                row[0]
            )

            if not isinstance(
                group_data,
                dict
            ):
                await interaction.followup.send(
                    "Could not retrieve the Novus WOM group.",
                    ephemeral=True
                )
                return

            await self.prepare_tenure_history(
                c,
                guild,
                group_data
            )

            accounts = self.get_linked_account_tenures(
                c,
                guild.id,
                group_data.get("id"),
                member.id,
                group_data.get(
                    "memberships",
                    []
                )
            )

            if not accounts:
                await interaction.followup.send(
                    "No linked account for that member is currently in the Novus WOM clan.",
                    ephemeral=True
                )
                return

            c.execute(
                """
                SELECT wom_role, discord_role_id
                FROM role_mappings
                WHERE guild_id = ?
                """,
                (guild.id,)
            )

            tenure_rank_by_role_id = {
                discord_role_id: wom_role.lower()
                for wom_role, discord_role_id
                in c.fetchall()
                if wom_role.lower()
                    in NOVUS_TENURE_ROLES
            }

            discord_tenure_ranks = [
                tenure_rank_by_role_id[
                    role.id
                ]
                for role in member.roles
                if role.id
                    in tenure_rank_by_role_id
            ]

            current_discord_rank = (
                max(
                    discord_tenure_ranks,
                    key=lambda rank:
                        TENURE_RANK_ORDER[
                            rank
                        ]
                )
                if discord_tenure_ranks
                else None
            )

            basis = (
                self.get_highest_discord_tenure_basis(
                    accounts
                )
            )

            lines = [
                f"Novus Tenure - {member.display_name}"
            ]

            for account in accounts:
                label = (
                    "Primary"
                    if account[
                        "account_type"
                    ] == "primary"
                    else "Alt"
                )

                joined = (
                    str(
                        account[
                            "joined_at"
                        ].date()
                    )
                    if account.get(
                        "joined_at"
                    )
                    else "unknown"
                )

                prior_days = int(
                    account.get(
                        "prior_service",
                        datetime.timedelta(0)
                    ).total_seconds()
                    / 86400
                )

                expected = account.get(
                    "expected_rank"
                )
                wom_role = (
                    account.get(
                        "wom_role"
                    )
                    or "unknown"
                )

                account_lines = [
                    "",
                    f"{label}: {account['rsn']}",
                    (
                        f"WOM rank: {wom_role} | "
                        f"current stint: {joined} | "
                        f"previous service: {prior_days}d"
                    ),
                ]

                if expected:
                    account_lines.append(
                        f"Tenure-qualified rank: {expected}"
                    )

                    next_rank = account.get(
                        "next_rank"
                    )
                    next_date = account.get(
                        "next_eligible_at"
                    )

                    if (
                        next_rank
                        and next_date
                    ):
                        account_lines.append(
                            f"Next milestone: {next_rank} on {next_date.date()}"
                        )

                    if (
                        wom_role
                            in TENURE_RANK_ORDER
                        and TENURE_RANK_ORDER[
                            wom_role
                        ] < TENURE_RANK_ORDER[
                            expected
                        ]
                    ):
                        due_date = account.get(
                            "eligible_at"
                        )
                        due_text = (
                            f"In-game promotion due: "
                            f"{wom_role} -> {expected}"
                        )
                        if due_date:
                            due_text += (
                                f" on {due_date.date()}"
                            )
                        account_lines.append(
                            due_text
                        )

                lines.extend(
                    account_lines
                )

            lines.extend(
                [
                    "",
                    (
                        "Discord tenure: "
                        f"{current_discord_rank or 'none'}"
                    ),
                ]
            )

            if basis:
                lines.append(
                    (
                        "Discord tenure basis: "
                        f"{basis['rsn']} "
                        f"({basis['account_type']}) -> "
                        f"{basis['expected_rank']}"
                    )
                )

                if (
                    current_discord_rank
                    and TENURE_RANK_ORDER[
                        current_discord_rank
                    ] > TENURE_RANK_ORDER[
                        basis["expected_rank"]
                    ]
                ):
                    lines.append(
                        "Higher existing Discord tenure retained; automatic downgrades are disabled."
                    )

            response = "\n".join(
                lines
            )

            if len(response) > 1950:
                response = (
                    response[:1900]
                    + "\n\n...additional account details omitted."
                )

            conn.commit()

            await interaction.followup.send(
                response,
                ephemeral=True
            )

        finally:
            conn.close()

    # ========================================================
    # /TENURECREDIT
    # ========================================================

    @app_commands.command(
        name="tenurecredit",
        description="Set verified days previously spent in Novus before a rejoin."
    )
    @app_commands.describe(
        member="Returning Novus member to credit.",
        previous_days="Total verified previous Novus days, excluding this stint. 0 clears."
    )
    async def tenurecredit(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        previous_days: app_commands.Range[int, 0, 3650]
    ):
        if not any(
            role.id in TENURE_CREDIT_ROLE_IDS
            for role in interaction.user.roles
        ):
            await interaction.response.send_message(
                "Only Novus Council or Moderators can set historical tenure.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        if guild is None or not has_novus_member_role(member):
            await interaction.followup.send(
                "This requires an active Discord Novus Member.",
                ephemeral=True
            )
            return

        conn = sqlite3.connect("wom_multi.db")
        try:
            c = conn.cursor()
            c.execute(
                "SELECT group_id FROM guild_configs WHERE guild_id = ?",
                (guild.id,)
            )
            config = c.fetchone()
            group_id = config[0] if config else None
            c.execute(
                "SELECT rsn, wom_id FROM links WHERE guild_id = ? AND discord_id = ?",
                (guild.id, member.id)
            )
            link = c.fetchone()

            if not group_id or not link:
                await interaction.followup.send(
                    "This member must have a linked primary RSN and a configured WOM group.",
                    ephemeral=True
                )
                return

            group_data, status = await self.get_wom_group(group_id)
            if not isinstance(group_data, dict):
                await interaction.followup.send(
                    "WOM is unavailable. No historical credit was changed.",
                    ephemeral=True
                )
                return

            memberships = group_data.get("memberships", [])
            if not is_active_novus_member_link(
                member, link[0], link[1], memberships
            ):
                await interaction.followup.send(
                    "The linked primary account is not in the current Novus WOM group.",
                    ephemeral=True
                )
                return

            current_membership = find_primary_wom_membership(
                link[0], link[1], memberships
            )
            player_id = current_membership["player"]["id"]
            joined_at = get_membership_joined_at(current_membership)
            if joined_at is None:
                await interaction.followup.send(
                    "WOM has no current clan join date. No credit was changed.",
                    ephemeral=True
                )
                return

            # Update our group events before computing the automatic credit.
            await self.prepare_tenure_history(c, guild, group_data)
            recorded = prior_tenure(
                c, guild.id, group_id, player_id, joined_at
            )
            set_manual_previous_tenure(
                c, guild.id, group_id, player_id, previous_days
            )
            effective, previous = cumulative_effective_join(
                c, guild.id, group_id, player_id, joined_at
            )
            conn.commit()

            message = (
                f"Historical service updated for {member.mention} "
                f"(`{current_membership['player']['username']}`).\n"
                f"Manual previous service: **{previous_days} days**\n"
                f"Recorded WOM previous service: **{recorded.days} days**\n"
                f"Previous service credited (whichever is greater): "
                f"**{previous.total_seconds() / 86400:.1f} days**\n"
                "The current stint is counted separately. "
                "Use `/syncuser` to recalculate their rank now."
            )
            await interaction.followup.send(message, ephemeral=True)
        finally:
            conn.close()

    # ========================================================
    # /SYNCUSER
    # ========================================================

    @app_commands.command(
        name="syncuser",
        description="Immediately sync one linked Novus member."
    )
    @app_commands.describe(
        member="The Discord member to sync."
    )
    async def syncuser(
        self,
        interaction: discord.Interaction,
        member: discord.Member
    ):
        if not has_novus_command_permission(
            interaction.user
        ):
            await interaction.response.send_message(
                "You must be a Trial Moderator, Moderator, or Council member to use this command.",
                ephemeral=True
            )
            return

        await interaction.response.defer(
            ephemeral=True,
            thinking=True
        )

        guild = interaction.guild

        if guild is None:
            await interaction.followup.send(
                "This command can only be used inside the Novus server.",
                ephemeral=True
            )
            return

        conn = sqlite3.connect(
            "wom_multi.db"
        )
        c = conn.cursor()

        c.execute(
            """
            SELECT group_id
            FROM guild_configs
            WHERE guild_id = ?
            """,
            (guild.id,)
        )

        config = c.fetchone()

        if not config or not config[0]:
            conn.close()
            await interaction.followup.send(
                "No Wise Old Man group is configured.",
                ephemeral=True
            )
            return

        group_data, api_status = await self.get_wom_group(
            config[0]
        )

        if group_data is None:
            conn.close()

            message = (
                f"Wise Old Man returned API error `{api_status}`."
                if api_status
                else "The Wise Old Man API request failed."
            )

            await interaction.followup.send(
                message,
                ephemeral=True
            )
            return

        await self.prepare_tenure_history(c, guild, group_data)

        result = await self.sync_one_member(
            guild,
            member,
            group_data,
            c
        )

        conn.commit()
        conn.close()

        status = result["status"]

        if status == "non_clan_ignored":
            await interaction.followup.send(
                f"{member.mention} has Guest or Clan Friend and is excluded "
                "from Novus Sync. Any stored account links have been forgotten.",
                ephemeral=True
            )
            return

        if status == "not_linked":
            await interaction.followup.send(
                f"{member.mention} is not linked yet. Use `/linkuser` first.",
                ephemeral=True
            )
            return

        if status == "inactive_member_cleaned":
            removed_roles = result.get(
                "roles_removed",
                []
            )
            removed_text = (
                ", ".join(
                    f"`{role_name}`"
                    for role_name in removed_roles
                )
                if removed_roles
                else "no removable membership roles were present"
            )
            await interaction.followup.send(
                f"{member.mention} has no linked RSN currently active in Novus. "
                f"Removed: {removed_text}. Saved account links and tenure history "
                "were preserved for a future rejoin.",
                ephemeral=True
            )
            return

        if status in (
            "inactive_cleanup_forbidden",
            "inactive_cleanup_error"
        ):
            await interaction.followup.send(
                f"{member.mention} has no active linked RSN, but I could not "
                "remove the Member/tenure roles. Check the bot's Manage Roles "
                "permission and role hierarchy.",
                ephemeral=True
            )
            return

        if status == "not_current_member":
            await interaction.followup.send(
                f"{member.mention} is not an active Novus member. "
                "The Discord Member role and at least one linked RSN in the "
                "current Novus WOM group are required.",
                ephemeral=True
            )
            return

        if status == "staff_role_missing":
            await interaction.followup.send(
                f"Found `{result['rsn']}` as WOM rank "
                f"`{result['wom_role']}`, but the matching "
                f"Novus staff role could not be found.",
                ephemeral=True
            )
            return

        if status == "forbidden":
            await interaction.followup.send(
                "I could not update that member's roles. Check the Novus Sync "
                "role hierarchy and Manage Roles permission.",
                ephemeral=True
            )
            return

        if status == "error":
            await interaction.followup.send(
                "Something went wrong while updating that member's roles.",
                ephemeral=True
            )
            return

        added = result.get(
            "roles_added",
            []
        )
        removed = result.get(
            "roles_removed",
            []
        )

        heading = (
            "Novus Sync Complete"
            if added or removed
            else "Already synchronized"
        )

        current_tenure = result.get(
            "current_discord_tenure_rank"
        )
        expected_tenure = result.get(
            "expected_tenure_rank"
        )
        tenure_status = result.get(
            "tenure_status"
        )

        response = (
            f"**{heading}**\n\n"
            f"Member: {member.mention}\n"
            f"Primary RSN: `{result['rsn']}`\n"
            f"WOM Rank: `{result['wom_role']}`"
        )

        joined_at = result.get(
            "joined_at"
        )

        if joined_at:
            response += (
                f"\nCurrent Clan Join Date: `{joined_at.date()}`"
            )
            prior_days = result.get("prior_tenure_days", 0)
            if prior_days > 0:
                response += (
                    f"\nPrevious Novus Service Credited: "
                    f"**{prior_days:.1f} days**"
                )
            response += (
                f"\nDocumented Clan Tenure: "
                f"**{(datetime.datetime.now(datetime.timezone.utc) - joined_at).total_seconds() / 86400 + prior_days:.1f} days**"
                " (may exclude older stints not recorded by WOM)"
            )

        if expected_tenure:
            response += (
                f"\nExpected Tenure Rank: `{expected_tenure}`"
            )

            basis_rsn = result.get(
                "tenure_basis_rsn"
            )
            basis_type = result.get(
                "tenure_basis_type"
            )
            if basis_rsn:
                response += (
                    f"\nDiscord Tenure Basis: `{basis_rsn}` "
                    f"({basis_type})"
                )

        response += (
            f"\nDiscord Tenure Before Sync: "
            f"`{current_tenure or 'none'}`"
        )

        tenure_messages = {
            "promoted": "Promoted upward based on tenure.",
            "current": "Tenure rank is correct.",
            "duplicate_cleanup": "Removed duplicate lower tenure role(s).",
            "retained_rank": "Existing tenure rank retained. Historical Novus service may be missing from WOM records; no demotion was made.",
            "missing_join_date": "WOM join date is missing. No tenure change was made.",
            "not_mapped": "Expected tenure rank is not mapped to a Discord role. No tenure change was made.",
        }

        if tenure_status in tenure_messages:
            response += (
                f"\nTenure Check: **{tenure_messages[tenure_status]}**"
            )

        if result.get("wom_tenure_mismatch"):
            response += "\nWOM Tenure Mismatch:"
            for account in result.get(
                "wom_tenure_mismatches",
                []
            ):
                response += (
                    f"\n- `{account['rsn']}` ({account['account_type']}): "
                    f"`{account['wom_role']}` -> expected "
                    f"`{account['expected_rank']}`"
                )

        added_text = (
            ", ".join(role.mention for role in added)
            if added
            else "None"
        )
        removed_text = (
            ", ".join(role.mention for role in removed)
            if removed
            else "None"
        )

        response += (
            f"\nAdded: {added_text}\n"
            f"Removed: {removed_text}"
        )

        verified_alts = result.get(
            "verified_alts",
            []
        )
        if verified_alts:
            response += (
                "\n\nVerified alts: "
                + ", ".join(
                    f"`{alt}`"
                    for alt in verified_alts
                )
            )

        await interaction.followup.send(
            response[:1950],
            ephemeral=True
        )

    # ========================================================
    # /AUDIT
    # ========================================================

    @app_commands.command(
        name="audit",
        description="Show actionable Novus tenure, promotion, and link issues."
    )
    async def audit(
        self,
        interaction: discord.Interaction
    ):
        if not has_novus_command_permission(
            interaction.user
        ):
            await interaction.response.send_message(
                "You must be a Trial Moderator, Moderator, or Council member to use this command.",
                ephemeral=True
            )
            return

        await interaction.response.defer(
            ephemeral=True,
            thinking=True
        )

        guild = interaction.guild

        if guild is None:
            await interaction.followup.send(
                "This command can only be used inside the Novus server.",
                ephemeral=True
            )
            return

        conn = sqlite3.connect(
            "wom_multi.db"
        )

        try:
            c = conn.cursor()
            c.execute(
                """
                SELECT group_id
                FROM guild_configs
                WHERE guild_id = ?
                """,
                (guild.id,)
            )
            row = c.fetchone()

            if not row or not row[0]:
                await interaction.followup.send(
                    "No Wise Old Man group is configured.",
                    ephemeral=True
                )
                return

            group_data, api_status = await self.get_wom_group(
                row[0]
            )

            if not isinstance(
                group_data,
                dict
            ):
                await interaction.followup.send(
                    "Could not retrieve the Novus WOM group.",
                    ephemeral=True
                )
                return

            await self.prepare_tenure_history(
                c,
                guild,
                group_data
            )

            memberships = group_data.get(
                "memberships",
                []
            )

            promotion_audit = (
                self.get_promotion_audit_entries(
                    c,
                    guild,
                    group_data
                )
            )

            c.execute(
                """
                SELECT wom_role, discord_role_id
                FROM role_mappings
                WHERE guild_id = ?
                """,
                (guild.id,)
            )

            tenure_rank_by_role_id = {
                discord_role_id: wom_role.lower()
                for wom_role, discord_role_id
                in c.fetchall()
                if wom_role.lower()
                    in NOVUS_TENURE_ROLES
            }

            c.execute(
                """
                SELECT discord_id, rsn, wom_id
                FROM links
                WHERE guild_id = ?
                """,
                (guild.id,)
            )

            primary_links = c.fetchall()

            discord_mismatches = []
            inactive_links = []
            linked_ids = set()
            linked_names = set()
            linked_discord_members = 0

            for (
                discord_id,
                rsn,
                wom_id
            ) in primary_links:
                if wom_id is not None:
                    linked_ids.add(
                        wom_id
                    )

                if rsn:
                    linked_names.add(
                        rsn.casefold()
                    )

                member = guild.get_member(
                    discord_id
                )

                if (
                    member is None
                    or has_non_clan_role(
                        member
                    )
                    or not has_novus_member_role(
                        member
                    )
                ):
                    continue

                linked_discord_members += 1

                accounts = (
                    self.get_linked_account_tenures(
                        c,
                        guild.id,
                        group_data.get("id"),
                        discord_id,
                        memberships
                    )
                )

                if not accounts:
                    inactive_links.append(
                        (
                            f"- {member.mention}: "
                            "no linked RSN is currently in Novus"
                        )
                    )
                    continue

                basis = (
                    self.get_highest_discord_tenure_basis(
                        accounts
                    )
                )

                if basis:
                    current_ranks = [
                        tenure_rank_by_role_id[
                            role.id
                        ]
                        for role in member.roles
                        if role.id
                            in tenure_rank_by_role_id
                    ]

                    current_rank = (
                        max(
                            current_ranks,
                            key=lambda rank:
                                TENURE_RANK_ORDER[
                                    rank
                                ]
                        )
                        if current_ranks
                        else None
                    )

                    current_level = (
                        TENURE_RANK_ORDER[
                            current_rank
                        ]
                        if current_rank
                        else -1
                    )

                    expected_level = (
                        TENURE_RANK_ORDER[
                            basis[
                                "expected_rank"
                            ]
                        ]
                    )

                    if (
                        current_level
                        < expected_level
                    ):
                        discord_mismatches.append(
                            (
                                f"- {member.mention}: "
                                f"{current_rank or 'none'} -> "
                                f"{basis['expected_rank']} "
                                f"(basis {basis['rsn']})"
                            )
                        )

            c.execute(
                """
                SELECT rsn, wom_id
                FROM alt_links
                WHERE guild_id = ?
                """,
                (guild.id,)
            )

            alt_links = c.fetchall()

            for (
                alt_rsn,
                alt_wom_id
            ) in alt_links:
                if alt_wom_id is not None:
                    linked_ids.add(
                        alt_wom_id
                    )

                if alt_rsn:
                    linked_names.add(
                        alt_rsn.casefold()
                    )

            unmatched = []

            for membership in memberships:
                player = membership.get(
                    "player",
                    {}
                )

                wom_role = (
                    membership.get(
                        "role",
                        ""
                    ).lower()
                )

                if (
                    wom_role
                    in NOVUS_TRIAL_WOM_RANKS
                ):
                    continue

                player_id = player.get(
                    "id"
                )
                rsn = player.get(
                    "username"
                )

                if (
                    player_id in linked_ids
                    or (
                        rsn
                        and rsn.casefold()
                            in linked_names
                    )
                ):
                    continue

                if rsn:
                    unmatched.append(
                        (
                            f"- {rsn} "
                            f"({wom_role or 'unknown rank'})"
                        )
                    )

            due_today = (
                promotion_audit[
                    "due_today"
                ]
            )
            overdue = (
                promotion_audit[
                    "overdue"
                ]
            )

            response = (
                "**Novus Audit**\n"
                f"WOM accounts: {len(memberships)} | "
                f"Linked Discord members: {linked_discord_members} | "
                f"Saved alts: {len(alt_links)}\n"
                f"Due today: {len(due_today)} | "
                f"Overdue: {len(overdue)} | "
                f"Discord tenure mismatches: {len(discord_mismatches)} | "
                f"Unlinked full clan accounts: {len(unmatched)}"
            )

            sections = []

            if due_today:
                sections.append(
                    "**Due Today**\n"
                    + "\n".join(
                        (
                            f"- {entry['member'].mention}: "
                            f"{entry['current_rank']} → "
                            f"{entry['target_rank']}"
                        )
                        for entry
                        in due_today
                    )
                )

            if overdue:
                sections.append(
                    "**Overdue Promotions**\n"
                    + "\n".join(
                        (
                            f"- {entry['member'].mention}: "
                            f"{entry['current_rank']} → "
                            f"{entry['target_rank']} "
                            f"({entry['overdue_days']}d overdue)"
                        )
                        for entry
                        in overdue
                    )
                )

            if discord_mismatches:
                sections.append(
                    "**Discord Tenure Needs Sync**\n"
                    + "\n".join(
                        discord_mismatches
                    )
                )

            if unmatched:
                sections.append(
                    "**Unlinked Full Clan Accounts**\n"
                    + "\n".join(
                        unmatched
                    )
                )

            if inactive_links:
                sections.append(
                    (
                        "**Linked Discord Members With "
                        "No Active Linked RSN**\n"
                    )
                    + "\n".join(
                        inactive_links
                    )
                )

            missing_join = (
                promotion_audit[
                    "missing_join_dates"
                ]
            )

            if missing_join:
                sections.append(
                    "**Missing WOM Join Date**\n"
                    + "\n".join(
                        (
                            f"- {entry['member'].mention} "
                            f"({entry['rsn']} "
                            f"{entry['account_type']})"
                        )
                        for entry
                        in missing_join
                    )
                )

            if not sections:
                sections.append(
                    "No actionable tenure, promotion, or linking issues found."
                )

            response += (
                "\n\n"
                + "\n\n".join(
                    sections
                )
            )

            if len(response) > 1950:
                response = (
                    response[:1900]
                    + "\n\n...additional audit details omitted."
                )

            conn.commit()

            await interaction.followup.send(
                response,
                ephemeral=True
            )

        finally:
            conn.close()

    # ========================================================
    # /SYNCALL
    # ========================================================

    @app_commands.command(
        name="syncall",
        description="Immediately sync all linked Novus members."
    )
    async def syncall(
        self,
        interaction: discord.Interaction
    ):
        if not has_novus_command_permission(
            interaction.user
        ):
            await interaction.response.send_message(
                "You must be a Trial Moderator, Moderator, or Council member to use this command.",
                ephemeral=True
            )
            return

        await interaction.response.defer(
            ephemeral=True,
            thinking=True
        )

        guild = interaction.guild

        if guild is None:
            await interaction.followup.send(
                "This command can only be used inside the Novus server.",
                ephemeral=True
            )
            return

        conn = sqlite3.connect(
            "wom_multi.db"
        )
        c = conn.cursor()

        c.execute(
            """
            SELECT group_id
            FROM guild_configs
            WHERE guild_id = ?
            """,
            (guild.id,)
        )

        config = c.fetchone()

        if not config or not config[0]:
            conn.close()
            await interaction.followup.send(
                "No Wise Old Man group is configured.",
                ephemeral=True
            )
            return

        group_data, api_status = await self.get_wom_group(
            config[0]
        )

        if group_data is None:
            conn.close()
            await interaction.followup.send(
                "Could not retrieve the Novus WOM group.",
                ephemeral=True
            )
            return

        await self.prepare_tenure_history(c, guild, group_data)

        c.execute(
            """
            SELECT discord_id, rsn, wom_id
            FROM links
            WHERE guild_id = ?
            """,
            (guild.id,)
        )

        linked_primaries = c.fetchall()
        memberships = group_data.get("memberships", [])

        checked = 0
        changed = 0
        automatic_promotions = []
        wom_name_updates = []
        inactive_member_cleanups = []
        inactive_trial_cleanups = []
        wom_mismatches = []
        missing_join_dates = []
        mapping_issues = []
        skipped_members = []
        failed_members = []

        trial_cleanup_result = (
            await self.cleanup_inactive_trial_roles(
                c,
                guild,
                group_data
            )
        )
        inactive_trial_cleanups.extend(
            trial_cleanup_result.get(
                "cleaned",
                []
            )
        )
        failed_members.extend(
            trial_cleanup_result.get(
                "failed",
                []
            )
        )
        changed += len(
            inactive_trial_cleanups
        )
        for discord_id, rsn, wom_id in linked_primaries:
            member = guild.get_member(discord_id)

            if member is not None and has_non_clan_role(member):
                forget_member_links(c, guild.id, member.id)
                continue

            if (
                member is None
                or not has_novus_member_role(
                    member
                )
            ):
                continue

            result = await self.sync_one_member(
                guild,
                member,
                group_data,
                c
            )

            if result["status"] in (
                "non_clan_ignored",
                "not_current_member"
            ):
                continue

            checked += 1

            if result["status"] == "inactive_member_cleaned":
                changed += 1
                removed_roles = result.get(
                    "roles_removed",
                    []
                )
                removed_text = (
                    ", ".join(
                        f"`{role_name}`"
                        for role_name in removed_roles
                    )
                    if removed_roles
                    else "no roles"
                )
                inactive_member_cleanups.append(
                    f"- {member.mention}: removed {removed_text}; "
                    "saved RSN/tenure history retained"
                )
                continue

            if result["status"] != "success":
                issue_line = format_sync_issue(
                    member,
                    result
                )

                if classify_sync_issue(result) == "failed":
                    failed_members.append(issue_line)
                else:
                    skipped_members.append(issue_line)
                continue

            if (
                result["roles_added"]
                or result["roles_removed"]
                or result.get(
                    "nickname_updated_from_wom"
                )
            ):
                changed += 1

            if result.get(
                "primary_rsn_changed"
            ):
                rename_status = (
                    "Discord nickname updated"
                    if result.get(
                        "nickname_updated_from_wom"
                    )
                    else (
                        "Discord nickname update failed"
                        if result.get(
                            "nickname_update_failed"
                        )
                        else "saved RSN updated"
                    )
                )
                wom_name_updates.append(
                    f"- {member.mention}: "
                    f"`{result.get('previous_primary_rsn') or 'unknown'}` "
                    f"-> `{result['rsn']}` ({rename_status})"
                )

            if result.get("tenure_status") == "promoted":
                automatic_promotions.append(
                    f"- {member.mention}: "
                    f"`{result.get('current_discord_tenure_rank') or 'none'}` "
                    f"→ `{result['expected_tenure_rank']}`"
                )

            elif result.get("tenure_status") == "missing_join_date":
                missing_join_dates.append(
                    f"- {member.mention} (`{result['rsn']}`)"
                )

            elif result.get("tenure_status") == "not_mapped":
                mapping_issues.append(
                    f"- {member.mention} (`{result['rsn']}`): expected "
                    f"`{result.get('expected_tenure_rank')}`"
                )

            if result.get("wom_tenure_mismatch"):
                for account in result.get(
                    "wom_tenure_mismatches",
                    []
                ):
                    wom_mismatches.append(
                        f"- {member.mention}: "
                        f"`{account['wom_role']}` → "
                        f"`{account['expected_rank']}`"
                    )

        promotion_audit = (
            self.get_promotion_audit_entries(
                c,
                guild,
                group_data
            )
        )
        due_today_promotions = (
            promotion_audit[
                "due_today"
            ]
        )
        overdue_promotions = (
            promotion_audit[
                "overdue"
            ]
        )

        link_audit = self.get_link_audit_entries(
            c,
            guild,
            group_data
        )
        unlinked_full_wom_accounts = (
            link_audit[
                "unlinked_full_wom_accounts"
            ]
        )
        linked_members_without_active_rsn = (
            link_audit[
                "linked_members_without_active_rsn"
            ]
        )

        now = datetime.datetime.now().isoformat()

        c.execute(
            """
            UPDATE guild_configs
            SET last_sync = ?
            WHERE guild_id = ?
            """,
            (
                now,
                guild.id
            )
        )

        conn.commit()
        conn.close()

        clan_members = len(
            group_data.get("memberships", [])
        )

        response = (
            f"**Novus Sync Complete**\n\n"
            f"Clan members: **{clan_members}**\n"
            f"Linked primary members: **{checked}**\n"
            f"Members changed: **{changed}**\n"
            f"Automatic Discord tenure promotions: **{len(automatic_promotions)}**\n"
            f"Inactive members cleaned: **{len(inactive_member_cleanups)}**\n"
            f"Inactive trials cleaned: **{len(inactive_trial_cleanups)}**\n"
            f"WOM primary-name updates: **{len(wom_name_updates)}**\n"
            f"In-game promotions due today: **{len(due_today_promotions)}**\n"
            f"In-game promotions overdue: **{len(overdue_promotions)}**\n"
            f"WOM tenure mismatches: **{len(wom_mismatches)}**\n"
            f"Missing WOM join date: **{len(missing_join_dates)}**"
        )

        sections = []

        if inactive_member_cleanups:
            sections.append(
                "**Inactive Members Cleaned**\n"
                + "\n".join(
                    inactive_member_cleanups
                )
            )

        if inactive_trial_cleanups:
            sections.append(
                "**Inactive Trials Cleaned**\n"
                + "\n".join(
                    inactive_trial_cleanups
                )
            )

        if wom_name_updates:
            sections.append(
                "**WOM Primary Name Updates**\n"
                + "\n".join(
                    wom_name_updates
                )
            )

        if automatic_promotions:
            sections.append(
                "**Automatic Discord Tenure Promotions**\n"
                + "\n".join(automatic_promotions)
            )

        if due_today_promotions:
            sections.append(
                "**In-Game Promotions Due Today**\n"
                + "\n".join(
                    (
                        f"- {entry['member'].mention}: "
                        f"{entry['current_rank']} → "
                        f"{entry['target_rank']}"
                    )
                    for entry
                    in due_today_promotions
                )
            )

        if overdue_promotions:
            sections.append(
                "**Overdue In-Game Promotions**\n"
                + "\n".join(
                    (
                        f"- {entry['member'].mention}: "
                        f"{entry['current_rank']} → "
                        f"{entry['target_rank']} "
                        f"({entry['overdue_days']}d overdue)"
                    )
                    for entry
                    in overdue_promotions
                )
            )

        if wom_mismatches:
            sections.append(
                "**WOM Tenure Mismatches**\n"
                + "\n".join(wom_mismatches)
            )

        if unlinked_full_wom_accounts:
            sections.append(
                "**Unlinked Full Clan Accounts**\n"
                + "\n".join(
                    unlinked_full_wom_accounts
                )
            )

        if linked_members_without_active_rsn:
            sections.append(
                "**Linked Members With No Active RSN**\n"
                + "\n".join(
                    linked_members_without_active_rsn
                )
            )

        if missing_join_dates:
            sections.append(
                "**Missing WOM Join Date**\n"
                + "\n".join(missing_join_dates)
            )

        if mapping_issues:
            sections.append(
                "**Tenure Mapping Issues**\n"
                + "\n".join(mapping_issues)
            )

        if skipped_members:
            sections.append(
                "**Skipped**\n"
                + "\n".join(skipped_members)
            )

        if failed_members:
            sections.append(
                "**Failed**\n"
                + "\n".join(failed_members)
            )

        if sections:
            response += "\n\n" + "\n\n".join(sections)

        if len(response) > 1950:
            response = (
                response[:1900]
                + "\n\n...additional sync details omitted."
            )

        await interaction.followup.send(
            response,
            ephemeral=True
        )

    # ========================================================
    # /PROMOTIONS
    # ========================================================

    @app_commands.command(
        name="promotions",
        description="Show primary/alt tenure promotions due, overdue, or coming up."
    )
    async def promotions(
        self,
        interaction: discord.Interaction
    ):
        if not has_novus_command_permission(
            interaction.user
        ):
            await interaction.response.send_message(
                "You must be a Trial Moderator, Moderator, or Council member to use this command.",
                ephemeral=True
            )
            return

        await interaction.response.defer(
            ephemeral=True,
            thinking=True
        )

        guild = interaction.guild

        if guild is None:
            await interaction.followup.send(
                "This command can only be used inside the Novus server.",
                ephemeral=True
            )
            return

        conn = sqlite3.connect(
            "wom_multi.db"
        )

        try:
            c = conn.cursor()
            c.execute(
                """
                SELECT group_id
                FROM guild_configs
                WHERE guild_id = ?
                """,
                (guild.id,)
            )
            config = c.fetchone()

            if not config or not config[0]:
                await interaction.followup.send(
                    "No Wise Old Man group is configured.",
                    ephemeral=True
                )
                return

            group_data, api_status = await self.get_wom_group(
                config[0]
            )

            if not isinstance(
                group_data,
                dict
            ):
                await interaction.followup.send(
                    "Could not retrieve the Novus WOM group.",
                    ephemeral=True
                )
                return

            await self.prepare_tenure_history(
                c,
                guild,
                group_data
            )

            audit = (
                self.get_promotion_audit_entries(
                    c,
                    guild,
                    group_data,
                    upcoming_days=14
                )
            )

            due_today = audit[
                "due_today"
            ]
            overdue = audit[
                "overdue"
            ]
            upcoming = audit[
                "upcoming"
            ]

            response = (
                "**Novus Promotion Audit**\n"
                "Active linked in-clan accounts checked: "
                f"{audit['active_accounts']}\n"
                f"Due today: {len(due_today)} | "
                f"Overdue: {len(overdue)} | "
                f"Coming up (14d): {len(upcoming)}"
            )

            sections = []

            if due_today:
                sections.append(
                    "**Due Today**\n"
                    + "\n".join(
                        (
                            f"- {entry['member'].mention}: "
                            f"{entry['current_rank']} → "
                            f"{entry['target_rank']} today"
                        )
                        for entry
                        in due_today
                    )
                )

            if overdue:
                sections.append(
                    "**Overdue**\n"
                    + "\n".join(
                        (
                            f"- {entry['member'].mention}: "
                            f"{entry['current_rank']} → "
                            f"{entry['target_rank']} "
                            f"({entry['overdue_days']}d overdue)"
                        )
                        for entry
                        in overdue
                    )
                )

            if upcoming:
                sections.append(
                    "**Coming Up (Next 14 Days)**\n"
                    + "\n".join(
                        (
                            f"- {entry['member'].mention}: "
                            f"{entry['current_rank']} → "
                            f"{entry['target_rank']} on "
                            f"{entry['eligible_at'].date()} "
                            f"({entry['days_until']}d)"
                        )
                        for entry
                        in upcoming
                    )
                )

            missing_join = audit[
                "missing_join_dates"
            ]

            if missing_join:
                sections.append(
                    "**Missing WOM Join Date**\n"
                    + "\n".join(
                        (
                            f"- {entry['member'].mention} "
                            f"({entry['rsn']} "
                            f"{entry['account_type']})"
                        )
                        for entry
                        in missing_join
                    )
                )

            if not sections:
                sections.append(
                    "No linked primary or alt account has an actionable tenure promotion right now."
                )

            response += (
                "\n\n"
                + "\n\n".join(
                    sections
                )
            )

            if len(response) > 1950:
                response = (
                    response[:1900]
                    + "\n\n...additional promotion details omitted."
                )

            conn.commit()

            await interaction.followup.send(
                response,
                ephemeral=True
            )

        finally:
            conn.close()

    async def cleanup_inactive_trial_roles(
        self,
        cursor,
        guild: discord.Guild,
        group_data: dict
    ):
        """Remove stale Discord Trial Member roles.

        Trial Member is valid only when the Discord user has a saved linked RSN
        whose current WOM role is gnome_child. No nickname/name guessing is used.
        Saved links/history are preserved.
        """

        trial_role = next(
            (
                role
                for role in guild.roles
                if getattr(
                    role,
                    "name",
                    ""
                ).strip().casefold()
                    == NOVUS_TRIAL_DISCORD_ROLE_NAME
            ),
            None
        )

        if trial_role is None:
            return {
                "cleaned": [],
                "failed": [],
                "role_missing": True,
            }

        memberships = group_data.get(
            "memberships",
            []
        )

        wom_by_id = {
            membership["player"]["id"]: membership
            for membership in memberships
            if isinstance(
                membership,
                dict
            )
            and isinstance(
                membership.get(
                    "player"
                ),
                dict
            )
            and membership["player"].get(
                "id"
            ) is not None
        }

        wom_by_name = {
            membership["player"]["username"].casefold(): membership
            for membership in memberships
            if isinstance(
                membership,
                dict
            )
            and isinstance(
                membership.get(
                    "player"
                ),
                dict
            )
            and membership["player"].get(
                "username"
            )
        }

        cursor.execute(
            """
            SELECT discord_id, rsn, wom_id
            FROM links
            WHERE guild_id = ?
            """,
            (guild.id,)
        )
        primary_rows = cursor.fetchall()

        cursor.execute(
            """
            SELECT discord_id, rsn, wom_id
            FROM alt_links
            WHERE guild_id = ?
            """,
            (guild.id,)
        )
        alt_rows = cursor.fetchall()

        saved_accounts = {}

        for (
            discord_id,
            rsn,
            wom_id
        ) in (
            primary_rows
            + alt_rows
        ):
            saved_accounts.setdefault(
                discord_id,
                []
            ).append(
                (
                    rsn,
                    wom_id
                )
            )

        cleaned = []
        failed = []

        for member in list(
            trial_role.members
        ):
            still_active_trial = False

            for (
                saved_rsn,
                saved_wom_id
            ) in saved_accounts.get(
                member.id,
                []
            ):
                membership = None

                if saved_wom_id is not None:
                    membership = wom_by_id.get(
                        saved_wom_id
                    )

                if (
                    membership is None
                    and saved_rsn
                ):
                    membership = wom_by_name.get(
                        saved_rsn.casefold()
                    )

                if (
                    membership is not None
                    and membership.get(
                        "role",
                        ""
                    ).casefold()
                        in NOVUS_TRIAL_WOM_RANKS
                ):
                    still_active_trial = True
                    break

            if still_active_trial:
                continue

            try:
                await member.remove_roles(
                    trial_role,
                    reason=(
                        "No saved linked RSN currently has "
                        "the Novus WOM gnome_child trial role"
                    )
                )
                cleaned.append(
                    f"- {member.mention}: removed `{trial_role.name}`"
                )
            except discord.Forbidden:
                failed.append(
                    f"- {member.mention}: could not remove "
                    f"`{trial_role.name}` (permission/role hierarchy)"
                )
            except Exception:
                logger.exception(
                    "Failed stale Trial Member cleanup for %s (%s)",
                    member,
                    member.id
                )
                failed.append(
                    f"- {member.mention}: unexpected error removing "
                    f"`{trial_role.name}`"
                )

        return {
            "cleaned": cleaned,
            "failed": failed,
            "role_missing": False,
        }

    # ========================================================
    # /TRIALS
    # ========================================================

    @app_commands.command(
        name="trials",
        description="Show current Trial Members and time remaining in their 30-day trial."
    )
    async def trials_command(
        self,
        interaction: discord.Interaction
    ):
        if not has_novus_command_permission(
            interaction.user
        ):
            await interaction.response.send_message(
                "You must be a Trial Moderator, Moderator, or Council member to use this command.",
                ephemeral=True
            )
            return

        await interaction.response.defer(
            ephemeral=True,
            thinking=True
        )

        guild = interaction.guild

        if guild is None:
            await interaction.followup.send(
                "This command can only be used inside the Novus server.",
                ephemeral=True
            )
            return

        conn = sqlite3.connect(
            "wom_multi.db"
        )
        c = conn.cursor()

        c.execute(
            """
            SELECT group_id
            FROM guild_configs
            WHERE guild_id = ?
            """,
            (guild.id,)
        )

        config = c.fetchone()

        if not config or not config[0]:
            conn.close()
            await interaction.followup.send(
                "No Wise Old Man group is configured.",
                ephemeral=True
            )
            return

        group_data, api_status = await self.get_wom_group(
            config[0]
        )

        if group_data is None:
            conn.close()
            await interaction.followup.send(
                "Could not retrieve the Novus WOM group.",
                ephemeral=True
            )
            return

        trial_cleanup_result = (
            await self.cleanup_inactive_trial_roles(
                c,
                guild,
                group_data
            )
        )
        conn.commit()

        c.execute(
            """
            SELECT discord_id, rsn, wom_id
            FROM links
            WHERE guild_id = ?
            """,
            (guild.id,)
        )

        link_rows = c.fetchall()
        conn.close()

        discord_by_wom_id = {
            wom_id: discord_id
            for discord_id, _, wom_id in link_rows
            if wom_id is not None
        }
        discord_by_rsn = {
            rsn.lower(): discord_id
            for discord_id, rsn, _ in link_rows
            if rsn
        }

        now = datetime.datetime.now(
            datetime.timezone.utc
        )
        trials = []

        for membership in group_data.get(
            "memberships",
            []
        ):
            wom_role = (
                membership.get(
                    "role",
                    ""
                ).lower()
            )

            if wom_role not in NOVUS_TRIAL_WOM_RANKS:
                continue

            player = membership.get(
                "player",
                {}
            )
            rsn = player.get(
                "username",
                "Unknown"
            )
            wom_id = player.get(
                "id"
            )
            joined_at = get_membership_joined_at(
                membership
            )

            discord_id = discord_by_wom_id.get(
                wom_id
            )

            if discord_id is None:
                discord_id = discord_by_rsn.get(
                    rsn.lower()
                )

            member = (
                guild.get_member(discord_id)
                if discord_id is not None
                else None
            )

            if member is not None and has_non_clan_role(member):
                continue

            trial_end = None
            days_remaining = None

            if joined_at:
                trial_end = joined_at + datetime.timedelta(
                    days=TRIAL_PERIOD_DAYS
                )
                days_remaining = (
                    trial_end.date()
                    - now.date()
                ).days

            trials.append(
                {
                    "member": member,
                    "rsn": rsn,
                    "joined": joined_at,
                    "ends": trial_end,
                    "days_remaining": days_remaining,
                }
            )

        trials.sort(
            key=lambda entry: (
                entry["ends"] is None,
                entry["ends"] or datetime.datetime.max.replace(
                    tzinfo=datetime.timezone.utc
                ),
                entry["rsn"].lower()
            )
        )

        if not trials:
            response = (
                "**Novus Trial Members**\n\n"
                "No current Trial Members found in WOM."
            )

            if trial_cleanup_result.get(
                "cleaned"
            ):
                response += (
                    "\n\n**Stale Trial Roles Removed**\n"
                    + "\n".join(
                        trial_cleanup_result[
                            "cleaned"
                        ]
                    )
                )

            if trial_cleanup_result.get(
                "failed"
            ):
                response += (
                    "\n\n**Trial Cleanup Failed**\n"
                    + "\n".join(
                        trial_cleanup_result[
                            "failed"
                        ]
                    )
                )

            await interaction.followup.send(
                response[:1950],
                ephemeral=True
            )
            return

        lines = []

        for entry in trials:
            identity = (
                f"{entry['member'].mention} (`{entry['rsn']}`)"
                if entry["member"]
                else f"`{entry['rsn']}`"
            )

            if entry["joined"] is None:
                lines.append(
                    f"{identity}\nWOM join date unavailable - trial timing cannot be calculated."
                )
                continue

            days_remaining = entry["days_remaining"]

            if days_remaining > 0:
                status_text = (
                    f"**{days_remaining} day(s) remaining**"
                )
            elif days_remaining == 0:
                status_text = "**Trial ends today**"
            else:
                status_text = (
                    f"**Overdue by {abs(days_remaining)} day(s)**"
                )

            lines.append(
                f"{identity}\n"
                f"Joined: `{entry['joined'].date()}` | "
                f"Ends: `{entry['ends'].date()}` | {status_text}"
            )

        response = (
            f"**Novus Trial Members**\n\n"
            f"Current trials: **{len(trials)}**\n\n"
            + "\n\n".join(lines)
        )

        if trial_cleanup_result.get(
            "cleaned"
        ):
            response += (
                "\n\n**Stale Trial Roles Removed**\n"
                + "\n".join(
                    trial_cleanup_result[
                        "cleaned"
                    ]
                )
            )

        if trial_cleanup_result.get(
            "failed"
        ):
            response += (
                "\n\n**Trial Cleanup Failed**\n"
                + "\n".join(
                    trial_cleanup_result[
                        "failed"
                    ]
                )
            )

        if len(response) > 1950:
            response = (
                response[:1900]
                + "\n\n...additional trial members omitted."
            )

        await interaction.followup.send(
            response,
            ephemeral=True
        )

    # ========================================================
    # SERVER-WIDE AUTOMATIC SYNC
    # ========================================================

    async def sync_guild(
        self,
        guild,
        group_id,
        log_channel_id,
        nickname_enforcement,
        dm_notifications_on
    ):
        log_channel = (
            self.bot.get_channel(
                log_channel_id
            )
            if log_channel_id
            else None
        )

        # Forget non-clan links even if the WOM API is unavailable.
        conn = sqlite3.connect("wom_multi.db")
        try:
            cursor = conn.cursor()
            forget_non_clan_links(cursor, guild)
            conn.commit()
        finally:
            conn.close()

        group_data, api_status = await self.get_wom_group(
            group_id
        )

        if group_data is None:
            if log_channel:
                try:
                    await log_channel.send(
                        "WOM sync failed because the Wise Old Man API could not be reached."
                    )
                except discord.Forbidden:
                    pass
            return

        # Noon workflow mirrors /autolink first, then /syncall-style syncing.
        autolink_result = None
        config_cog = self.bot.get_cog(
            "ConfigCog"
        )

        if (
            config_cog is not None
            and hasattr(
                config_cog,
                "run_autolink_for_guild"
            )
        ):
            try:
                autolink_result = (
                    await config_cog.run_autolink_for_guild(
                        guild,
                        group_data
                    )
                )
            except Exception:
                logger.exception(
                    "Scheduled autolink failed for guild %s",
                    guild.id
                )

        conn = sqlite3.connect(
            "wom_multi.db"
        )
        c = conn.cursor()

        await self.prepare_tenure_history(c, guild, group_data)

        trial_cleanup_result = (
            await self.cleanup_inactive_trial_roles(
                c,
                guild,
                group_data
            )
        )

        c.execute(
            """
            SELECT discord_id, rsn, wom_id
            FROM links
            WHERE guild_id = ?
            """,
            (guild.id,)
        )

        links = c.fetchall()
        memberships = group_data.get("memberships", [])
        active_links_checked = 0
        eligible_links = []

        automatic_promotions = []
        wom_name_updates = []
        autolink_updates = []
        inactive_member_cleanups = []
        inactive_trial_cleanups = list(
            trial_cleanup_result.get(
                "cleaned",
                []
            )
        )
        staff_updates = []
        duplicate_cleanups = []
        wom_rank_mismatches = []
        missing_join_dates = []
        tenure_mapping_issues = []
        skipped_members = []
        failed_members = list(
            trial_cleanup_result.get(
                "failed",
                []
            )
        )
        removed_users_count = 0

        if autolink_result:
            for member, rsn in autolink_result.get(
                "linked_members",
                []
            ):
                autolink_updates.append(
                    f"- Linked {member.mention} -> `{rsn}`"
                )

            for (
                member,
                old_rsn,
                new_rsn
            ) in autolink_result.get(
                "repaired_members",
                []
            ):
                autolink_updates.append(
                    f"- Repaired {member.mention}: "
                    f"`{old_rsn or 'unknown'}` -> `{new_rsn}`"
                )

            for member, alt_rsn in autolink_result.get(
                "alt_links_added",
                []
            ):
                autolink_updates.append(
                    f"- Added alt for {member.mention}: `{alt_rsn}`"
                )

        for discord_id, stored_rsn, stored_wom_id in links:
            member = guild.get_member(
                discord_id
            )

            if member is not None and has_non_clan_role(member):
                forget_member_links(c, guild.id, member.id)
                continue

            if not member:
                c.execute(
                    """
                    DELETE FROM links
                    WHERE guild_id = ?
                    AND discord_id = ?
                    """,
                    (
                        guild.id,
                        discord_id
                    )
                )

                c.execute(
                    """
                    DELETE FROM alt_links
                    WHERE guild_id = ?
                    AND discord_id = ?
                    """,
                    (
                        guild.id,
                        discord_id
                    )
                )

                removed_users_count += 1
                continue

            if not has_novus_member_role(
                member
            ):
                continue

            result = await self.sync_one_member(
                guild,
                member,
                group_data,
                c
            )

            if result["status"] in (
                "non_clan_ignored",
                "not_current_member"
            ):
                continue

            active_links_checked += 1

            if result["status"] == "inactive_member_cleaned":
                removed_roles = result.get(
                    "roles_removed",
                    []
                )
                removed_text = (
                    ", ".join(
                        f"`{role_name}`"
                        for role_name in removed_roles
                    )
                    if removed_roles
                    else "no roles"
                )
                inactive_member_cleanups.append(
                    f"- {member.mention}: removed {removed_text}; "
                    "saved RSN/tenure history retained"
                )
                continue

            eligible_links.append(
                (
                    discord_id,
                    stored_rsn,
                    stored_wom_id
                )
            )

            if result["status"] != "success":
                issue_line = format_sync_issue(
                    member,
                    result
                )

                if classify_sync_issue(result) == "failed":
                    failed_members.append(issue_line)
                else:
                    skipped_members.append(issue_line)
                continue

            if result.get(
                "primary_rsn_changed"
            ):
                rename_status = (
                    "Discord nickname updated"
                    if result.get(
                        "nickname_updated_from_wom"
                    )
                    else (
                        "Discord nickname update failed"
                        if result.get(
                            "nickname_update_failed"
                        )
                        else "saved RSN updated"
                    )
                )
                wom_name_updates.append(
                    f"- {member.mention}: "
                    f"`{result.get('previous_primary_rsn') or 'unknown'}` "
                    f"-> `{result['rsn']}` ({rename_status})"
                )

            tenure_status = result.get(
                "tenure_status"
            )

            if tenure_status == "promoted":
                automatic_promotions.append(
                    f"- {member.mention}: "
                    f"`{result.get('current_discord_tenure_rank') or 'none'}` "
                    f"→ `{result['expected_tenure_rank']}`"
                )

            elif tenure_status == "duplicate_cleanup":
                duplicate_cleanups.append(
                    f"- {member.mention} (`{result['rsn']}`): kept "
                    f"`{result['expected_tenure_rank']}` and removed lower duplicate(s)"
                )

            elif tenure_status == "missing_join_date":
                missing_join_dates.append(
                    f"- {member.mention} (`{result['rsn']}`)"
                )

            elif tenure_status == "not_mapped":
                tenure_mapping_issues.append(
                    f"- {member.mention} (`{result['rsn']}`): expected "
                    f"`{result.get('expected_tenure_rank')}`"
                )

            if result.get("wom_tenure_mismatch"):
                for account in result.get(
                    "wom_tenure_mismatches",
                    []
                ):
                    wom_rank_mismatches.append(
                        f"- {member.mention}: "
                        f"`{account['wom_role']}` → "
                        f"`{account['expected_rank']}`"
                    )

            if (
                result.get("staff_roles_added")
                or result.get("staff_roles_removed")
            ):
                old_roles = (
                    ", ".join(
                        role.mention
                        for role in result.get(
                            "staff_roles_removed",
                            []
                        )
                    )
                    or "none"
                )
                new_roles = (
                    ", ".join(
                        role.mention
                        for role in result.get(
                            "staff_roles_added",
                            []
                        )
                    )
                    or "none"
                )

                staff_updates.append(
                    f"- {member.mention} (`{result['rsn']}`): "
                    f"{old_roles} -> {new_roles}"
                )

            if (
                dm_notifications_on
                and result.get("user_dm_on")
                and (
                    result.get("roles_added")
                    or result.get("roles_removed")
                )
            ):
                try:
                    await member.send(
                        "Your Novus roles have been updated."
                    )
                except discord.Forbidden:
                    pass
                except Exception as e:
                    logger.error(
                        f"Could not DM {member.id}: {e}"
                    )

            # Optional continuous enforcement keeps the exact current WOM
            # primary RSN plus any currently linked in-clan alts. Preserve the
            # member's existing '/' versus '|' separator style.
            if nickname_enforcement:
                desired_nickname = build_wom_server_nickname(
                    result.get(
                        "rsn"
                    ),
                    result.get(
                        "verified_alts",
                        []
                    ),
                    member.nick
                )

                if (
                    desired_nickname
                    and member.nick != desired_nickname
                ):
                    try:
                        await member.edit(
                            nick=desired_nickname,
                            reason=(
                                "Novus WOM linked-account nickname enforcement"
                            )
                        )
                    except discord.Forbidden:
                        logger.warning(
                            "Could not enforce WOM nickname for %s (%s): "
                            "missing role hierarchy or Manage Nicknames permission.",
                            member,
                            member.id,
                        )
                    except Exception as e:
                        logger.error(
                            "Could not enforce WOM nickname for %s (%s): %s",
                            member,
                            member.id,
                            e,
                        )

        promotion_audit = (
            self.get_promotion_audit_entries(
                c,
                guild,
                group_data,
                upcoming_days=0
            )
        )
        due_today_promotions = (
            promotion_audit[
                "due_today"
            ]
        )
        overdue_promotions = (
            promotion_audit[
                "overdue"
            ]
        )
        link_audit = self.get_link_audit_entries(
            c,
            guild,
            group_data
        )
        unlinked_full_wom_accounts = (
            link_audit[
                "unlinked_full_wom_accounts"
            ]
        )
        linked_members_without_active_rsn = (
            link_audit[
                "linked_members_without_active_rsn"
            ]
        )

        # ----------------------------------------------------
        # TRIAL MEMBERS ENDING SOON
        # ----------------------------------------------------

        discord_by_wom_id = {
            wom_id: discord_id
            for discord_id, _, wom_id in eligible_links
            if wom_id is not None
        }
        discord_by_rsn = {
            rsn.lower(): discord_id
            for discord_id, rsn, _ in eligible_links
            if rsn
        }

        now_local = datetime.datetime.now(
            ZoneInfo("America/New_York")
        )
        trials_ending_soon = []

        for membership in group_data.get(
            "memberships",
            []
        ):
            current_wom_role = (
                membership.get(
                    "role",
                    ""
                ).lower()
            )

            if current_wom_role not in NOVUS_TRIAL_WOM_RANKS:
                continue

            player = membership.get(
                "player",
                {}
            )
            rsn = player.get(
                "username",
                "Unknown"
            )
            wom_id = player.get(
                "id"
            )
            joined_at = get_membership_joined_at(
                membership
            )

            if joined_at is None:
                continue

            trial_end = joined_at + datetime.timedelta(
                days=TRIAL_PERIOD_DAYS
            )
            days_remaining = (
                trial_end.date()
                - now_local.date()
            ).days

            if days_remaining > TRIAL_ENDING_SOON_DAYS:
                continue

            discord_id = discord_by_wom_id.get(
                wom_id
            )

            if discord_id is None:
                discord_id = discord_by_rsn.get(
                    rsn.lower()
                )

            member = (
                guild.get_member(discord_id)
                if discord_id is not None
                else None
            )

            identity = (
                f"{member.mention} (`{rsn}`)"
                if member
                else f"`{rsn}`"
            )

            if days_remaining > 0:
                status_text = (
                    f"{days_remaining} day(s) remaining"
                )
            elif days_remaining == 0:
                status_text = "ends today"
            else:
                status_text = (
                    f"overdue by {abs(days_remaining)} day(s)"
                )

            trials_ending_soon.append(
                (
                    days_remaining,
                    f"- {identity}: {status_text} - ends `{trial_end.date()}`"
                )
            )

        trials_ending_soon.sort(
            key=lambda entry: entry[0]
        )
        trial_lines = [
            line
            for _, line in trials_ending_soon
        ]

        now = datetime.datetime.now().isoformat()

        c.execute(
            """
            UPDATE guild_configs
            SET last_sync = ?
            WHERE guild_id = ?
            """,
            (
                now,
                guild.id
            )
        )

        actual_role_changes = (
            automatic_promotions
            or inactive_member_cleanups
            or inactive_trial_cleanups
            or staff_updates
            or duplicate_cleanups
        )

        if actual_role_changes:
            c.execute(
                """
                UPDATE guild_configs
                SET last_change_timestamp = ?
                WHERE guild_id = ?
                """,
                (
                    now,
                    guild.id
                )
            )

        conn.commit()
        conn.close()

        actionable_items = any((
            automatic_promotions,
            wom_name_updates,
            autolink_updates,
            inactive_member_cleanups,
            inactive_trial_cleanups,
            due_today_promotions,
            overdue_promotions,
            staff_updates,
            duplicate_cleanups,
            wom_rank_mismatches,
            missing_join_dates,
            tenure_mapping_issues,
            trial_lines,
            skipped_members,
            failed_members,
            unlinked_full_wom_accounts,
            linked_members_without_active_rsn,
            removed_users_count,
        ))

        # The noon staff report posts every day. If there are no exceptions
        # or automatic changes, it explicitly reports that no action is needed.
        if log_channel:
            embed = discord.Embed(
                title=f"Sync Complete for {guild.name}",
                color=discord.Color.blue(),
                timestamp=datetime.datetime.now()
            )

            embed.description = (
                f"Checked **{active_links_checked}** active Novus members | "
                f"**{len(group_data.get('memberships', []))}** WOM clan accounts\n"
                "Actionable daily exceptions and automatic changes are shown below. "
                "Tenure roles only move upward automatically."
            )

            embed.set_footer(
                text="Novus Noon Staff Sync"
            )

            if wom_name_updates:
                embed.add_field(
                    name=f"WOM Primary Name Updates ({len(wom_name_updates)})",
                    value="\n".join(
                        wom_name_updates
                    )[:1024],
                    inline=False
                )

            if autolink_updates:
                embed.add_field(
                    name=f"Auto-Link Updates ({len(autolink_updates)})",
                    value="\n".join(
                        autolink_updates
                    )[:1024],
                    inline=False
                )

            if inactive_member_cleanups:
                embed.add_field(
                    name=(
                        "Inactive Members Cleaned "
                        f"({len(inactive_member_cleanups)})"
                    ),
                    value="\n".join(
                        inactive_member_cleanups
                    )[:1024],
                    inline=False
                )

            if inactive_trial_cleanups:
                embed.add_field(
                    name=(
                        "Inactive Trials Cleaned "
                        f"({len(inactive_trial_cleanups)})"
                    ),
                    value="\n".join(
                        inactive_trial_cleanups
                    )[:1024],
                    inline=False
                )

            if automatic_promotions:
                embed.add_field(
                    name=(
                        "Automatic Discord Tenure Promotions "
                        f"({len(automatic_promotions)})"
                    ),
                    value="\n".join(
                        automatic_promotions
                    )[:1024],
                    inline=False
                )

            if due_today_promotions:
                embed.add_field(
                    name=(
                        "In-Game Promotions Due Today "
                        f"({len(due_today_promotions)})"
                    ),
                    value="\n".join(
                        (
                            f"- {entry['member'].mention}: "
                            f"{entry['current_rank']} → "
                            f"{entry['target_rank']}"
                        )
                        for entry
                        in due_today_promotions
                    )[:1024],
                    inline=False
                )

            if overdue_promotions:
                embed.add_field(
                    name=(
                        "Overdue In-Game Promotions "
                        f"({len(overdue_promotions)})"
                    ),
                    value="\n".join(
                        (
                            f"- {entry['member'].mention}: "
                            f"{entry['current_rank']} → "
                            f"{entry['target_rank']} "
                            f"({entry['overdue_days']}d overdue)"
                        )
                        for entry
                        in overdue_promotions
                    )[:1024],
                    inline=False
                )

            if wom_rank_mismatches:
                embed.add_field(
                    name=f"WOM Tenure Mismatches ({len(wom_rank_mismatches)})",
                    value="\n".join(wom_rank_mismatches)[:1024],
                    inline=False
                )

            if trial_lines:
                embed.add_field(
                    name=f"Trials Ending Soon ({len(trial_lines)})",
                    value="\n".join(trial_lines)[:1024],
                    inline=False
                )

            if unlinked_full_wom_accounts:
                embed.add_field(
                    name=(
                        "Unlinked Full Clan Accounts "
                        f"({len(unlinked_full_wom_accounts)})"
                    ),
                    value="\n".join(
                        unlinked_full_wom_accounts
                    )[:1024],
                    inline=False
                )

            if linked_members_without_active_rsn:
                embed.add_field(
                    name=(
                        "Linked Members With No Active RSN "
                        f"({len(linked_members_without_active_rsn)})"
                    ),
                    value="\n".join(
                        linked_members_without_active_rsn
                    )[:1024],
                    inline=False
                )

            if missing_join_dates:
                embed.add_field(
                    name=f"Missing WOM Join Date ({len(missing_join_dates)})",
                    value="\n".join(missing_join_dates)[:1024],
                    inline=False
                )

            if tenure_mapping_issues:
                embed.add_field(
                    name=f"Tenure Mapping Issues ({len(tenure_mapping_issues)})",
                    value="\n".join(tenure_mapping_issues)[:1024],
                    inline=False
                )

            if duplicate_cleanups:
                embed.add_field(
                    name=f"Duplicate Tenure Roles Cleaned ({len(duplicate_cleanups)})",
                    value="\n".join(duplicate_cleanups)[:1024],
                    inline=False
                )

            if staff_updates:
                embed.add_field(
                    name=f"Staff Role Updates ({len(staff_updates)})",
                    value="\n".join(staff_updates)[:1024],
                    inline=False
                )

            if removed_users_count:
                embed.add_field(
                    name="Users Removed",
                    value=(
                        f"{removed_users_count} linked user(s) were no longer in the Discord server."
                    ),
                    inline=False
                )

            if skipped_members:
                embed.add_field(
                    name=f"Skipped ({len(skipped_members)})",
                    value="\n".join(skipped_members)[:1024],
                    inline=False
                )

            if failed_members:
                embed.add_field(
                    name=f"Failed ({len(failed_members)})",
                    value="\n".join(failed_members)[:1024],
                    inline=False
                )

            if not actionable_items:
                embed.add_field(
                    name="No Action Needed",
                    value=(
                        "No promotions, linking issues, trial deadlines, "
                        "role mismatches, or sync failures need staff attention today."
                    ),
                    inline=False
                )

            try:
                await log_channel.send(
                    embed=embed
                )
            except discord.Forbidden:
                logger.warning(
                    f"Could not send log message to channel {log_channel_id}"
                )

        logger.info(
            f"Synced roles for guild {guild.name} ({guild.id}). "
            f"{len(links)} linked members checked."
        )

        return (
            len(automatic_promotions) + len(inactive_member_cleanups) + len(inactive_trial_cleanups) + len(staff_updates) + len(duplicate_cleanups),
            len(skipped_members) + len(failed_members),
            active_links_checked
        )

    # ========================================================
    # AUTOMATIC HISTORICAL TENURE BACKFILL
    # ========================================================

    @tasks.loop(hours=2)
    async def history_backfill_loop(self):
        """Recover older WOM membership periods without staff commands.

        This loop only saves WOM history. Normal daily role sync applies any
        newly recovered service to promotions, preserving the rank ladder.
        """
        await self.bot.wait_until_ready()

        for guild in self.bot.guilds:
            conn = sqlite3.connect("wom_multi.db")
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT group_id FROM guild_configs WHERE guild_id = ?",
                    (guild.id,)
                )
                row = cursor.fetchone()
                group_id = row[0] if row else None
            finally:
                conn.close()

            if not group_id:
                continue

            group_data, api_status = await self.get_wom_group(group_id)
            if not isinstance(group_data, dict):
                logger.warning(
                    "Skipped tenure history refresh for guild %s: WOM status %s",
                    guild.id, api_status
                )
                continue

            conn = sqlite3.connect("wom_multi.db")
            try:
                cursor = conn.cursor()
                await self.prepare_tenure_history(
                    cursor, guild, group_data
                )
                conn.commit()
            except Exception:
                conn.rollback()
                logger.exception(
                    "Historical tenure refresh failed for guild %s", guild.id
                )
            finally:
                conn.close()

            # Space API requests when multiple guilds use this bot.
            await asyncio.sleep(3)

    # ========================================================
    # DAILY SYNC LOOP
    # ========================================================

    @tasks.loop(
        time=datetime.time(
            hour=12,
            minute=0,
            tzinfo=ZoneInfo("America/New_York")
        )
    )
    async def sync_roles_loop(self):
        await self.bot.wait_until_ready()

        logger.info(
            "Daily noon Eastern sync started."
        )

        conn = sqlite3.connect(
            "wom_multi.db"
        )
        c = conn.cursor()

        c.execute(
            """
            SELECT
                guild_id,
                group_id,
                log_channel_id,
                nickname_enforcement,
                dm_notifications_on
            FROM guild_configs
            WHERE group_id IS NOT NULL
            """
        )

        configs = c.fetchall()

        conn.close()

        for (
            guild_id,
            group_id,
            log_channel_id,
            nickname_enforcement,
            dm_notifications_on
        ) in configs:

            guild = self.bot.get_guild(
                guild_id
            )

            if not guild:
                continue

            # The scheduled noon staff report always goes to #mod-chat.
            # Other/manual logging can continue using the saved log channel.
            await self.sync_guild(
                guild,
                group_id,
                NOVUS_MOD_CHAT_CHANNEL_ID,
                nickname_enforcement,
                dm_notifications_on
            )

            await asyncio.sleep(2)

        current_time_iso = (
            datetime.datetime.now().isoformat()
        )

        conn = sqlite3.connect(
            "wom_multi.db"
        )
        c = conn.cursor()

        c.execute(
            """
            INSERT OR REPLACE INTO bot_stats
            (key, value)
            VALUES (?, ?)
            """,
            (
                "last_global_sync",
                current_time_iso
            )
        )

        conn.commit()
        conn.close()

        logger.info(
            "Daily noon Eastern sync finished. "
            f"Global sync time updated to "
            f"{current_time_iso}."
        )

    # ========================================================
    # STATS
    # ========================================================

    @tasks.loop(minutes=5)
    async def update_stats(self):
        await self.bot.wait_until_ready()

        server_count = len(
            self.bot.guilds
        )

        conn = sqlite3.connect(
            "wom_multi.db"
        )
        c = conn.cursor()

        c.execute(
            """
            INSERT OR REPLACE INTO bot_stats
            (key, value)
            VALUES (?, ?)
            """,
            (
                "server_count",
                str(server_count)
            )
        )

        conn.commit()
        conn.close()

        logger.info(
            f"Updated server count "
            f"to {server_count}"
        )

    # ========================================================
    # INACTIVE GUILD CLEANUP
    # ========================================================

    @tasks.loop(hours=24)
    async def cleanup_inactive_guilds(self):
        await self.bot.wait_until_ready()

        logger.info(
            "Running daily cleanup "
            "of inactive guilds."
        )

        conn = sqlite3.connect(
            "wom_multi.db"
        )
        c = conn.cursor()

        c.execute(
            """
            SELECT guild_id, inactive_since
            FROM guild_configs
            """
        )

        all_guilds = c.fetchall()

        guilds_to_delete = []

        for (
            guild_id,
            inactive_since_str
        ) in all_guilds:

            guild = self.bot.get_guild(
                guild_id
            )

            if guild:
                if inactive_since_str:
                    c.execute(
                        """
                        UPDATE guild_configs
                        SET inactive_since = NULL
                        WHERE guild_id = ?
                        """,
                        (guild_id,)
                    )
            else:
                if not inactive_since_str:
                    now_str = (
                        datetime.datetime.now()
                        .isoformat()
                    )

                    c.execute(
                        """
                        UPDATE guild_configs
                        SET inactive_since = ?
                        WHERE guild_id = ?
                        """,
                        (
                            now_str,
                            guild_id
                        )
                    )

                else:
                    inactive_since = (
                        datetime.datetime.fromisoformat(
                            inactive_since_str
                        )
                    )

                    if (
                        datetime.datetime.now()
                        - inactive_since
                        > datetime.timedelta(
                            days=30
                        )
                    ):
                        guilds_to_delete.append(
                            guild_id
                        )

        for guild_id in guilds_to_delete:
            c.execute(
                """
                DELETE FROM guild_configs
                WHERE guild_id = ?
                """,
                (guild_id,)
            )

            c.execute(
                """
                DELETE FROM links
                WHERE guild_id = ?
                """,
                (guild_id,)
            )

            c.execute(
                """
                DELETE FROM alt_links
                WHERE guild_id = ?
                """,
                (guild_id,)
            )

            c.execute(
                """
                DELETE FROM role_mappings
                WHERE guild_id = ?
                """,
                (guild_id,)
            )

            logger.info(
                f"Deleted data for inactive "
                f"guild {guild_id} after "
                f"30-day grace period."
            )

        conn.commit()
        conn.close()

        logger.info(
            "Daily cleanup finished."
        )

    # ========================================================
    # WOM SYNC REMINDERS
    # ========================================================

    @tasks.loop(hours=24)
    async def check_reminders(self):
        await self.bot.wait_until_ready()

        logger.info(
            "Running daily reminder check."
        )

        conn = sqlite3.connect(
            "wom_multi.db"
        )
        c = conn.cursor()

        c.execute(
            """
            SELECT
                guild_id,
                log_channel_id,
                last_change_timestamp,
                reminder_interval_days
            FROM guild_configs
            WHERE group_id IS NOT NULL
            AND reminder_interval_days > 0
            AND inactive_since IS NULL
            """
        )

        guilds_to_check = c.fetchall()

        now = datetime.datetime.now()

        for (
            guild_id,
            log_channel_id,
            last_change_str,
            reminder_days
        ) in guilds_to_check:

            if not log_channel_id:
                continue

            guild = self.bot.get_guild(
                guild_id
            )

            if not guild:
                continue

            if not last_change_str:
                c.execute(
                    """
                    UPDATE guild_configs
                    SET last_change_timestamp = ?
                    WHERE guild_id = ?
                    """,
                    (
                        now.isoformat(),
                        guild_id
                    )
                )

                continue

            last_change_time = (
                datetime.datetime.fromisoformat(
                    last_change_str
                )
            )

            if (
                now - last_change_time
            ).days >= reminder_days:

                log_channel = (
                    self.bot.get_channel(
                        log_channel_id
                    )
                )

                if log_channel:
                    try:
                        await log_channel.send(
                            "Please sync the Novus clan "
                            "with Wise Old Man so the "
                            "latest in-game ranks and "
                            "join dates are available."
                        )

                        c.execute(
                            """
                            UPDATE guild_configs
                            SET last_change_timestamp = ?
                            WHERE guild_id = ?
                            """,
                            (
                                now.isoformat(),
                                guild_id
                            )
                        )

                    except discord.Forbidden:
                        logger.warning(
                            f"Could not send reminder "
                            f"to channel "
                            f"{log_channel_id}."
                        )

                    except Exception as e:
                        logger.error(
                            f"Reminder failed for "
                            f"guild {guild_id}: {e}"
                        )

        conn.commit()
        conn.close()

        logger.info(
            "Daily reminder check finished."
        )

    # ========================================================
    # DATABASE BACKUP
    # ========================================================

    @tasks.loop(hours=24)
    async def backup_database(self):
        await self.bot.wait_until_ready()

        db_file = "wom_multi.db"
        backup_dir = "backups"

        os.makedirs(
            backup_dir,
            exist_ok=True
        )

        timestamp = (
            datetime.datetime.now()
            .strftime("%Y-%m-%d")
        )

        backup_file = os.path.join(
            backup_dir,
            f"wom_multi_{timestamp}.db"
        )

        try:
            shutil.copy(
                db_file,
                backup_file
            )

            logger.info(
                f"Successfully backed up "
                f"database to {backup_file}"
            )

        except Exception as e:
            logger.error(
                f"Failed to back up "
                f"database: {e}"
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(
        TasksCog(bot)
    )