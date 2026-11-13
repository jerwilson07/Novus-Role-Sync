"""Role-safety regressions for Novus WOM sync."""

import datetime as dt
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cogs.tasks_cog import (
    TasksCog,
    MODERATOR_ROLE_ID,
    TRIAL_MOD_ROLE_ID,
    staff_role_removal_confirmed,
)

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 12, 18, 0, tzinfo=UTC)


def role(name, role_id):
    return SimpleNamespace(
        id=role_id, name=name, managed=False, is_default=lambda: False,
    )


class StaffRoleGraceTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.c = self.db.cursor()

    def tearDown(self):
        self.db.close()

    def check(self, role_id, wom_role, now=T0):
        return staff_role_removal_confirmed(
            self.c, 1, 2, role_id, wom_role, now=now,
        )

    def test_moderator_removal_waits_full_24_hours(self):
        self.assertFalse(self.check(MODERATOR_ROLE_ID, "squire"))
        self.assertFalse(self.check(
            MODERATOR_ROLE_ID, "squire", T0 + dt.timedelta(hours=23, minutes=59)
        ))
        self.assertTrue(self.check(
            MODERATOR_ROLE_ID, "squire", T0 + dt.timedelta(hours=24)
        ))

    def test_trial_mod_candidate_is_independent_and_restarts_on_rank_change(self):
        self.assertFalse(self.check(TRIAL_MOD_ROLE_ID, "squire", T0))
        self.assertFalse(self.check(
            TRIAL_MOD_ROLE_ID, "duellist", T0 + dt.timedelta(hours=23)
        ))
        self.assertFalse(self.check(
            TRIAL_MOD_ROLE_ID, "duellist", T0 + dt.timedelta(hours=24)
        ))
        self.assertTrue(self.check(
            TRIAL_MOD_ROLE_ID, "duellist", T0 + dt.timedelta(hours=47)
        ))

    def test_unverified_role_clears_pending_removal(self):
        self.assertFalse(self.check(MODERATOR_ROLE_ID, "squire", T0))
        self.assertFalse(self.check(
            MODERATOR_ROLE_ID, None, T0 + dt.timedelta(hours=23)
        ))
        self.assertFalse(self.check(
            MODERATOR_ROLE_ID, "squire", T0 + dt.timedelta(hours=24)
        ))


class TrialGraduationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.c = self.db.cursor()
        self.c.execute(
            "CREATE TABLE links (guild_id INTEGER, discord_id INTEGER, "
            "rsn TEXT, wom_id INTEGER, dm_notifications_on INTEGER)"
        )
        self.trial = role("Trial Member", 10)
        self.member_role = role("Member", 20)
        self.user = SimpleNamespace(
            id=2, mention="<@2>", roles=[self.trial],
            add_roles=AsyncMock(), remove_roles=AsyncMock(),
        )
        self.trial.members = [self.user]
        self.guild = SimpleNamespace(id=1, roles=[self.trial, self.member_role])
        self.cog = TasksCog.__new__(TasksCog)

    def tearDown(self):
        self.db.close()

    def link(self, rsn="Primary", wom_id=99):
        self.c.execute("INSERT INTO links VALUES (1, 2, ?, ?, 0)", (rsn, wom_id))

    async def run_cleanup(self, memberships):
        return await self.cog.cleanup_inactive_trial_roles(
            self.c, self.guild, {"memberships": memberships}
        )

    @staticmethod
    def wom(rsn="primary", rank="squire", player_id=99):
        return {
            "player": {"id": player_id, "username": rsn},
            "role": rank,
        }

    async def test_graduation_grants_member_before_removing_trial(self):
        self.link()
        order = []

        async def add(*args, **kwargs):
            order.append(("add", args[0].name))

        async def remove(*args, **kwargs):
            order.append(("remove", args[0].name))

        self.user.add_roles.side_effect = add
        self.user.remove_roles.side_effect = remove
        result = await self.run_cleanup([self.wom(rank="duellist")])
        self.assertEqual(order, [("add", "Member"), ("remove", "Trial Member")])
        self.assertEqual(len(result["cleaned"]), 1)

    async def test_no_graduation_on_missing_wom_member_or_wrong_id(self):
        self.link()
        await self.run_cleanup([self.wom(rank="squire", player_id=555)])
        self.user.add_roles.assert_not_awaited()
        self.user.remove_roles.assert_not_awaited()

    async def test_no_graduation_for_wom_trial_or_staff_rank(self):
        self.link()
        for rank in ("gnome_child", "gamer", "explorer"):
            with self.subTest(rank=rank):
                await self.run_cleanup([self.wom(rank=rank)])
                self.user.add_roles.assert_not_awaited()
                self.user.remove_roles.assert_not_awaited()

    async def test_primary_trial_not_promoted_because_alt_has_tenure(self):
        self.link()
        await self.run_cleanup([
            self.wom(rank="gnome_child"),
            self.wom(rsn="Alt", rank="squire", player_id=100),
        ])
        self.user.add_roles.assert_not_awaited()
        self.user.remove_roles.assert_not_awaited()

    async def test_failed_member_grant_keeps_trial_role(self):
        self.link()
        self.user.add_roles.side_effect = RuntimeError("Discord unavailable")
        await self.run_cleanup([self.wom(rank="squire")])
        self.user.remove_roles.assert_not_awaited()

    async def test_existing_member_only_removes_trial(self):
        self.link()
        self.user.roles.append(self.member_role)
        result = await self.run_cleanup([self.wom(rank="inquisitor")])
        self.user.add_roles.assert_not_awaited()
        self.user.remove_roles.assert_awaited_once()
        self.assertIs(
            self.user.remove_roles.await_args.args[0], self.trial
        )
        self.assertEqual(len(result["cleaned"]), 1)


class RoleEngineProtectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.c = self.db.cursor()
        self.c.executescript(
            """
            CREATE TABLE role_mappings (
                guild_id INTEGER, wom_role TEXT, discord_role_id INTEGER
            );
            CREATE TABLE links (
                guild_id INTEGER, discord_id INTEGER, rsn TEXT,
                wom_id INTEGER, dm_notifications_on INTEGER
            );
            CREATE TABLE alt_links (
                guild_id INTEGER, discord_id INTEGER, rsn TEXT, wom_id INTEGER
            );
            INSERT INTO links VALUES (1, 2, 'Primary', 99, 0);
            """
        )
        self.member_role = role("Member", 20)
        self.mod_role = role("Moderator", MODERATOR_ROLE_ID)
        self.trial_mod_role = role("Trial Moderator", TRIAL_MOD_ROLE_ID)
        self.squire_role = role("Squire", 30)
        all_roles = [
            self.member_role, self.mod_role,
            self.trial_mod_role, self.squire_role,
        ]
        self.guild = SimpleNamespace(
            id=1, get_role=lambda role_id: next(
                (r for r in all_roles if r.id == role_id), None
            )
        )
        self.user = SimpleNamespace(
            id=2, mention="<@2>", roles=[self.member_role, self.mod_role],
            nick="Primary", display_name="Primary", 
            add_roles=AsyncMock(), remove_roles=AsyncMock(), edit=AsyncMock(),
        )
        self.cog = TasksCog.__new__(TasksCog)
        self.cog.get_linked_account_tenures = lambda *args: [{
            "account_type": "primary", "wom_role": "squire",
            "expected_rank": None,
        }]

    def tearDown(self):
        self.db.close()

    async def test_inactive_cleanup_removes_tenure_but_not_member_or_staff(self):
        self.user.roles.append(self.squire_role)
        self.c.execute(
            "INSERT INTO role_mappings VALUES (1, 'squire', 30)"
        )
        result = await self.cog.remove_inactive_member_roles(
            self.c, self.guild, self.user,
        )
        self.assertEqual(result["roles_removed"], ["Squire"])
        self.assertEqual(
            self.user.remove_roles.await_args.args, (self.squire_role,)
        )

    async def test_staff_mod_removal_only_after_confirmed_grace(self):
        group = {
            "id": 25802,
            "memberships": [{
                "player": {
                    "id": 99, "username": "primary", "displayName": "Primary"
                },
                "role": "squire",
            }],
        }
        result = await self.cog.sync_one_member(
            self.guild, self.user, group, self.c
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["staff_roles_removed"], [])
        self.user.remove_roles.assert_not_awaited()

        # Simulate a second WOM verification more than 24 hours later.
        self.c.execute(
            "UPDATE staff_role_removal_candidates "
            "SET first_seen_at = '2026-11-10T00:00:00+00:00'"
        )
        result = await self.cog.sync_one_member(
            self.guild, self.user, group, self.c
        )
        self.assertIn(self.mod_role, result["staff_roles_removed"])
        self.assertEqual(
            self.user.remove_roles.await_args.args, (self.mod_role,)
        )
        self.assertNotIn(self.member_role, result["roles_removed"])

    async def test_matching_wom_staff_role_is_never_removed(self):
        group = {
            "id": 25802,
            "memberships": [{
                "player": {
                    "id": 99, "username": "primary", "displayName": "Primary"
                },
                "role": "explorer",
            }],
        }
        self.cog.get_linked_account_tenures = lambda *args: [{
            "account_type": "primary", "wom_role": "explorer",
            "expected_rank": None,
        }]
        result = await self.cog.sync_one_member(
            self.guild, self.user, group, self.c
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["staff_roles_removed"], [])
        self.user.remove_roles.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
