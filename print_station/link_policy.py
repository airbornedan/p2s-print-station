"""When the station asks the printer for reports, and when a quiet link means it has dropped.

During a job the station asks every few seconds and expects reports constantly; a quiet link means a
dead connection, so it reconnects (and alerts if that keeps failing). Once the job ends it only listens:
the printer announces its own changes, including a job started from its screen. Someone looking at the
station (web page, phone) can ask for one fresh report when the data is old."""

ACTIVE = {"PREPARE", "SLICING", "RUNNING", "PAUSE"}


class LinkPolicy:
    def __init__(self, poll_s=4, stale_s=30, demand_age_s=60, demand_gap_s=10, alert_s=180):
        self.poll_s, self.stale_s, self.alert_s = poll_s, stale_s, alert_s
        self.demand_age_s, self.demand_gap_s = demand_age_s, demand_gap_s
        self.state = None
        self.last_report = self.connected_at = self.last_poll = self.last_demand = 0.0
        self.want = self.alerted = False

    def connected(self, now):
        self.connected_at, self.last_poll = now, now

    def observe(self, report, now):
        self.last_report = now
        if "gcode_state" in report:
            self.state = report["gcode_state"]
        self.alerted = False

    def active(self):
        return self.state in ACTIVE

    def demand(self, now):
        """A viewer wants current data: ask once if what we have is old."""
        if now - self.last_report > self.demand_age_s and now - self.last_demand >= self.demand_gap_s:
            self.want, self.last_demand = True, now

    def poll_due(self, now):
        return (self.active() and now - self.last_poll >= self.poll_s) or self.want

    def polled(self, now):
        self.last_poll, self.want = now, False

    def stale(self, now):
        """A job is running but nothing has arrived since well after (re)connecting."""
        return self.active() and now - max(self.last_report, self.connected_at) > self.stale_s

    def lost(self, now):
        """True once when a job has gone without reports for alert_s."""
        if self.active() and not self.alerted and now - self.last_report > self.alert_s:
            self.alerted = True
            return True
        return False
