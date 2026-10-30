"""Regression tests for Novus active membership: WOM AND Discord Member role."""

import unittest
from types import SimpleNamespace

from common import (
    GUEST_ROLE_ID,
    CLAN_FRIEND_ROLE_ID,
    find_primary_wom_membership,
    has_novus_member_role,
    is_active_novus_member_link,
)


def discord_user(*roles):
    return SimpleNamespace(roles=[
        SimpleNamespace(id=role_id, name=name)
        for role_id, name in roles
    ])


ROSTER = [
    {"player": {"id": 12345, "username": "Novus Main"}},
    {"player": {"id": 22222, "username": "Current Novus"}},
]
MEMBER = discord_user((100, "Verified"), (200, "Member"))
VERIFIED_ONLY = discord_user((100, "Verified"))
GUEST_MEMBER = discord_user((GUEST_ROLE_ID, "Guest"), (200, "Member"))
FRIEND_MEMBER = discord_user((CLAN_FRIEND_ROLE_ID, "Clan Friend"), (200, "Member"))


class ActiveMembershipTests(unittest.TestCase):
    def test_member_role_and_wom_are_both_required(self):
        self.assertTrue(is_active_novus_member_link(
            MEMBER, "Novus Main", 12345, ROSTER
        ))
        self.assertFalse(is_active_novus_member_link(
            VERIFIED_ONLY, "Novus Main", 12345, ROSTER
        ))
        self.assertFalse(is_active_novus_member_link(
            MEMBER, "Former Main", 54321, ROSTER
        ))

    def test_verified_former_member_is_not_current(self):
        self.assertFalse(is_active_novus_member_link(
            VERIFIED_ONLY, "Former Main", 54321, ROSTER
        ))
        self.assertFalse(has_novus_member_role(VERIFIED_ONLY))

    def test_guests_and_friends_never_qualify_even_with_member(self):
        self.assertFalse(has_novus_member_role(GUEST_MEMBER))
        self.assertFalse(has_novus_member_role(FRIEND_MEMBER))
        self.assertFalse(is_active_novus_member_link(
            GUEST_MEMBER, "Novus Main", 12345, ROSTER
        ))
        self.assertFalse(is_active_novus_member_link(
            FRIEND_MEMBER, "Novus Main", 12345, ROSTER
        ))

    def test_rename_keeps_membership_by_stable_wom_id(self):
        self.assertTrue(is_active_novus_member_link(
            MEMBER, "Old Name", 12345, ROSTER
        ))

    def test_case_insensitive_role_and_rsn(self):
        member = discord_user((200, "mEmBeR"))
        self.assertTrue(is_active_novus_member_link(
            member, "novus main", None, ROSTER
        ))

    def test_missing_or_invalid_roster_never_validates_membership(self):
        for missing in ([], None, {"error": "temporary"}, [None]):
            self.assertFalse(is_active_novus_member_link(
                MEMBER, "Novus Main", 12345, missing
            ))

    def test_role_name_must_be_exact(self):
        self.assertFalse(has_novus_member_role(
            discord_user((200, "Former Member"), (201, "Verified"))
        ))
        self.assertFalse(has_novus_member_role(
            discord_user((200, "Trial Member"))
        ))
        self.assertTrue(has_novus_member_role(
            discord_user((200, " MEMBER "))
        ))

    def test_wrong_wom_identity_does_not_match(self):
        self.assertIsNone(find_primary_wom_membership(
            "Former Main", 999, ROSTER
        ))
        self.assertFalse(is_active_novus_member_link(
            MEMBER, "Former Main", 999, ROSTER
        ))


if __name__ == "__main__":
    unittest.main()
