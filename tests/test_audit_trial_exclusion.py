"""Audit excludes WOM trial accounts from actionable unmatched list.

Trial Members do not have the Discord full Member role and are managed
separately by /trials. They must not appear as broken /audit links.
"""

import unittest
from cogs.config_cog import find_unmatched_nontrial_wom_accounts


def account(rsn, wom_role, player_id):
    return {
        "player": {"username": rsn, "id": player_id},
        "role": wom_role,
    }


class AuditTrialExclusionTests(unittest.TestCase):
    def test_three_unlinked_trial_members_are_not_unmatched(self):
        memberships = [
            account("dumbstruq", "gnome_child", 111),
            account("juicy lute", "gnome_child", 112),
            account("lnstruction", "gnome_child", 113),
        ]
        self.assertEqual(
            find_unmatched_nontrial_wom_accounts(memberships, set(), set()),
            [],
        )

    def test_unlinked_full_member_still_reported(self):
        memberships = [
            account("juicy lute", "gnome_child", 112),
            account("Real Member", "squire", 114),
        ]
        self.assertEqual(
            find_unmatched_nontrial_wom_accounts(memberships, set(), set()),
            ["Real Member"],
        )

    def test_linked_regular_members_not_reported(self):
        memberships = [
            account("One", "squire", 100),
            account("Two", "duellist", 200),
        ]
        self.assertEqual(
            find_unmatched_nontrial_wom_accounts(
                memberships, {100}, {"two"}
            ),
            [],
        )

    def test_staff_and_alt_accounts_still_reported_if_unmatched(self):
        memberships = [
            account("A staff account", "quester", 100),
            account("An alt account", "duellist", 200),
        ]
        self.assertEqual(
            find_unmatched_nontrial_wom_accounts(memberships, set(), set()),
            ["A staff account", "An alt account"],
        )

    def test_missing_or_malformed_entries_are_safely_skipped(self):
        memberships = [
            None, {}, {"player": None},
            account("TRIAL", " GNOME_CHILD ", 100),
            account("Person", "SQUIRE", 101),
        ]
        self.assertEqual(
            find_unmatched_nontrial_wom_accounts(memberships, set(), set()),
            ["Person"],
        )


if __name__ == "__main__":
    unittest.main()
