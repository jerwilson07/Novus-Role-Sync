"""Regression tests for cumulative Novus clan tenure across re-joins."""

import datetime as dt
import asyncio
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
    get_tenure_diagnostics,
    refresh_activity_history,
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


def activity(kind, when, player_id=PLAYER, group_id=GROUP):
    return {
        "groupId": group_id,
        "playerId": player_id,
        "type": kind,
        "createdAt": when.isoformat(),
    }


class FakeActivityResponse:
    status = 200

    def __init__(self, events):
        self.events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return False

    async def json(self):
        return self.events


class FakeActivitySession:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, headers, params, timeout):
        page = params["offset"]
        self.calls.append(page)
        return FakeActivityResponse(self.pages.get(page, []))


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

    def test_ignores_membership_activity_from_other_clans(self):
        current = d("2026-10-29")
        events = [
            # A join to a different WOM group must never anchor Novus time.
            activity("joined", d("2026-07-31"), group_id=9999),
            activity("changed_role", d("2026-08-01"), group_id=9999),
            activity("left", d("2026-10-23")),
            activity("joined", current),
        ]
        inserted = store_group_activity(self.c, GUILD, GROUP, events)
        self.assertEqual(inserted, 2)
        self.assertEqual(
            prior_tenure(self.c, GUILD, GROUP, PLAYER, current),
            dt.timedelta(),
        )

    def test_novus_only_joins_leaves_and_role_changes_credit_exact_stints(self):
        current = d("2026-10-29")
        events = [
            activity("changed_role", d("2026-07-31")),
            activity("changed_role", d("2026-08-10")),
            activity("left", d("2026-10-24")),
            # Outside Novus: these events belong to a different group.
            activity("joined", d("2026-10-25"), group_id=9999),
            activity("changed_role", d("2026-10-27"), group_id=9999),
            activity("left", d("2026-10-28"), group_id=9999),
            activity("joined", current),
        ]
        store_group_activity(self.c, GUILD, GROUP, events)
        self.assertEqual(
            prior_tenure(self.c, GUILD, GROUP, PLAYER, current),
            dt.timedelta(days=85),
        )

    def test_ignores_missing_group_ids_and_unknown_activity_types(self):
        current = d("2026-10-29")
        legitimate = activity("left", d("2026-10-24"))
        invalid = [
            {k: v for k, v in activity(
                "joined", d("2026-07-31")
            ).items() if k != "groupId"},
            activity("joined", d("2026-07-31"), group_id=999),
            activity("updated", d("2026-07-31")),
            legitimate,
            activity("joined", current),
        ]
        self.assertEqual(
            store_group_activity(self.c, GUILD, GROUP, invalid), 2
        )
        self.assertEqual(
            prior_tenure(self.c, GUILD, GROUP, PLAYER, current),
            dt.timedelta(),
        )

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

    def test_diagnostic_shows_missing_historic_pair_not_fake_service(self):
        current = d("2026-10-29")
        store_group_activity(self.c, GUILD, GROUP, [
            activity("left", d("2026-10-24")),
            activity("joined", current),
        ])
        details = get_tenure_diagnostics(
            self.c, GUILD, GROUP, PLAYER, current
        )
        self.assertEqual(details["wom_previous_days"], 0)
        self.assertEqual(details["matched_previous_periods"], 0)
        self.assertEqual(len(details["events"]), 2)
        self.assertFalse(details["backfill_complete"])

    def test_diagnostic_shows_recovered_previous_stint(self):
        current = d("2026-10-29")
        store_group_activity(self.c, GUILD, GROUP, [
            activity("joined", d("2026-07-31")),
            activity("left", d("2026-10-24")),
            activity("joined", current),
        ])
        details = get_tenure_diagnostics(
            self.c, GUILD, GROUP, PLAYER, current
        )
        self.assertEqual(details["matched_previous_periods"], 1)
        self.assertEqual(details["wom_previous_days"], 85)
        self.assertEqual(len(details["events"]), 3)

    def test_history_import_limits_requests_and_resumes_older_pages(self):
        # Group activity is paginated newest-first, with the first event
        # on page 0 and an earlier join appearing hundreds of entries later.
        filler = [
            activity("changed_role", d("2026-05-01"), i + 1000)
            for i in range(50)
        ]
        pages = {
            0: [activity("left", d("2026-10-23"))] + filler[:49],
            50: filler,
            100: filler,
            150: filler,
            200: [activity("joined", d("2026-07-31"))] + filler[:49],
            250: filler[:5],
        }
        session = FakeActivitySession(pages)

        asyncio.run(refresh_activity_history(
            session, {}, self.c, GUILD, GROUP
        ))
        self.assertEqual(session.calls, [0, 50, 100, 150])
        self.assertEqual(prior_tenure(
            self.c, GUILD, GROUP, PLAYER, d("2026-10-29")
        ), dt.timedelta())
        offset = self.c.execute(
            "SELECT offset FROM tenure_backfill WHERE guild_id=? AND group_id=?",
            (GUILD, GROUP)
        ).fetchone()[0]
        self.assertEqual(offset, 200)

        # A normal sync within an hour uses the cached result.
        asyncio.run(refresh_activity_history(
            session, {}, self.c, GUILD, GROUP
        ))
        self.assertEqual(len(session.calls), 4)

        # Simulate the next scheduled refresh and resume with overlap.
        self.c.execute(
            "UPDATE tenure_backfill SET last_checked_at=? WHERE group_id=?",
            (d("2025-01-01").isoformat(), GROUP)
        )
        asyncio.run(refresh_activity_history(
            session, {}, self.c, GUILD, GROUP
        ))
        self.assertEqual(session.calls[4:], [0, 150, 200, 250])
        self.assertEqual(prior_tenure(
            self.c, GUILD, GROUP, PLAYER, d("2026-10-29")
        ), dt.timedelta(days=84))

        row = self.c.execute(
            "SELECT complete FROM tenure_backfill WHERE group_id=?", (GROUP,)
        ).fetchone()
        self.assertEqual(row[0], 1)

    def test_complete_backfill_checks_only_recent_pages(self):
        filler = [activity("changed_role", d("2026-09-01"), i+500) for i in range(50)]
        session = FakeActivitySession({0:filler,50:[]})
        self.c.execute(
            """INSERT INTO tenure_backfill
               (guild_id,group_id,offset,complete,last_checked_at)
               VALUES (?,?,?,?,?)""",
            (GUILD,GROUP,400,1,None)
        )
        asyncio.run(refresh_activity_history(
            session, {}, self.c, GUILD, GROUP
        ))
        self.assertEqual(session.calls,[0,50])

    def test_lone_rank_change_does_not_imply_completed_tenure(self):
        self.assertEqual(store_group_activity(self.c, GUILD, GROUP, [
            activity("changed_role", d("2025-03-01"))
        ]), 1)
        self.assertEqual(prior_tenure(
            self.c, GUILD, GROUP, PLAYER, d("2026-01-01")
        ), dt.timedelta())

    def test_founder_without_join_event_gets_lower_bound_of_prior_tenure(self):
        # Initial WOM group creation may not produce a 'joined' event.
        # A dated rank change proves this player belonged at that point.
        events = [
            activity("changed_role", d("2026-08-01")),
            activity("changed_role", d("2026-09-05")),
            activity("left", d("2026-10-24")),
            activity("joined", d("2026-10-29")),
        ]
        store_group_activity(self.c, GUILD, GROUP, events)
        previous = prior_tenure(
            self.c, GUILD, GROUP, PLAYER, d("2026-10-29")
        )
        self.assertEqual(previous, dt.timedelta(days=84))
        diagnostics = get_tenure_diagnostics(
            self.c, GUILD, GROUP, PLAYER, d("2026-10-29")
        )
        self.assertEqual(diagnostics["matched_previous_periods"], 1)

    def test_rank_change_does_not_bridge_two_membership_periods(self):
        events = [
            activity("changed_role", d("2026-07-31")),
            activity("left", d("2026-08-15")),
            activity("changed_role", d("2026-09-01")),
            activity("left", d("2026-09-10")),
            activity("joined", d("2026-10-29")),
        ]
        store_group_activity(self.c, GUILD, GROUP, events)
        self.assertEqual(prior_tenure(
            self.c, GUILD, GROUP, PLAYER, d("2026-10-29")
        ), dt.timedelta(days=24))

    def test_explicit_join_overrides_rank_activity_anchor(self):
        events = [
            activity("changed_role", d("2026-07-31")),
            activity("joined", d("2026-08-05")),
            activity("left", d("2026-09-01")),
        ]
        store_group_activity(self.c, GUILD, GROUP, events)
        self.assertEqual(prior_tenure(
            self.c, GUILD, GROUP, PLAYER, d("2026-10-29")
        ), dt.timedelta(days=27))

    def test_existing_completed_import_is_reset_once_for_new_evidence(self):
        # Simulate a database created before changed_role activity was stored.
        self.c.execute("DROP TABLE tenure_backfill")
        self.c.execute("""
            CREATE TABLE tenure_backfill (
                guild_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                offset INTEGER NOT NULL DEFAULT 0,
                complete INTEGER NOT NULL DEFAULT 0,
                last_checked_at TEXT,
                PRIMARY KEY (guild_id, group_id)
            )
        """)
        self.c.execute("""
            INSERT INTO tenure_backfill
            (guild_id, group_id, offset, complete, last_checked_at)
            VALUES (?, ?, ?, ?, ?)
        """, (GUILD, GROUP, 1500, 1, d("2026-10-30").isoformat()))
        initialize_tenure_tables(self.c)

        session = FakeActivitySession({
            0: [activity("left", d("2026-10-24"))],
        })
        asyncio.run(refresh_activity_history(
            session, {}, self.c, GUILD, GROUP
        ))
        self.assertEqual(session.calls, [0])
        version, complete, offset = self.c.execute(
            """SELECT activity_version, complete, offset
               FROM tenure_backfill WHERE guild_id=? AND group_id=?""",
            (GUILD, GROUP)
        ).fetchone()
        self.assertEqual((version, complete, offset), (2, 1, 1))

        # A repeat should respect the freshness cache, not rescan endlessly.
        asyncio.run(refresh_activity_history(
            session, {}, self.c, GUILD, GROUP
        ))
        self.assertEqual(session.calls, [0])


if __name__ == "__main__":
    unittest.main()
