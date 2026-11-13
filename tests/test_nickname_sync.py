"""Regression tests for 24-hour WOM Discord nickname confirmation."""

import datetime as dt
import sqlite3
import unittest

from cogs.tasks_cog import (
    TasksCog,
    build_wom_server_nickname,
    nickname_alts_with_wom_safety,
    nickname_change_confirmed,
    is_case_only_nickname_change,
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

    def test_spilled_primary_rename_to_dctr_after_24_hours(self):
        # The stable WOM player ID is used by sync_one_member to read
        # the current name Dctr and propose this server nickname.
        desired = build_wom_server_nickname("Dctr", [], "spilled")
        self.assertEqual(desired, "Dctr")
        self.assertFalse(self.confirmed("spilled", desired, START))
        self.assertFalse(self.confirmed(
            "spilled", desired, START + dt.timedelta(hours=23)
        ))
        self.assertTrue(self.confirmed(
            "spilled", desired, START + dt.timedelta(hours=24)
        ))

    def test_unlinked_manual_alt_still_present_in_wom_is_preserved(self):
        # Do not remove a nickname's alt merely because the bot has no
        # alt_links row when the alt is demonstrably on the current roster.
        visible = nickname_alts_with_wom_safety(
            self.cursor, 1, 2, "Vecs | ActiveAlt", [],
            in_clan_names={"activealt": "ActiveAlt"},
        )
        self.assertEqual(visible, ["ActiveAlt"])
        self.assertEqual(
            build_wom_server_nickname("Vecs", visible, "Vecs | ActiveAlt"),
            "Vecs | ActiveAlt",
        )

    def test_underscore_alias_of_active_wom_alt_survives(self):
        visible = nickname_alts_with_wom_safety(
            self.cursor, 1, 2, "Vecs | p_p", [],
            in_clan_names={"p p": "p p"},
        )
        self.assertEqual(visible, ["p p"])
        self.assertEqual(
            build_wom_server_nickname("Vecs", visible, "Vecs | p_p"),
            "Vecs | p p",
        )
        # Formatting changes also obey the same 24-hour confirmation.
        self.assertFalse(self.confirmed("Vecs | p_p", "Vecs | p p", START))
        self.assertTrue(self.confirmed(
            "Vecs | p_p", "Vecs | p p",
            START + dt.timedelta(hours=24),
        ))

    def test_registered_alt_with_wom_underscore_alias_stays_verified(self):
        self.add_alt("p_p", 2345)
        visible = nickname_alts_with_wom_safety(
            self.cursor, 1, 2, "Vecs | p_p", ["p p"],
            in_clan_names={"p p": "p p"},
        )
        self.assertEqual(visible, ["p_p"])

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

    def test_recycled_rsn_does_not_transfer_known_alt_wom_id(self):
        self.add_alt("OldAlt", 2345)
        cog = TasksCog.__new__(TasksCog)
        imposter = {"player": {"id": 9999, "username": "OldAlt"}}
        verified, missing = cog.sync_alt_links_in_database(
            self.cursor, 1, 2,
            {9999: imposter}, {"oldalt": imposter},
        )
        self.assertEqual(verified, [])
        self.assertEqual(missing, ["OldAlt"])
        self.assertEqual(
            self.cursor.execute(
                "SELECT rsn, wom_id FROM alt_links"
            ).fetchall(), [("OldAlt", 2345)],
        )

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


    def test_case_only_wom_corrections_are_immediate(self):
        self.assertTrue(is_case_only_nickname_change("mavara", "Mavara"))
        self.assertTrue(is_case_only_nickname_change(
            "mavara | ozilda", "Mavara | Ozilda"
        ))
        self.assertTrue(is_case_only_nickname_change("t1vecs", "T1Vecs"))

    def test_actual_renames_and_membership_changes_still_wait(self):
        for current, proposed in (
            ("spilled", "Dctr"),
            ("Vecs", "Vecs | NewAlt"),
            ("Vecs | OldAlt", "Vecs"),
            ("Vecs / Alt", "Vecs | Alt"),
            (None, "Mavara"),
        ):
            with self.subTest(current=current, proposed=proposed):
                self.assertFalse(is_case_only_nickname_change(
                    current, proposed
                ))

    def test_primary_and_alt_capitalization_matches_live_wom_after_24_hours(self):
        # Case-only changes should be proposed, but never skip the grace period.
        desired = build_wom_server_nickname(
            "Mavara", ["Ozilda"], "mavara | ozilda"
        )
        self.assertEqual(desired, "Mavara | Ozilda")
        self.assertFalse(self.confirmed("mavara | ozilda", desired, START))
        self.assertFalse(self.confirmed(
            "mavara | ozilda", desired,
            START + dt.timedelta(hours=23),
        ))
        self.assertTrue(self.confirmed(
            "mavara | ozilda", desired,
            START + dt.timedelta(hours=24),
        ))

    def test_linked_alt_case_only_change_refreshes_database_and_nickname(self):
        self.add_alt("ozilda", 2345)
        cog = TasksCog.__new__(TasksCog)
        membership = {"player": {"id": 2345, "username": "Ozilda"}}
        verified, missing = cog.sync_alt_links_in_database(
            self.cursor, 1, 2, {2345: membership}, {"ozilda": membership}
        )
        self.assertEqual((verified, missing), (["Ozilda"], []))
        self.assertEqual(
            self.cursor.execute("SELECT rsn, wom_id FROM alt_links").fetchall(),
            [("Ozilda", 2345)],
        )
        visible = self.nickname_alts("Mavara | ozilda", verified)
        self.assertEqual(visible, ["Ozilda"])
        self.assertEqual(
            build_wom_server_nickname("Mavara", visible, "Mavara | ozilda"),
            "Mavara | Ozilda",
        )

    def test_unlinked_manual_alt_adopts_wom_capitalization(self):
        visible = nickname_alts_with_wom_safety(
            self.cursor, 1, 2, "Mavara | ozilda", [],
            in_clan_names={"ozilda": "Ozilda"},
        )
        self.assertEqual(visible, ["Ozilda"])


if __name__ == "__main__":
    unittest.main()
