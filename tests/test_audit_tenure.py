"""Compact personal tenure display used by /audit."""

import datetime
import unittest

from cogs.config_cog import format_personal_tenure

UTC = datetime.timezone.utc


def date(value):
    return datetime.datetime.fromisoformat(value + "T00:00:00+00:00")


class PersonalTenureAuditTests(unittest.TestCase):
    def test_returning_member_without_recovered_service_has_no_fake_eta(self):
        now = date("2026-10-31")
        line = format_personal_tenure(
            "duellist", date("2026-10-29"), now=now
        )
        self.assertIn("Duellist", line)
        self.assertIn("2.0 days recorded", line)
        self.assertIn("Inquisitor: older service not verified", line)
        self.assertNotIn("Nov ", line)

    def test_recovered_service_shows_next_eligibility(self):
        now = date("2026-10-31")
        # The current rejoin date is Oct 29; prior verified service is 84d.
        effective_join = date("2026-10-29") - datetime.timedelta(days=84)
        line = format_personal_tenure(
            "duellist", effective_join, now=now
        )
        self.assertIn("Inquisitor: 6 days (Nov 06)", line)
        self.assertIn("86.0 days recorded", line)

    def test_due_promotion_shows_eligible_now(self):
        now = date("2026-11-06")
        line = format_personal_tenure(
            "duellist", date("2026-08-06"), now=now
        )
        self.assertIn("Inquisitor: eligible now", line)

    def test_max_rank_has_no_next_rank_countdown(self):
        line = format_personal_tenure(
            "paladin", date("2025-01-01"), now=date("2026-10-30")
        )
        self.assertIn("Highest tenure rank", line)
        self.assertNotIn("older service", line)

    def test_missing_current_join_date_does_not_invent_tenure(self):
        line = format_personal_tenure("expert", None, now=date("2026-10-30"))
        self.assertEqual(
            line, "**Your tenure:** Expert | Join date unavailable"
        )

    def test_unlinked_member_does_not_add_tenure_noise(self):
        self.assertIsNone(
            format_personal_tenure(None, date("2026-10-01"))
        )

    def test_first_rung_counts_down_to_duellist(self):
        line = format_personal_tenure(
            "squire", date("2026-10-15"), now=date("2026-10-30")
        )
        self.assertIn("Duellist: 16 days (Nov 15)", line)


if __name__ == "__main__":
    unittest.main()
