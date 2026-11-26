"""Noon report includes only new or weekly Discord Member-without-tenure flags."""

import datetime
import json
import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from cogs.tasks_cog import (
    NOVUS_GUILD_ID,
    NOVUS_MOD_CHAT_CHANNEL_ID,
    NOVUS_TENURE_ROLES,
    TasksCog,
    member_ids_without_tenure,
    missing_tenure_report_due,
)


def role(id, name):
    return SimpleNamespace(id=id, name=name)


MEMBER = role(10, "Member")
SQUIRE = role(11, "Squire")
VERIFIED = role(12, "Verified")
ALIAS = role(13, "Custom tenure role")


def member(id, *roles, bot=False):
    return SimpleNamespace(id=id, roles=roles, bot=bot, mention=f"<@{id}>")


class MissingTenureReportTests(unittest.TestCase):
    def test_discord_member_role_without_any_tenure_role(self):
        roster = [
            member(1, MEMBER), member(2, VERIFIED), member(3, MEMBER, SQUIRE),
            member(4, MEMBER, ALIAS), member(5, MEMBER, bot=True),
            member(6, role(14, "Former Member")),
            *[member(100 + i, MEMBER, role(200 + i, rank.title()))
              for i, rank in enumerate(NOVUS_TENURE_ROLES)],
        ]
        self.assertEqual(member_ids_without_tenure(roster, {ALIAS.id}), {1})

    def test_successful_role_edits_override_stale_cache(self):
        roster = [member(1, MEMBER), member(2, MEMBER, SQUIRE), member(3, MEMBER)]
        edits = {1: ([SQUIRE], []), 2: ([], [SQUIRE]), 3: ([], [MEMBER])}
        self.assertEqual(member_ids_without_tenure(roster, role_changes=edits), {2})

    def test_each_member_has_their_own_seven_day_cooldown(self):
        today = datetime.date(2026, 11, 15)
        due, retained = missing_tenure_report_due(
            {1, 2, 3}, {"1": "2026-11-14", "2": "2026-11-08"}, today,
        )
        self.assertEqual(due, [2, 3])
        self.assertEqual(retained, {"1": "2026-11-14"})


class DailyReportIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "wom_multi.db")
        self.connect = sqlite3.connect
        with self.connect(self.path) as conn:
            conn.executescript("""
                CREATE TABLE bot_stats (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE links (guild_id INTEGER, discord_id INTEGER, rsn TEXT, wom_id INTEGER);
                CREATE TABLE role_mappings (guild_id INTEGER, wom_role TEXT, discord_role_id INTEGER);
                CREATE TABLE guild_configs (guild_id INTEGER, last_sync TEXT, last_change_timestamp TEXT);
            """)
            conn.execute("INSERT INTO links VALUES (?, 2, 'Second', 2002)", (NOVUS_GUILD_ID,))
            conn.execute("INSERT INTO links VALUES (?, 3, 'Third', 2003)", (NOVUS_GUILD_ID,))
        self.roster = [member(1, MEMBER), member(2, MEMBER), member(3, MEMBER)]
        self.guild = SimpleNamespace(
            id=NOVUS_GUILD_ID, name="Novus", members=self.roster,
            get_role=lambda _: None,
            get_member=lambda identity: next((m for m in self.roster if m.id == identity), None),
        )
        self.channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=100)))
        self.cog = TasksCog.__new__(TasksCog)
        self.cog.bot = SimpleNamespace(get_channel=lambda _: self.channel, get_cog=lambda _: None)
        self.cog._last_staff_report_message_ids = {}
        self.cog.get_wom_group = AsyncMock(return_value=({"memberships": [{}]}, 200))
        self.cog.prepare_tenure_history = AsyncMock()
        self.cog.cleanup_inactive_trial_roles = AsyncMock(return_value={})
        self.cog.sync_one_member = AsyncMock(side_effect=[
            {"status": "membership_unverified"},
            {"status": "success", "roles_added": [SQUIRE]},
        ] * 3)
        self.cog.get_promotion_audit_entries = lambda *_a, **_kw: {
            "due_today": [], "overdue": [], "missing_join_dates": [],
        }
        self.cog.get_link_audit_entries = lambda *_a: {
            "unlinked_full_wom_accounts": [],
            "linked_members_without_active_rsn": [
                "- <@2>: no linked RSN is currently active in Novus",
            ],
        }

    async def run_sync(self):
        with patch("cogs.tasks_cog.sqlite3.connect", side_effect=lambda _: self.connect(self.path)):
            await self.cog.sync_guild(self.guild, 42, NOVUS_MOD_CHAT_CHANNEL_ID, False, False)
        return self.channel.send.await_args.kwargs["embed"]

    def saved(self):
        with self.connect(self.path) as conn:
            row = conn.execute("SELECT value FROM bot_stats").fetchone()
        return json.loads(row[0]) if row else {}

    async def test_reports_unlinked_and_linked_once_and_persists_cooldown(self):
        report = await self.run_sync()
        fields = {f.name: f.value for f in report.fields}
        self.assertEqual(fields, {
            "Member Role Without Tenure (2)": "- <@1>\n- <@2> (no active linked RSN)",
        })
        self.assertEqual(set(self.saved()), {"1", "2"})
        next_report = await self.run_sync()
        self.assertEqual([f.name for f in next_report.fields], ["No Action Needed"])

    async def test_ca_candidates_append_to_existing_report_and_remain_actionable(self):
        rp = SimpleNamespace(promotion_report=lambda *_: (
            ["<@42> • **Grandmaster CA** • 2,100 pts"],
            "1 members with verified CA data; staff review required."
        ))
        self.cog.bot.get_cog=lambda name: rp if name=="RuneProfileCog" else None
        report=await self.run_sync()
        self.assertEqual(report.fields[0].name,"Member Role Without Tenure (2)")
        self.assertEqual(report.fields[-1].name,"Combat Achievement Promotions (1)")
        self.assertIn("Grandmaster CA",report.fields[-1].value)
        next_report=await self.run_sync()
        self.assertEqual([f.name for f in next_report.fields],["Combat Achievement Promotions (1)"])

    async def test_failed_discord_delivery_does_not_start_cooldown(self):
        response = SimpleNamespace(status=403, reason="Forbidden")
        self.channel.send.side_effect = discord.Forbidden(response, "Forbidden")
        await self.run_sync()
        self.assertEqual(self.saved(), {})
        self.channel.send.side_effect = None
        report = await self.run_sync()
        self.assertEqual(report.fields[0].name, "Member Role Without Tenure (2)")
        self.assertEqual(set(self.saved()), {"1", "2"})


if __name__ == "__main__":
    unittest.main()
