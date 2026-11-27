import unittest
import datetime as dt
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from cogs.runeprofile_cog import candidate_tier, parse_tasks, MASTER, GRANDMASTER, tenure_gate, ingame_ca_promotion

class RuneProfileTests(unittest.TestCase):
    def test_only_missing_highest_rank_is_reported(self):
        self.assertEqual(candidate_tier("Master",set()),"Master")
        self.assertEqual(candidate_tier("Grandmaster",{MASTER}),"Grandmaster")
        self.assertEqual(candidate_tier("Grandmaster",set()),"Grandmaster")
        self.assertIsNone(candidate_tier("Master",{MASTER}))
        self.assertIsNone(candidate_tier("Master",{GRANDMASTER}))
        self.assertIsNone(candidate_tier("Grandmaster",{GRANDMASTER}))
        self.assertIsNone(candidate_tier("Elite",set()))

    def test_tenure_gate_calendar_months_and_unknown(self):
        now=dt.datetime(2026,11,26,12,tzinfo=ZoneInfo("America/New_York"))
        start=dt.datetime(2026,8,26,tzinfo=dt.timezone.utc)
        accounts=[{"effective_joined_at":start,"expected_rank":"inquisitor","wom_role":"inquisitor"}]
        sync=SimpleNamespace(get_linked_account_tenures=lambda *_:accounts)
        self.assertEqual(tenure_gate(sync,None,1,[],now),(True,dt.date(2026,11,26)))
        accounts[0]["effective_joined_at"]=start+dt.timedelta(days=1)
        self.assertEqual(tenure_gate(sync,None,1,[],now),(False,dt.date(2026,11,27)))
        accounts[0]["effective_joined_at"]=None
        self.assertEqual(tenure_gate(sync,None,1,[],now),(None,None))
        accounts[0].update(effective_joined_at=start,wom_role="gnome_child")
        self.assertEqual(tenure_gate(sync,None,1,[],now),(None,None))

    def test_unsynced_or_exempt_data_is_not_proof(self):
        self.assertIsNone(parse_tasks({"totalPoints":0,"tierReached":None,"data":[{"completed":False,"exempt":False}]}))
        self.assertIsNone(parse_tasks({"totalPoints":10,"tierReached":"Master","data":[{"completed":True,"exempt":True}]}))
        self.assertIsNone(parse_tasks({"totalPoints":10,"tierReached":"Master","data":[]}))
        self.assertEqual(parse_tasks({"totalPoints":1000,"tierReached":"Master","data":[{"completed":True,"exempt":False}]}),("Master",1000))

    def test_ca_promotions_use_award_date_and_highest_discord_role(self):
        import sqlite3
        today = dt.date(2026, 11, 27)
        with sqlite3.connect(":memory:") as conn:
            c = conn.cursor()
            c.execute("""CREATE TABLE runeprofile_ca_awards (
                wom_id INTEGER, discord_id INTEGER, tier TEXT, awarded_at TEXT)""")
            c.execute("INSERT INTO runeprofile_ca_awards VALUES (?,?,?,?)",
                      (55, 10, "Grandmaster", "2026-11-26T17:00:00+00:00"))
            member = SimpleNamespace(id=10, bot=False, roles=[
                SimpleNamespace(id=1, name="Member"),
                SimpleNamespace(id=MASTER, name="Master"),
                SimpleNamespace(id=GRANDMASTER, name="Grandmaster")])
            primary = dict(account_type="primary", player_id=55, rsn="Main",
                wom_role="master", expected_rank="knight",
                effective_joined_at=dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc))
            alt = dict(primary, account_type="alt", player_id=56, rsn="Alt", wom_role="squire")
            row = ingame_ca_promotion(c, member, [primary, alt], today)
            self.assertEqual((row["player_id"], row["current_rank"], row["target_rank"], row["overdue_days"]),
                             (55, "Master CA", "GrandMaster CA", 1))
            for rank in ("grandmaster", "quester", "explorer", "gamer", "owner", "gnome_child", "unknown"):
                self.assertIsNone(ingame_ca_promotion(c, member, [dict(primary,wom_role=rank),alt], today))
            member.roles = member.roles[:2]
            self.assertIsNone(ingame_ca_promotion(c, member, [primary,alt], today))
            primary["wom_role"] = "knight"
            row = ingame_ca_promotion(c, member, [primary,alt], today)
            self.assertEqual((row["target_rank"], row["overdue_days"]), ("Master CA",0))
            self.assertEqual(ingame_ca_promotion(c, member, [primary,alt], today+dt.timedelta(days=1))["overdue_days"], 1)
            member.roles = member.roles[:1]
            self.assertIsNone(ingame_ca_promotion(c, member, [primary,alt], today))

    def test_ca_promotion_requires_current_tenure_and_full_membership(self):
        import sqlite3
        today = dt.date(2026, 11, 27)
        member = SimpleNamespace(id=10, roles=[SimpleNamespace(id=1,name="Member"),
                                               SimpleNamespace(id=MASTER,name="Master")])
        account = dict(account_type="primary",player_id=55,rsn="Main",wom_role="squire",
                       expected_rank="squire",effective_joined_at=dt.datetime(2026,11,1,tzinfo=dt.timezone.utc))
        with sqlite3.connect(":memory:") as conn:
            self.assertIsNone(ingame_ca_promotion(conn.cursor(),member,[account],today))
            account["effective_joined_at"]=dt.datetime(2026,7,1,tzinfo=dt.timezone.utc)
            member.roles[0].name="Verified"
            self.assertIsNone(ingame_ca_promotion(conn.cursor(),member,[account],today))

    def test_combined_audit_replaces_primary_tenure_but_retains_alt(self):
        import sqlite3
        from cogs.tasks_cog import TasksCog, NOVUS_GUILD_ID
        member = SimpleNamespace(id=10, roles=[SimpleNamespace(id=1,name="Member"),
                                               SimpleNamespace(id=MASTER,name="Master")])
        joined = dt.datetime(2025,1,1,tzinfo=dt.timezone.utc)
        primary = dict(account_type="primary", player_id=55,rsn="Main",wom_role="squire",
                       expected_rank="duellist",joined_at=joined,effective_joined_at=joined)
        alt = dict(primary,account_type="alt",player_id=56,rsn="Alt")
        guild = SimpleNamespace(id=NOVUS_GUILD_ID,get_member=lambda _:member)
        cog = TasksCog.__new__(TasksCog)
        cog.get_linked_account_tenures=lambda *_:[primary,alt]
        with sqlite3.connect(":memory:") as conn:
            c=conn.cursor()
            c.execute("CREATE TABLE links(guild_id INTEGER,discord_id INTEGER)")
            c.execute("INSERT INTO links VALUES (?,?)",(NOVUS_GUILD_ID,10))
            audit=cog.get_promotion_audit_entries(c,guild,{"id":25802,"memberships":[]})
            self.assertEqual([(r["player_id"],r["target_rank"]) for r in audit["due_today"]],[(55,"Master CA")])
            self.assertEqual([(r["player_id"],r["target_rank"]) for r in audit["overdue"]],[(56,"duellist")])


if __name__=="__main__":
    unittest.main()
