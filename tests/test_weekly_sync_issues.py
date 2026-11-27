import datetime as dt
import sqlite3
import unittest
from unittest.mock import patch

from cogs.tasks_cog import weekly_sync_issues, acknowledge_weekly_sync_issues, deduplicate_skipped_link_issues


class WeeklySyncIssueTests(unittest.TestCase):
    def test_new_changed_weekly_and_resolved(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE bot_stats(key TEXT PRIMARY KEY,value TEXT)")
        today = dt.date(2026,11,27)
        def state(lines, day=today, scope="staff"):
            return weekly_sync_issues(conn.cursor(), 1, scope, {"Skipped": lines}, day)
        def ack(s, visible, day=today):
            with patch("cogs.tasks_cog.sqlite3.connect",return_value=conn):
                acknowledge_weekly_sync_issues(s, visible, day)
        a,b = "- <@1>: issue A","- <@2>: issue B"
        first=state([a,b,a])
        self.assertEqual(first[1]["Skipped"],[a,b])
        ack(first,[a])
        self.assertEqual(state([a,b])[1]["Skipped"],[b])
        self.assertEqual(state([a],today+dt.timedelta(days=6))[1]["Skipped"],[])
        self.assertEqual(state([a],today+dt.timedelta(days=7))[1]["Skipped"],[a])
        self.assertEqual(state([a],scope="manual")[1]["Skipped"],[a])
        self.assertEqual(state([a+" changed"])[1]["Skipped"],[a+" changed"])
        ack(state([]),[])
        self.assertEqual(state([a])[1]["Skipped"],[a])

    def test_duplicate_membership_warning_removed_but_unrelated_skip_retained(self):
        duplicate="- <@1> (`Main`) - WOM membership not verified; Discord roles retained"
        distinct="- <@2> (`Alt`) - WOM membership not verified"
        other="- <@1> - WOM tenure rank is not mapped"
        self.assertEqual(deduplicate_skipped_link_issues([duplicate,distinct,other],
            ["- <@1>: no linked RSN is currently active in Novus"]),[distinct,other])
