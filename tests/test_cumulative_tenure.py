"""Regression tests for cumulative Novus clan tenure across re-joins."""

import datetime as dt
import sqlite3
import unittest

from tenure_history import (
    UTC,
    cumulative_effective_join,
    initialize_tenure_tables,
    parse_timestamp,
    prior_tenure,
    record_roster,
    store_group_activity,
    set_manual_previous_tenure,
    get_manual_previous_tenure,
)

GUILD = 1
GROUP = 42
PLAYER = 123


def d(s):
    return parse_timestamp(s + "T00:00:00Z")


def membership(joined, player_id=PLAYER):
    return {
        "player": {"id": player_id, "username": "NovusMain"},
        "clientSyncJoinedAt": joined.isoformat() if joined else None
    }


def activity(kind, when, player_id=PLAYER):
    return {"playerId": player_id, "type": kind, "createdAt": when.isoformat()}


class CumulativeTenureTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.c = self.conn.cursor()
        initialize_tenure_tables(self.c)

    def tearDown(self):
        self.conn.close()

    def test_new_member_without_prior_stints(self):
        joined = d("2026-08-01")
        effective, past = cumulative_effective_join(
            self.c, GUILD, GROUP, PLAYER, joined
        )
        self.assertEqual(effective, joined)
        self.assertEqual(past, dt.timedelta())

    def test_historic_join_leave_credits_previous_period(self):
        events = [
            activity("joined", d("2025-01-01")),
            activity("left", d("2025-04-01")),
            activity("joined", d("2026-07-01")),
        ]
        store_group_activity(self.c, GUILD, GROUP, events)
        joined = d("2026-07-01")
        effective, past = cumulative_effective_join(
            self.c, GUILD, GROUP, PLAYER, joined
        )
        self.assertEqual(past, dt.timedelta(days=90))
        self.assertEqual(effective, joined - dt.timedelta(days=90))

    def test_multiple_stints_exclude_time_between_clans(self):
        events = [
            activity("joined", d("2024-01-01")),
            activity("left", d("2024-04-01")),
            activity("joined", d("2025-01-01")),
            activity("left", d("2025-04-01")),
        ]
        store_group_activity(self.c, GUILD, GROUP, events)
        expected = dt.timedelta(days=91+90)
        self.assertEqual(prior_tenure(
            self.c, GUILD, GROUP, PLAYER, d("2026-01-01")
        ), expected)

    def test_no_credit_for_incomplete_join_only(self):
        store_group_activity(self.c, GUILD, GROUP, [
            activity("joined", d("2024-01-01"))
        ])
        self.assertEqual(prior_tenure(
            self.c, GUILD, GROUP, PLAYER, d("2026-01-01")
        ), dt.timedelta())

    def test_no_credit_for_other_player_or_group(self):
        store_group_activity(self.c, GUILD, GROUP, [
            activity("joined", d("2024-01-01"), 200),
            activity("left", d("2024-04-01"), 200),
        ])
        self.assertEqual(prior_tenure(
            self.c, GUILD, GROUP, PLAYER, d("2026-01-01")
        ), dt.timedelta())

    def test_duplicate_activity_is_idempotent(self):
        events = [
            activity("joined", d("2025-01-01")),
            activity("left", d("2025-02-01")),
        ]
        self.assertEqual(store_group_activity(self.c, GUILD, GROUP, events), 2)
        self.assertEqual(store_group_activity(self.c, GUILD, GROUP, events), 0)
        self.assertEqual(prior_tenure(
            self.c, GUILD, GROUP, PLAYER, d("2026-01-01")
        ), dt.timedelta(days=31))

    def test_previous_observed_stint_kept_after_member_leaves(self):
        first = d("2025-01-01")
        record_roster(self.c, GUILD, GROUP, [membership(first)], d("2025-04-01"))
        record_roster(self.c, GUILD, GROUP, [
            membership(first, player_id=234)
        ], d("2025-04-02"))
        previous = prior_tenure(
            self.c, GUILD, GROUP, PLAYER, d("2026-01-01")
        )
        self.assertEqual(previous, dt.timedelta(days=90))

    def test_overlapping_activity_and_observations_not_double_counted(self):
        events = [
            activity("joined", d("2025-01-01")),
            activity("left", d("2025-04-01")),
        ]
        store_group_activity(self.c, GUILD, GROUP, events)
        record_roster(
            self.c, GUILD, GROUP, [membership(d("2025-01-01"))],
            d("2025-03-28")
        )
        record_roster(self.c, GUILD, GROUP, [
            membership(d("2025-01-01"), 234)
        ], d("2025-04-02"))
        self.assertEqual(prior_tenure(
            self.c, GUILD, GROUP, PLAYER, d("2026-01-01")
        ), dt.timedelta(days=90))

    def test_current_stint_not_double_counted(self):
        store_group_activity(self.c, GUILD, GROUP, [
            activity("joined", d("2025-01-01")),
            activity("left", d("2025-04-01")),
            activity("joined", d("2026-01-01")),
        ])
        current = d("2026-01-01")
        record_roster(
            self.c, GUILD, GROUP, [membership(current)],
            d("2026-03-01")
        )
        self.assertEqual(prior_tenure(
            self.c, GUILD, GROUP, PLAYER, current
        ), dt.timedelta(days=90))

    def test_empty_and_malformed_roster_cannot_close_stints(self):
        first = d("2025-01-01")
        record_roster(self.c, GUILD, GROUP, [membership(first)], d("2025-04-01"))
        record_roster(self.c, GUILD, GROUP, [], d("2025-04-02"))
        record_roster(self.c, GUILD, GROUP, [{"error": "WOM"}], d("2025-04-03"))
        row = self.c.execute(
            "SELECT closed_at FROM tenure_observed WHERE player_id = ?",
            (PLAYER,)
        ).fetchone()
        self.assertIsNone(row[0])

    def test_manual_previous_service_fills_missing_wom_history(self):
        joined = d("2026-10-01")
        set_manual_previous_tenure(self.c, GUILD, GROUP, PLAYER, 245)
        effective, credited = cumulative_effective_join(
            self.c, GUILD, GROUP, PLAYER, joined
        )
        self.assertEqual(credited, dt.timedelta(days=245))
        self.assertEqual(effective, joined - dt.timedelta(days=245))

    def test_manual_credit_and_wom_history_use_max_not_sum(self):
        store_group_activity(self.c, GUILD, GROUP, [
            activity("joined", d("2025-01-01")),
            activity("left", d("2025-04-01")),
        ])
        current = d("2026-10-01")
        set_manual_previous_tenure(self.c, GUILD, GROUP, PLAYER, 160)
        _, credited = cumulative_effective_join(
            self.c, GUILD, GROUP, PLAYER, current
        )
        self.assertEqual(credited, dt.timedelta(days=160))
        set_manual_previous_tenure(self.c, GUILD, GROUP, PLAYER, 20)
        _, credited = cumulative_effective_join(
            self.c, GUILD, GROUP, PLAYER, current
        )
        self.assertEqual(credited, dt.timedelta(days=90))

    def test_manual_credit_can_be_cleared_and_isolated_by_player(self):
        set_manual_previous_tenure(self.c, GUILD, GROUP, PLAYER, 365)
        self.assertEqual(
            get_manual_previous_tenure(self.c, GUILD, GROUP, PLAYER),
            dt.timedelta(days=365)
        )
        self.assertEqual(
            get_manual_previous_tenure(self.c, GUILD, GROUP, 98765),
            dt.timedelta()
        )
        set_manual_previous_tenure(self.c, GUILD, GROUP, PLAYER, 0)
        self.assertEqual(
            get_manual_previous_tenure(self.c, GUILD, GROUP, PLAYER),
            dt.timedelta()
        )

    def test_manual_credit_rejects_invalid_values(self):
        for value in (-1, 3651, 2.5, "500"):
            with self.assertRaises(ValueError):
                set_manual_previous_tenure(self.c, GUILD, GROUP, PLAYER, value)

    def test_role_changes_do_not_affect_tenure(self):
        self.assertEqual(store_group_activity(self.c, GUILD, GROUP, [
            activity("changed_role", d("2025-03-01"))
        ]), 0)
        self.assertEqual(prior_tenure(
            self.c, GUILD, GROUP, PLAYER, d("2026-01-01")
        ), dt.timedelta())


if __name__ == "__main__":
    unittest.main()
