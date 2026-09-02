import discord
from discord import app_commands
from discord.ext import tasks, commands
import sqlite3
import datetime
import logging
import asyncio
import os
import shutil

from common import WOM_API_KEY


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
}

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
                missing.append(alt_rsn)
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
        Sync one Discord member.

        Only the primary RSN determines their Discord rank.
        """

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
            membership["player"]["username"].lower():
                membership
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
            return {
                "status": "primary_not_found",
                "rsn": rsn
            }

        current_rsn = (
            membership["player"]["username"]
        )

        current_wom_id = (
            membership["player"]["id"]
        )

        current_wom_role = (
            membership["role"].lower()
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
            wom_role.lower():
                guild.get_role(discord_role_id)
            for wom_role, discord_role_id
            in cursor.fetchall()
            if guild.get_role(discord_role_id)
        }

        tenure_roles = {
            role
            for wom_role, role
            in configured_map.items()
            if wom_role in NOVUS_TENURE_ROLES
            and role is not None
        }

        staff_roles = {
            guild.get_role(role_id)
            for role_id
            in NOVUS_STAFF_WOM_ROLES.values()
            if guild.get_role(role_id)
        }

        member_roles = set(member.roles)

        roles_to_add = []
        roles_to_remove = []

        target_role = None
        sync_type = None

        # ----------------------------------------------------
        # STAFF ACCOUNT
        # ----------------------------------------------------

        if current_wom_role in NOVUS_STAFF_WOM_ROLES:
            sync_type = "staff"

            staff_role_id = (
                NOVUS_STAFF_WOM_ROLES[
                    current_wom_role
                ]
            )

            target_role = guild.get_role(
                staff_role_id
            )

            if target_role is None:
                return {
                    "status": "staff_role_missing",
                    "rsn": current_rsn,
                    "wom_role": current_wom_role,
                    "role_id": staff_role_id,
                    "verified_alts": verified_alts,
                    "missing_alts": missing_alts,
                }

            # Give correct staff role.
            if target_role not in member_roles:
                roles_to_add.append(
                    target_role
                )

            # Remove other Novus staff roles.
            for role in staff_roles:
                if (
                    role != target_role
                    and role in member_roles
                ):
                    roles_to_remove.append(
                        role
                    )

            # IMPORTANT:
            # We intentionally leave Squire-Paladin alone.
            #
            # A staff WOM rank replaces the normal clan rank in WOM,
            # so WOM no longer tells us what the member's underlying
            # tenure rank should be.

        # ----------------------------------------------------
        # NORMAL TENURE ACCOUNT
        # ----------------------------------------------------

        elif current_wom_role in NOVUS_TENURE_ROLES:
            sync_type = "tenure"

            target_role = configured_map.get(
                current_wom_role
            )

            if target_role is None:
                return {
                    "status": "tenure_not_mapped",
                    "rsn": current_rsn,
                    "wom_role": current_wom_role,
                    "verified_alts": verified_alts,
                    "missing_alts": missing_alts,
                }

            # Correct tenure role.
            if target_role not in member_roles:
                roles_to_add.append(
                    target_role
                )

            # Remove incorrect tenure roles.
            for role in tenure_roles:
                if (
                    role != target_role
                    and role in member_roles
                ):
                    roles_to_remove.append(
                        role
                    )

            # If WOM no longer says they're staff,
            # remove old Novus staff roles.
            for role in staff_roles:
                if role in member_roles:
                    roles_to_remove.append(
                        role
                    )

        # ----------------------------------------------------
        # OTHER WOM RANK
        # ----------------------------------------------------

        else:
            return {
                "status": "unsupported_rank",
                "rsn": current_rsn,
                "wom_role": current_wom_role,
                "verified_alts": verified_alts,
                "missing_alts": missing_alts,
            }

        # ----------------------------------------------------
        # APPLY DISCORD CHANGES
        # ----------------------------------------------------

        try:
            if roles_to_remove:
                await member.remove_roles(
                    *roles_to_remove,
                    reason="Novus WOM role synchronization"
                )

            if roles_to_add:
                await member.add_roles(
                    *roles_to_add,
                    reason="Novus WOM role synchronization"
                )

        except discord.Forbidden:
            return {
                "status": "forbidden",
                "rsn": current_rsn,
                "wom_role": current_wom_role,
                "target_role": target_role,
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
                "target_role": target_role,
            }

        return {
            "status": "success",
            "rsn": current_rsn,
            "wom_role": current_wom_role,
            "target_role": target_role,
            "sync_type": sync_type,
            "roles_added": roles_to_add,
            "roles_removed": roles_to_remove,
            "verified_alts": verified_alts,
            "missing_alts": missing_alts,
            "user_dm_on": user_dm_on,
        }

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
                "You must be a Moderator or Council member "
                "to use this command.",
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
                "This command can only be used "
                "inside the Novus server.",
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

        group_id = config[0]

        group_data, status = (
            await self.get_wom_group(
                group_id
            )
        )

        if group_data is None:
            conn.close()

            if status:
                message = (
                    f"Wise Old Man returned "
                    f"API error `{status}`."
                )
            else:
                message = (
                    "The Wise Old Man API request failed."
                )

            await interaction.followup.send(
                message,
                ephemeral=True
            )
            return

        result = await self.sync_one_member(
            guild,
            member,
            group_data,
            c
        )

        conn.commit()
        conn.close()

        status = result["status"]

        if status == "not_linked":
            await interaction.followup.send(
                f"{member.mention} is not linked yet. "
                f"Use `/linkuser` first.",
                ephemeral=True
            )
            return

        if status == "primary_not_found":
            await interaction.followup.send(
                f"Could not find primary RSN "
                f"`{result['rsn']}` in the Novus WOM group.",
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

        if status == "tenure_not_mapped":
            await interaction.followup.send(
                f"`{result['rsn']}` has WOM rank "
                f"`{result['wom_role']}`, but that tenure "
                f"rank has not been mapped with `/linkrole`.",
                ephemeral=True
            )
            return

        if status == "unsupported_rank":
            await interaction.followup.send(
                f"`{result['rsn']}` currently has WOM rank "
                f"`{result['wom_role']}`.\n\n"
                f"That rank is not configured as a Novus "
                f"tenure or staff rank, so no Discord roles "
                f"were changed.",
                ephemeral=True
            )
            return

        if status == "forbidden":
            await interaction.followup.send(
                "I could not update that member's roles. "
                "Check the Novus Sync role hierarchy and "
                "Manage Roles permission.",
                ephemeral=True
            )
            return

        if status == "error":
            await interaction.followup.send(
                "Something went wrong while updating "
                "that member's roles.",
                ephemeral=True
            )
            return

        added = result["roles_added"]
        removed = result["roles_removed"]

        added_text = (
            ", ".join(
                role.mention
                for role in added
            )
            if added
            else "None"
        )

        removed_text = (
            ", ".join(
                role.mention
                for role in removed
            )
            if removed
            else "None"
        )

        if (
            not added
            and not removed
        ):
            heading = "Already synchronized"
        else:
            heading = "WOM Sync Complete"

        response = (
            f"**{heading}**\n\n"
            f"Member: {member.mention}\n"
            f"Primary RSN: `{result['rsn']}`\n"
            f"WOM Rank: `{result['wom_role']}`\n"
            f"Discord Role: "
            f"{result['target_role'].mention}\n"
            f"Added: {added_text}\n"
            f"Removed: {removed_text}"
        )

        verified_alts = result.get(
            "verified_alts",
            []
        )

        missing_alts = result.get(
            "missing_alts",
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

        if missing_alts:
            response += (
                "\nAlts not found in WOM: "
                + ", ".join(
                    f"`{alt}`"
                    for alt in missing_alts
                )
            )

        await interaction.followup.send(
            response,
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
                "You must be a Moderator or Council member "
                "to use this command.",
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
                "This command can only be used "
                "inside the Novus server.",
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

        group_data, api_status = (
            await self.get_wom_group(
                config[0]
            )
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
            SELECT discord_id
            FROM links
            WHERE guild_id = ?
            """,
            (guild.id,)
        )

        discord_ids = [
            row[0]
            for row in c.fetchall()
        ]

        checked = 0
        changed = 0
        skipped_members = []
        failed_members = []
        unfound_rsns = []

        for discord_id in discord_ids:
            member = guild.get_member(
                discord_id
            )

            if not member:
                continue

            checked += 1

            result = await self.sync_one_member(
                guild,
                member,
                group_data,
                c
            )

            if result["status"] == "success":
                if (
                    result["roles_added"]
                    or result["roles_removed"]
                ):
                    changed += 1
                continue

            if result["status"] == "primary_not_found":
                unfound_rsns.append(
                    f"- {member.mention} (`{result['rsn']}`)"
                )
                continue

            issue_line = format_sync_issue(
                member,
                result
            )

            if classify_sync_issue(result) == "failed":
                failed_members.append(issue_line)
            else:
                skipped_members.append(issue_line)

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

        response = (
            f"**Novus Sync Complete**\n\n"
            f"Linked members checked: **{checked}**\n"
            f"Members changed: **{changed}**\n"
            f"Skipped: **{len(skipped_members)}**\n"
            f"Failed: **{len(failed_members)}**\n"
            f"Primary RSN not found: **{len(unfound_rsns)}**"
        )

        if skipped_members:
            response += (
                "\n\n**Skipped**\n"
                + "\n".join(skipped_members)
            )

        if failed_members:
            response += (
                "\n\n**Failed**\n"
                + "\n".join(failed_members)
            )

        if unfound_rsns:
            response += (
                "\n\n**Primary RSN Not Found**\n"
                + "\n".join(unfound_rsns)
            )

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
                "You must be a Moderator or Council member "
                "to use this command.",
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
                "This command can only be used "
                "inside the Novus server.",
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

        group_data, api_status = (
            await self.get_wom_group(
                config[0]
            )
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

        wom_by_id = {
            membership["player"]["id"]: membership
            for membership in memberships
        }

        wom_by_username = {
            membership["player"]["username"].lower():
                membership
            for membership in memberships
        }

        # IMPORTANT:
        # Promotions are evaluated from the primary link table only.
        # Alt accounts are deliberately ignored.
        c.execute(
            """
            SELECT discord_id, rsn, wom_id
            FROM links
            WHERE guild_id = ?
            """,
            (guild.id,)
        )

        primary_links = c.fetchall()

        overdue_members = []
        skipped_missing_join_date = 0
        skipped_primary_not_found = 0
        skipped_staff = 0
        checked_tenure_members = 0

        now = datetime.datetime.now(
            datetime.timezone.utc
        )

        for discord_id, stored_rsn, stored_wom_id in primary_links:
            member = guild.get_member(
                discord_id
            )

            if member is None:
                continue

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
                skipped_primary_not_found += 1
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

            # Keep the primary link current if WOM has a rename/new ID.
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

            current_rank = (
                membership.get(
                    "role",
                    ""
                ).lower()
            )

            # Staff are excluded because their WOM staff rank replaces
            # the underlying tenure rank.
            if current_rank in NOVUS_STAFF_WOM_ROLES:
                skipped_staff += 1
                continue

            if current_rank not in TENURE_RANK_ORDER:
                continue

            checked_tenure_members += 1

            joined_value = membership.get(
                "clientSyncJoinedAt"
            )

            if joined_value is None:
                joined_value = player.get(
                    "clientSyncJoinedAt"
                )

            joined_at = parse_wom_datetime(
                joined_value
            )

            if joined_at is None:
                skipped_missing_join_date += 1
                continue

            expected_rank = expected_tenure_rank(
                joined_at
            )

            current_level = TENURE_RANK_ORDER[
                current_rank
            ]

            expected_level = TENURE_RANK_ORDER[
                expected_rank
            ]

            if expected_level <= current_level:
                continue

            required_months = next(
                months
                for rank, months
                in PROMOTION_THRESHOLDS
                if rank == expected_rank
            )

            eligibility_month = (
                joined_at.month
                - 1
                + required_months
            )

            eligibility_year = (
                joined_at.year
                + eligibility_month // 12
            )

            eligibility_month = (
                eligibility_month % 12
                + 1
            )

            day = joined_at.day

            while day > 28:
                try:
                    eligible_at = joined_at.replace(
                        year=eligibility_year,
                        month=eligibility_month,
                        day=day
                    )
                    break
                except ValueError:
                    day -= 1
            else:
                eligible_at = joined_at.replace(
                    year=eligibility_year,
                    month=eligibility_month,
                    day=day
                )

            overdue_days = max(
                (now - eligible_at).days,
                0
            )

            overdue_members.append(
                {
                    "member": member,
                    "rsn": current_rsn,
                    "current": current_rank,
                    "expected": expected_rank,
                    "joined": joined_at,
                    "eligible": eligible_at,
                    "overdue_days": overdue_days,
                }
            )

        conn.commit()
        conn.close()

        overdue_members.sort(
            key=lambda entry:
                entry["overdue_days"],
            reverse=True
        )

        summary = (
            f"**Novus Promotion Audit**\n\n"
            f"Linked primary accounts checked: "
            f"**{len(primary_links)}**\n"
            f"Tenure members evaluated: "
            f"**{checked_tenure_members}**\n"
            f"Promotions due: "
            f"**{len(overdue_members)}**"
        )

        if skipped_staff:
            summary += (
                f"\nStaff excluded: "
                f"**{skipped_staff}**"
            )

        if skipped_missing_join_date:
            summary += (
                f"\nMissing WOM join date: "
                f"**{skipped_missing_join_date}**"
            )

        if skipped_primary_not_found:
            summary += (
                f"\nPrimary RSN not found in WOM: "
                f"**{skipped_primary_not_found}**"
            )

        if not overdue_members:
            await interaction.followup.send(
                summary
                + "\n\nNo linked tenure members are "
                + "currently overdue for promotion.",
                ephemeral=True
            )
            return

        lines = []

        for entry in overdue_members:
            lines.append(
                f"{entry['member'].mention} "
                f"(`{entry['rsn']}`)\n"
                f"`{entry['current']}` -> "
                f"`{entry['expected']}`\n"
                f"Joined: "
                f"{entry['joined'].date()}\n"
                f"Eligible: "
                f"{entry['eligible'].date()}\n"
                f"Overdue: "
                f"{entry['overdue_days']} day(s)"
            )

        output = "\n\n".join(lines)

        response = (
            summary
            + "\n\n**Promotions Due**\n"
            + output
        )

        if len(response) > 1950:
            response = (
                response[:1900]
                + "\n\n...additional promotions omitted."
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

        group_data, api_status = (
            await self.get_wom_group(
                group_id
            )
        )

        if group_data is None:
            if log_channel:
                try:
                    await log_channel.send(
                        "WOM sync failed because the "
                        "Wise Old Man API could not be reached."
                    )
                except discord.Forbidden:
                    pass

            return

        conn = sqlite3.connect(
            "wom_multi.db"
        )
        c = conn.cursor()

        c.execute(
            """
            SELECT discord_id
            FROM links
            WHERE guild_id = ?
            """,
            (guild.id,)
        )

        links = c.fetchall()

        role_updates = []
        skipped_members = []
        failed_members = []
        removed_users_count = 0
        unfound_rsns = []

        for (discord_id,) in links:
            member = guild.get_member(
                discord_id
            )

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

            result = await self.sync_one_member(
                guild,
                member,
                group_data,
                c
            )

            if result["status"] == "primary_not_found":
                unfound_rsns.append(
                    f"- {member.mention} "
                    f"(`{result['rsn']}`)"
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
            ):
                old_roles = (
                    ", ".join(
                        role.mention
                        for role
                        in result["roles_removed"]
                    )
                    or "none"
                )

                new_roles = (
                    ", ".join(
                        role.mention
                        for role
                        in result["roles_added"]
                    )
                    or "none"
                )

                role_updates.append(
                    f"- {member.mention} "
                    f"(`{result['rsn']}`): "
                    f"{old_roles} -> {new_roles}"
                )

                if (
                    dm_notifications_on
                    and result.get(
                        "user_dm_on"
                    )
                ):
                    try:
                        await member.send(
                            f"Your Novus roles have been "
                            f"updated from Wise Old Man."
                        )
                    except discord.Forbidden:
                        pass
                    except Exception as e:
                        logger.error(
                            f"Could not DM "
                            f"{member.id}: {e}"
                        )

            # Optional nickname enforcement uses the linked account
            # structure: Primary | Alt | Alt.
            #
            # Only the primary account affects Discord rank. Alts are
            # included here for display purposes only.
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

                # Discord nicknames are limited to 32 characters.
                # Keep as many complete linked account names as will fit.
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
                            f"Could not update nickname for "
                            f"{member} ({member.id}): {e}"
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

        if role_updates:
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

        if log_channel and (
            role_updates
            or skipped_members
            or failed_members
            or unfound_rsns
            or removed_users_count
        ):
            embed = discord.Embed(
                title=f"Sync Complete for {guild.name}",
                color=discord.Color.blue(),
                timestamp=datetime.datetime.now()
            )

            embed.set_footer(
                text="Novus WOM Role Sync"
            )

            if role_updates:
                embed.add_field(
                    name="Role Updates",
                    value="\n".join(
                        role_updates
                    )[:1024],
                    inline=False
                )

            if unfound_rsns:
                embed.add_field(
                    name="Primary RSN Not Found",
                    value="\n".join(
                        unfound_rsns
                    )[:1024],
                    inline=False
                )

            if removed_users_count:
                embed.add_field(
                    name="Users Removed",
                    value=(
                        f"{removed_users_count} linked "
                        f"user(s) were no longer "
                        f"in the Discord server."
                    ),
                    inline=False
                )

            if skipped_members:
                embed.add_field(
                    name=f"Skipped ({len(skipped_members)})",
                    value="\n".join(
                        skipped_members
                    )[:1024],
                    inline=False
                )

            if failed_members:
                embed.add_field(
                    name=f"Failed ({len(failed_members)})",
                    value="\n".join(
                        failed_members
                    )[:1024],
                    inline=False
                )

            try:
                await log_channel.send(
                    embed=embed
                )
            except discord.Forbidden:
                logger.warning(
                    f"Could not send log message "
                    f"to channel {log_channel_id}"
                )

        logger.info(
            f"Synced roles for guild "
            f"{guild.name} ({guild.id}). "
            f"{len(links)} linked members checked."
        )

        return (
            len(role_updates),
            len(skipped_members) + len(failed_members),
            len(links)
        )

    # ========================================================
    # DAILY SYNC LOOP
    # ========================================================

    @tasks.loop(hours=24)
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