import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from cogs.tasks_cog import (
    COUNCIL_ROLE_ID, MODERATOR_ROLE_ID, NOVUS_GUILD_ID, TRIAL_MOD_ROLE_ID, TasksCog,
)
from common import GUEST_ROLE_ID


def wom(wom_id, display_name):
    return {"player": {"id": wom_id, "username": display_name.lower(),
                       "displayName": display_name}}


def member(discord_id, nick, role_id, rank=2):
    return SimpleNamespace(
        id=discord_id, name=nick, nick=nick, bot=False,
        guild=SimpleNamespace(owner_id=99), top_role=rank,
        roles=[SimpleNamespace(id=role_id)],
        edit=AsyncMock(), send=AsyncMock(), add_roles=AsyncMock(),
        remove_roles=AsyncMock(),
    )


class SilentNicknamePassTests(unittest.IsolatedAsyncioTestCase):
    async def test_edits_only_the_nicknames_for_moderator_trial_mod_and_member(self):
        for role in (MODERATOR_ROLE_ID, TRIAL_MOD_ROLE_ID, 123):
            with self.subTest(role=role):
                target = member(7, "xaffluence / altname", role)
                outcome = await TasksCog._sync_nickname_only(
                    target, 3, "xaffluence", 1,
                    [wom(1, "Xaffluence"), wom(2, "AltName")], [("altname", 2)],
                )
                self.assertEqual(outcome, "updated")
                self.assertEqual(target.edit.await_args.kwargs, {
                    "nick": "Xaffluence / AltName",
                    "reason": "One-time Novus WOM display-name sync",
                })
                target.add_roles.assert_not_called()
                target.remove_roles.assert_not_called()
                target.send.assert_not_called()

    async def test_council_higher_owner_guests_and_bots_are_untouched(self):
        cases = [
            (member(7, "xaffluence", COUNCIL_ROLE_ID, 3), "council_or_higher"),
            (member(7, "xaffluence", 12, 4), "council_or_higher"),
            (member(99, "xaffluence", 12), "council_or_higher"),
            (member(7, "xaffluence", GUEST_ROLE_ID), "excluded"),
        ]
        bot = member(7, "xaffluence", 12)
        bot.bot = True
        cases.append((bot, "excluded"))
        for target, expected in cases:
            self.assertEqual(await TasksCog._sync_nickname_only(
                target, 3, "xaffluence", 1, [wom(1, "Xaffluence")], [],
            ), expected)
            target.edit.assert_not_called()

    async def test_no_guessing_primary_or_stripping_missing_linked_alt(self):
        target = member(7, "xaffluence | Missing", MODERATOR_ROLE_ID)
        roster = [wom(1, "Xaffluence"), wom(2, "Missing")]
        self.assertEqual(await TasksCog._sync_nickname_only(
            target, 3, "xaffluence", 9, roster, [],
        ), "unverified_primary")
        self.assertEqual(await TasksCog._sync_nickname_only(
            target, 3, "xaffluence", 1, roster, [("Missing", 9)],
        ), "missing_alt_deferred")
        target.edit.assert_not_called()

    async def test_discord_forbidden_is_counted(self):
        target = member(7, "xaffluence", MODERATOR_ROLE_ID)
        target.edit.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions",
        )
        with self.assertLogs("WOMBot", level="WARNING"):
            result = await TasksCog._sync_nickname_only(
                target, 3, "xaffluence", 1, [wom(1, "Xaffluence")], [],
            )
        self.assertEqual(result, "discord_denied")

    async def test_startup_pass_is_silent_and_runs_once(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.executescript(f"""
            CREATE TABLE guild_configs (guild_id INTEGER, group_id INTEGER);
            CREATE TABLE links (guild_id INTEGER, discord_id INTEGER, rsn TEXT, wom_id INTEGER);
            CREATE TABLE alt_links (guild_id INTEGER, discord_id INTEGER, rsn TEXT, wom_id INTEGER);
            CREATE TABLE bot_stats (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO guild_configs VALUES ({NOVUS_GUILD_ID}, 25802);
            INSERT INTO links VALUES ({NOVUS_GUILD_ID}, 7, 'xaffluence', 1);
        """)
        target = member(7, "xaffluence", MODERATOR_ROLE_ID)
        guild = SimpleNamespace(id=NOVUS_GUILD_ID, chunked=True, get_role=Mock(return_value=3),
                                get_member=Mock(return_value=target), members=[target])
        bot = SimpleNamespace(get_guild=Mock(return_value=guild), wait_until_ready=AsyncMock())
        cog = object.__new__(TasksCog)
        cog.bot = bot
        cog.get_wom_group = AsyncMock(return_value=({"memberships": [wom(1, "Xaffluence")]}, 200))
        cog.sync_guild = AsyncMock()
        proxy = Mock(wraps=db)
        proxy.close = Mock()
        with patch("cogs.tasks_cog.sqlite3.connect", return_value=proxy):
            await cog._run_one_time_case_sync()
            await cog._run_one_time_case_sync()
        target.edit.assert_awaited_once()
        cog.get_wom_group.assert_awaited_once_with(25802)
        cog.sync_guild.assert_not_called()
        target.send.assert_not_called()
