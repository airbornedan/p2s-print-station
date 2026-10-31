"""Run on the droplet with PHP installed. Uses isolated fixtures, no live state."""
import json
from pathlib import Path
import subprocess
import tempfile
import time

source = Path(__file__).with_name('status.php').read_text()
now = time.time()
base = {'schema_version': 1, 'state': 'RUNNING', 'progress_percent': 25,
        'remaining_minutes': 120, 'ams': [], 'generated_at_unix': now,
        'observed_at_unix': now, 'ams_observed_at_unix': now, 'received_at_unix': now}
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    snapshot, expires, started = (root / n for n in ('snapshot', 'expires', 'started'))
    handler = root / 'handler.php'
    handler.write_text(source.replace('/var/lib/printer-status/snapshot.json', str(snapshot))
                       .replace('/etc/printer-remote/expires', str(expires))
                       .replace('/etc/printer-remote/session_started', str(started)))
    started.touch()
    expires.write_text(str(int(now + 600)))
    for name, changes, expected in (
        ('recent', {}, 'recent'),
        ('overdue publisher', {'received_at_unix': now - 601}, 'stale'),
        ('printer offline', {'observed_at_unix': None, 'ams_observed_at_unix': None}, 'unavailable'),
        ('old printer report', {'observed_at_unix': now - 120}, 'unavailable'),
    ):
        # The receipt must follow the session's start, including in stale fixtures.
        import os
        os.utime(started, (now - 1000, now - 1000))
        snapshot.write_text(json.dumps(dict(base, **changes)))
        d = json.loads(subprocess.check_output(['php', str(handler)]))
        assert d['availability'] == expected, (name, d)
        if name == 'recent':
            assert d['progress_remaining_percent'] == 75
        print(name + ': passed')
    expires.write_text('0')
    d = json.loads(subprocess.check_output(['php', str(handler)]))
    assert d['availability'] == 'off' and 'state' not in d and 'ams' not in d
    print('off hides existing snapshot: passed')
    expires.write_text(str(int(now + 600)))
    os.utime(started, (now + 10, now + 10))
    d = json.loads(subprocess.check_output(['php', str(handler)]))
    assert d['availability'] == 'unavailable' and 'state' not in d
    print('new session hides previous snapshot: passed')
