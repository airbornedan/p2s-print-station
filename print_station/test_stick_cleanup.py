import threading, unittest
from stick_cleanup import StickCleanup, clean, old_prints

DAY = 86400
NOW = 1_800_000_000


class FakeFTP:
    """Stick with top-level files {name: age_days} and /timelapse holding clips plus a thumbnail folder."""

    def __init__(self, files, clips=("a.mp4",), thumbs=("a.jpg", "a_mini.jpg")):
        self.files = dict(files)
        self.tree = {"/timelapse": {"thumbnail": None, **{c: 1 for c in clips}},
                     "/timelapse/thumbnail": {t: 1 for t in thumbs}}
        self.deleted = []

    def nlst(self, path):
        return ["/" + n for n in self.files] + ["/timelapse"]

    def sendcmd(self, cmd):
        from datetime import datetime, timezone
        age = self.files[cmd.split("/", 1)[1]]
        return "213 " + datetime.fromtimestamp(NOW - age * DAY, timezone.utc).strftime("%Y%m%d%H%M%S")

    def retrlines(self, cmd, cb):
        for name, v in self.tree[cmd[5:]].items():
            cb(("drwxr-xr-x 2 103 107 8192 Nov 12 17:48 " if v is None else
                "-rwxr-xr-x 1 103 107 100 Nov 12 17:48 ") + name)

    def delete(self, path):
        self.deleted.append(path)
        d, n = path.rsplit("/", 1)
        if d:
            del self.tree[d][n]
        else:
            del self.files[n]

    def quit(self):
        pass


class Test(unittest.TestCase):
    def test_old_prints(self):
        m = {"a.gcode.3mf": NOW - 6 * DAY, "b.gcode.3mf": NOW - 4 * DAY, "c.stl": NOW - 9 * DAY,
             "run.gcode.3mf": NOW - 9 * DAY}
        self.assertEqual(old_prints(m, {"run.gcode.3mf"}, NOW), ["a.gcode.3mf"])

    def test_clean(self):
        f = FakeFTP({"old.gcode.3mf": 6, "new.gcode.3mf": 1, "run.gcode.3mf": 30})
        gone, clips = clean(lambda: f, {"run.gcode.3mf"}, now=NOW, log=lambda m: None)
        self.assertEqual(gone, ["old.gcode.3mf"])
        self.assertEqual(clips, 3)
        self.assertEqual(set(f.files), {"new.gcode.3mf", "run.gcode.3mf"})
        self.assertEqual(f.tree["/timelapse"], {"thumbnail": None})   # folders kept, files gone
        self.assertEqual(f.tree["/timelapse/thumbnail"], {})

    def test_timelapse_setting_off(self):
        f = FakeFTP({"old.gcode.3mf": 6})
        gone, clips = clean(lambda: f, set(), now=NOW, log=lambda m: None, timelapse=False)
        self.assertEqual((gone, clips), (["old.gcode.3mf"], 0))
        self.assertEqual(len(f.tree["/timelapse/thumbnail"]), 2)

    def test_runs_at_start_and_each_new_job(self):
        runs = []
        s = StickCleanup(None, log=lambda m: None)
        s.run = lambda keep: runs.append(keep)
        threading.Thread = _Inline
        try:
            s.observe({"gcode_state": "IDLE"})                                         # station start
            s.observe({"gcode_state": "IDLE"})
            s.observe({"gcode_state": "PREPARE", "task_id": "1", "subtask_name": "leg"})  # new job
            s.observe({"gcode_state": "RUNNING", "task_id": "1", "subtask_name": "leg"})
            s.observe({"layer_num": 4})                                                # partial report
            s.observe({"gcode_state": "FINISH", "task_id": "1", "subtask_name": "leg"})
            s.observe({"gcode_state": "RUNNING", "task_id": "2", "subtask_name": "lid"})  # next job
        finally:
            threading.Thread = _RealThread
        self.assertEqual(runs, [set(), {"leg.gcode.3mf"}, {"lid.gcode.3mf"}])


_RealThread = threading.Thread


class _Inline:
    def __init__(self, target, args=(), daemon=None):
        self.t, self.a = target, args

    def start(self):
        self.t(*self.a)


if __name__ == "__main__":
    unittest.main()
