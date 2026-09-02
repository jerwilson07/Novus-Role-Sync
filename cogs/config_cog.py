import discord
from discord import app_commands
from discord.ext import commands
import sqlite3
import logging
import re

from common import sanitize_rsn, WOM_API_KEY
from typing import List


logger = logging.getLogger("WOMBot")


# ============================================================
# NOVUS STAFF PERMISSIONS
# ============================================================

COUNCIL_ROLE_ID = 1532830356875513906
MODERATOR_ROLE_ID = 1532830960062693396
GUEST_ROLE_ID = 1532905619412226199
CLAN_FRIEND_ROLE_ID = 1534581044274270388

NOVUS_COMMAND_ROLE_IDS = {
    COUNCIL_ROLE_ID,
    MODERATOR_ROLE_ID,
}

NON_CLAN_ROLE_IDS = {
    GUEST_ROLE_ID,
    CLAN_FRIEND_ROLE_ID,
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


def build_wom_headers():
    headers = {
        "User-Agent": "NovusRoleSync/1.0"
    }

    if WOM_API_KEY:
        headers["x-api-key"] = WOM_API_KEY

    return headers


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
    @app_commands.checks.has_permissions(administrator=True)
    async def set_log_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel = None
    ):
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
    @app_commands.checks.has_permissions(administrator=True)
    async def set_group_id(
        self,
        interaction: discord.Interaction,
        group_id: int
    ):
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
    @app_commands.checks.has_permissions(administrator=True)
    async def linkrole(
        self,
        interaction: discord.Interaction,
        wom_role: str,
        discord_role: discord.Role
    ):
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
    @app_commands.checks.has_permissions(administrator=True)
    async def unlinkrole(
        self,
        interaction: discord.Interaction,
        wom_role: str
    ):
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
    @app_commands.checks.has_permissions(administrator=True)
    async def linkuser(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        rsn: str
    ):
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
        conn.close()

        if alt_rsns:
            alt_text = "\n".join(
                f"- `{alt}`"
                for alt in alt_rsns
            )

            await interaction.response.send_message(
                f"Linked {user.mention}.\n\n"
                f"**Primary RSN:** `{primary_rsn}`\n"
                f"**Alt accounts:**\n"
                f"{alt_text}\n\n"
                f"Only the primary account will determine "
                f"the member's Discord rank.",
                ephemeral=True
            )

        else:
            await interaction.response.send_message(
                f"Linked {user.mention}.\n\n"
                f"**Primary RSN:** `{primary_rsn}`\n"
                f"**Alt accounts:** None",
                ephemeral=True
            )

    # ========================================================
    # AUTOMATIC USER LINKING
    # ========================================================

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
            logger.error(
                f"/autolink WOM request failed: {e}"
            )

            conn.close()

            await interaction.followup.send(
                "Could not retrieve the Novus WOM group.",
                ephemeral=True
            )
            return

        memberships = data.get(
            "memberships",
            []
        )

        wom_by_username = {
            membership["player"]["username"].lower():
                membership
            for membership in memberships
        }

        # Existing primary links are protected and will not be replaced.
        c.execute(
            """
            SELECT discord_id, rsn, wom_id
            FROM links
            WHERE guild_id = ?
            """,
            (guild.id,)
        )

        existing_rows = c.fetchall()

        existing_links = {
            row[0]
            for row in existing_rows
        }

        # Track every WOM account already associated with a Discord user.
        claimed_wom_ids = {
            wom_id
            for _, _, wom_id in existing_rows
            if wom_id is not None
        }

        claimed_wom_names = {
            rsn.lower()
            for _, rsn, _ in existing_rows
            if rsn
        }

        c.execute(
            """
            SELECT rsn, wom_id
            FROM alt_links
            WHERE guild_id = ?
            """,
            (guild.id,)
        )

        existing_alt_rows = c.fetchall()

        claimed_wom_ids.update(
            wom_id
            for _, wom_id in existing_alt_rows
            if wom_id is not None
        )

        claimed_wom_names.update(
            rsn.lower()
            for rsn, _ in existing_alt_rows
            if rsn
        )

        linked_members = []
        already_linked = []
        unmatched_discord_members = []
        partial_alt_matches = []
        ignored_guest_members = []
        ignored_clan_friend_members = []
        membership_conflicts = []

        for member in guild.members:

            if member.bot:
                continue

            if member.id in existing_links:
                already_linked.append(
                    member
                )
                continue

            member_role_ids = {
                role.id
                for role in member.roles
            }

            has_guest_role = (
                GUEST_ROLE_ID in member_role_ids
            )
            has_clan_friend_role = (
                CLAN_FRIEND_ROLE_ID in member_role_ids
            )

            display_name = member.display_name

            # Supported formats:
            # Main RSN
            # Main RSN | Alt RSN
            # Main RSN / Alt RSN
            # Either separator may be used more than once.
            raw_parts = [
                part.strip()
                for part in re.split(r"[|/]", display_name)
                if part.strip()
            ]

            if not raw_parts:
                unmatched_discord_members.append(
                    member
                )
                continue

            accounts = [
                sanitize_rsn(part)
                for part in raw_parts
            ]

            primary_candidate = accounts[0]

            primary_membership = (
                wom_by_username.get(
                    primary_candidate.lower()
                )
            )

            # Guest and Clan Friend are intentionally non-clan roles.
            # If one of them exactly matches a current WOM member,
            # report it as a role conflict instead of silently ignoring it.
            if has_guest_role or has_clan_friend_role:
                if primary_membership is not None:
                    conflict_roles = []

                    if has_guest_role:
                        conflict_roles.append(
                            "Guest"
                        )

                    if has_clan_friend_role:
                        conflict_roles.append(
                            "Clan Friend"
                        )

                    membership_conflicts.append(
                        {
                            "member": member,
                            "rsn": primary_membership["player"]["username"],
                            "roles": conflict_roles,
                        }
                    )
                else:
                    if has_guest_role:
                        ignored_guest_members.append(
                            member
                        )

                    if has_clan_friend_role:
                        ignored_clan_friend_members.append(
                            member
                        )

                continue

            # Never guess the primary identity.
            if primary_membership is None:
                unmatched_discord_members.append(
                    member
                )
                continue

            actual_primary_rsn = (
                primary_membership[
                    "player"
                ]["username"]
            )

            primary_wom_id = (
                primary_membership[
                    "player"
                ]["id"]
            )

            # Do not associate one WOM primary account with two Discord users.
            if (
                primary_wom_id in claimed_wom_ids
                or actual_primary_rsn.lower() in claimed_wom_names
            ):
                unmatched_discord_members.append(
                    member
                )
                continue

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

            claimed_wom_ids.add(
                primary_wom_id
            )
            claimed_wom_names.add(
                actual_primary_rsn.lower()
            )

            verified_alts = []
            missing_alts = []

            for alt_candidate in accounts[1:]:

                alt_membership = (
                    wom_by_username.get(
                        alt_candidate.lower()
                    )
                )

                if alt_membership is None:
                    missing_alts.append(
                        alt_candidate
                    )
                    continue

                actual_alt_rsn = (
                    alt_membership[
                        "player"
                    ]["username"]
                )

                alt_wom_id = (
                    alt_membership[
                        "player"
                    ]["id"]
                )

                # Never associate the same WOM account with two people.
                if (
                    alt_wom_id in claimed_wom_ids
                    or actual_alt_rsn.lower() in claimed_wom_names
                ):
                    missing_alts.append(
                        alt_candidate
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

                claimed_wom_ids.add(
                    alt_wom_id
                )
                claimed_wom_names.add(
                    actual_alt_rsn.lower()
                )

                verified_alts.append(
                    actual_alt_rsn
                )

            linked_members.append(
                {
                    "member": member,
                    "primary": actual_primary_rsn,
                    "alts": verified_alts,
                }
            )

            if missing_alts:
                partial_alt_matches.append(
                    {
                        "member": member,
                        "missing": missing_alts,
                    }
                )

        conn.commit()

        # Re-read all links after this run so the reverse WOM audit
        # reflects both old and newly-created associations.
        c.execute(
            """
            SELECT rsn, wom_id
            FROM links
            WHERE guild_id = ?
            """,
            (guild.id,)
        )
        all_primary_links = c.fetchall()

        c.execute(
            """
            SELECT rsn, wom_id
            FROM alt_links
            WHERE guild_id = ?
            """,
            (guild.id,)
        )
        all_alt_links = c.fetchall()

        conn.close()

        final_claimed_ids = {
            wom_id
            for _, wom_id in all_primary_links + all_alt_links
            if wom_id is not None
        }

        final_claimed_names = {
            rsn.lower()
            for rsn, _ in all_primary_links + all_alt_links
            if rsn
        }

        unmatched_wom_accounts = []

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
                wom_id not in final_claimed_ids
                and username.lower() not in final_claimed_names
            ):
                unmatched_wom_accounts.append(
                    username
                )

        unmatched_wom_accounts.sort(
            key=str.lower
        )

        # ----------------------------------------------------
        # BUILD REPORT
        # ----------------------------------------------------

        response = (
            f"**Novus Auto-Link Complete**\n\n"
            f"Current WOM clan accounts: "
            f"**{len(memberships)}**\n"
            f"New Discord members linked: "
            f"**{len(linked_members)}**\n"
            f"Already linked: "
            f"**{len(already_linked)}**\n"
            f"Discord members not confidently matched: "
            f"**{len(unmatched_discord_members)}**\n"
            f"WOM clan accounts still unmatched: "
            f"**{len(unmatched_wom_accounts)}**\n"
            f"Guests ignored: "
            f"**{len(ignored_guest_members)}**\n"
            f"Clan Friends ignored: "
            f"**{len(ignored_clan_friend_members)}**"
        )

        if partial_alt_matches:
            response += (
                f"\nMembers with an unmatched alt: "
                f"**{len(partial_alt_matches)}**"
            )

        if linked_members:
            examples = []

            for entry in linked_members[:10]:
                text = (
                    f"{entry['member'].mention} "
                    f"-> `{entry['primary']}`"
                )

                if entry["alts"]:
                    text += (
                        " | "
                        + " | ".join(
                            f"`{alt}`"
                            for alt in entry["alts"]
                        )
                    )

                examples.append(
                    text
                )

            response += (
                "\n\n**New Links**\n"
                + "\n".join(examples)
            )

            if len(linked_members) > 10:
                response += (
                    f"\n...and "
                    f"{len(linked_members) - 10} more."
                )

        if unmatched_wom_accounts:
            wom_lines = [
                f"- `{username}`"
                for username in unmatched_wom_accounts[:30]
            ]

            response += (
                "\n\n**WOM Accounts Still Unmatched**\n"
                + "\n".join(wom_lines)
            )

            if len(unmatched_wom_accounts) > 30:
                response += (
                    f"\n...and "
                    f"{len(unmatched_wom_accounts) - 30} more."
                )

        if membership_conflicts:
            conflict_lines = []

            for entry in membership_conflicts[:20]:
                conflict_lines.append(
                    f"- {entry['member'].mention}: "
                    f"`{entry['rsn']}` is in WOM but has "
                    + " / ".join(entry["roles"])
                )

            response += (
                "\n\n**Possible Membership Conflicts**\n"
                + "\n".join(conflict_lines)
            )

            if len(membership_conflicts) > 20:
                response += (
                    f"\n...and "
                    f"{len(membership_conflicts) - 20} more."
                )

        if partial_alt_matches:
            partial_lines = []

            for entry in partial_alt_matches[:10]:
                partial_lines.append(
                    f"- {entry['member'].mention}: "
                    + ", ".join(
                        f"`{name}`"
                        for name in entry["missing"]
                    )
                )

            response += (
                "\n\n**Unmatched Alt Names**\n"
                + "\n".join(partial_lines)
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
    # STAFF AUDIT
    # ========================================================

    @app_commands.command(
        name="audit",
        description="Run a combined Novus WOM/Discord link audit."
    )
    async def audit(
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

        linked_wom_ids = set()
        linked_names = set()

        broken_primaries = []
        missing_alts = []

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
            else:
                member = guild.get_member(
                    discord_id
                )

                member_text = (
                    member.mention
                    if member
                    else f"<@{discord_id}>"
                )

                broken_primaries.append(
                    f"- {member_text}: `{rsn}`"
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
            else:
                member = guild.get_member(
                    discord_id
                )

                member_text = (
                    member.mention
                    if member
                    else f"<@{discord_id}>"
                )

                missing_alts.append(
                    f"- {member_text}: `{rsn}`"
                )

        # ----------------------------------------------------
        # WOM ACCOUNTS NOT ASSOCIATED WITH ANY DISCORD LINK
        # ----------------------------------------------------

        unmatched_wom = []

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
                ""
            )

            if (
                wom_id not in linked_wom_ids
                and username.lower() not in linked_names
            ):
                unmatched_wom.append(
                    username
                )

        unmatched_wom.sort(
            key=str.lower
        )

        # ----------------------------------------------------
        # GUEST / CLAN FRIEND MEMBERSHIP CONFLICTS
        # ----------------------------------------------------

        membership_conflicts = []

        for member in guild.members:
            member_role_ids = {
                role.id
                for role in member.roles
            }

            non_clan_roles = (
                member_role_ids
                & NON_CLAN_ROLE_IDS
            )

            if not non_clan_roles:
                continue

            display_name = (
                member.display_name
                or member.name
            )

            parts = [
                sanitize_rsn(part)
                for part in re.split(
                    r"[|/]",
                    display_name
                )
                if sanitize_rsn(part)
            ]

            if not parts:
                continue

            primary_name = parts[0].lower()

            if primary_name in wom_by_username:
                labels = []

                if GUEST_ROLE_ID in non_clan_roles:
                    labels.append(
                        "Guest"
                    )

                if CLAN_FRIEND_ROLE_ID in non_clan_roles:
                    labels.append(
                        "Clan Friend"
                    )

                role_text = " / ".join(
                    labels
                )

                membership_conflicts.append(
                    f"- {member.mention}: "
                    f"`{parts[0]}` ({role_text})"
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
            f"**{len(unmatched_wom)}**\n"
            f"Broken primary links: "
            f"**{len(broken_primaries)}**\n"
            f"Missing linked alts: "
            f"**{len(missing_alts)}**\n"
            f"Guest/Clan Friend conflicts: "
            f"**{len(membership_conflicts)}**"
        )

        sections = []

        if unmatched_wom:
            sections.append(
                "**Unmatched WOM Accounts**\n"
                + "\n".join(
                    f"- `{name}`"
                    for name in unmatched_wom
                )
            )

        if broken_primaries:
            sections.append(
                "**Broken Primary Links**\n"
                + "\n".join(
                    broken_primaries
                )
            )

        if missing_alts:
            sections.append(
                "**Missing Linked Alts**\n"
                + "\n".join(
                    missing_alts
                )
            )

        if membership_conflicts:
            sections.append(
                "**Possible Membership Conflicts**\n"
                + "\n".join(
                    membership_conflicts
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

        wom_by_id = {
            membership["player"]["id"]: membership
            for membership in memberships
        }

        wom_by_username = {
            membership["player"]["username"].lower():
                membership
            for membership in memberships
        }

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
            else:
                wom_id_text = (
                    f"`{alt_wom_id}`"
                    if alt_wom_id is not None
                    else "Not set"
                )

                alt_lines.append(
                    f"- `{alt_rsn}` "
                    f"(WOM ID: {wom_id_text}) - Not found in current WOM group"
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
            "\n\nOnly the primary account determines "
            "the member's Discord rank."
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
                "You must be a Moderator or Council member "
                "to use this command.",
                ephemeral=True
            )
            return

        response = (
            "**Novus Sync Staff Commands**\n\n"
            "**/syncuser @member**\n"
            "Sync one linked member's WOM rank to Discord.\n\n"

            "**/syncall**\n"
            "Sync all currently linked Novus members.\n\n"

            "**/accounts @member**\n"
            "Show a member's primary RSN, alts, WOM IDs, and current WOM rank.\n\n"

            "**/audit**\n"
            "Run one combined audit for unmatched WOM accounts, broken links, missing alts, and membership conflicts.\n\n"

            "**/promotions**\n"
            "Show linked primary accounts that are overdue "
            "for a tenure promotion.\n\n"

            "**/unmatched**\n"
            "Show current WOM clan accounts that are not "
            "associated with a Discord user.\n\n"

            "**/autolink**\n"
            "Automatically link exact Discord/WOM name matches. "
            "Supports `Main | Alt` and `Main / Alt`.\n\n"

            "**/linkuser @member RSN**\n"
            "Manually link a member. Put the primary RSN first, "
            "then optional alts separated by `|` or `/`. "
            "Only the primary determines Discord rank.\n\n"

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
            """,
            (guild.id,)
        )
        primary_links = c.fetchall()

        c.execute(
            """
            SELECT rsn, wom_id
            FROM alt_links
            WHERE guild_id = ?
            """,
            (guild.id,)
        )
        alt_links = c.fetchall()

        conn.close()

        linked_ids = {
            wom_id
            for _, wom_id in primary_links + alt_links
            if wom_id is not None
        }

        linked_names = {
            rsn.lower()
            for rsn, _ in primary_links + alt_links
            if rsn
        }

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
    @app_commands.checks.has_permissions(administrator=True)
    async def unlinkuser(
        self,
        interaction: discord.Interaction,
        user: discord.Member
    ):
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
    @app_commands.checks.has_permissions(administrator=True)
    async def nickname(
        self,
        interaction: discord.Interaction,
        state: app_commands.Choice[str]
    ):
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
    @app_commands.checks.has_permissions(administrator=True)
    async def reminder(
        self,
        interaction: discord.Interaction,
        interval: app_commands.Choice[str]
    ):
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
    @app_commands.checks.has_permissions(administrator=True)
    async def notifyplayers(
        self,
        interaction: discord.Interaction,
        state: app_commands.Choice[str]
    ):
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