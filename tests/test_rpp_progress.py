import unittest
from rpp_progress import RPP_CA, ca_progress, rpp_progress

class RPPTests(unittest.TestCase):
    def test_exact_task_ids_and_identity(self):
        tasks=[{"index":i,"name":n,"monster":m,"completed":True,"exempt":False} for i,n,m in RPP_CA]
        result=rpp_progress({},{"data":tasks})
        self.assertEqual(result["ca_completed"],8)
        self.assertTrue(result["staff_approval_required"])
        self.assertTrue(result["manual_proof_required"])
        tasks[0]["monster"]="Wrong"
        self.assertEqual(ca_progress({"data":tasks})[0]["status"],"unverified")
        tasks[1]["exempt"]=True
        self.assertEqual(ca_progress({"data":tasks})[1]["status"],"exempt_needs_review")

    def test_final_raid_stage_requires_kc_only(self):
        result=rpp_progress({"chambers_of_xeric":5,"theatre_of_blood":5,"tombs_of_amascut_expert":10},None)
        for stage in result["kc"][-3:]:
            self.assertEqual(stage["status"],"baseline_met")
        self.assertFalse(any(item.startswith("Raids:") for item in result["manual_proof_required"]))
        self.assertTrue(result["staff_approval_required"])
        self.assertEqual(len(result["manual_proof_required"]),6)

    def test_missing_and_range_are_not_automatic_passes(self):
        result=rpp_progress({"the_corrupted_gauntlet":7},None)
        self.assertEqual(result["ca_completed"],0)
        self.assertTrue(all(t["status"]=="unverified" for t in result["ca"]))
        self.assertEqual(result["kc"][0]["status"],"unverified")
        self.assertEqual(result["kc"][3]["status"],"baseline_range_review")
        self.assertEqual(rpp_progress({"the_corrupted_gauntlet":10},None)["kc"][3]["status"],"baseline_met")

if __name__=="__main__":
    unittest.main()
