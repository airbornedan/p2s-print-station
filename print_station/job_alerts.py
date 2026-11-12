"""Phone alerts for every print, however it was started: AMS spool switch, pause/run-out, error, stall, end.

Fed the raw incremental MQTT reports; keeps its own merged view of the few fields it needs.
`tick()` runs every 30 s for the stall check. Alerts go to `notify(message, priority)`.
"""
import threading
import time

from printer_stage import STAGES

NO_TRAY = {None, '255', '254'}          # 255 = nothing loaded, 254 = external spool
RUNNING, PAUSED, ENDED = 'RUNNING', 'PAUSE', ('FINISH', 'FAILED')


class JobAlerts:
    def __init__(self, notify, clock=time.time, stall_min=20):
        self.notify, self.clock, self.stall = notify, clock, stall_min * 60
        self.lock = threading.Lock()
        self.s = {}                     # merged fields
        self.layer_at = clock()         # when the layer last changed
        self.stalled = False
        self.errors = set()
        self.last_tray = None

    def _tell(self, msg, pri=4):
        job = self.s.get('subtask_name') or 'print'
        self.notify('%s: %s' % (job, msg), pri)

    def _layer(self):
        return '%s/%s' % (self.s.get('layer_num', '?'), self.s.get('total_layer_num', '?'))

    def observe(self, p):
        with self.lock:
            old = dict(self.s)
            for k in ('gcode_state', 'subtask_name', 'layer_num', 'total_layer_num', 'stg_cur', 'print_error'):
                if k in p:
                    self.s[k] = p[k]
            tray = (p.get('ams') or {}).get('tray_now')
            if tray is not None:
                self.s['tray_now'] = str(tray)
            state, was = self.s.get('gcode_state'), old.get('gcode_state')
            layer = self.s.get('layer_num') or 0

            if was is None and state in (RUNNING, PAUSED):                 # first report after a restart:
                err = self.s.get('print_error')                            # adopt the running job silently
                self.errors = {err} if err not in (None, 0, '0') else set()
                t = self.s.get('tray_now')
                self.last_tray = t if t not in NO_TRAY else None
                self.layer_at, self.stalled = self.clock(), False
                return

            if state == RUNNING and was not in (RUNNING, PAUSED):          # new job
                self.errors.clear()
                self.last_tray = self.s.get('tray_now') if self.s.get('tray_now') not in NO_TRAY else None
                self.layer_at, self.stalled = self.clock(), False
            if self.s.get('layer_num') != old.get('layer_num'):
                self.layer_at, self.stalled = self.clock(), False

            t1 = self.s.get('tray_now')                                    # compare with the last real tray:
            if t1 not in NO_TRAY:                                           # a refill unloads (255) in between
                t0, self.last_tray = self.last_tray, t1
                if state in (RUNNING, PAUSED) and layer >= 1 and t0 is not None and t0 != t1:
                    self._tell('switched from AMS slot %d to slot %d at layer %s.' % (int(t0) + 1, int(t1) + 1, self._layer()))

            if state == PAUSED and was != PAUSED:
                why = STAGES.get(self.s.get('stg_cur'), 'paused')
                self._tell('%s at layer %s.' % (why, self._layer()), 5)

            err = self.s.get('print_error')
            if err not in (None, 0, '0') and err not in self.errors:
                self.errors.add(err)
                self._tell('printer error %s at layer %s.' % (err, self._layer()), 5)

            if state in ENDED and was in (RUNNING, PAUSED):
                self._tell('finished. Clear the bed.' if state == 'FINISH' else 'stopped or failed at layer %s.' % self._layer(),
                           3 if state == 'FINISH' else 5)

    def tick(self):
        with self.lock:
            if (self.s.get('gcode_state') == RUNNING and (self.s.get('layer_num') or 0) >= 1 and not self.stalled
                    and self.clock() - self.layer_at > self.stall):
                self.stalled = True
                why = STAGES.get(self.s.get('stg_cur'), 'stage %s' % self.s.get('stg_cur'))
                self._tell('no new layer for %d min (layer %s, %s).' % (self.stall // 60, self._layer(), why), 5)
