"""Regression tests for Guest/Clan Friend exclusion and account-link cleanup."""

import sqlite3
import unittest
from types import SimpleNamespace

from common import (
    CLAN_FRIEND_ROLE_ID,
    GUEST_ROLE_ID,
    forget_member_links,
    forget_non_clan_links,
    has_non_clan_role,
)


class DummyGuild:
    def __init__(self, guild_id, members):
        self.id = guild_id
        self._members = {member.id: member for member in members}

    def get_member(self, user_id):
        return self._members.get(user_id)


def member(user_id, *role_ids):
    return SimpleNamespace(
        id=user_id,
        roles=[SimpleNamespace(id=role_id) for role_id in role_ids],
    )


class NonClanExclusionTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.cur = self.db.cursor()
        self.cur.execute(
            "CREATE TABLE links (guild_id INTEGER, discord_id INTEGER, rsn TEXT)"
        )
        self.cur.execute(
            "CREATE TABLE alt_links (guild_id INTEGER, discord_id INTEGER, rsn TEXT)"
        )
        for guild_id, discord_id, rsn in (
            (1, 10, "GuestMain"), (1, 20, "FriendMain"),
            (1, 30, "MemberMain"), (2, 10, "OtherGuildMain"),
        ):
            self.cur.execute(
                "INSERT INTO links VALUES (?, ?, ?)", (guild_id, discord_id, rsn)
            )
        self.cur.executemany(
            "INSERT INTO alt_links VALUES (?, ?, ?)",
            [(1, 10, "GuestAlt"), (1, 20, "FriendAlt"), (1, 30, "MemberAlt")],
        )
        self.guild = DummyGuild(
            1,
            [
                member(10, GUEST_ROLE_ID),
                member(20, CLAN_FRIEND_ROLE_ID),
                member(30, 987654),
            ],
        )

    def tearDown(self):
        self.db.close()

    def count(self, table, guild_id, user_id):
        return self.cur.execute(
            f"SELECT COUNT(*) FROM {table} WHERE guild_id = ? AND discord_id = ?",
            (guild_id, user_id),
        ).fetchone()[0]

    def test_role_recognition(self):
        self.assertTrue(has_non_clan_role(member(10, GUEST_ROLE_ID)))
        self.assertTrue(has_non_clan_role(member(20, CLAN_FRIEND_ROLE_ID)))
        self.assertFalse(has_non_clan_role(member(30, 987654)))

    def test_purge_removes_only_excluded_members_and_alts(self):
        people, accounts = forget_non_clan_links(self.cur, self.guild)
        self.assertEqual((people, accounts), (2, 4))
        for user_id in (10, 20):
            self.assertEqual(self.count("links", 1, user_id), 0)
            self.assertEqual(self.count("alt_links", 1, user_id), 0)
        self.assertEqual(self.count("links", 1, 30), 1)
        self.assertEqual(self.count("alt_links", 1, 30), 1)
        self.assertEqual(self.count("links", 2, 10), 1)

    def test_purge_is_idempotent(self):
        forget_non_clan_links(self.cur, self.guild)
        self.assertEqual(forget_non_clan_links(self.cur, self.guild), (0, 0))

    def test_single_user_cleanup_does_not_touch_others(self):
        self.assertEqual(forget_member_links(self.cur, 1, 10), 2)
        self.assertEqual(self.count("links", 1, 30), 1)
        self.assertEqual(self.count("links", 2, 10), 1)


if __name__ == "__main__":
    unittest.main()
