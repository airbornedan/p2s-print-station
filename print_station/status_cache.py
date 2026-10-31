"""Timestamped observations for the read-only mobile boundary."""
import threading
import time
from json_status.schema import clean
from printer_stage import stage_label


class StatusCache:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.lock = threading.Lock()
        self.data = {}
        self.connected = False
        self.stage_code = None
        self.stage_observed = None
        self.stage_job = None

    def observe(self, report):
        with self.lock:
            name = report.get('subtask_name')
            if isinstance(name, str) and name != self.stage_job:
                self.stage_code = self.stage_observed = None
                self.stage_job = name
            if report.get('gcode_state') in {'IDLE', 'FINISH', 'FAILED'}:
                self.stage_code = self.stage_observed = None
            elif 'stg_cur' in report:
                self.stage_code = report['stg_cur']
                self.stage_observed = self.clock()
            if 'gcode_state' in report:
                for src, dest in [('gcode_state', 'state'), ('mc_percent', 'progress_percent'),
                                  ('mc_remaining_time', 'remaining_minutes'), ('layer_num', 'layer'),
                                  ('total_layer_num', 'total_layers')]:
                    self.data[dest] = report.get(src)
                self.data['observed_at_unix'] = self.clock()
                # Job identity must belong to this observation, not a retained prior job.
                name = report.get('subtask_name')
                self.data['filename'] = name[:255] if isinstance(name, str) else None
            units = (report.get('ams') or {}).get('ams')
            if isinstance(units, list):
                slots = []
                for unit in units:
                    for tray in unit.get('tray', []):
                        slots.append(dict(unit=unit.get('id'), slot_index=tray.get('id'),
                            material=tray.get('tray_type'), name=tray.get('tray_sub_brands'),
                            color='#' + (tray.get('tray_color') or '')[:6],
                            remaining_percent=tray.get('remain')))
                self.data['ams'] = slots
                self.data['ams_observed_at_unix'] = self.clock()

    def connection(self, connected):
        with self.lock:
            self.connected = connected

    def snapshot(self):
        with self.lock:
            doc = clean(dict(self.data, generated_at_unix=self.clock()))
            # Private mobile-only field; never change the public JSON schema.
            doc['filename'] = self.data.get('filename')
            age = self.clock() - self.stage_observed if self.stage_observed is not None else None
            fresh = (self.connected and age is not None and 0 <= age <= 30
                     and self.data.get('state') in {'RUNNING', 'PREPARE', 'SLICING', 'PAUSE'})
            label = stage_label(self.stage_code) if fresh else None
            doc['stage'] = label
            doc['stage_code'] = self.stage_code if label else None
            doc['stage_observed_at_unix'] = self.stage_observed
            doc['connected'] = self.connected
            return doc
