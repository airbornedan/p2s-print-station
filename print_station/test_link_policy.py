import unittest
from link_policy import LinkPolicy


class LinkPolicyTests(unittest.TestCase):
    def setUp(self):
        self.p = LinkPolicy(poll_s=4, stale_s=30, demand_age_s=60, demand_gap_s=10, alert_s=180)
        self.p.connected(0)

    def test_polls_only_during_a_job(self):
        self.p.observe({"gcode_state": "RUNNING"}, 1)
        self.assertTrue(self.p.poll_due(5))
        self.p.polled(5)
        self.assertFalse(self.p.poll_due(7))
        self.p.observe({"gcode_state": "FINISH"}, 8)
        self.assertFalse(self.p.poll_due(100))
        self.p.observe({"gcode_state": "FAILED"}, 101)
        self.assertFalse(self.p.poll_due(200))

    def test_screen_started_job_resumes_polling(self):
        self.p.observe({"gcode_state": "IDLE"}, 1)
        self.assertFalse(self.p.poll_due(50))
        self.p.observe({"gcode_state": "PREPARE"}, 60)   # the printer announces it unasked
        self.assertTrue(self.p.poll_due(65))

    def test_viewer_demand_is_one_report_when_old(self):
        self.p.observe({"gcode_state": "IDLE"}, 1)
        self.p.demand(30)                                 # data 29 s old: fresh enough
        self.assertFalse(self.p.poll_due(30))
        self.p.demand(100)
        self.assertTrue(self.p.poll_due(100))
        self.p.polled(100)
        self.assertFalse(self.p.poll_due(101))
        self.p.demand(105)                                # still old, but asked 5 s ago
        self.assertFalse(self.p.poll_due(105))
        self.p.demand(111)
        self.assertTrue(self.p.poll_due(111))

    def test_quiet_link_during_a_job_is_stale(self):
        self.p.observe({"gcode_state": "RUNNING"}, 10)
        self.assertFalse(self.p.stale(39))
        self.assertTrue(self.p.stale(41))
        self.p.connected(41)                              # reconnect gets its own grace period
        self.assertFalse(self.p.stale(60))
        self.assertTrue(self.p.stale(72))
        self.p.observe({"mc_percent": 50}, 72)            # any report counts
        self.assertFalse(self.p.stale(80))

    def test_quiet_when_idle_is_not_stale(self):
        self.p.observe({"gcode_state": "FINISH"}, 10)
        self.assertFalse(self.p.stale(100000))

    def test_lost_alert_once_per_outage(self):
        self.p.observe({"gcode_state": "RUNNING"}, 10)
        self.assertFalse(self.p.lost(100))
        self.assertTrue(self.p.lost(200))
        self.assertFalse(self.p.lost(300))
        self.p.observe({"mc_percent": 60}, 310)
        self.assertTrue(self.p.lost(500))


if __name__ == "__main__":
    unittest.main()
