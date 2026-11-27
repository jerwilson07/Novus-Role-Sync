import os


WOM_API_KEY = os.getenv("WOM_API_KEY")


def sanitize_rsn(rsn: str) -> str:
    return " ".join(
        rsn.replace("-", " ").replace("_", " ").split()
    )


def wom_display_rsn(player: dict, fallback: str | None = None) -> str:
    """Return WOM's capitalization-preserving RSN, not its lowercase username.

    WOM's `username` is a lowercase lookup key; `displayName` is the
    canonical RuneScape display spelling. When an older/mock WOM payload lacks
    displayName, preserve a previously linked spelling only if it still
    matches the same username, otherwise use the verified username.
    """
    if not isinstance(player, dict):
        return fallback or ""

    display_name = player.get("displayName")
    if isinstance(display_name, str) and display_name.strip():
        return display_name.strip()

    username = player.get("username")
    if isinstance(username, str) and username.strip():
        username = username.strip()
        if (
            fallback
            and sanitize_rsn(fallback).casefold()
                == sanitize_rsn(username).casefold()
        ):
            return fallback
        return username

    return fallback or ""


# Discord roles indicating a person is not an active Novus clan member.
CLAN_FRIEND_ROLE_ID = 1534581044274270388
NON_CLAN_ROLE_IDS = frozenset({CLAN_FRIEND_ROLE_ID})


def has_non_clan_role(member) -> bool:
    """Identify non-clan roles that block active clan sync, not saved links."""
    return any(
        role.id in NON_CLAN_ROLE_IDS
        for role in getattr(member, "roles", ())
    )


def forget_member_links(cursor, guild_id: int, discord_id: int) -> int:
    """Delete saved primary and alt RSNs; never modify Discord roles."""
    deleted = 0
    for table in ("links", "alt_links"):
        cursor.execute(
            f"DELETE FROM {table} WHERE guild_id = ? AND discord_id = ?",
            (guild_id, discord_id),
        )
        deleted += cursor.rowcount
    return deleted


def clan_sync_link_rows(guild, rows):
    """Keep Clan Friend links out of clan matching and reports."""
    return [row for row in rows if not has_non_clan_role(guild.get_member(row[0]))]


def forget_unmatched_alt_links(cursor, guild_id: int, memberships: list) -> int:
    """Legacy compatibility no-op.

    Linked alts are retained when they temporarily leave the WOM roster so
    their independent tenure history remains associated with the same Discord
    member if they later rejoin. Out-of-clan alts are simply ignored by sync.
    """
    return 0



# Verified is a server-access role, not evidence of active clan membership.
# Regular Novus membership requires BOTH the exact Discord "Member" role
# and a linked primary account in the current Novus WOM group.
NOVUS_MEMBER_ROLE_NAME = "member"


def has_novus_member_role(member) -> bool:
    """True only for Discord Member-role holders who aren't Clan Friend."""
    if member is None or has_non_clan_role(member):
        return False
    return any(
        getattr(role, "name", "").strip().casefold() == NOVUS_MEMBER_ROLE_NAME
        for role in member.roles
    )


def find_primary_wom_membership(rsn, wom_id, memberships):
    """Find a linked primary on the Novus WOM roster, including renamed RSNs.

    This function only determines eligibility; missing members are never
    unlinked merely because WOM temporarily omitted them.
    """
    if not isinstance(memberships, list):
        return None

    if wom_id is not None:
        # A verified ID is authoritative. If WOM temporarily loses this
        # player, never reassign the link to a different player who happens
        # to use the saved RSN (including a recycled name).
        for entry in memberships:
            if isinstance(entry, dict) and isinstance(entry.get("player"), dict):
                if entry["player"].get("id") == wom_id:
                    return entry
        return None

    if rsn:
        candidate_key = sanitize_rsn(rsn).casefold()
        for entry in memberships:
            if isinstance(entry, dict) and isinstance(entry.get("player"), dict):
                username = entry["player"].get("username")
                if username and sanitize_rsn(username).casefold() == candidate_key:
                    return entry
    return None


def is_active_novus_member_link(member, rsn, wom_id, memberships) -> bool:
    """Active clan member = Discord Member role AND current WOM primary."""
    return has_novus_member_role(member) and (
        find_primary_wom_membership(rsn, wom_id, memberships) is not None
    )
