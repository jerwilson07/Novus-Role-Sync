"""Regression tests for rename-safe Novus Discord nickname synchronization."""

import datetime as dt
import sqlite3
import unittest

from cogs.tasks_cog import (
    TasksCog,
    build_wom_server_nickname,
    nickname_alts_with_wom_safety,
)


UTC = dt.timezone.utc
START = dt.datetime(2026, 11, 11, 14, 0, tzinfo=UTC)


class NicknameSyncTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.cursor = self.conn.cursor()
        self.cursor.execute(
            """CREATE TABLE alt_links (
                guild_id INTEGER,
                discord_id INTEGER,
                rsn TEXT,
                wom_id INTEGER,
                PRIMARY KEY (guild_id, discord_id, rsn)
            )"""
        )

    def tearDown(self):
        self.conn.close()

    def add_alt(self, rsn, wom_id):
        self.cursor.execute(
            "INSERT INTO alt_links (guild_id, discord_id, rsn, wom_id) "
            "VALUES (1, 2, ?, ?)",
            (rsn, wom_id),
        )

    def nickname_alts(self, current_nickname, verified_alts=(), at=START):
        return nickname_alts_with_wom_safety(
            self.cursor, 1, 2, current_nickname, list(verified_alts), now=at
        )

    def test_no_alt_cleans_up_separator_and_manual_unlinked_name(self):
        visible = self.nickname_alts("Vecs | NotInClan")
        self.assertEqual(visible, [])
        self.assertEqual(
            build_wom_server_nickname("Vecs", visible, "Vecs | NotInClan"),
            "Vecs",
        )

    def test_linked_alt_in_clan_is_kept(self):
        self.add_alt("AltName", 2345)
        visible = self.nickname_alts("Vecs | AltName", ["AltName"])
        self.assertEqual(visible, ["AltName"])
        self.assertEqual(
            build_wom_server_nickname("Vecs", visible, "Vecs | AltName"),
            "Vecs | AltName",
        )

    def test_missing_linked_alt_gets_full_24_hour_grace(self):
        self.add_alt("MaybeRenamed", 2345)
        self.assertEqual(
            self.nickname_alts("Vecs | MaybeRenamed"), ["MaybeRenamed"]
        )
        self.assertEqual(
            self.nickname_alts(
                "Vecs | MaybeRenamed", at=START + dt.timedelta(hours=23, minutes=59)
            ),
            ["MaybeRenamed"],
        )
        visible = self.nickname_alts(
            "Vecs | MaybeRenamed", at=START + dt.timedelta(hours=24)
        )
        self.assertEqual(visible, [])
        self.assertEqual(
            build_wom_server_nickname("Vecs", visible, "Vecs | MaybeRenamed"),
            "Vecs",
        )
        # Removing an inactive alt from a nickname does not delete its link.
        self.assertEqual(
            self.cursor.execute("SELECT rsn, wom_id FROM alt_links").fetchall(),
            [("MaybeRenamed", 2345)],
        )

    def test_verified_reappearance_resets_missing_timer(self):
        self.add_alt("KnownAlt", 2345)
        self.nickname_alts("Vecs | KnownAlt")
        self.assertEqual(
            self.nickname_alts(
                "Vecs | KnownAlt", ["KnownAlt"], START + dt.timedelta(hours=12)
            ),
            ["KnownAlt"],
        )
        self.assertEqual(
            self.nickname_alts(
                "Vecs | KnownAlt", at=START + dt.timedelta(hours=30)
            ),
            ["KnownAlt"],
        )

    def test_alt_rename_resolves_by_stable_wom_id(self):
        self.add_alt("OldAlt", 2345)
        cog = TasksCog.__new__(TasksCog)
        membership = {"player": {"id": 2345, "username": "NewAlt"}}
        verified, missing = cog.sync_alt_links_in_database(
            self.cursor, 1, 2, {2345: membership}, {"newalt": membership}
        )
        self.assertEqual((verified, missing), (["NewAlt"], []))
        self.assertEqual(
            self.cursor.execute("SELECT rsn, wom_id FROM alt_links").fetchall(),
            [("NewAlt", 2345)],
        )
        self.assertEqual(
            build_wom_server_nickname(
                "Vecs", self.nickname_alts("Vecs | OldAlt", verified), "Vecs | OldAlt"
            ),
            "Vecs | NewAlt",
        )

    def test_unknown_wom_id_is_preserved_until_identity_verified(self):
        self.add_alt("PotentialRename", None)
        self.nickname_alts("Vecs | PotentialRename")
        # Without a stable WOM ID, an absent old RSN may actually be renamed.
        self.assertEqual(
            self.nickname_alts(
                "Vecs | PotentialRename", at=START + dt.timedelta(days=4)
            ),
            ["PotentialRename"],
        )


if __name__ == "__main__":
    unittest.main()
