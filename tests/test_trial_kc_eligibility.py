import datetime as dt
import unittest
from zoneinfo import ZoneInfo

from cogs.trial_kc_eligibility_cog import kc_qualification, week_key, trial_deadline

class TrialKCEligibilityTests(unittest.TestCase):
    def test_standard_total_includes_supported_raid_modes_only(self):
        values={"chambers_of_xeric":50,"chambers_of_xeric_challenge_mode":20,
                "theatre_of_blood":40,"theatre_of_blood_hard_mode":10,
                "tombs_of_amascut_expert":50,"the_fractured_archive":30,
                "tombs_of_amascut":900,"phosanis_nightmare":500}
        self.assertEqual(kc_qualification(values),(200,"200+ total raid KC"))

    def test_alternative_needs_100_and_a_cape_or_quiver(self):
        self.assertEqual(kc_qualification({"theatre_of_blood":100,"tzkal_zuk":1}),
                         (100,"100+ raid KC + Inferno/Colosseum"))
        self.assertEqual(kc_qualification({"theatre_of_blood":100,"sol_heredit":1}),
                         (100,"100+ raid KC + Inferno/Colosseum"))
        self.assertEqual(kc_qualification({"theatre_of_blood":99,"tzkal_zuk":1}),(99,None))
        self.assertEqual(kc_qualification({"theatre_of_blood":100,"tzkal_zuk":None}),(100,None))

    def test_unknown_or_negative_values_never_create_false_qualification(self):
        self.assertEqual(kc_qualification({"chambers_of_xeric":-1,"theatre_of_blood":None,
                                           "the_fractured_archive":True}),(0,None))

    def test_trial_deadline_depends_on_pathway_and_verified_start(self):
        et=ZoneInfo("America/New_York")
        started=dt.datetime(2026,1,31,12,tzinfo=et)
        self.assertEqual(trial_deadline(started,"raidtrial"),dt.datetime(2026,2,14,12,tzinfo=et))
        for path in ("kc","rpp"):
            self.assertEqual(trial_deadline(started,path),dt.datetime(2026,2,28,12,tzinfo=et))
        self.assertIsNone(trial_deadline(started,None))
        self.assertIsNone(trial_deadline(None,"raidtrial"))

    def test_week_boundary_is_monday_five_pm_eastern(self):
        et=ZoneInfo("America/New_York")
        self.assertEqual(week_key(dt.datetime(2026,11,30,16,59,tzinfo=et)),"2026-11-23")
        self.assertEqual(week_key(dt.datetime(2026,11,30,17,0,tzinfo=et)),"2026-11-30")
        self.assertEqual(week_key(dt.datetime(2026,12,1,12,0,tzinfo=et)),"2026-11-30")

if __name__=="__main__":
    unittest.main()
