import unittest
from cogs.runeprofile_cog import candidate_tier, parse_tasks, MASTER, GRANDMASTER

class RuneProfileTests(unittest.TestCase):
    def test_only_missing_highest_rank_is_reported(self):
        self.assertEqual(candidate_tier("Master",set()),"Master")
        self.assertEqual(candidate_tier("Grandmaster",{MASTER}),"Grandmaster")
        self.assertEqual(candidate_tier("Grandmaster",set()),"Grandmaster")
        self.assertIsNone(candidate_tier("Master",{MASTER}))
        self.assertIsNone(candidate_tier("Master",{GRANDMASTER}))
        self.assertIsNone(candidate_tier("Grandmaster",{GRANDMASTER}))
        self.assertIsNone(candidate_tier("Elite",set()))

    def test_unsynced_or_exempt_data_is_not_proof(self):
        self.assertIsNone(parse_tasks({"totalPoints":0,"tierReached":None,"data":[{"completed":False,"exempt":False}]}))
        self.assertIsNone(parse_tasks({"totalPoints":10,"tierReached":"Master","data":[{"completed":True,"exempt":True}]}))
        self.assertIsNone(parse_tasks({"totalPoints":10,"tierReached":"Master","data":[]}))
        self.assertEqual(parse_tasks({"totalPoints":1000,"tierReached":"Master","data":[{"completed":True,"exempt":False}]}),("Master",1000))

if __name__=="__main__":
    unittest.main()
