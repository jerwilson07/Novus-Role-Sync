"""Regression tests for 24-hour WOM Discord nickname confirmation."""

import datetime as dt
import sqlite3
import unittest

from cogs.tasks_cog import (
    TasksCog,
    build_wom_server_nickname,
    nickname_alts_with_wom_safety,
    nickname_change_confirmed,
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

    def nickname_alts(self, current_nickname, verified_alts=()):
        return nickname_alts_with_wom_safety(
            self.cursor, 1, 2, current_nickname, list(verified_alts)
        )

    def confirmed(self, old, proposed, at=START):
        return nickname_change_confirmed(
            self.cursor, 1, 2, old, proposed, now=at
        )

    def test_manually_added_unlinked_alt_not_kept_in_wom_candidate(self):
        visible = self.nickname_alts("Vecs | NotInClan")
        self.assertEqual(visible, [])
        self.assertEqual(
            build_wom_server_nickname("Vecs", visible, "Vecs | NotInClan"),
            "Vecs",
        )

    def test_no_name_changes_without_24_hour_second_check(self):
        for initial, proposed in (
            ("Vecs", "Vecs | NewAlt"),
            ("Vecs | OldAlt", "Vecs"),
            ("OldPrimary", "RenamedPrimary"),
            ("Vecs | OldAlt", "Vecs | RenamedAlt"),
        ):
            with self.subTest(initial=initial, proposed=proposed):
                self.cursor.execute("DELETE FROM nickname_change_candidates") if (
                    self.cursor.execute(
                        "SELECT name FROM sqlite_master WHERE name='nickname_change_candidates'"
                    ).fetchone()
                ) else None
                self.assertFalse(self.confirmed(initial, proposed, START))
                self.assertFalse(self.confirmed(
                    initial, proposed, START + dt.timedelta(hours=23, minutes=59)
                ))
                self.assertTrue(self.confirmed(
                    initial, proposed, START + dt.timedelta(hours=24)
                ))

    def test_candidate_change_restarts_timer(self):
        self.assertFalse(self.confirmed("Vecs", "Vecs | A", START))
        self.assertFalse(self.confirmed(
            "Vecs", "Vecs | B", START + dt.timedelta(hours=23)
        ))
        self.assertFalse(self.confirmed(
            "Vecs", "Vecs | B", START + dt.timedelta(hours=24)
        ))
        self.assertTrue(self.confirmed(
            "Vecs", "Vecs | B", START + dt.timedelta(hours=47)
        ))

    def test_wom_recovery_cancels_pending_removal(self):
        self.assertFalse(self.confirmed("Vecs | Alt", "Vecs", START))
        self.assertFalse(self.confirmed(
            "Vecs | Alt", "Vecs | Alt", START + dt.timedelta(hours=12)
        ))
        self.assertFalse(self.confirmed(
            "Vecs | Alt", "Vecs", START + dt.timedelta(hours=25)
        ))
        self.assertTrue(self.confirmed(
            "Vecs | Alt", "Vecs", START + dt.timedelta(hours=49)
        ))

    def test_missing_linked_alt_only_changes_candidate_not_discord_yet(self):
        self.add_alt("MaybeRenamed", 2345)
        visible = self.nickname_alts("Vecs | MaybeRenamed")
        self.assertEqual(visible, [])
        desired = build_wom_server_nickname("Vecs", visible, "Vecs | MaybeRenamed")
        self.assertEqual(desired, "Vecs")
        self.assertFalse(self.confirmed("Vecs | MaybeRenamed", desired, START))
        self.assertFalse(self.confirmed(
            "Vecs | MaybeRenamed", desired, START + dt.timedelta(hours=12)
        ))
        self.assertTrue(self.confirmed(
            "Vecs | MaybeRenamed", desired, START + dt.timedelta(hours=24)
        ))
        self.assertEqual(
            self.cursor.execute("SELECT rsn, wom_id FROM alt_links").fetchall(),
            [("MaybeRenamed", 2345)],
        )

    def test_verified_alt_reappears_during_grace_and_is_retained(self):
        self.add_alt("KnownAlt", 2345)
        self.assertFalse(self.confirmed("Vecs | KnownAlt", "Vecs", START))
        visible = self.nickname_alts("Vecs | KnownAlt", ["KnownAlt"])
        self.assertEqual(visible, ["KnownAlt"])
        self.assertFalse(self.confirmed(
            "Vecs | KnownAlt",
            build_wom_server_nickname("Vecs", visible, "Vecs | KnownAlt"),
            START + dt.timedelta(hours=12),
        ))

    def test_alt_rename_resolves_by_stable_wom_id_before_grace(self):
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
        desired = build_wom_server_nickname(
            "Vecs", self.nickname_alts("Vecs | OldAlt", verified), "Vecs | OldAlt"
        )
        self.assertEqual(desired, "Vecs | NewAlt")
        self.assertFalse(self.confirmed("Vecs | OldAlt", desired, START))
        self.assertTrue(self.confirmed(
            "Vecs | OldAlt", desired, START + dt.timedelta(hours=24)
        ))

    def test_unknown_wom_id_alt_is_removed_only_after_24_hour_confirmation(self):
        self.add_alt("PotentialRename", None)
        visible = self.nickname_alts("Vecs | PotentialRename")
        self.assertEqual(visible, [])
        proposed = build_wom_server_nickname(
            "Vecs", visible, "Vecs | PotentialRename"
        )
        self.assertEqual(proposed, "Vecs")
        self.assertFalse(self.confirmed("Vecs | PotentialRename", proposed, START))
        self.assertFalse(self.confirmed(
            "Vecs | PotentialRename", proposed, START + dt.timedelta(hours=23)
        ))
        self.assertTrue(self.confirmed(
            "Vecs | PotentialRename", proposed, START + dt.timedelta(hours=24)
        ))
        # Historical link and tenure eligibility are never deleted by cleanup.
        self.assertEqual(
            self.cursor.execute("SELECT rsn, wom_id FROM alt_links").fetchall(),
            [("PotentialRename", None)]
        )

    def test_unlinked_manually_typed_alt_is_removed_after_24_hours(self):
        visible = self.nickname_alts("Vecs | ManualAlt")
        self.assertEqual(visible, [])
        self.assertFalse(self.confirmed("Vecs | ManualAlt", "Vecs", START))
        self.assertTrue(self.confirmed(
            "Vecs | ManualAlt", "Vecs", START + dt.timedelta(hours=24)
        ))

    def test_completed_change_clears_old_pending_state(self):
        self.assertFalse(self.confirmed("Old", "New", START))
        self.assertTrue(self.confirmed("Old", "New", START + dt.timedelta(hours=24)))
        self.assertFalse(self.confirmed("New", "New", START + dt.timedelta(hours=24)))
        self.assertFalse(self.confirmed("New", "Another", START + dt.timedelta(hours=25)))


if __name__ == "__main__":
    unittest.main()
