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
    WOM_API_KEY, has_non_clan_role, forget_member_links,
    forget_non_clan_links, forget_unmatched_alt_links,
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

# Known WOM ranks intentionally left unmanaged by Novus Sync.
# These should not appear as routine skipped-sync noise.
IGNORED_WOM_RANKS = {
    "owner",
    "gnome_child",
}

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


def months_between(start: datetime.datetime, end: datetime.datetime) -> int:
    """
    Returns the number of fully completed calendar months between two dates.
    """

    months = (
        (end.year - start.year) * 12
        + end.month
        - start.month
    )

    if end.day < start.day:
        months -= 1

    return max(months, 0)


def expected_tenure_rank(joined_at: datetime.datetime) -> str:
    completed_months = months_between(
        joined_at,
        datetime.datetime.now(datetime.timezone.utc)
    )

    for role_name, required_months in PROMOTION_THRESHOLDS:
        if completed_months >= required_months:
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
        self.cleanup_inactive_guilds.start()
        self.backup_database.start()
        self.update_stats.start()
        self.check_reminders.start()

    def cog_unload(self):
        self.sync_roles_loop.cancel()
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
        Resolve and update alt RSNs/WOM IDs.

        Alts are informational only. They never return a rank and therefore
        can never affect Discord roles.
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

            if membership is None:
                membership = wom_by_username.get(
                    alt_rsn.lower()
                )

            if membership is None:
                # Unlinked alts outside Novus do not belong in Novus Sync.
                cursor.execute(
                    """
                    DELETE FROM alt_links
                    WHERE guild_id = ? AND discord_id = ? AND rsn = ?
                    """,
                    (guild_id, discord_id, alt_rsn),
                )
                continue

            current_alt_rsn = (
                membership["player"]["username"]
            )
            current_alt_id = (
                membership["player"]["id"]
            )

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

            verified.append(current_alt_rsn)

        return verified, missing

    # ========================================================
    # ROLE SYNC ENGINE
    # ========================================================

    async def sync_one_member(
        self,
        guild: discord.Guild,
        member: discord.Member,
        group_data: dict,
        cursor
    ):
        """
        Sync one linked Discord member.

        Tenure roles are based on WOM clientSyncJoinedAt, not the visible WOM
        clan rank. Tenure may move upward automatically, but never downward.
        """

        if has_non_clan_role(member):
            forget_member_links(cursor, guild.id, member.id)
            return {"status": "non_clan_ignored"}

        if not has_novus_member_role(member):
            return {"status": "not_current_member"}

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
        }

        wom_by_username = {
            membership["player"]["username"].lower(): membership
            for membership in memberships
        }

        # ----------------------------------------------------
        # PRIMARY ACCOUNT
        # ----------------------------------------------------

        membership = None

        if wom_id:
            membership = wom_by_id.get(wom_id)

        if membership is None:
            membership = wom_by_username.get(
                rsn.lower()
            )

        if membership is None:
            # Verified Discord access alone does not establish clan membership.
            # Retain the link for a possible future return; don't report it.
            return {"status": "not_current_member"}

        player = membership.get(
            "player",
            {}
        )

        current_rsn = player.get(
            "username",
            rsn
        )

        current_wom_id = player.get(
            "id"
        )

        current_wom_role = (
            membership.get(
                "role",
                ""
            ).lower()
        )

        # Keep database current if WOM ID or RSN changed.
        if (
            current_wom_id != wom_id
            or current_rsn.lower() != rsn.lower()
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

        verified_alts, missing_alts = (
            self.sync_alt_links_in_database(
                cursor,
                guild.id,
                member.id,
                wom_by_id,
                wom_by_username
            )
        )

        # Trials and the owner rank intentionally stay outside the tenure
        # synchronization engine. Trial timing is handled separately.
        if current_wom_role in IGNORED_WOM_RANKS:
            return {
                "status": "ignored_rank",
                "rsn": current_rsn,
                "wom_role": current_wom_role,
                "joined_at": get_membership_joined_at(membership),
                "verified_alts": verified_alts,
                "missing_alts": missing_alts,
            }

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
            wom_role.lower(): guild.get_role(discord_role_id)
            for wom_role, discord_role_id in cursor.fetchall()
            if guild.get_role(discord_role_id)
        }

        tenure_role_by_id = {
            role.id: wom_role
            for wom_role, role in configured_map.items()
            if wom_role in NOVUS_TENURE_ROLES
            and role is not None
        }

        staff_roles = {
            guild.get_role(role_id)
            for role_id in NOVUS_STAFF_WOM_ROLES.values()
            if guild.get_role(role_id)
        }

        member_roles = set(member.roles)

        tenure_roles_to_add = []
        tenure_roles_to_remove = []
        staff_roles_to_add = []
        staff_roles_to_remove = []

        # ----------------------------------------------------
        # STAFF ROLE SYNC
        # ----------------------------------------------------

        staff_target_role = None

        if current_wom_role in NOVUS_STAFF_WOM_ROLES:
            staff_role_id = NOVUS_STAFF_WOM_ROLES[
                current_wom_role
            ]

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

        elif current_wom_role in NOVUS_TENURE_ROLES:
            # A normal tenure WOM rank means WOM no longer identifies this
            # account as staff, so stale Novus staff roles can be removed.
            for role in staff_roles:
                if role in member_roles:
                    staff_roles_to_remove.append(
                        role
                    )

        # Specialty/status WOM ranks are deliberately left alone. They do not
        # cause specialty Discord roles or existing staff roles to be changed.

        # ----------------------------------------------------
        # TENURE SYNC - PROMOTIONS ONLY
        # ----------------------------------------------------

        joined_at = get_membership_joined_at(membership)
        effective_joined_at, prior_service = cumulative_effective_join(
            cursor, guild.id, group_data.get("id"), current_wom_id, joined_at
        )

        expected_rank = None
        expected_role = None
        current_discord_tenure_rank = None
        tenure_status = "missing_join_date"

        discord_tenure_ranks = [
            tenure_role_by_id[role.id]
            for role in member.roles
            if role.id in tenure_role_by_id
        ]

        if discord_tenure_ranks:
            current_discord_tenure_rank = max(
                discord_tenure_ranks,
                key=lambda rank: TENURE_RANK_ORDER[rank]
            )

        if joined_at is not None:
            expected_rank = expected_tenure_rank(
                effective_joined_at
            )

            expected_role = configured_map.get(
                expected_rank
            )

            if expected_role is None:
                tenure_status = "not_mapped"
            else:
                expected_level = TENURE_RANK_ORDER[
                    expected_rank
                ]

                if current_discord_tenure_rank is None:
                    tenure_roles_to_add.append(
                        expected_role
                    )
                    tenure_status = "promoted"

                else:
                    current_level = TENURE_RANK_ORDER[
                        current_discord_tenure_rank
                    ]

                    if current_level < expected_level:
                        # Safe upward movement: add the rank supported by tenure
                        # and remove only lower tenure roles being replaced.
                        if expected_role not in member_roles:
                            tenure_roles_to_add.append(
                                expected_role
                            )

                        for role in member.roles:
                            mapped_rank = tenure_role_by_id.get(
                                role.id
                            )

                            if (
                                mapped_rank is not None
                                and TENURE_RANK_ORDER[mapped_rank] < expected_level
                            ):
                                tenure_roles_to_remove.append(
                                    role
                                )

                        tenure_status = "promoted"

                    elif current_level == expected_level:
                        # If duplicate lower tenure roles exist, safely clean
                        # them up without moving the member backward.
                        for role in member.roles:
                            mapped_rank = tenure_role_by_id.get(
                                role.id
                            )

                            if (
                                mapped_rank is not None
                                and TENURE_RANK_ORDER[mapped_rank] < expected_level
                            ):
                                tenure_roles_to_remove.append(
                                    role
                                )

                        tenure_status = (
                            "duplicate_cleanup"
                            if tenure_roles_to_remove
                            else "current"
                        )

                    else:
                        # WOM group activity may omit earlier in-game clan
                        # stints. Retain the assigned rank without alleging
                        # a premature promotion.
                        tenure_status = "retained_rank"

        wom_tenure_mismatch = False
        wom_tenure_direction = None

        if (
            expected_rank is not None
            and current_wom_role in TENURE_RANK_ORDER
            and TENURE_RANK_ORDER[current_wom_role]
                < TENURE_RANK_ORDER[expected_rank]
        ):
            # A lower WOM rank can genuinely lag calculated service.
            # A higher WOM rank could reflect historical service omitted
            # from the WOM group activity log, so it is not a mismatch.
            wom_tenure_mismatch = True
            wom_tenure_direction = "behind"

        roles_to_add = list(dict.fromkeys(
            tenure_roles_to_add
            + staff_roles_to_add
        ))

        roles_to_remove = list(dict.fromkeys(
            tenure_roles_to_remove
            + staff_roles_to_remove
        ))

        # ----------------------------------------------------
        # APPLY DISCORD CHANGES
        # ----------------------------------------------------

        try:
            # Add the new/higher role first. If Discord rejects the add, the
            # member keeps their existing tenure role instead of losing it.
            if roles_to_add:
                await member.add_roles(
                    *roles_to_add,
                    reason="Novus role synchronization"
                )

            if roles_to_remove:
                await member.remove_roles(
                    *roles_to_remove,
                    reason="Novus role synchronization"
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
            "joined_at": joined_at,
            "effective_joined_at": effective_joined_at,
            "prior_tenure_days": prior_service.total_seconds() / 86400,
            "expected_tenure_rank": expected_rank,
            "expected_tenure_role": expected_role,
            "current_discord_tenure_rank": current_discord_tenure_rank,
            "tenure_status": tenure_status,
            "wom_tenure_mismatch": wom_tenure_mismatch,
            "wom_tenure_direction": wom_tenure_direction,
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

        forget_unmatched_alt_links(c, guild.id, group_data.get("memberships", []))
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

        if status == "ignored_rank":
            if result.get("wom_role") in NOVUS_TRIAL_WOM_RANKS:
                joined_at = result.get("joined_at")

                trial_text = ""
                if joined_at:
                    trial_end = joined_at + datetime.timedelta(
                        days=TRIAL_PERIOD_DAYS
                    )
                    days_remaining = (
                        trial_end.date()
                        - datetime.datetime.now(
                            datetime.timezone.utc
                        ).date()
                    ).days

                    if days_remaining > 0:
                        trial_text = (
                            f"\nTrial ends: `{trial_end.date()}` "
                            f"({days_remaining} day(s) remaining)"
                        )
                    elif days_remaining == 0:
                        trial_text = (
                            f"\nTrial ends: `{trial_end.date()}` (today)"
                        )
                    else:
                        trial_text = (
                            f"\nTrial ended: `{trial_end.date()}` "
                            f"({abs(days_remaining)} day(s) overdue)"
                        )

                await interaction.followup.send(
                    f"`{result['rsn']}` is still a Trial Member "
                    f"(`{result['wom_role']}`). No tenure role was changed."
                    f"{trial_text}",
                    ephemeral=True
                )
            else:
                await interaction.followup.send(
                    f"`{result['rsn']}` has WOM rank "
                    f"`{result['wom_role']}`. That rank is intentionally "
                    f"unmanaged by Novus Sync, so no Discord roles were changed.",
                    ephemeral=True
                )
            return

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

        if status == "not_current_member":
            await interaction.followup.send(
                f"{member.mention} is not an active Novus member. "
                "Both the Discord Member role and a primary RSN in the "
                "current Novus WOM group are required. No account links "
                "or Discord roles were changed.",
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
            direction = result.get(
                "wom_tenure_direction",
                "mismatched"
            )
            response += (
                f"\nWOM Tenure Mismatch: `{result['wom_role']}` is "
                f"**{direction}** relative to the expected "
                f"`{expected_tenure}` rank."
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

        forget_unmatched_alt_links(c, guild.id, group_data.get("memberships", []))
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
        wom_mismatches = []
        missing_join_dates = []
        mapping_issues = []
        skipped_members = []
        failed_members = []
        for discord_id, rsn, wom_id in linked_primaries:
            member = guild.get_member(discord_id)

            if member is not None and has_non_clan_role(member):
                forget_member_links(c, guild.id, member.id)
                continue

            if not is_active_novus_member_link(
                member, rsn, wom_id, memberships
            ):
                continue

            checked += 1

            result = await self.sync_one_member(
                guild,
                member,
                group_data,
                c
            )

            if result["status"] in (
                "ignored_rank", "non_clan_ignored", "not_current_member"
            ):
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

            if result["roles_added"] or result["roles_removed"]:
                changed += 1

            if result.get("tenure_status") == "promoted":
                automatic_promotions.append(
                    f"- {member.mention} (`{result['rsn']}`): "
                    f"`{result.get('current_discord_tenure_rank') or 'none'}` "
                    f"-> `{result['expected_tenure_rank']}`"
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
                wom_mismatches.append(
                    f"- {member.mention} (`{result['rsn']}`): WOM "
                    f"`{result['wom_role']}`, expected "
                    f"`{result['expected_tenure_rank']}` "
                    f"({result['wom_tenure_direction']})"
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
            f"Automatic tenure promotions: **{len(automatic_promotions)}**\n"

            f"WOM tenure mismatches: **{len(wom_mismatches)}**\n"
            f"Missing WOM join date: **{len(missing_join_dates)}**"
        )

        sections = []

        if automatic_promotions:
            sections.append(
                "**Automatic Promotions**\n"
                + "\n".join(automatic_promotions)
            )

        if wom_mismatches:
            sections.append(
                "**WOM Tenure Mismatches**\n"
                + "\n".join(wom_mismatches)
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
        description="Show linked Novus members overdue for a tenure promotion."
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

        memberships = group_data.get(
            "memberships",
            []
        )
        await self.prepare_tenure_history(c, guild, group_data)

        wom_by_id = {
            membership["player"]["id"]: membership
            for membership in memberships
        }

        wom_by_username = {
            membership["player"]["username"].lower(): membership
            for membership in memberships
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
            for wom_role, discord_role_id in c.fetchall()
            if wom_role.lower() in NOVUS_TENURE_ROLES
        }

        overdue_members = []
        missing_join_date_members = []
        trial_members_excluded = 0
        checked_tenure_members = 0
        active_primary_members = 0

        now = datetime.datetime.now(
            datetime.timezone.utc
        )

        for discord_id, stored_rsn, stored_wom_id in primary_links:
            member = guild.get_member(
                discord_id
            )

            if not is_active_novus_member_link(
                member, stored_rsn, stored_wom_id, memberships
            ):
                continue

            active_primary_members += 1
            membership = None

            if stored_wom_id:
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

            current_rsn = player.get(
                "username",
                stored_rsn
            )
            current_wom_id = player.get(
                "id"
            )

            if (
                current_rsn
                and (
                    current_rsn.lower() != stored_rsn.lower()
                    or current_wom_id != stored_wom_id
                )
            ):
                c.execute(
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
                        discord_id
                    )
                )

            current_wom_rank = (
                membership.get(
                    "role",
                    ""
                ).lower()
            )

            if current_wom_rank in NOVUS_TRIAL_WOM_RANKS:
                trial_members_excluded += 1
                continue

            if current_wom_rank == "owner":
                continue

            joined_at = get_membership_joined_at(
                membership
            )

            if joined_at is None:
                missing_join_date_members.append(
                    f"- {member.mention} (`{current_rsn}`)"
                )
                continue

            checked_tenure_members += 1

            effective_joined_at, prior_service = cumulative_effective_join(
                c, guild.id, group_data.get("id"), current_wom_id, joined_at
            )
            expected_rank = expected_tenure_rank(
                effective_joined_at
            )
            expected_level = TENURE_RANK_ORDER[
                expected_rank
            ]

            discord_tenure_ranks = [
                tenure_rank_by_role_id[role.id]
                for role in member.roles
                if role.id in tenure_rank_by_role_id
            ]

            current_tenure_rank = None
            current_level = -1

            if discord_tenure_ranks:
                current_tenure_rank = max(
                    discord_tenure_ranks,
                    key=lambda rank: TENURE_RANK_ORDER[rank]
                )
                current_level = TENURE_RANK_ORDER[
                    current_tenure_rank
                ]

            if current_level < expected_level:
                eligible_at = tenure_eligible_at(
                    effective_joined_at,
                    expected_rank
                )
                overdue_days = max(
                    (now - eligible_at).days,
                    0
                )

                overdue_members.append(
                    {
                        "member": member,
                        "rsn": current_rsn,
                        "current": current_tenure_rank,
                        "expected": expected_rank,
                        "joined": joined_at,
                        "prior_tenure_days": prior_service.days,
                        "eligible": eligible_at,
                        "overdue_days": overdue_days,
                    }
                )

        conn.commit()
        conn.close()

        overdue_members.sort(
            key=lambda entry: entry["overdue_days"],
            reverse=True
        )

        summary = (
            f"**Novus Promotion Audit**\n\n"
            f"Active Novus primary accounts: **{active_primary_members}**\n"
            f"Tenure members evaluated: **{checked_tenure_members}**\n"
            f"Promotions due: **{len(overdue_members)}**"
        )

        if trial_members_excluded:
            summary += (
                f"\nTrial members excluded: **{trial_members_excluded}**"
            )

        if missing_join_date_members:
            summary += (
                f"\nMissing WOM join date: **{len(missing_join_date_members)}**"
            )

        sections = []

        if overdue_members:
            lines = []
            for entry in overdue_members:
                lines.append(
                    f"{entry['member'].mention} (`{entry['rsn']}`)\n"
                    f"`{entry['current'] or 'none'}` -> `{entry['expected']}`\n"
                    f"Current stint joined: `{entry['joined'].date()}`\n"
                    f"Previous service: **{entry['prior_tenure_days']} days**\n"
                    f"Eligible: `{entry['eligible'].date()}`\n"
                    f"Overdue: **{entry['overdue_days']} day(s)**"
                )
            sections.append(
                "**Promotions Due**\n"
                + "\n\n".join(lines)
            )
        else:
            sections.append(
                "No linked tenure members are currently overdue for promotion."
            )

        if missing_join_date_members:
            sections.append(
                "**Missing WOM Join Date**\n"
                + "\n".join(missing_join_date_members)
            )

        response = summary + "\n\n" + "\n\n".join(sections)

        if len(response) > 1950:
            response = (
                response[:1900]
                + "\n\n...additional promotion details omitted."
            )

        await interaction.followup.send(
            response,
            ephemeral=True
        )

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
            await interaction.followup.send(
                "**Novus Trial Members**\n\nNo current Trial Members found in WOM.",
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

        conn = sqlite3.connect(
            "wom_multi.db"
        )
        c = conn.cursor()

        forget_unmatched_alt_links(
            c, guild.id, group_data.get("memberships", [])
        )
        await self.prepare_tenure_history(c, guild, group_data)

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
        staff_updates = []
        duplicate_cleanups = []
        wom_rank_mismatches = []
        missing_join_dates = []
        tenure_mapping_issues = []
        skipped_members = []
        failed_members = []
        removed_users_count = 0

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

            if not is_active_novus_member_link(
                member, stored_rsn, stored_wom_id, memberships
            ):
                # Former Novus members may retain Verified on Discord.
                # Do not sync, flag or count them.
                continue

            active_links_checked += 1
            eligible_links.append((discord_id, stored_rsn, stored_wom_id))

            result = await self.sync_one_member(
                guild,
                member,
                group_data,
                c
            )

            if result["status"] in (
                "ignored_rank", "non_clan_ignored", "not_current_member"
            ):
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

            tenure_status = result.get(
                "tenure_status"
            )

            if tenure_status == "promoted":
                automatic_promotions.append(
                    f"- {member.mention} (`{result['rsn']}`): "
                    f"`{result.get('current_discord_tenure_rank') or 'none'}` "
                    f"-> `{result['expected_tenure_rank']}`"
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
                wom_rank_mismatches.append(
                    f"- {member.mention} (`{result['rsn']}`): WOM "
                    f"`{result['wom_role']}`, expected "
                    f"`{result['expected_tenure_rank']}` "
                    f"({result['wom_tenure_direction']})"
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

            # Optional nickname enforcement uses the linked account
            # structure: Primary | Alt | Alt.
            if nickname_enforcement:
                c.execute(
                    """
                    SELECT rsn
                    FROM alt_links
                    WHERE guild_id = ?
                    AND discord_id = ?
                    ORDER BY rowid
                    """,
                    (
                        guild.id,
                        member.id
                    )
                )

                nickname_parts = [
                    result["rsn"]
                ]

                nickname_parts.extend(
                    row[0]
                    for row in c.fetchall()
                    if row[0]
                )

                desired_nickname = " | ".join(
                    nickname_parts
                )

                if len(desired_nickname) > 32:
                    fitted_parts = []

                    for account_name in nickname_parts:
                        candidate = " | ".join(
                            fitted_parts + [account_name]
                        )

                        if len(candidate) <= 32:
                            fitted_parts.append(
                                account_name
                            )
                        else:
                            break

                    if fitted_parts:
                        desired_nickname = " | ".join(
                            fitted_parts
                        )
                    else:
                        desired_nickname = result["rsn"][:32]

                if member.nick != desired_nickname:
                    try:
                        await member.edit(
                            nick=desired_nickname,
                            reason="Novus linked account nickname enforcement"
                        )
                    except discord.Forbidden:
                        pass
                    except Exception as e:
                        logger.error(
                            f"Could not update nickname for {member} ({member.id}): {e}"
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

        now_utc = datetime.datetime.now(
            datetime.timezone.utc
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
                - now_utc.date()
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

        should_log = any((
            automatic_promotions,
            staff_updates,
            duplicate_cleanups,
            wom_rank_mismatches,
            missing_join_dates,
            tenure_mapping_issues,
            trial_lines,
            skipped_members,
            failed_members,
            removed_users_count,
        ))

        if log_channel and should_log:
            embed = discord.Embed(
                title=f"Sync Complete for {guild.name}",
                color=discord.Color.blue(),
                timestamp=datetime.datetime.now()
            )

            embed.description = (
                f"Checked **{active_links_checked}** active Novus members | "
                f"**{len(group_data.get('memberships', []))}** WOM clan accounts\n"
                f"Tenure roles only move upward automatically."
            )

            embed.set_footer(
                text="Novus Tenure Sync"
            )

            if automatic_promotions:
                embed.add_field(
                    name=f"Automatic Promotions ({len(automatic_promotions)})",
                    value="\n".join(automatic_promotions)[:1024],
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
            len(automatic_promotions) + len(staff_updates) + len(duplicate_cleanups),
            len(skipped_members) + len(failed_members),
            active_links_checked
        )

    # ========================================================
    # DAILY SYNC LOOP
    # ========================================================

    @tasks.loop(
        time=datetime.time(
            hour=17,
            minute=0,
            tzinfo=ZoneInfo("America/New_York")
        )
    )
    async def sync_roles_loop(self):
        await self.bot.wait_until_ready()

        logger.info(
            "Daily sync started."
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

            await self.sync_guild(
                guild,
                group_id,
                log_channel_id,
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
            "Daily sync finished. "
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