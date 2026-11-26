import unittest
import datetime as dt
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from cogs.runeprofile_cog import candidate_tier, parse_tasks, MASTER, GRANDMASTER, tenure_gate

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

if __name__=="__main__":
    unittest.main()
