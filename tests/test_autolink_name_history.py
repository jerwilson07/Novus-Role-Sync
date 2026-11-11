"""Historical WOM rename matching must not guess or overwrite identities."""

import asyncio
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cogs.config_cog import (
    ConfigCog,
    approved_prior_rsns,
)


def member(user_id, nickname):
    return SimpleNamespace(
        id=user_id,
        bot=False,
        display_name=nickname,
        global_name=None,
        name=f"discord-{user_id}",
        roles=[SimpleNamespace(id=999, name="Member")],
    )


def wom(player_id, username):
    return {"player": {"id": player_id, "username": username}, "role": "squire"}


class Response:
    def __init__(self, status=200, names=None):
        self.status = status
        self.names = names or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self.names


class Session:
    def __init__(self, names):
        self.names = names
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return Response(names=self.names)


class HistoricalAutolinkTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.c = self.conn.cursor()
        self.c.execute(
            """CREATE TABLE links (
               guild_id INTEGER, discord_id INTEGER, rsn TEXT,
               wom_id INTEGER, PRIMARY KEY (guild_id, discord_id))"""
        )
        self.c.execute(
            """CREATE TABLE alt_links (
               guild_id INTEGER, discord_id INTEGER, rsn TEXT,
               wom_id INTEGER, PRIMARY KEY (guild_id, discord_id, rsn))"""
        )

    def tearDown(self):
        self.conn.close()

    @staticmethod
    def result():
        return {
            "linked_members": [],
            "repaired_members": [],
            "alt_links_added": [],
            "history_links": [],
            "history_checked": 0,
            "history_lookup_errors": 0,
        }

    def run_match(self, discord_members, members, name_history, *, primary=None,
                  unmatched=None, unclaimed=None):
        guild = SimpleNamespace(
            id=1,
            members=discord_members,
            get_member=lambda mid: next(
                (m for m in discord_members if m.id == mid), None
            ),
        )
        session = Session(name_history)
        cog = ConfigCog.__new__(ConfigCog)
        cog.bot = SimpleNamespace(http_session=session)
        primary = primary or {}
        linked_ids = {
            pid: did for did, (_, pid) in primary.items() if pid is not None
        }
        linked_names = {
            rsn.casefold(): did for did, (rsn, _) in primary.items()
        }
        unmatched = (discord_members.copy() if unmatched is None else unmatched)
        unclaimed = (
            [x["player"]["username"] for x in members]
            if unclaimed is None else unclaimed
        )
        result = self.result()
        wom_by_key = {
            x["player"]["username"].casefold(): [x] for x in members
        }

        async def no_delay(_):
            return None

        with patch("cogs.config_cog.asyncio.sleep", new=no_delay):
            asyncio.run(cog.match_renamed_wom_accounts(
                guild, self.c, members, wom_by_key, linked_ids,
                linked_names, primary, unmatched, unclaimed, result
            ))
        return result, unmatched, unclaimed, session.calls

    def test_approved_names_only_and_same_player_id(self):
        records = [
            {"playerId": 71, "oldName": "OldRSN", "status": "approved"},
            {"playerId": 71, "oldName": "Pending", "status": "pending"},
            {"playerId": 999, "oldName": "Imposter", "status": "approved"},
        ]
        self.assertEqual(
            approved_prior_rsns(71, records), {"oldrsn"}
        )

    def test_unambiguous_prior_rsn_links_the_current_account(self):
        old = member(22, "OldRSN")
        result, unmatched, unclaimed, calls = self.run_match(
            [old], [wom(71, "p p")],
            [{"playerId": 71, "oldName": "OldRSN", "status": "approved"}],
        )
        self.assertEqual(
            self.c.execute("SELECT rsn,wom_id FROM links").fetchall(),
            [("p p", 71)],
        )
        self.assertEqual(result["history_links"][0][1:], ("oldrsn", "p p", "primary"))
        self.assertEqual(unmatched, [])
        self.assertEqual(unclaimed, [])
        self.assertEqual(len(calls), 1)
        self.assertIn("p%20p/names", calls[0])
        # Nickname changes are intentionally separate and still require 24h.
        self.assertEqual(old.display_name, "OldRSN")

    def test_two_discord_accounts_with_same_old_name_are_not_linked(self):
        result, unmatched, unclaimed, _ = self.run_match(
            [member(22, "OldRSN"), member(23, "OldRSN")],
            [wom(71, "p p")],
            [{"playerId": 71, "oldName": "OldRSN", "status": "approved"}],
        )
        self.assertEqual(result["history_links"], [])
        self.assertEqual(len(unmatched), 2)
        self.assertEqual(unclaimed, ["p p"])
        self.assertEqual(
            self.c.execute("SELECT * FROM links").fetchall(), []
        )

    def test_name_reused_as_a_current_wom_rsn_is_ignored(self):
        result, _, unclaimed, _ = self.run_match(
            [member(22, "OldRSN")],
            [wom(71, "p p"), wom(88, "OldRSN")],
            [{"playerId": 71, "oldName": "OldRSN", "status": "approved"}],
            unclaimed=["p p"],
        )
        self.assertEqual(result["history_links"], [])
        self.assertEqual(unclaimed, ["p p"])

    def test_unapproved_rename_is_ignored(self):
        result, _, unclaimed, _ = self.run_match(
            [member(22, "OldRSN")],
            [wom(71, "p p")],
            [{"playerId": 71, "oldName": "OldRSN", "status": "pending"}],
        )
        self.assertEqual(result["history_links"], [])
        self.assertEqual(unclaimed, ["p p"])

    def test_approved_old_alt_name_links_as_alt_not_primary(self):
        account = member(22, "Vecs | OldAlt")
        self.c.execute(
            "INSERT INTO links VALUES (?,?,?,?)", (1, 22, "Vecs", 50)
        )
        result, _, unclaimed, _ = self.run_match(
            [account], [wom(50, "Vecs"), wom(71, "p p")],
            [{"playerId": 71, "oldName": "OldAlt", "status": "approved"}],
            primary={22: ("Vecs", 50)}, unmatched=[], unclaimed=["p p"]
        )
        self.assertEqual(
            self.c.execute("SELECT rsn,wom_id FROM alt_links").fetchall(),
            [("p p", 71)],
        )
        self.assertEqual(result["history_links"][0][3], "alt")
        self.assertEqual(result["linked_members"], [])
        self.assertEqual(unclaimed, [])


if __name__ == "__main__":
    unittest.main()
