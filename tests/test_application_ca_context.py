import unittest
from cogs.application_cog import applicant_ca_context, application_research_baseline, report_embed, RAIDS, EXTRAS
from rpp_progress import RPP_CA, RPP_KC

class ApplicantCAContextTests(unittest.TestCase):
    def test_unavailable_not_treated_as_incomplete(self):
        ca,guide=applicant_ca_context({},{"status":"not_found"})
        self.assertIn("profile not found",ca)
        self.assertIn("RPP CAs:** Not verified",ca)
        self.assertIn("WOM data incomplete",guide)
    def test_near_kc_target_preferred_even_with_high_ca(self):
        values={k:0 for _,k in RAIDS+EXTRAS}
        values["tombs_of_amascut_expert"]=185
        evidence={"status":"verified","tier":"Master","points":1200,"completed_tasks":300,"tasks":{"data":[]}}
        ca,guide=applicant_ca_context(values,evidence)
        self.assertIn("15 raid KC remaining",guide)
        self.assertIn("Suggested: KC pathway",guide)
    def test_high_ca_is_advisory(self):
        values={k:0 for _,k in RAIDS+EXTRAS}
        evidence={"status":"verified","tier":"Master","points":1200,"completed_tasks":300,"tasks":{"data":[]}}
        ca,guide=applicant_ca_context(values,evidence)
        self.assertIn("Master CA",ca)
        self.assertIn("Consider Raid Trial",guide)
        embed=report_embed("test",{"values":values,"runeprofile":evidence})
        fields={f.name:f.value for f in embed.fields}
        self.assertIn("KC thresholds not met",fields["Application Status"])
        self.assertIn("no automatic approval",fields["Pathway Guidance"])
    def test_rpp_complete_requires_staff(self):
        values={k:0 for _,k in RAIDS+EXTRAS}
        values.update({k:hi for _,k,lo,hi in RPP_KC})
        tasks=[{"index":i,"name":n,"monster":m,"completed":True,"exempt":False} for i,n,m in RPP_CA]
        evidence={"status":"verified","tier":"Hard","points":500,"completed_tasks":150,"tasks":{"data":tasks}}
        ca,guide=applicant_ca_context(values,evidence)
        self.assertIn("8/8 complete",ca)
        self.assertIn("Suggested: RPP",guide)
        self.assertIn("staff approval",guide)
    def test_research_baseline_preserves_unknowns(self):
        row=application_research_baseline(None)
        self.assertIsNone(row["ca_points"])
        self.assertIsNone(row["metrics"]["raid_total_observed"])
        self.assertIsNone(row["metrics"]["rpp_ca_completed"])
        self.assertFalse(row["metrics"]["raid_total_complete"])
        row=application_research_baseline({"values":{"chambers_of_xeric":7},"runeprofile":{"status":"not_found"}})
        self.assertEqual(row["metrics"]["raid_total_observed"],7)
        self.assertIsNone(row["metrics"]["raid_kc"]["theatre_of_blood"])
        self.assertEqual(row["ca_status"],"not_found")

    def test_full_kc_report_unchanged(self):
        values={k:0 for _,k in RAIDS+EXTRAS}
        values["tombs_of_amascut_expert"]=200
        embed=report_embed("test",{"values":values})
        fields={f.name:f.value for f in embed.fields}
        self.assertNotIn("Pathway Guidance",fields)
        self.assertIn("KC requirement met",fields["Application Status"])

if __name__=="__main__":
    unittest.main()
