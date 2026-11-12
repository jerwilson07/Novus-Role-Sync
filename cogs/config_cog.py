import discord
from discord import app_commands
from discord.ext import commands
import sqlite3
import logging
import re
import datetime
import math
import asyncio
import json
from urllib.parse import quote

from common import (
    sanitize_rsn, WOM_API_KEY, has_non_clan_role,
    forget_non_clan_links,
    has_novus_member_role, is_active_novus_member_link,
    find_primary_wom_membership,
)
from tenure_history import cumulative_effective_join
from cogs.tasks_cog import (
    TENURE_RANK_ORDER, tenure_eligible_at, get_membership_joined_at,
    NOVUS_TRIAL_WOM_RANKS, nickname_change_confirmed,
)
from typing import List


logger = logging.getLogger("WOMBot")


# ============================================================
# NOVUS STAFF PERMISSIONS
# ============================================================

COUNCIL_ROLE_ID = 1532830356875513906
MODERATOR_ROLE_ID = 1532830960062693396
TRIAL_MOD_ROLE_ID = 1533587986204069908
NOVUS_COMMAND_ROLE_IDS = {
    COUNCIL_ROLE_ID,
    MODERATOR_ROLE_ID,
    TRIAL_MOD_ROLE_ID,
}

# Only these WOM ranks are allowed to be mapped to Discord tenure roles.
# Specialty/achievement roles are intentionally never managed by Novus Sync.
NOVUS_TENURE_ROLES = {
    "squire",
    "duellist",
    "inquisitor",
    "expert",
    "knight",
    "paladin",
}


def has_novus_command_permission(member: discord.Member) -> bool:
    return any(
        role.id in NOVUS_COMMAND_ROLE_IDS
        for role in member.roles
    )


def can_manually_link_novus_member(member) -> bool:
    """Allow manual linking for Novus Members or Trial Members only."""
    if member is None or has_non_clan_role(member):
        return False
    return has_novus_member_role(member) or any(
        getattr(role, "name", "").strip().casefold() == "trial member"
        for role in member.roles
    )


def build_wom_headers():
    headers = {
        "User-Agent": "NovusRoleSync/1.0"
    }

    if WOM_API_KEY:
        headers["x-api-key"] = WOM_API_KEY

    return headers


def approved_prior_rsns(player_id, history):
    """Only accept approved WOM names belonging to this stable player ID."""
    if player_id is None or not isinstance(history, list):
        return set()
    result = set()
    for item in history:
        if not isinstance(item, dict):
            continue
        if item.get("status") != "approved":
            continue
        if str(item.get("playerId")) != str(player_id):
            continue
        name = item.get("oldName")
        if isinstance(name, str) and sanitize_rsn(name):
            result.add(sanitize_rsn(name).casefold())
    return result


def format_personal_tenure(rank, effective_joined_at, now=None):
    """One compact /audit line, using exactly the sync's tenure thresholds.

    Do not offer a countdown if the recorded service cannot even justify
    the member's existing rank. WOM may have lost an older membership stint.
    """
    if not rank:
        return None

    if effective_joined_at is None:
        return f"**Your tenure:** {rank.title()} | Join date unavailable"

    now = now or datetime.datetime.now(datetime.timezone.utc)
    if effective_joined_at.tzinfo is None:
        effective_joined_at = effective_joined_at.replace(
            tzinfo=datetime.timezone.utc
        )
    effective_joined_at = effective_joined_at.astimezone(
        datetime.timezone.utc
    )
    service_days = max(
        0, (now - effective_joined_at).total_seconds() / 86400
    )

    ordered_ranks = sorted(
        TENURE_RANK_ORDER, key=TENURE_RANK_ORDER.get
    )
    if rank not in ordered_ranks:
        return None

    header = f"**Your tenure:** {rank.title()} | {service_days:.1f} days recorded"
    index = ordered_ranks.index(rank)
    if index == len(ordered_ranks) - 1:
        return header + " | Highest tenure rank"

    next_rank = ordered_ranks[index + 1]
    current_eligible = tenure_eligible_at(
        effective_joined_at, rank
    )

    if current_eligible and current_eligible > now:
        # Insufficient documented history is not proof of premature rank.
        return (
            header + f" | {next_rank.title()}: older service not verified"
        )

    next_eligible = tenure_eligible_at(
        effective_joined_at, next_rank
    )
    if next_eligible is None:
        return header

    days_left = max(
        0, math.ceil((next_eligible - now).total_seconds() / 86400)
    )
    if days_left == 0:
        return header + f" | {next_rank.title()}: eligible now"
    return (
        header + f" | {next_rank.title()}: {days_left} days"
        f" ({next_eligible.strftime('%b %d')})"
    )


def find_unmatched_nontrial_wom_accounts(
    memberships, linked_wom_ids, linked_names
):
    """Only full members lacking a Discord link are actionable audit issues.

    WOM trial members are handled by /trials, not /audit, whether or not
    they already have a Discord Member role.
    """
    unmatched = []

    for membership in memberships:
        if not isinstance(membership, dict):
            continue
        wom_role = str(membership.get("role") or "").strip().casefold()
        if wom_role in NOVUS_TRIAL_WOM_RANKS:
            continue

        player = membership.get("player")
        if not isinstance(player, dict):
            continue
        wom_id = player.get("id")
        username = player.get("username")
        if not isinstance(username, str) or not username.strip():
            continue

        if (
            wom_id not in linked_wom_ids
            and username.casefold() not in linked_names
        ):
            unmatched.append(username)

    return sorted(unmatched, key=str.casefold)


# ============================================================
# WOM ROLES
# ============================================================

WOM_ROLES = {
    'achiever', 'adamant', 'adept', 'administrator', 'admiral', 'adventurer',
    'air', 'anchor', 'apothecary', 'archer', 'armadylean', 'artillery',
    'artisan', 'asgarnian', 'assassin', 'assistant', 'astral', 'athlete',
    'attacker', 'bandit', 'bandosian', 'barbarian', 'battlemage', 'beast',
    'berserker', 'blisterwood', 'blood', 'blue', 'bob', 'body',
    'brassican', 'brawler', 'brigadier', 'brigand', 'bronze', 'bruiser',
    'bulwark', 'burglar', 'burnt', 'cadet', 'captain', 'carry', 'champion',
    'chaos', 'cleric', 'collector', 'colonel', 'commander', 'competitor',
    'completionist', 'constructor', 'cook', 'coordinator', 'corporal',
    'cosmic', 'councillor', 'crafter', 'crew', 'crusader', 'cutpurse',
    'death', 'defender', 'defiler', 'deputy_owner', 'destroyer', 'diamond',
    'diseased', 'doctor', 'dogsbody', 'dragon', 'dragonstone', 'druid',
    'duellist', 'earth', 'elite', 'emerald', 'enforcer', 'epic',
    'executive', 'expert', 'explorer', 'farmer', 'feeder', 'fighter',
    'fire', 'firemaker', 'firestarter', 'fisher', 'fletcher', 'forager',
    'fremennik', 'gamer', 'gatherer', 'general', 'gnome_child',
    'gnome_elder', 'goblin', 'gold', 'goon', 'green', 'grey', 'guardian',
    'guthixian', 'harpoon', 'healer', 'hellcat', 'helper', 'herbologist',
    'hero', 'holy', 'hoarder', 'hunter', 'ignitor', 'illusionist', 'imp',
    'infantry', 'inquisitor', 'iron', 'jade', 'justiciar', 'kandarin',
    'karamjan', 'kharidian', 'kitten', 'knight', 'labourer', 'law',
    'leader', 'learner', 'legacy', 'legend', 'legionnaire', 'lieutenant',
    'looter', 'lumberjack', 'magic', 'magician', 'major', 'maple',
    'marshal', 'master', 'maxed', 'mediator', 'medic', 'mentor', 'member',
    'merchant', 'mind', 'miner', 'minion', 'misthalinian', 'mithril',
    'moderator', 'monarch', 'morytanian', 'mystic', 'myth', 'natural',
    'nature', 'necromancer', 'ninja', 'noble', 'novice', 'nurse', 'oak',
    'officer', 'onyx', 'opal', 'oracle', 'orange', 'owner', 'page',
    'paladin', 'pawn', 'pilgrim', 'pine', 'pink', 'prefect', 'priest',
    'private', 'prodigy', 'proselyte', 'prospector', 'protector', 'pure',
    'purple', 'pyromancer', 'quester', 'racer', 'raider', 'ranger',
    'record_chaser', 'recruit', 'recruiter', 'red_topaz', 'red', 'rogue',
    'ruby', 'rune', 'runecrafter', 'sage', 'sapphire', 'saradominist',
    'saviour', 'scavenger', 'scholar', 'scourge', 'scout', 'scribe',
    'seer', 'senator', 'sentry', 'serenist', 'sergeant', 'shaman',
    'sheriff', 'short_green_guy', 'skiller', 'skulled', 'slayer',
    'smiter', 'smith', 'smuggler', 'sniper', 'soul', 'specialist',
    'speed_runner', 'spellcaster', 'squire', 'staff', 'steel', 'strider',
    'striker', 'summoner', 'superior', 'supervisor', 'teacher', 'templar',
    'therapist', 'thief', 'tirannian', 'trialist', 'trickster', 'tzkal',
    'tztok', 'unholy', 'vagrant', 'vanguard', 'walker', 'wanderer',
    'warden', 'warlock', 'warrior', 'water', 'wild', 'willow', 'wily',
    'wintumber', 'witch', 'wizard', 'worker', 'wrath', 'xerician',
    'yellow', 'yew', 'zamorakian', 'zarosian', 'zealot', 'zenyte'
}


# ============================================================
# CONFIG COG
# ============================================================

class ConfigCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def role_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str
    ) -> List[app_commands.Choice[str]]:

        conn = sqlite3.connect("wom_multi.db")
        c = conn.cursor()

        c.execute(
            """
            SELECT wom_role
            FROM role_mappings
            WHERE guild_id = ?
            """,
            (interaction.guild_id,)
        )

        mapped_roles = [
            row[0]
            for row in c.fetchall()
        ]

        conn.close()

        return [
            app_commands.Choice(
                name=role,
                value=role
            )
            for role in mapped_roles
            if current.lower() in role.lower()
        ]

    # ========================================================
    # LOG CHANNEL
    # ========================================================

    @app_commands.command(
        name="logchannel",
        description="Set a channel for the bot to log sync events."
    )
    @app_commands.describe(
        channel="The channel to send log messages in. Leave blank to disable."
    )
    async def set_log_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel = None
    ):
        if not has_novus_command_permission(
            interaction.user
        ):
            await interaction.response.send_message(
                "You must be a Trial Moderator, Moderator, or Council member "
                "to use this command.",
                ephemeral=True
            )
            return

        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True
            )
            return

        conn = sqlite3.connect("wom_multi.db")
        c = conn.cursor()

        c.execute(
            """
            SELECT group_id
            FROM guild_configs
            WHERE guild_id = ?
            """,
            (interaction.guild_id,)
        )

        if not c.fetchone():
            conn.close()

            await interaction.response.send_message(
                "Please set a Group ID first with `/groupid`.",
                ephemeral=True
            )
            return

        log_channel_id = None

        if channel:
            bot_member = interaction.guild.get_member(
                self.bot.user.id
            )

            permissions = channel.permissions_for(
                bot_member
            )

            if (
                not permissions.send_messages
                or not permissions.embed_links
            ):
                conn.close()

                await interaction.response.send_message(
                    f"I don't have permission to send messages "
                    f"and embeds in {channel.mention}.",
                    ephemeral=True
                )
                return

            log_channel_id = channel.id

        c.execute(
            """
            UPDATE guild_configs
            SET log_channel_id = ?
            WHERE guild_id = ?
            """,
            (
                log_channel_id,
                interaction.guild_id
            )
        )

        conn.commit()
        conn.close()

        if log_channel_id:
            await interaction.response.send_message(
                f"Sync events will now be logged "
                f"in {channel.mention}.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "Sync event logging has been disabled.",
                ephemeral=True
            )

    # ========================================================
    # GROUP ID
    # ========================================================

    @app_commands.command(
        name="groupid",
        description="Set the WOM Group ID."
    )
    async def set_group_id(
        self,
        interaction: discord.Interaction,
        group_id: int
    ):
        if not has_novus_command_permission(
            interaction.user
        ):
            await interaction.response.send_message(
                "You must be a Trial Moderator, Moderator, or Council member "
                "to use this command.",
                ephemeral=True
            )
            return

        conn = sqlite3.connect(
            "wom_multi.db"
        )
        c = conn.cursor()

        c.execute(
            """
            INSERT INTO guild_configs (
                guild_id,
                group_id,
                last_sync,
                inactive_since,
                log_channel_id
            )
            VALUES (?, ?, NULL, NULL, NULL)
            ON CONFLICT(guild_id)
            DO UPDATE SET
                group_id = excluded.group_id,
                inactive_since = NULL
            """,
            (
                interaction.guild_id,
                group_id
            )
        )

        conn.commit()
        conn.close()

        await interaction.response.send_message(
            f"Group ID set to **{group_id}**.",
            ephemeral=True
        )

    # ========================================================
    # ROLE MAPPINGS
    # ========================================================

    @app_commands.command(
        name="linkrole",
        description="Map an approved Novus tenure rank to a Discord role."
    )
    @app_commands.describe(
        wom_role=(
            "Novus tenure rank: "
            "Squire, Duellist, Inquisitor, Expert, Knight, or Paladin."
        ),
        discord_role="The Discord role Novus Sync should manage."
    )
    async def linkrole(
        self,
        interaction: discord.Interaction,
        wom_role: str,
        discord_role: discord.Role
    ):
        if not has_novus_command_permission(
            interaction.user
        ):
            await interaction.response.send_message(
                "You must be a Trial Moderator, Moderator, or Council member "
                "to use this command.",
                ephemeral=True
            )
            return

        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True
            )
            return

        wom_role = wom_role.lower().strip()

        # Hard safety boundary:
        # Novus Sync may only manage the six normal tenure ranks.
        # Specialty/achievement roles must remain completely independent.
        if wom_role not in NOVUS_TENURE_ROLES:
            allowed_roles = ", ".join(
                role.title()
                for role in [
                    "squire",
                    "duellist",
                    "inquisitor",
                    "expert",
                    "knight",
                    "paladin",
                ]
            )

            await interaction.response.send_message(
                f"**{wom_role}** cannot be mapped by Novus Sync.\n\n"
                f"Allowed tenure ranks: **{allowed_roles}**.\n"
                f"Specialty and achievement roles are intentionally "
                f"left unmanaged.",
                ephemeral=True
            )
            return

        if discord_role.is_default():
            await interaction.response.send_message(
                "The `@everyone` role cannot be used as a "
                "Novus Sync role mapping.",
                ephemeral=True
            )
            return

        if discord_role.managed:
            await interaction.response.send_message(
                f"{discord_role.mention} is managed by Discord or an "
                f"integration and cannot be used as a Novus Sync mapping.",
                ephemeral=True
            )
            return

        bot_member = interaction.guild.me

        if bot_member is None:
            bot_member = interaction.guild.get_member(
                self.bot.user.id
            )

        if bot_member is None:
            await interaction.response.send_message(
                "I could not verify my Discord role hierarchy.",
                ephemeral=True
            )
            return

        if not bot_member.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "Novus Sync does not currently have the "
                "`Manage Roles` permission.",
                ephemeral=True
            )
            return

        if discord_role >= bot_member.top_role:
            await interaction.response.send_message(
                f"I cannot manage {discord_role.mention} because it is "
                f"at or above my highest Discord role. Move the "
                f"**Novus Sync** bot role above it first.",
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
            (interaction.guild_id,)
        )

        config = c.fetchone()

        if not config or config[0] is None:
            conn.close()

            await interaction.response.send_message(
                "Please set the server's WOM Group ID "
                "first using `/groupid`.",
                ephemeral=True
            )
            return

        # Prevent one Discord role from accidentally representing
        # two different tenure ranks.
        c.execute(
            """
            SELECT wom_role
            FROM role_mappings
            WHERE guild_id = ?
            AND discord_role_id = ?
            AND wom_role != ?
            """,
            (
                interaction.guild_id,
                discord_role.id,
                wom_role
            )
        )

        conflicting_mapping = c.fetchone()

        if conflicting_mapping:
            conn.close()

            await interaction.response.send_message(
                f"{discord_role.mention} is already mapped to "
                f"**{conflicting_mapping[0]}**.\n\n"
                f"Remove that mapping first with `/unlinkrole`.",
                ephemeral=True
            )
            return

        c.execute(
            """
            INSERT OR REPLACE INTO role_mappings (
                guild_id,
                wom_role,
                discord_role_id
            )
            VALUES (?, ?, ?)
            """,
            (
                interaction.guild_id,
                wom_role,
                discord_role.id
            )
        )

        conn.commit()
        conn.close()

        await interaction.response.send_message(
            f"Mapped Novus tenure rank **{wom_role.title()}** "
            f"to {discord_role.mention}.",
            ephemeral=True
        )

    @app_commands.command(
        name="unlinkrole",
        description="Remove a WOM role mapping."
    )
    @app_commands.describe(
        wom_role="The WOM role mapping to remove."
    )
    @app_commands.autocomplete(
        wom_role=role_autocomplete
    )
    async def unlinkrole(
        self,
        interaction: discord.Interaction,
        wom_role: str
    ):
        if not has_novus_command_permission(
            interaction.user
        ):
            await interaction.response.send_message(
                "You must be a Trial Moderator, Moderator, or Council member "
                "to use this command.",
                ephemeral=True
            )
            return

        conn = sqlite3.connect(
            "wom_multi.db"
        )
        c = conn.cursor()

        c.execute(
            """
            DELETE FROM role_mappings
            WHERE guild_id = ?
            AND wom_role = ?
            """,
            (
                interaction.guild_id,
                wom_role.lower()
            )
        )

        changes = c.rowcount

        conn.commit()
        conn.close()

        if changes:
            await interaction.response.send_message(
                f"Mapping for **{wom_role}** removed.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"No mapping was found for **{wom_role}**.",
                ephemeral=True
            )

    # ========================================================
    # MANUAL USER LINKING
    # ========================================================

    @app_commands.command(
        name="linkuser",
        description="Link a Discord user to their main RSN and optional alts."
    )
    @app_commands.describe(
        user="The Discord member to link.",
        rsn="Main RSN first, followed by optional alts separated by | or /"
    )
    async def linkuser(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        rsn: str
    ):
        if not has_novus_command_permission(
            interaction.user
        ):
            await interaction.response.send_message(
                "You must be a Trial Moderator, Moderator, or Council member "
                "to use this command.",
                ephemeral=True
            )
            return

        if has_non_clan_role(user):
            await interaction.response.send_message(
                f"{user.mention} has Guest or Clan Friend and cannot be linked "
                "while either role is assigned.",
                ephemeral=True
            )
            return

        if not can_manually_link_novus_member(user):
            await interaction.response.send_message(
                f"{user.mention} needs the Novus Member or Trial Member role. "
                "Verified alone does not qualify for clan linking.",
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
            (interaction.guild_id,)
        )

        config = c.fetchone()

        if not config or config[0] is None:
            conn.close()

            await interaction.response.send_message(
                "Please set the server's WOM Group ID first.",
                ephemeral=True
            )
            return

        raw_accounts = [
            account.strip()
            for account in re.split(r"[|/]", rsn)
            if account.strip()
        ]

        if not raw_accounts:
            conn.close()

            await interaction.response.send_message(
                "Please provide at least one RuneScape name.",
                ephemeral=True
            )
            return

        cleaned_accounts = [
            sanitize_rsn(account)
            for account in raw_accounts
        ]

        unique_accounts = []
        seen_accounts = set()

        for account in cleaned_accounts:
            key = account.lower()

            if key not in seen_accounts:
                unique_accounts.append(
                    account
                )
                seen_accounts.add(
                    key
                )

        primary_rsn = unique_accounts[0]
        alt_rsns = unique_accounts[1:]

        # Staff-entered names are applied immediately to the Discord nickname.
        # Preserve the requested separator and never truncate account names
        # to fit Discord's maximum 32-character nickname.
        separator = " / " if "/" in rsn and "|" not in rsn else " | "
        desired_nickname = separator.join(unique_accounts)
        if len(desired_nickname) > 32:
            conn.close()
            await interaction.response.send_message(
                "That linked nickname is longer than Discord's 32-character "
                "limit. Shorten the list of visible alts and try again; "
                "no links were changed.",
                ephemeral=True,
            )
            return

        c.execute(
            """
            INSERT INTO links (
                guild_id,
                discord_id,
                rsn,
                wom_id
            )
            VALUES (?, ?, ?, NULL)
            ON CONFLICT(guild_id, discord_id)
            DO UPDATE SET
                rsn = excluded.rsn,
                wom_id = NULL
            """,
            (
                interaction.guild_id,
                user.id,
                primary_rsn
            )
        )

        c.execute(
            """
            DELETE FROM alt_links
            WHERE guild_id = ?
            AND discord_id = ?
            """,
            (
                interaction.guild_id,
                user.id
            )
        )

        for alt_rsn in alt_rsns:
            c.execute(
                """
                INSERT INTO alt_links (
                    guild_id,
                    discord_id,
                    rsn,
                    wom_id
                )
                VALUES (?, ?, ?, NULL)
                """,
                (
                    interaction.guild_id,
                    user.id,
                    alt_rsn
                )
            )

        conn.commit()

        # Staff-authorized manual linking sets the nickname immediately.
        # Subsequent WOM-driven automatic edits still require a stable
        # 24-hour candidate; they are not part of this manual action.
        await interaction.response.defer(ephemeral=True)
        nickname_status = f"**Discord nickname:** `{desired_nickname}`"
        nickname_applied = user.nick == desired_nickname
        if not nickname_applied:
            try:
                await user.edit(
                    nick=desired_nickname,
                    reason=f"Novus manual account link by {interaction.user}",
                )
                nickname_applied = True
            except discord.Forbidden:
                logger.warning(
                    "Manual link saved but insufficient permissions to rename "
                    "Discord member %s (%s).", user, user.id,
                )
                nickname_status = (
                    "**Discord nickname:** Couldn't update (check Manage "
                    "Nicknames permission and bot role hierarchy)."
                )
            except discord.HTTPException:
                logger.exception(
                    "Manual link saved but Discord rejected nickname for %s (%s)",
                    user, user.id,
                )
                nickname_status = (
                    "**Discord nickname:** Couldn't update (Discord API error)."
                )

        if nickname_applied:
            # Clear any stale automatic candidate after a successful staff edit.
            nickname_change_confirmed(
                c, interaction.guild_id, user.id,
                desired_nickname, desired_nickname,
            )
            conn.commit()
        conn.close()

        alt_text = (
            "\n".join(f"- `{alt}`" for alt in alt_rsns)
            if alt_rsns else "None"
        )
        await interaction.followup.send(
            f"Linked {user.mention}.\n\n"
            f"**Primary RSN:** `{primary_rsn}`\n"
            f"**Alt accounts:**\n{alt_text}\n\n"
            f"{nickname_status}\n\n"
            "Primary and alt tenure stay separate; the highest independently "
            "earned linked-account tenure can raise the member's Discord tenure role.",
            ephemeral=True,
        )

    # ========================================================
    # AUTOMATIC USER LINKING
    # ========================================================

    async def match_renamed_wom_accounts(
        self,
        guild,
        cursor,
        memberships,
        wom_by_key,
        claimed_by_id,
        claimed_by_key,
        primary_rows,
        unmatched_members,
        unmatched_wom_accounts,
        result,
        group_id=None,
    ):
        """Conservatively link old Discord RSNs to approved WOM name histories.

        Exact current-name and stable player-ID matches are handled first.
        Only unique historical matches are accepted. Never guess identities,
        alter Discord nicknames, or change tenure based on a name similarity.
        """
        if not unmatched_wom_accounts:
            return

        # Potential Discord account owners and their primary/alt name slots.
        unmatched_ids = {member.id for member in unmatched_members}
        targets_by_old_name = {}

        def add_target(value, member_id, account_type):
            if not isinstance(value, str) or not sanitize_rsn(value):
                return
            key = sanitize_rsn(value).casefold()
            targets_by_old_name.setdefault(key, set()).add(
                (member_id, account_type)
            )

        for member in guild.members:
            if (
                member.bot or has_non_clan_role(member)
                or not (
                    has_novus_member_role(member)
                    or any(
                        getattr(role, "name", "").strip().casefold()
                        == "trial member"
                        for role in member.roles
                    )
                )
            ):
                continue
            parts = [
                part.strip() for part in re.split(r"[|/]", member.display_name)
                if part.strip()
            ]
            if member.id in unmatched_ids:
                if parts:
                    add_target(parts[0], member.id, "primary")
                add_target(getattr(member, "global_name", None), member.id, "primary")
                add_target(getattr(member, "name", None), member.id, "primary")
            else:
                stored = primary_rows.get(member.id)
                if not stored:
                    continue
                if find_primary_wom_membership(
                    stored[0], stored[1], memberships
                ) is None:
                    continue
                for alt in parts[1:]:
                    add_target(alt, member.id, "alt")

        # Still inspect historical names when no Discord candidate matches,
        # so the staff report explains why an account stayed unmatched.
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS wom_name_history_cache (
                guild_id INTEGER NOT NULL,
                player_id INTEGER NOT NULL,
                fetched_at TEXT NOT NULL,
                names_json TEXT NOT NULL,
                PRIMARY KEY (guild_id, player_id)
            )"""
        )

        now = datetime.datetime.now(datetime.timezone.utc)
        possible = {}
        group_old_names = {}
        if isinstance(group_id, int):
            try:
                endpoint = (
                    "https://api.wiseoldman.net/v2/groups/"
                    f"{group_id}/name-changes?limit=100"
                )
                async with self.bot.http_session.get(
                    endpoint, headers=build_wom_headers(), timeout=15,
                ) as response:
                    if response.status == 200:
                        changes = await response.json()
                    else:
                        changes = []
                if isinstance(changes, list):
                    for entry in changes:
                        if not isinstance(entry, dict):
                            continue
                        pid = entry.get("playerId")
                        approved = approved_prior_rsns(pid, [entry])
                        if approved and isinstance(pid, int):
                            group_old_names.setdefault(pid, set()).update(
                                approved
                            )
            except Exception:
                logger.warning(
                    "Could not retrieve Novus WOM group name history",
                    exc_info=True,
                )

        for current_name in unmatched_wom_accounts[:12]:
            matches = wom_by_key.get(
                sanitize_rsn(current_name).casefold(), []
            )
            if len(matches) != 1:
                continue
            membership = matches[0]
            player = membership.get("player", {})
            player_id = player.get("id")
            if not isinstance(player_id, int):
                continue
            if player_id in claimed_by_id:
                continue

            old_names = None
            cursor.execute(
                """SELECT fetched_at, names_json
                   FROM wom_name_history_cache
                   WHERE guild_id = ? AND player_id = ?""",
                (guild.id, player_id),
            )
            cached = cursor.fetchone()
            if cached:
                try:
                    cached_at = datetime.datetime.fromisoformat(cached[0])
                    if cached_at.tzinfo is None:
                        cached_at = cached_at.replace(
                            tzinfo=datetime.timezone.utc
                        )
                    if now - cached_at < datetime.timedelta(hours=1):
                        old_names = set(json.loads(cached[1]))
                except (TypeError, ValueError):
                    old_names = None

            if old_names is None:
                endpoint = (
                    "https://api.wiseoldman.net/v2/players/"
                    f"{quote(current_name, safe='')}/names"
                )
                try:
                    async with self.bot.http_session.get(
                        endpoint, headers=build_wom_headers(), timeout=15
                    ) as response:
                        if response.status == 429:
                            logger.warning(
                                "WOM rate-limited historical Novus auto-link"
                            )
                            result["history_lookup_errors"] += 1
                            # Group history can still supply approved names.
                            old_names = group_old_names.get(player_id)
                            if old_names is None:
                                break
                            history = []
                        elif response.status != 200:
                            result["history_lookup_errors"] += 1
                            old_names = group_old_names.get(player_id)
                            if old_names is None:
                                continue
                            history = []
                        else:
                            history = await response.json()
                    if not isinstance(history, list):
                        result["history_lookup_errors"] += 1
                        old_names = group_old_names.get(player_id)
                        if old_names is None:
                            continue
                    if old_names is None:
                        old_names = approved_prior_rsns(player_id, history)
                    old_names.update(group_old_names.get(player_id, set()))
                    cursor.execute(
                        """INSERT INTO wom_name_history_cache
                           (guild_id, player_id, fetched_at, names_json)
                           VALUES (?, ?, ?, ?)
                           ON CONFLICT(guild_id, player_id)
                           DO UPDATE SET fetched_at = excluded.fetched_at,
                                         names_json = excluded.names_json""",
                        (
                            guild.id, player_id, now.isoformat(),
                            json.dumps(sorted(old_names)),
                        ),
                    )
                    await asyncio.sleep(0.3)
                except Exception:
                    result["history_lookup_errors"] += 1
                    logger.exception(
                        "Could not fetch WOM names for player %s", player_id
                    )
                    old_names = group_old_names.get(player_id)
                    if old_names is None:
                        continue

            old_names.update(group_old_names.get(player_id, set()))
            result["history_checked"] += 1
            result.setdefault("history_aliases", {})[current_name] = sorted(old_names)[:10]
            for old_key in old_names:
                # Never match an alias currently owned by another WOM player.
                if old_key in wom_by_key:
                    continue
                owners = targets_by_old_name.get(old_key, set())
                if len(owners) != 1:
                    continue
                owner = next(iter(owners))
                possible.setdefault(player_id, set()).add(
                    (owner[0], owner[1], old_key)
                )

        # Require one Discord owner + one slot per WOM player, and never
        # assign the same Discord primary or alt slot to two WOM accounts.
        unique = {}
        for player_id, candidates in possible.items():
            slots = {(member_id, role) for member_id, role, _ in candidates}
            if len(slots) == 1:
                member_id, role = next(iter(slots))
                old_key = sorted(
                    old for cid, kind, old in candidates
                    if (cid, kind) == (member_id, role)
                )[0]
                unique[player_id] = (member_id, role, old_key)
        reverse = {}
        for pid, (member_id, role, _) in unique.items():
            reverse.setdefault((member_id, role), set()).add(pid)

        for current_name in list(unmatched_wom_accounts):
            matches = wom_by_key.get(
                sanitize_rsn(current_name).casefold(), []
            )
            if len(matches) != 1:
                continue
            membership = matches[0]
            player = membership["player"]
            player_id = player.get("id")
            if player_id not in unique or player_id in claimed_by_id:
                continue
            member_id, role, old_key = unique[player_id]
            if len(reverse[(member_id, role)]) != 1:
                continue
            member = guild.get_member(member_id)
            if member is None:
                continue

            # Another account could have claimed this current WOM RSN
            # earlier in the same run. Never override an existing owner.
            current_key = sanitize_rsn(current_name).casefold()
            if current_key in claimed_by_key:
                continue

            if role == "primary":
                old_link = primary_rows.get(member_id)
                # Stable WOM player IDs must never be overwritten by a
                # historical name that happens to have been reused.
                if (
                    old_link and old_link[1] is not None
                    and old_link[1] != player_id
                ):
                    continue
                if old_link:
                    cursor.execute(
                        """UPDATE links SET rsn = ?, wom_id = ?
                           WHERE guild_id = ? AND discord_id = ?""",
                        (current_name, player_id, guild.id, member_id),
                    )
                    result["repaired_members"].append(
                        (member, old_link[0], current_name)
                    )
                else:
                    cursor.execute(
                        """INSERT INTO links (guild_id, discord_id, rsn, wom_id)
                           VALUES (?, ?, ?, ?)""",
                        (guild.id, member_id, current_name, player_id),
                    )
                    result["linked_members"].append(
                        (member, current_name)
                    )
                primary_rows[member_id] = (current_name, player_id)
                unmatched_members[:] = [
                    candidate for candidate in unmatched_members
                    if candidate.id != member_id
                ]
            else:
                cursor.execute(
                    """SELECT rsn, wom_id FROM alt_links
                       WHERE guild_id = ? AND discord_id = ?""",
                    (guild.id, member_id),
                )
                existing_alts = cursor.fetchall()
                matched_alt = next(
                    (
                        (rsn, wom_id) for rsn, wom_id in existing_alts
                        if wom_id == player_id
                        or sanitize_rsn(rsn).casefold() == old_key
                    ),
                    None,
                )
                if (
                    matched_alt
                    and matched_alt[1] is not None
                    and matched_alt[1] != player_id
                ):
                    # A recycled historical RSN cannot replace a stored
                    # stable ID belonging to a different account.
                    continue
                if matched_alt:
                    cursor.execute(
                        """UPDATE alt_links SET rsn = ?, wom_id = ?
                           WHERE guild_id = ? AND discord_id = ? AND rsn = ?""",
                        (
                            current_name, player_id, guild.id,
                            member_id, matched_alt[0],
                        ),
                    )
                else:
                    cursor.execute(
                        """INSERT OR IGNORE INTO alt_links
                           (guild_id, discord_id, rsn, wom_id)
                           VALUES (?, ?, ?, ?)""",
                        (guild.id, member_id, current_name, player_id),
                    )
                    if cursor.rowcount == 0:
                        continue
                    result["alt_links_added"].append((member, current_name))

            claimed_by_id[player_id] = member_id
            claimed_by_key[current_key] = member_id
            result["history_links"].append(
                (member, old_key, current_name, role)
            )
            unmatched_wom_accounts.remove(current_name)

    async def run_autolink_for_guild(
        self,
        guild: discord.Guild,
        data: dict
    ):
        """Run the same conservative auto-link engine used by /autolink."""

        memberships = data.get(
            "memberships",
            []
        )

        result = {
            "linked_members": [],
            "repaired_members": [],
            "refreshed_members": [],
            "alt_links_added": [],
            "unmatched_members": [],
            "unmatched_wom_accounts": [],
            "history_links": [],
            "history_checked": 0,
            "history_lookup_errors": 0,
            "history_aliases": {},
        }

        if not isinstance(
            memberships,
            list
        ):
            result["error"] = (
                "Wise Old Man returned an invalid clan roster."
            )
            return result

        conn = sqlite3.connect(
            "wom_multi.db"
        )

        try:
            c = conn.cursor()

            forget_non_clan_links(
                c,
                guild
            )

            wom_by_id = {}
            wom_by_key = {}

            for membership in memberships:
                if (
                    not isinstance(
                        membership,
                        dict
                    )
                    or not isinstance(
                        membership.get("player"),
                        dict
                    )
                ):
                    continue

                player = membership[
                    "player"
                ]
                player_id = player.get(
                    "id"
                )
                username = player.get(
                    "username"
                )

                if player_id is not None:
                    wom_by_id[
                        player_id
                    ] = membership

                if username:
                    key = sanitize_rsn(
                        username
                    ).casefold()

                    wom_by_key.setdefault(
                        key,
                        []
                    ).append(
                        membership
                    )

            def resolve_exact(
                candidate
            ):
                if not candidate:
                    return None

                key = sanitize_rsn(
                    candidate
                ).casefold()

                matches = wom_by_key.get(
                    key,
                    []
                )

                if len(matches) == 1:
                    return matches[0]

                return None

            c.execute(
                """
                SELECT discord_id, rsn, wom_id
                FROM links
                WHERE guild_id = ?
                """,
                (guild.id,)
            )

            primary_rows = {
                discord_id: (
                    rsn,
                    wom_id
                )
                for (
                    discord_id,
                    rsn,
                    wom_id
                ) in c.fetchall()
            }

            c.execute(
                """
                SELECT discord_id, rsn, wom_id
                FROM alt_links
                WHERE guild_id = ?
                """,
                (guild.id,)
            )

            alt_rows = c.fetchall()
            alts_by_discord = {}

            for (
                discord_id,
                alt_rsn,
                alt_wom_id
            ) in alt_rows:
                alts_by_discord.setdefault(
                    discord_id,
                    []
                ).append(
                    (
                        alt_rsn,
                        alt_wom_id
                    )
                )

            claimed_by_id = {}
            claimed_by_key = {}

            for (
                discord_id,
                (
                    stored_rsn,
                    stored_wom_id
                )
            ) in primary_rows.items():
                membership = find_primary_wom_membership(
                    stored_rsn,
                    stored_wom_id,
                    memberships
                )

                if membership is None:
                    continue

                player = membership[
                    "player"
                ]
                player_id = player.get(
                    "id"
                )
                username = player.get(
                    "username"
                )

                if player_id is not None:
                    claimed_by_id[
                        player_id
                    ] = discord_id

                if username:
                    claimed_by_key[
                        sanitize_rsn(
                            username
                        ).casefold()
                    ] = discord_id

            for (
                discord_id,
                alt_rsn,
                alt_wom_id
            ) in alt_rows:
                membership = None

                if alt_wom_id is not None:
                    membership = wom_by_id.get(
                        alt_wom_id
                    )

                if (
                    membership is None
                    and alt_wom_id is None
                    and alt_rsn
                ):
                    membership = resolve_exact(
                        alt_rsn
                    )

                if membership is None:
                    continue

                player = membership[
                    "player"
                ]
                player_id = player.get(
                    "id"
                )
                username = player.get(
                    "username"
                )

                if player_id is not None:
                    claimed_by_id[
                        player_id
                    ] = discord_id

                if username:
                    claimed_by_key[
                        sanitize_rsn(
                            username
                        ).casefold()
                    ] = discord_id

            def claimed_by_other(
                membership,
                discord_id
            ):
                player = membership.get(
                    "player",
                    {}
                )
                player_id = player.get(
                    "id"
                )
                username = player.get(
                    "username"
                )
                key = (
                    sanitize_rsn(
                        username
                    ).casefold()
                    if username
                    else None
                )

                id_owner = (
                    claimed_by_id.get(
                        player_id
                    )
                    if player_id is not None
                    else None
                )
                name_owner = (
                    claimed_by_key.get(
                        key
                    )
                    if key
                    else None
                )

                return (
                    (
                        id_owner is not None
                        and id_owner != discord_id
                    )
                    or (
                        name_owner is not None
                        and name_owner != discord_id
                    )
                )

            def claim(
                membership,
                discord_id
            ):
                player = membership[
                    "player"
                ]
                player_id = player.get(
                    "id"
                )
                username = player.get(
                    "username"
                )

                if player_id is not None:
                    claimed_by_id[
                        player_id
                    ] = discord_id

                if username:
                    claimed_by_key[
                        sanitize_rsn(
                            username
                        ).casefold()
                    ] = discord_id

            linked_members = result[
                "linked_members"
            ]
            repaired_members = result[
                "repaired_members"
            ]
            refreshed_members = result[
                "refreshed_members"
            ]
            alt_links_added = result[
                "alt_links_added"
            ]
            unmatched_members = result[
                "unmatched_members"
            ]

            for member in guild.members:
                if (
                    member.bot
                    or has_non_clan_role(member)
                    or not (
                        has_novus_member_role(member)
                        or any(
                            getattr(role, "name", "").strip().casefold()
                            == "trial member"
                            for role in member.roles
                        )
                    )
                ):
                    continue

                display_parts = [
                    part.strip()
                    for part in re.split(
                        r"[|/]",
                        member.display_name
                    )
                    if part.strip()
                ]

                existing_primary = (
                    primary_rows.get(
                        member.id
                    )
                )

                existing_membership = None

                if existing_primary:
                    existing_membership = (
                        find_primary_wom_membership(
                            existing_primary[0],
                            existing_primary[1],
                            memberships
                        )
                    )

                primary_membership = (
                    existing_membership
                )

                if primary_membership is None:
                    candidate_strings = []

                    if display_parts:
                        candidate_strings.append(
                            display_parts[0]
                        )

                    global_name = getattr(
                        member,
                        "global_name",
                        None
                    )

                    if global_name:
                        candidate_strings.append(
                            global_name
                        )

                    if member.name:
                        candidate_strings.append(
                            member.name
                        )

                    seen_candidates = set()

                    for candidate in candidate_strings:
                        normalized = sanitize_rsn(
                            candidate
                        ).casefold()

                        if normalized in seen_candidates:
                            continue

                        seen_candidates.add(
                            normalized
                        )

                        candidate_membership = resolve_exact(
                            candidate
                        )

                        if (
                            candidate_membership is None
                            or claimed_by_other(
                                candidate_membership,
                                member.id
                            )
                        ):
                            continue

                        primary_membership = (
                            candidate_membership
                        )
                        break

                if primary_membership is None:
                    unmatched_members.append(
                        member
                    )
                    continue

                player = primary_membership[
                    "player"
                ]
                actual_primary_rsn = player[
                    "username"
                ]
                primary_wom_id = player[
                    "id"
                ]

                if claimed_by_other(
                    primary_membership,
                    member.id
                ):
                    unmatched_members.append(
                        member
                    )
                    continue

                if existing_primary is None:
                    c.execute(
                        """
                        INSERT INTO links (
                            guild_id,
                            discord_id,
                            rsn,
                            wom_id
                        )
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            guild.id,
                            member.id,
                            actual_primary_rsn,
                            primary_wom_id
                        )
                    )

                    linked_members.append(
                        (
                            member,
                            actual_primary_rsn
                        )
                    )

                else:
                    old_rsn, old_wom_id = (
                        existing_primary
                    )

                    same_rsn_ignoring_case = bool(
                        old_rsn
                        and actual_primary_rsn
                        and old_rsn.casefold()
                            == actual_primary_rsn.casefold()
                    )

                    # Display spelling/case comes from the verified WOM
                    # account, not the previously stored Discord name.
                    stored_primary_rsn = actual_primary_rsn

                    true_name_change = bool(
                        old_rsn
                        and actual_primary_rsn
                        and not same_rsn_ignoring_case
                    )

                    if (
                        old_wom_id != primary_wom_id
                        or old_rsn != stored_primary_rsn
                    ):
                        c.execute(
                            """
                            UPDATE links
                            SET rsn = ?, wom_id = ?
                            WHERE guild_id = ?
                            AND discord_id = ?
                            """,
                            (
                                stored_primary_rsn,
                                primary_wom_id,
                                guild.id,
                                member.id
                            )
                        )

                        if existing_membership is None:
                            repaired_members.append(
                                (
                                    member,
                                    old_rsn,
                                    stored_primary_rsn
                                )
                            )
                        elif true_name_change:
                            refreshed_members.append(
                                (
                                    member,
                                    stored_primary_rsn
                                )
                            )

                claim(
                    primary_membership,
                    member.id
                )

                nickname_primary = (
                    resolve_exact(
                        display_parts[0]
                    )
                    if display_parts
                    else None
                )

                if (
                    nickname_primary is None
                    or nickname_primary[
                        "player"
                    ].get("id")
                        != primary_wom_id
                ):
                    continue

                current_alt_rows = (
                    alts_by_discord.get(
                        member.id,
                        []
                    )
                )

                current_alt_ids = {
                    alt_wom_id
                    for _, alt_wom_id
                    in current_alt_rows
                    if alt_wom_id is not None
                }

                current_alt_names_by_id = {
                    alt_wom_id: alt_rsn
                    for alt_rsn, alt_wom_id
                    in current_alt_rows
                    if alt_wom_id is not None
                }

                for alt_candidate in display_parts[1:]:
                    alt_membership = resolve_exact(
                        alt_candidate
                    )

                    if (
                        alt_membership is None
                        or claimed_by_other(
                            alt_membership,
                            member.id
                        )
                    ):
                        continue

                    alt_player = alt_membership[
                        "player"
                    ]
                    alt_wom_id = alt_player[
                        "id"
                    ]
                    actual_alt_rsn = alt_player[
                        "username"
                    ]

                    if alt_wom_id == primary_wom_id:
                        continue

                    if alt_wom_id in current_alt_ids:
                        stored_alt_rsn = (
                            current_alt_names_by_id.get(
                                alt_wom_id
                            )
                        )

                        if stored_alt_rsn != actual_alt_rsn:
                            c.execute(
                                """
                                UPDATE alt_links
                                SET rsn = ?
                                WHERE guild_id = ?
                                AND discord_id = ?
                                AND wom_id = ?
                                """,
                                (
                                    actual_alt_rsn,
                                    guild.id,
                                    member.id,
                                    alt_wom_id
                                )
                            )

                        claim(
                            alt_membership,
                            member.id
                        )
                        continue

                    c.execute(
                        """
                        INSERT OR IGNORE INTO alt_links (
                            guild_id,
                            discord_id,
                            rsn,
                            wom_id
                        )
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            guild.id,
                            member.id,
                            actual_alt_rsn,
                            alt_wom_id
                        )
                    )

                    if c.rowcount:
                        alt_links_added.append(
                            (
                                member,
                                actual_alt_rsn
                            )
                        )

                        current_alt_ids.add(
                            alt_wom_id
                        )

                    claim(
                        alt_membership,
                        member.id
                    )

            c.execute(
                """
                SELECT discord_id, rsn, wom_id
                FROM links
                WHERE guild_id = ?
                """,
                (guild.id,)
            )
            all_primary_rows = c.fetchall()

            c.execute(
                """
                SELECT discord_id, rsn, wom_id
                FROM alt_links
                WHERE guild_id = ?
                """,
                (guild.id,)
            )
            all_alt_rows = c.fetchall()

            active_linked_ids = set()
            active_linked_names = set()

            for (
                discord_id,
                stored_rsn,
                stored_wom_id
            ) in (
                all_primary_rows
                + all_alt_rows
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
                    membership = resolve_exact(
                        stored_rsn
                    )

                if membership is None:
                    continue

                player = membership[
                    "player"
                ]
                player_id = player.get(
                    "id"
                )
                username = player.get(
                    "username"
                )

                if player_id is not None:
                    active_linked_ids.add(
                        player_id
                    )

                if username:
                    active_linked_names.add(
                        sanitize_rsn(
                            username
                        ).casefold()
                    )

            unmatched_wom_accounts = result[
                "unmatched_wom_accounts"
            ]

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
                username = player.get(
                    "username"
                )

                if (
                    player_id in active_linked_ids
                    or (
                        username
                        and sanitize_rsn(
                            username
                        ).casefold()
                            in active_linked_names
                    )
                ):
                    continue

                if username:
                    unmatched_wom_accounts.append(
                        username
                    )

            unmatched_wom_accounts.sort(
                key=str.casefold
            )

            await self.match_renamed_wom_accounts(
                guild, c, memberships, wom_by_key, claimed_by_id,
                claimed_by_key, primary_rows, unmatched_members,
                unmatched_wom_accounts, result, data.get("id"),
            )

            # Discord interaction replies are ephemeral, so staff cannot
            # troubleshoot them from Railway unless we also log a summary.
            # Only log public WOM RSNs / Discord numeric IDs, never tokens.
            logger.info(
                "Novus autolink guild=%s: new=%d repaired=%d "
                "new_alts=%d historical_matches=%d unmatched_discord=%s "
                "unmatched_wom=%s history_checked=%d history_errors=%d "
                "approved_history=%s",
                guild.id,
                len(result["linked_members"]),
                len(result["repaired_members"]),
                len(result["alt_links_added"]),
                len(result["history_links"]),
                [member.id for member in unmatched_members],
                list(unmatched_wom_accounts),
                result["history_checked"],
                result["history_lookup_errors"],
                {
                    name: result["history_aliases"].get(name, [])
                    for name in unmatched_wom_accounts
                },
            )

            conn.commit()

            return result

        finally:
            conn.close()

    def format_autolink_result(
        self,
        result: dict
    ) -> str:
        if result.get(
            "error"
        ):
            return result[
                "error"
            ]

        linked_members = result[
            "linked_members"
        ]
        repaired_members = result[
            "repaired_members"
        ]
        alt_links_added = result[
            "alt_links_added"
        ]
        unmatched_members = result[
            "unmatched_members"
        ]
        unmatched_wom_accounts = result[
            "unmatched_wom_accounts"
        ]
        history_links = result.get("history_links", [])
        aliases = result.get("history_aliases", {})
        history_checked = result.get("history_checked", 0)
        history_errors = result.get("history_lookup_errors", 0)

        response = (
            "**Novus Auto-Link Complete**\n\n"
            f"New Discord members linked: **{len(linked_members)}**\n"
            f"Stale primary links repaired: **{len(repaired_members)}**\n"
            f"Alt links added: **{len(alt_links_added)}**\n"
            f"Discord members still unmatched: **{len(unmatched_members)}**\n"
            f"Full WOM accounts still unmatched: **{len(unmatched_wom_accounts)}**"
        )

        sections = []

        if linked_members:
            sections.append(
                "**New Primary Links**\n"
                + "\n".join(
                    (
                        f"- {member.mention} -> "
                        f"`{rsn}`"
                    )
                    for member, rsn
                    in linked_members[:15]
                )
            )

        if repaired_members:
            sections.append(
                "**Repaired Stale Primary Links**\n"
                + "\n".join(
                    (
                        f"- {member.mention}: "
                        f"`{old_rsn or 'unknown'}` -> "
                        f"`{new_rsn}`"
                    )
                    for (
                        member,
                        old_rsn,
                        new_rsn
                    ) in repaired_members[:15]
                )
            )

        if alt_links_added:
            sections.append(
                "**New Alt Links**\n"
                + "\n".join(
                    (
                        f"- {member.mention} -> "
                        f"`{alt_rsn}`"
                    )
                    for (
                        member,
                        alt_rsn
                    ) in alt_links_added[:15]
                )
            )

        if history_links:
            sections.append(
                "**Verified Historical RSN Matches**\n"
                + "\n".join(
                    f"- {member.mention}: `{old_rsn}` -> `{current_rsn}` "
                    f"({role})"
                    for member, old_rsn, current_rsn, role in history_links[:15]
                )
            )

        if unmatched_wom_accounts:
            sections.append(
                "**WOM Accounts Still Unmatched**\n"
                + "\n".join(
                    f"- `{username}`"
                    + (
                        " (prior names: "
                        + ", ".join(f"`{name}`" for name in aliases[username][:5])
                        + "; no unique Discord match)"
                        if aliases.get(username)
                        else " (no approved prior names returned)"
                        if username in aliases
                        else " (history unavailable)"
                    )
                    for username in unmatched_wom_accounts[:15]
                )
            )
            sections.append(
                f"Historical WOM lookups: {history_checked} checked, "
                f"{history_errors} API errors. "
                "Unapproved or ambiguous renames are not auto-linked."
            )

        if sections:
            response += (
                "\n\n"
                + "\n\n".join(
                    sections
                )
            )

        if len(response) > 1950:
            response = (
                response[:1900]
                + "\n\n...report shortened."
            )

        return response

    @app_commands.command(
        name="autolink",
        description="Automatically link Discord members to Novus WOM accounts."
    )
    async def autolink(
        self,
        interaction: discord.Interaction
    ):
        if not has_novus_command_permission(
            interaction.user
        ):
            await interaction.response.send_message(
                "You must be a Trial Moderator, Moderator, or Council member "
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
        finally:
            conn.close()

        if not config or not config[0]:
            await interaction.followup.send(
                "No WOM group is configured.",
                ephemeral=True
            )
            return

        url = (
            f"https://api.wiseoldman.net/v2/groups/"
            f"{config[0]}"
        )

        try:
            async with self.bot.http_session.get(
                url,
                headers=build_wom_headers(),
                timeout=30
            ) as response:

                if response.status != 200:
                    await interaction.followup.send(
                        f"Wise Old Man returned API "
                        f"error `{response.status}`.",
                        ephemeral=True
                    )
                    return

                data = await response.json()

        except Exception as e:
            logger.error(
                f"/autolink WOM request failed: {e}"
            )

            await interaction.followup.send(
                "Could not retrieve the Novus WOM group.",
                ephemeral=True
            )
            return

        result = await self.run_autolink_for_guild(
            guild,
            data
        )

        await interaction.followup.send(
            self.format_autolink_result(
                result
            ),
            ephemeral=True
        )

    # ========================================================
    # STAFF AUDIT
    # ========================================================

    @app_commands.command(
        name="linkaudit",
        description="Legacy WOM/Discord link audit."
    )
    async def audit(
        self,
        interaction: discord.Interaction
    ):
        if not has_novus_command_permission(
            interaction.user
        ):
            await interaction.response.send_message(
                "You must be a Trial Moderator, Moderator, or Council member "
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
        forget_non_clan_links(c, guild)
        conn.commit()

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
                "No WOM group is configured.",
                ephemeral=True
            )
            return

        group_id = config[0]

        url = (
            f"https://api.wiseoldman.net/v2/groups/"
            f"{group_id}"
        )

        try:
            async with self.bot.http_session.get(
                url,
                headers=build_wom_headers(),
                timeout=30
            ) as response:

                if response.status != 200:
                    conn.close()

                    await interaction.followup.send(
                        f"Wise Old Man returned API "
                        f"error `{response.status}`.",
                        ephemeral=True
                    )
                    return

                data = await response.json()

        except Exception as e:
            conn.close()

            logger.error(
                f"/audit WOM request failed: {e}"
            )

            await interaction.followup.send(
                "Could not retrieve the Novus WOM group.",
                ephemeral=True
            )
            return

        memberships = data.get(
            "memberships",
            []
        )

        c.execute(
            "SELECT discord_id, rsn, wom_id FROM alt_links WHERE guild_id = ?",
            (guild.id,),
        )
        alt_links = c.fetchall()

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
        # LOAD PRIMARY + ALT LINKS
        # ----------------------------------------------------

        c.execute(
            """
            SELECT discord_id, rsn, wom_id
            FROM links
            WHERE guild_id = ?
            """,
            (guild.id,)
        )

        primary_links = [
            (discord_id, rsn, wom_id)
            for discord_id, rsn, wom_id in c.fetchall()
            if is_active_novus_member_link(
                guild.get_member(discord_id), rsn, wom_id, memberships
            )
        ]
        active_discord_ids = {row[0] for row in primary_links}

        c.execute(
            """
            SELECT discord_id, rsn, wom_id
            FROM alt_links
            WHERE guild_id = ?
            """,
            (guild.id,)
        )

        alt_links = [
            (discord_id, rsn, wom_id)
            for discord_id, rsn, wom_id in c.fetchall()
            if discord_id in active_discord_ids
        ]

        linked_wom_ids = set()
        linked_names = set()


        for discord_id, rsn, wom_id in primary_links:
            membership = None

            if wom_id:
                membership = wom_by_id.get(
                    wom_id
                )

            if membership is None:
                membership = wom_by_username.get(
                    rsn.lower()
                )

            if membership:
                linked_wom_ids.add(
                    membership["player"]["id"]
                )
                linked_names.add(
                    membership["player"]["username"].lower()
                )
        for discord_id, rsn, wom_id in alt_links:
            membership = None

            if wom_id:
                membership = wom_by_id.get(
                    wom_id
                )

            if membership is None:
                membership = wom_by_username.get(
                    rsn.lower()
                )

            if membership:
                linked_wom_ids.add(
                    membership["player"]["id"]
                )
                linked_names.add(
                    membership["player"]["username"].lower()
                )
        # ----------------------------------------------------
        # WOM ACCOUNTS NOT ASSOCIATED WITH ANY DISCORD LINK
        # ----------------------------------------------------

        unmatched_wom = find_unmatched_nontrial_wom_accounts(
            memberships, linked_wom_ids, linked_names
        )

        # Only show the caller's own tenure, never a server-wide tenure dump.
        personal_tenure = None
        own_link = next(
            (
                (rsn, wom_id)
                for discord_id, rsn, wom_id in primary_links
                if discord_id == interaction.user.id
            ),
            None
        )

        if own_link:
            own_membership = find_primary_wom_membership(
                own_link[0], own_link[1], memberships
            )
            if own_membership:
                player_id = own_membership["player"].get("id")
                joined_at = get_membership_joined_at(own_membership)
                effective_joined_at, _ = cumulative_effective_join(
                    c, guild.id, group_id, player_id, joined_at
                )

                c.execute(
                    """
                    SELECT wom_role, discord_role_id
                    FROM role_mappings
                    WHERE guild_id = ?
                    """,
                    (guild.id,)
                )
                tenure_by_role_id = {
                    discord_id: wom_role.lower()
                    for wom_role, discord_id in c.fetchall()
                    if wom_role.lower() in TENURE_RANK_ORDER
                }
                own_ranks = [
                    tenure_by_role_id[role.id]
                    for role in interaction.user.roles
                    if role.id in tenure_by_role_id
                ]
                if own_ranks:
                    highest_rank = max(
                        own_ranks, key=TENURE_RANK_ORDER.get
                    )
                    personal_tenure = format_personal_tenure(
                        highest_rank, effective_joined_at
                    )

        conn.close()

        # ----------------------------------------------------
        # BUILD REPORT
        # ----------------------------------------------------

        summary = (
            f"**Novus Full Audit**\n\n"
            f"Current WOM clan accounts: "
            f"**{len(memberships)}**\n"
            f"Linked Discord primaries: "
            f"**{len(primary_links)}**\n"
            f"Linked alt accounts: "
            f"**{len(alt_links)}**\n"
            f"Unmatched WOM accounts: "
            f"**{len(unmatched_wom)}**"
        )

        if personal_tenure:
            summary += "\n\n" + personal_tenure

        sections = []

        if unmatched_wom:
            sections.append(
                "**Unmatched WOM Accounts**\n"
                + "\n".join(
                    f"- `{name}`"
                    for name in unmatched_wom
                )
            )

        if not sections:
            sections.append(
                "**No issues found.**"
            )

        response = (
            summary
            + "\n\n"
            + "\n\n".join(
                sections
            )
        )

        if len(response) > 1950:
            response = (
                response[:1900]
                + "\n\n...audit shortened."
            )

        await interaction.followup.send(
            response,
            ephemeral=True
        )

    # ========================================================
    # ACCOUNT INSPECTION
    # ========================================================

    @app_commands.command(
        name="accounts",
        description="Show a member's linked Novus RuneScape accounts."
    )
    @app_commands.describe(
        member="The Discord member whose linked accounts you want to inspect."
    )
    async def accounts(
        self,
        interaction: discord.Interaction,
        member: discord.Member
    ):
        if not has_novus_command_permission(
            interaction.user
        ):
            await interaction.response.send_message(
                "You must be a Trial Moderator, Moderator, or Council member "
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

        if not has_novus_member_role(member):
            await interaction.followup.send(
                f"{member.mention} isn't an active Novus clan member "
                "(Discord Member role required).",
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
                "No WOM group is configured.",
                ephemeral=True
            )
            return

        group_id = config[0]

        c.execute(
            """
            SELECT rsn, wom_id
            FROM links
            WHERE guild_id = ?
            AND discord_id = ?
            """,
            (
                guild.id,
                member.id
            )
        )

        primary = c.fetchone()

        if not primary:
            conn.close()

            await interaction.followup.send(
                f"{member.mention} is not linked yet. "
                f"Use `/linkuser` first.",
                ephemeral=True
            )
            return

        primary_rsn, primary_wom_id = primary

        c.execute(
            """
            SELECT rsn, wom_id
            FROM alt_links
            WHERE guild_id = ?
            AND discord_id = ?
            ORDER BY rsn COLLATE NOCASE
            """,
            (
                guild.id,
                member.id
            )
        )

        alt_rows = c.fetchall()

        conn.close()

        url = (
            f"https://api.wiseoldman.net/v2/groups/"
            f"{group_id}"
        )

        try:
            async with self.bot.http_session.get(
                url,
                headers=build_wom_headers(),
                timeout=30
            ) as response:

                if response.status != 200:
                    await interaction.followup.send(
                        f"Wise Old Man returned API "
                        f"error `{response.status}`.",
                        ephemeral=True
                    )
                    return

                data = await response.json()

        except Exception as e:
            logger.error(
                f"/accounts WOM request failed: {e}"
            )

            await interaction.followup.send(
                "Could not retrieve the Novus WOM group.",
                ephemeral=True
            )
            return

        memberships = data.get(
            "memberships",
            []
        )

        if not is_active_novus_member_link(
            member, primary_rsn, primary_wom_id, memberships
        ):
            await interaction.followup.send(
                f"{member.mention} isn't in the current Novus WOM group "
                "and is excluded from clan reports.",
                ephemeral=True
            )
            return

        wom_by_id = {
            membership["player"]["id"]: membership
            for membership in memberships
        }

        wom_by_username = {
            membership["player"]["username"].lower():
                membership
            for membership in memberships
        }

        # Keep saved alt links for history; only current WOM alts are shown as active.
        conn = sqlite3.connect("wom_multi.db")
        try:
            c = conn.cursor()
            c.execute(
                """
                SELECT rsn, wom_id FROM alt_links
                WHERE guild_id = ? AND discord_id = ?
                ORDER BY rsn COLLATE NOCASE
                """,
                (guild.id, member.id),
            )
            alt_rows = c.fetchall()
        finally:
            conn.close()

        primary_membership = None

        if primary_wom_id:
            primary_membership = wom_by_id.get(
                primary_wom_id
            )

        if primary_membership is None:
            primary_membership = wom_by_username.get(
                primary_rsn.lower()
            )

        primary_status = "Not found in current WOM group"
        current_primary_rsn = primary_rsn
        current_rank = None
        current_primary_wom_id = primary_wom_id

        if primary_membership:
            current_primary_rsn = (
                primary_membership["player"]["username"]
            )
            current_primary_wom_id = (
                primary_membership["player"]["id"]
            )
            current_rank = (
                primary_membership.get(
                    "role",
                    "Unknown"
                )
            )
            primary_status = "Verified"

        alt_lines = []

        for alt_rsn, alt_wom_id in alt_rows:
            alt_membership = None

            if alt_wom_id:
                alt_membership = wom_by_id.get(
                    alt_wom_id
                )

            if alt_membership is None:
                alt_membership = wom_by_username.get(
                    alt_rsn.lower()
                )

            if alt_membership:
                current_alt_rsn = (
                    alt_membership["player"]["username"]
                )
                current_alt_wom_id = (
                    alt_membership["player"]["id"]
                )

                alt_lines.append(
                    f"- `{current_alt_rsn}` "
                    f"(WOM ID: `{current_alt_wom_id}`) - Verified"
                )
        primary_wom_id_text = (
            f"`{current_primary_wom_id}`"
            if current_primary_wom_id is not None
            else "Not set"
        )

        response = (
            f"**Novus Account Links**\n\n"
            f"Member: {member.mention}\n"
            f"Primary RSN: `{current_primary_rsn}`\n"
            f"Primary WOM ID: {primary_wom_id_text}\n"
            f"Primary Status: **{primary_status}**"
        )

        if current_rank:
            response += (
                f"\nCurrent WOM Rank: `{current_rank}`"
            )

        if alt_lines:
            response += (
                "\n\n**Alt Accounts**\n"
                + "\n".join(alt_lines)
            )
        else:
            response += (
                "\n\n**Alt Accounts**\nNone"
            )

        response += (
            "\n\nPrimary and alt tenure stay separate. The highest independently "
            "earned linked-account tenure can raise the Discord tenure role; "
            "tenure is never combined between accounts."
        )

        if len(response) > 1950:
            response = (
                response[:1900]
                + "\n\n...report shortened."
            )

        await interaction.followup.send(
            response,
            ephemeral=True
        )

    # ========================================================
    # STAFF COMMAND LIST
    # ========================================================

    @app_commands.command(
        name="commands",
        description="Show the useful Novus Sync staff commands."
    )
    async def commands_list(
        self,
        interaction: discord.Interaction
    ):
        if not has_novus_command_permission(
            interaction.user
        ):
            await interaction.response.send_message(
                "You must be a Trial Moderator, Moderator, or Council member "
                "to use this command.",
                ephemeral=True
            )
            return

        response = (
            "**Novus Sync Staff Commands**\n\n"
            "**/syncuser @member**\n"
            "Sync one linked member using tenure-based promotion rules and WOM staff data.\n\n"

            "**/syncall**\n"
            "Sync all linked members. Tenure can promote automatically but never demotes.\n\n"

            "**/accounts @member**\n"
            "Show a member's primary RSN, alts, WOM IDs, and current WOM rank.\n\n"

            "**/audit**\n"
            "Run one combined audit for unmatched WOM accounts, broken links, missing alts, and membership conflicts.\n\n"

            "**/promotions**\n"
            "Show linked primary and alt accounts that are due today, overdue, "
            "or coming up for a tenure promotion.\n\n"

            "**/trials**\n"
            "Show current Trial Members and how many days remain in their 30-day trial.\n\n"

            "**/unmatched**\n"
            "Show current WOM clan accounts that are not "
            "associated with a Discord user.\n\n"

            "**/autolink**\n"
            "Automatically link exact Discord/WOM name matches. "
            "Supports `Main | Alt` and `Main / Alt`.\n\n"

            "**/linkuser @member RSN**\n"
            "Manually link a member. Put the primary RSN first, "
            "then optional alts separated by `|` or `/`. "
            "Primary and alt tenure are separate; the highest independently earned "
            "linked-account tenure can set the Discord tenure role.\n\n"

            "**/unlinkuser @member**\n"
            "Remove the member's primary and alt account links.\n\n"

            "**Important:** Do not guess unmatched accounts. "
            "If the Discord name does not clearly match WOM, "
            "confirm the member's RSN first."
        )

        await interaction.response.send_message(
            response,
            ephemeral=True
        )

    # ========================================================
    # UNMATCHED WOM ACCOUNTS
    # ========================================================

    @app_commands.command(
        name="unmatched",
        description="Show current WOM clan accounts not linked to Discord."
    )
    async def unmatched(
        self,
        interaction: discord.Interaction
    ):
        if not has_novus_command_permission(
            interaction.user
        ):
            await interaction.response.send_message(
                "You must be a Trial Moderator, Moderator, or Council member "
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
                "No WOM group is configured.",
                ephemeral=True
            )
            return

        group_id = config[0]

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
            SELECT discord_id, rsn, wom_id
            FROM alt_links
            WHERE guild_id = ?
            """,
            (guild.id,)
        )
        alt_links = c.fetchall()

        conn.close()

        url = (
            f"https://api.wiseoldman.net/v2/groups/"
            f"{group_id}"
        )

        try:
            async with self.bot.http_session.get(
                url,
                headers=build_wom_headers(),
                timeout=30
            ) as response:

                if response.status != 200:
                    await interaction.followup.send(
                        f"Wise Old Man returned API "
                        f"error `{response.status}`.",
                        ephemeral=True
                    )
                    return

                data = await response.json()

        except Exception as e:
            logger.error(
                f"/unmatched WOM request failed: {e}"
            )

            await interaction.followup.send(
                "Could not retrieve the Novus WOM group.",
                ephemeral=True
            )
            return

        memberships = data.get(
            "memberships",
            []
        )

        active_discord_ids = {
            discord_id
            for discord_id, rsn, wom_id in primary_links
            if is_active_novus_member_link(
                guild.get_member(discord_id), rsn, wom_id, memberships
            )
        }
        active_links = [
            (rsn, wom_id)
            for discord_id, rsn, wom_id in primary_links + alt_links
            if discord_id in active_discord_ids
        ]
        linked_ids = {
            wom_id for _, wom_id in active_links if wom_id is not None
        }
        linked_names = {
            rsn.lower() for rsn, _ in active_links if rsn
        }

        unmatched_accounts = []

        for membership in memberships:
            player = membership.get(
                "player",
                {}
            )

            wom_id = player.get(
                "id"
            )

            username = player.get(
                "username",
                "Unknown"
            )

            if (
                wom_id not in linked_ids
                and username.lower() not in linked_names
            ):
                unmatched_accounts.append(
                    username
                )

        unmatched_accounts.sort(
            key=str.lower
        )

        if not unmatched_accounts:
            await interaction.followup.send(
                f"**Novus WOM Link Audit**\n\n"
                f"Current WOM clan accounts: "
                f"**{len(memberships)}**\n"
                f"Unmatched WOM accounts: **0**\n\n"
                f"Every current WOM account is associated "
                f"with a Discord member.",
                ephemeral=True
            )
            return

        account_lines = [
            f"- `{username}`"
            for username in unmatched_accounts
        ]

        response = (
            f"**Novus WOM Link Audit**\n\n"
            f"Current WOM clan accounts: "
            f"**{len(memberships)}**\n"
            f"Unmatched WOM accounts: "
            f"**{len(unmatched_accounts)}**\n\n"
            f"**Needs Review**\n"
            + "\n".join(account_lines)
        )

        if len(response) > 1950:
            response = (
                response[:1900]
                + "\n\n...report shortened."
            )

        await interaction.followup.send(
            response,
            ephemeral=True
        )

    # ========================================================
    # UNLINK USER
    # ========================================================

    @app_commands.command(
        name="unlinkuser",
        description="Unlink a user and all of their RuneScape accounts."
    )
    async def unlinkuser(
        self,
        interaction: discord.Interaction,
        user: discord.Member
    ):
        if not has_novus_command_permission(
            interaction.user
        ):
            await interaction.response.send_message(
                "You must be a Trial Moderator, Moderator, or Council member "
                "to use this command.",
                ephemeral=True
            )
            return

        conn = sqlite3.connect(
            "wom_multi.db"
        )
        c = conn.cursor()

        c.execute(
            """
            DELETE FROM links
            WHERE guild_id = ?
            AND discord_id = ?
            """,
            (
                interaction.guild_id,
                user.id
            )
        )

        primary_changes = c.rowcount

        c.execute(
            """
            DELETE FROM alt_links
            WHERE guild_id = ?
            AND discord_id = ?
            """,
            (
                interaction.guild_id,
                user.id
            )
        )

        alt_changes = c.rowcount

        conn.commit()
        conn.close()

        if (
            primary_changes > 0
            or alt_changes > 0
        ):
            await interaction.response.send_message(
                f"{user.mention} and all associated "
                f"RuneScape accounts have been unlinked.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"{user.mention} does not have "
                f"any linked accounts.",
                ephemeral=True
            )

    # ========================================================
    # NICKNAME ENFORCEMENT
    # ========================================================

    @app_commands.command(
        name="nickname",
        description="Toggle nickname enforcement."
    )
    @app_commands.describe(
        state="State of nickname enforcement."
    )
    @app_commands.choices(
        state=[
            app_commands.Choice(
                name="on",
                value="on"
            ),
            app_commands.Choice(
                name="off",
                value="off"
            ),
        ]
    )
    async def nickname(
        self,
        interaction: discord.Interaction,
        state: app_commands.Choice[str]
    ):
        if not has_novus_command_permission(
            interaction.user
        ):
            await interaction.response.send_message(
                "You must be a Trial Moderator, Moderator, or Council member "
                "to use this command.",
                ephemeral=True
            )
            return

        guild_id = interaction.guild_id

        new_state = (
            1
            if state.value == "on"
            else 0
        )

        conn = sqlite3.connect(
            "wom_multi.db"
        )
        c = conn.cursor()

        c.execute(
            """
            UPDATE guild_configs
            SET nickname_enforcement = ?
            WHERE guild_id = ?
            """,
            (
                new_state,
                guild_id
            )
        )

        conn.commit()
        conn.close()

        await interaction.response.send_message(
            f"Nickname enforcement has been "
            f"set to `{state.name}`.",
            ephemeral=True
        )

    # ========================================================
    # REMINDERS
    # ========================================================

    @app_commands.command(
        name="reminder",
        description="Set the inactivity reminder for the sync log channel."
    )
    @app_commands.describe(
        interval="How long to wait before sending a reminder."
    )
    @app_commands.choices(
        interval=[
            app_commands.Choice(
                name="Off",
                value="off"
            ),
            app_commands.Choice(
                name="3 Days",
                value="3d"
            ),
            app_commands.Choice(
                name="5 Days",
                value="5d"
            ),
            app_commands.Choice(
                name="7 Days (Default)",
                value="7d"
            ),
            app_commands.Choice(
                name="14 Days",
                value="14d"
            ),
            app_commands.Choice(
                name="30 Days",
                value="30d"
            ),
        ]
    )
    async def reminder(
        self,
        interaction: discord.Interaction,
        interval: app_commands.Choice[str]
    ):
        if not has_novus_command_permission(
            interaction.user
        ):
            await interaction.response.send_message(
                "You must be a Trial Moderator, Moderator, or Council member "
                "to use this command.",
                ephemeral=True
            )
            return

        interval_map = {
            "off": 0,
            "3d": 3,
            "5d": 5,
            "7d": 7,
            "14d": 14,
            "30d": 30,
        }

        reminder_days = interval_map.get(
            interval.value,
            7
        )

        conn = sqlite3.connect(
            "wom_multi.db"
        )
        c = conn.cursor()

        c.execute(
            """
            UPDATE guild_configs
            SET reminder_interval_days = ?
            WHERE guild_id = ?
            """,
            (
                reminder_days,
                interaction.guild_id
            )
        )

        conn.commit()
        conn.close()

        if reminder_days == 0:
            await interaction.response.send_message(
                "Inactivity reminders have been disabled.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"Inactivity reminders will be sent after "
                f"**{interval.name}** of no sync changes.",
                ephemeral=True
            )

    # ========================================================
    # PLAYER DM NOTIFICATIONS
    # ========================================================

    @app_commands.command(
        name="notifyplayers",
        description="Toggle DM notifications for player role changes."
    )
    @app_commands.describe(
        state="State of DM notifications."
    )
    @app_commands.choices(
        state=[
            app_commands.Choice(
                name="on",
                value="on"
            ),
            app_commands.Choice(
                name="off",
                value="off"
            ),
        ]
    )
    async def notifyplayers(
        self,
        interaction: discord.Interaction,
        state: app_commands.Choice[str]
    ):
        if not has_novus_command_permission(
            interaction.user
        ):
            await interaction.response.send_message(
                "You must be a Trial Moderator, Moderator, or Council member "
                "to use this command.",
                ephemeral=True
            )
            return

        new_state = (
            1
            if state.value == "on"
            else 0
        )

        conn = sqlite3.connect(
            "wom_multi.db"
        )
        c = conn.cursor()

        c.execute(
            """
            UPDATE guild_configs
            SET dm_notifications_on = ?
            WHERE guild_id = ?
            """,
            (
                new_state,
                interaction.guild_id
            )
        )

        conn.commit()
        conn.close()

        if new_state:
            await interaction.response.send_message(
                "Players will now receive role-change DMs.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "Players will no longer receive role-change DMs.",
                ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(
        ConfigCog(bot)
    )