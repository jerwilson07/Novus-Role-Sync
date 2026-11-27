"""Manual /linkuser saves accounts and immediately sets the Discord nickname."""

import asyncio
import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from cogs.config_cog import ConfigCog, COUNCIL_ROLE_ID
from common import CLAN_FRIEND_ROLE_ID
from cogs.tasks_cog import nickname_change_confirmed


class ManualLinkNicknameTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "wom_multi.db")
        with sqlite3.connect(self.path) as c:
            c.executescript(
                """
                CREATE TABLE guild_configs (guild_id INTEGER, group_id INTEGER);
                INSERT INTO guild_configs VALUES (1, 123);
                CREATE TABLE links (
                    guild_id INTEGER, discord_id INTEGER,
                    rsn TEXT, wom_id INTEGER,
                    PRIMARY KEY(guild_id, discord_id)
                );
                CREATE TABLE alt_links (
                    guild_id INTEGER, discord_id INTEGER,
                    rsn TEXT, wom_id INTEGER,
                    PRIMARY KEY(guild_id, discord_id, rsn)
                );
                """
            )
        self.real_connect = sqlite3.connect
        self.member = SimpleNamespace(
            id=2, nick="old nick", mention="<@2>",
            roles=[SimpleNamespace(id=123, name="Member")],
            edit=AsyncMock(),
        )
        self.interaction = SimpleNamespace(
            guild_id=1,
            user=SimpleNamespace(roles=[
                SimpleNamespace(id=COUNCIL_ROLE_ID, name="Council")
            ]),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        self.cog = ConfigCog.__new__(ConfigCog)

    def tearDown(self):
        self.tmp.cleanup()

    async def link(self, rsn):
        with patch(
            "cogs.config_cog.sqlite3.connect",
            side_effect=lambda _name: self.real_connect(self.path),
        ):
            await ConfigCog.linkuser.callback(
                self.cog, self.interaction, self.member, rsn
            )

    def read(self, sql):
        with self.real_connect(self.path) as c:
            return c.execute(sql).fetchall()

    async def test_main_and_alt_link_sets_nickname_immediately(self):
        await self.link("Mavara | Ozilda")
        self.member.edit.assert_awaited_once()
        self.assertEqual(
            self.member.edit.await_args.kwargs["nick"],
            "Mavara | Ozilda",
        )
        self.assertEqual(
            self.read("SELECT rsn,wom_id FROM links"),
            [("Mavara", None)],
        )
        self.assertEqual(
            self.read("SELECT rsn,wom_id FROM alt_links"),
            [("Ozilda", None)],
        )
        self.assertIn(
            "Discord nickname:** `Mavara | Ozilda`",
            self.interaction.followup.send.await_args.args[0],
        )
        self.interaction.response.defer.assert_awaited_once()

    async def test_staff_can_link_clan_friends_as_non_clan(self):
        for role_id, name in ((CLAN_FRIEND_ROLE_ID, "Clan Friend"),):
            self.member.roles = [SimpleNamespace(id=role_id, name=name)]
            await self.link("Wise Old Nut | Alt")
            self.assertEqual(self.read("SELECT rsn FROM links"), [("Wise Old Nut",)])
            self.assertEqual(self.read("SELECT rsn FROM alt_links"), [("Alt",)])
            self.assertIn("non-clan link", self.interaction.followup.send.await_args.args[0])
            self.interaction.response.send_message.assert_not_awaited()

    async def test_slash_separator_and_primary_only(self):
        await self.link("Mavara / Ozilda")
        self.assertEqual(
            self.member.edit.await_args.kwargs["nick"], "Mavara / Ozilda"
        )
        self.member.edit.reset_mock()
        self.member.nick = "Mavara"
        await self.link("Mavara")
        self.member.edit.assert_not_awaited()
        self.assertEqual(self.read("SELECT rsn FROM alt_links"), [])

    async def test_staff_can_link_verified_only_without_granting_clan_roles(self):
        verified = SimpleNamespace(id=254, name="Verified")
        self.member.roles = [verified]
        self.member.add_roles = AsyncMock()
        self.member.remove_roles = AsyncMock()
        await self.link("Wise Old Nut")
        self.assertEqual(self.read("SELECT rsn FROM links"), [("Wise Old Nut",)])
        self.assertEqual(self.member.edit.await_args.kwargs["nick"], "Wise Old Nut")
        self.assertEqual(self.member.roles, [verified])
        self.member.add_roles.assert_not_awaited()
        self.member.remove_roles.assert_not_awaited()
        self.assertIn(
            "does not grant clan membership or a tenure role",
            self.interaction.followup.send.await_args.args[0],
        )

    async def test_verified_does_not_authorize_running_staff_link_command(self):
        self.interaction.user.roles = [SimpleNamespace(id=254, name="Verified")]
        await self.link("Wise Old Nut")
        self.assertEqual(self.read("SELECT rsn FROM links"), [])
        self.interaction.response.send_message.assert_awaited_once()
        self.member.edit.assert_not_awaited()

    async def test_old_candidate_is_cleared_after_manual_edit(self):
        with self.real_connect(self.path) as c:
            nickname_change_confirmed(
                c.cursor(), 1, 2, "old nick", "Old Pending",
            )
        self.assertEqual(
            len(self.read("SELECT * FROM nickname_change_candidates")), 1
        )
        await self.link("Mavara")
        self.assertEqual(
            self.read("SELECT * FROM nickname_change_candidates"), []
        )

    async def test_overlength_refuses_without_changing_links(self):
        with self.real_connect(self.path) as c:
            c.execute(
                "INSERT INTO links VALUES (?,?,?,?)", (1, 2, "OldName", 99)
            )
        await self.link("Mavara | 123456789012 | 123456789013")
        self.member.edit.assert_not_awaited()
        self.interaction.response.send_message.assert_awaited_once()
        self.assertIn(
            "32-character",
            self.interaction.response.send_message.await_args.args[0],
        )
        self.assertEqual(
            self.read("SELECT rsn,wom_id FROM links"), [("OldName", 99)]
        )

    async def test_discord_forbidden_keeps_links_and_explains_failure(self):
        response = SimpleNamespace(status=403, reason="Forbidden")
        self.member.edit.side_effect = discord.Forbidden(response, "Forbidden")
        await self.link("Mavara")
        self.assertEqual(self.read("SELECT rsn FROM links"), [("Mavara",)])
        self.assertIn(
            "Couldn't update",
            self.interaction.followup.send.await_args.args[0],
        )


if __name__ == "__main__":
    unittest.main()
