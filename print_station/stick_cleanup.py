"""Keep the P2S USB stick from filling. At each new job, and once when the station starts: empty /timelapse
(setting delete_timelapse, default on) and delete print files older than max_age_days (default 5), never the running
job's file. Sliced files can be remade from their STLs, so nothing is copied first."""
import sys, threading, time
from datetime import datetime, timezone

MAX_AGE_DAYS = 5
PRINT_EXT = ".gcode.3mf"
ACTIVE = {"PREPARE", "RUNNING", "PAUSE"}


def old_prints(mtimes, keep, now, max_age_days=MAX_AGE_DAYS):
    """Names of top-level print files older than max_age_days; mtimes maps name -> epoch seconds."""
    cut = now - max_age_days * 86400
    return sorted(n for n, t in mtimes.items() if n.endswith(PRINT_EXT) and n not in keep and t < cut)


def _mtime(f, path):
    return datetime.strptime(f.sendcmd("MDTM " + path)[4:].strip()[:14], "%Y%m%d%H%M%S").replace(
        tzinfo=timezone.utc).timestamp()


def _empty_dir(f, path):
    """Delete every file under path, keeping the folders themselves; returns the count."""
    lines = []
    f.retrlines("LIST " + path, lines.append)
    n = 0
    for line in lines:
        name = line.split(None, 8)[-1]
        if name in (".", ".."):
            continue
        child = path.rstrip("/") + "/" + name
        if line.startswith("d"):
            n += _empty_dir(f, child)
        else:
            f.delete(child)
            n += 1
    return n


def clean(connect, keep, now=None, log=print, timelapse=True, max_age_days=MAX_AGE_DAYS):
    """One cleanup pass over FTPS; keep = names that must stay (the running job's file)."""
    f = connect()
    try:
        names = [n.lstrip("/") for n in f.nlst("/")]
        prints = {n: _mtime(f, "/" + n) for n in names if n.endswith(PRINT_EXT)}
        gone = old_prints(prints, keep, now or time.time(), max_age_days)
        for n in gone:
            f.delete("/" + n)
        clips = _empty_dir(f, "/timelapse") if timelapse and "timelapse" in names else 0
    finally:
        try:
            f.quit()
        except Exception:
            f.close()
    if gone or clips:
        log("stick cleanup: deleted %d old print file(s) %s and %d timelapse file(s)" % (len(gone), gone, clips))
    return gone, clips


class StickCleanup:
    """Runs clean() once at start and again whenever a new job begins."""

    def __init__(self, connect, lock=None, log=None, options=None):
        """options() -> {"timelapse": bool, "max_age_days": int}, read at each run so settings apply live."""
        self._connect, self._lock = connect, lock or threading.Lock()
        self._options = options or (lambda: {})
        self._log = log or (lambda m: print(m, file=sys.stderr, flush=True))
        self._job, self._keep, self._started = None, set(), False

    def observe(self, p):
        if "gcode_state" not in p:
            return
        state, name = p.get("gcode_state"), p.get("subtask_name")
        job = (p.get("task_id"), name) if state in ACTIVE and name else None
        if job:
            self._keep = {name + PRINT_EXT}
        if (job and job != self._job) or not self._started:
            self._started = True
            threading.Thread(target=self.run, args=(set(self._keep),), daemon=True).start()
        if job:
            self._job = job

    def run(self, keep):
        with self._lock:
            try:
                clean(self._connect, keep, log=self._log, **self._options())
            except Exception as e:
                self._log("stick cleanup failed: %s %s" % (type(e).__name__, e))
