import json
import unittest
from cogs.council_analytics_cog import monthly_series, observed_rate, render_dashboards

class CouncilTrendTests(unittest.TestCase):
    def test_calendar_gaps_and_lifetime_exclusion(self):
        rows=[("2026-11",json.dumps({"full":120,"approval_hours":None})),
              ("lifetime-baseline-2026",json.dumps({"full":999})),
              ("2027-01","{}")]
        result=monthly_series(rows,"2027-02",{"full":130})
        self.assertEqual([m for m,_ in result],["2026-11","2026-12","2027-01","2027-02"])
        self.assertEqual(result[1][1],{})
        self.assertEqual(result[-1][1]["full"],130)
        self.assertIsNone(result[0][1]["approval_hours"])

    def test_zero_is_not_missing(self):
        self.assertEqual(observed_rate(0,10),0)
        self.assertIsNone(observed_rate(0,0))
        self.assertEqual(observed_rate(9,10),90)

    def test_monthly_and_lifetime_dashboards(self):
        metrics={"full":124,"pvm_engagement_rate":90,"raid_kc":250,
                 "new_retention_30":75,"new_retention_60":None}
        d={"period":"Nov 2026","lifetime":False,"full":124,"trial":4,"wom":137,
           "linked":123,"tenures":[50,90],"wom_joins":2,"wom_leaves":1,"wom_complete":True,
           "retention":None,"opened":3,"approved":2,"complete":False,"unknown_apps":0,
           "active":4,"passed":1,"other":0,"unknown":0,"departed":0,"ranks":{},
           "raids":{"CoX":100,"ToB":150},"bosses":{},"covered":123,"engaged":110,
           "measured":123,"unmeasured":1,"owners":137,"first":{},"promotions":{},
           "prior":None,"metrics":metrics,"trend":[("2026-11",metrics),("2026-12",{}),("2027-01",metrics)]}
        for lifetime,count in [(False,6),(True,4)]:
            d["lifetime"]=lifetime
            images=render_dashboards(d)
            self.assertEqual(len(images),count)
            for image in images:
                self.assertEqual(image.read(8),b"\x89PNG\r\n\x1a\n")
                image.close()

if __name__=="__main__":
    unittest.main()
