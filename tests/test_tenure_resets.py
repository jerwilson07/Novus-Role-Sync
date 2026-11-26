import datetime as dt
import sqlite3
import unittest

from tenure_history import (
    NOVUS_GUILD_ID, REQUESTED_REJOIN_RESETS, RESET_POLICY_START,
    apply_tenure_resets, cumulative_effective_join, get_tenure_diagnostics,
    get_tenure_reset, initialize_tenure_tables, record_roster,
    set_manual_previous_tenure, store_group_activity,
)

G = NOVUS_GUILD_ID
GROUP = 25802
START = RESET_POLICY_START
OLD = START - dt.timedelta(days=53)
ZERO = dt.timedelta()


def member(pid, name, joined=OLD):
    return {"player": {"id": pid, "username": name.lower(), "displayName": name},
            "clientSyncJoinedAt": joined.isoformat() if joined else None}


class TenureResetTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.c = self.db.cursor()
        initialize_tenure_tables(self.c)
        self.addCleanup(self.db.close)

    def events(self, pid, *events, group=GROUP):
        return store_group_activity(self.c, G, GROUP, [
            {"groupId": group, "playerId": pid, "type": kind, "createdAt": date.isoformat()}
            for kind, date in events
        ])

    def reset(self, pid):
        return get_tenure_reset(self.c, G, GROUP, pid)

    def apply(self, *roster, now=START + dt.timedelta(days=4)):
        return apply_tenure_resets(self.c, G, GROUP, list(roster), now=now)

    def test_legacy_named_members_do_not_reset_without_verified_rejoin(self):
        roster = [member(pid, name, OLD if pid % 2 else None)
                  for pid, name in enumerate(REQUESTED_REJOIN_RESETS, 1)]
        for pid in range(1, 5):
            set_manual_previous_tenure(self.c, G, GROUP, pid, 150)
        self.assertEqual(self.apply(*roster), ([], []))
        for pid in range(1, 5):
            self.assertIsNone(self.reset(pid))
            self.assertEqual(cumulative_effective_join(self.c, G, GROUP, pid, OLD),
                             (OLD - dt.timedelta(days=150), dt.timedelta(days=150)))

    def test_pre_policy_leave_and_rejoin_keeps_credit_even_with_old_reset_row(self):
        left = START - dt.timedelta(days=2)
        rejoined = START - dt.timedelta(days=1)
        self.events(9, ("joined", OLD), ("left", left), ("joined", rejoined))
        self.c.execute("INSERT INTO tenure_resets VALUES (?, ?, ?, ?)",
                       (G, GROUP, 9, rejoined.astimezone(dt.timezone.utc).isoformat()))
        self.apply(member(9, "Grandfathered", rejoined))
        self.assertIsNone(self.reset(9))
        # Historical service is elapsed time, including the DST transition.
        credited = left.astimezone(dt.timezone.utc) - OLD.astimezone(dt.timezone.utc)
        effective = rejoined.astimezone(dt.timezone.utc) - credited
        self.assertEqual(cumulative_effective_join(self.c, G, GROUP, 9, rejoined),
                         (effective, credited))

    def test_new_join_without_departure_is_not_a_reset(self):
        self.apply(member(9, "New Member", START + dt.timedelta(days=1)))
        self.assertIsNone(self.reset(9))

    def test_confirmed_rejoin_discards_prior_service_and_stale_timestamp(self):
        self.events(9, ("joined", OLD), ("left", START))
        set_manual_previous_tenure(self.c, G, GROUP, 9, 140)
        rejoined = START + dt.timedelta(days=1)
        self.apply(member(9, "Rejoiner", rejoined))
        self.assertEqual(cumulative_effective_join(self.c, G, GROUP, 9, rejoined), (rejoined, ZERO))
        self.assertEqual(cumulative_effective_join(self.c, G, GROUP, 9, OLD), (rejoined, ZERO))
        self.assertIsNone(get_tenure_reset(self.c, G, GROUP + 1, 9))
        self.assertIsNone(get_tenure_reset(self.c, G, GROUP, 91))
        self.assertGreater(get_tenure_diagnostics(self.c, G, GROUP, 9, rejoined)["wom_previous_days"], 0)

    def test_name_change_and_roster_gap_and_wom_only_readd_do_not_reset(self):
        record_roster(self.c, G, GROUP, [member(9, "Initial")], now=START)
        record_roster(self.c, G, GROUP, [member(10, "Other")], now=START + dt.timedelta(hours=1))
        self.events(9, ("left", START), ("joined", START + dt.timedelta(hours=2)))
        roster = member(9, "Renamed")
        self.apply(roster)
        self.assertIsNone(self.reset(9))

    def test_pre_policy_or_other_group_departure_does_not_reset(self):
        self.events(9, ("left", START - dt.timedelta(seconds=1)))
        self.events(9, ("left", START), group=GROUP + 1)
        set_manual_previous_tenure(self.c, G, GROUP, 9, 42)
        rejoined = START + dt.timedelta(days=2)
        self.apply(member(9, "Grandfathered", rejoined))
        self.assertIsNone(self.reset(9))
        self.assertEqual(cumulative_effective_join(self.c, G, GROUP, 9, rejoined)[1].days, 42)

    def test_same_day_delayed_wom_events_with_older_observation(self):
        record_roster(self.c, G, GROUP, [member(9, "Same Day")], now=START)
        joined = START + dt.timedelta(hours=2)
        left = START + dt.timedelta(hours=3)
        self.events(9, ("left", left), ("joined", left + dt.timedelta(minutes=1)))
        self.apply(member(9, "Same Day", joined))
        self.assertEqual(self.reset(9), joined)


if __name__ == "__main__":
    unittest.main()
