import os


WOM_API_KEY = os.getenv("WOM_API_KEY")


def sanitize_rsn(rsn: str) -> str:
    return " ".join(
        rsn.replace("-", " ").replace("_", " ").split()
    )


# Discord roles indicating a person is not an active Novus clan member.
GUEST_ROLE_ID = 1532905619412226199
CLAN_FRIEND_ROLE_ID = 1534581044274270388
NON_CLAN_ROLE_IDS = frozenset({GUEST_ROLE_ID, CLAN_FRIEND_ROLE_ID})


def has_non_clan_role(member) -> bool:
    """Guests and Clan Friends are never eligible for Novus account syncing."""
    return any(
        role.id in NON_CLAN_ROLE_IDS
        for role in member.roles
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


def forget_non_clan_links(cursor, guild) -> tuple[int, int]:
    """Remove all saved links for current Guests/Clan Friends in this guild."""
    cursor.execute(
        """
        SELECT discord_id FROM links WHERE guild_id = ?
        UNION
        SELECT discord_id FROM alt_links WHERE guild_id = ?
        """,
        (guild.id, guild.id),
    )
    ids = [row[0] for row in cursor.fetchall()]
    people = accounts = 0
    for discord_id in ids:
        member = guild.get_member(discord_id)
        if member is None or not has_non_clan_role(member):
            continue
        deleted = forget_member_links(cursor, guild.id, discord_id)
        if deleted:
            people += 1
            accounts += deleted
    return people, accounts
