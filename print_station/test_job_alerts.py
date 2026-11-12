import unittest
from job_alerts import JobAlerts


class Clock:
    t = 1000.0
    def __call__(self):
        return self.t


def make():
    sent, clock = [], Clock()
    return JobAlerts(lambda m, p: sent.append((m, p)), clock=clock, stall_min=20), sent, clock


def running(a, layer=5, tray='1'):
    a.observe({'gcode_state': 'RUNNING', 'subtask_name': 'holder', 'layer_num': layer, 'total_layer_num': 100,
               'stg_cur': 0, 'print_error': 0, 'ams': {'tray_now': tray}})


class AlertsTest(unittest.TestCase):
    def test_restart_mid_print_adopts_silently_then_alerts_end(self):
        a, sent, _ = make()
        a.observe({'gcode_state': 'PAUSE', 'subtask_name': 'leg', 'layer_num': 80, 'total_layer_num': 300,
                   'stg_cur': 6, 'print_error': 7, 'ams': {'tray_now': '3'}})
        self.assertEqual(sent, [])           # existing pause and error are not re-sent
        a.observe({'gcode_state': 'RUNNING'})
        a.observe({'gcode_state': 'FINISH'})
        self.assertEqual(len(sent), 1)
        self.assertIn('finished', sent[0][0])

    def test_restart_then_new_error_alerts(self):
        a, sent, _ = make(); running(a)
        a.observe({'print_error': 1234})
        self.assertEqual(len(sent), 1)

    def test_switch_while_paused_for_runout(self):
        a, sent, _ = make(); running(a)
        a.observe({'gcode_state': 'PAUSE', 'stg_cur': 6, 'ams': {'tray_now': '255'}})
        a.observe({'ams': {'tray_now': '0'}})
        self.assertEqual(len(sent), 2)       # the pause, then the new spool

    def test_spool_switch(self):
        a, sent, _ = make(); running(a)
        a.observe({'ams': {'tray_now': '0'}})
        self.assertEqual(len(sent), 1)
        self.assertIn('slot 2 to slot 1', sent[0][0])

    def test_first_load_is_not_a_switch(self):
        a, sent, _ = make(); running(a, layer=0, tray='255')
        a.observe({'ams': {'tray_now': '1'}})
        a.observe({'layer_num': 1})
        self.assertEqual(sent, [])

    def test_refill_through_unload(self):
        a, sent, _ = make(); running(a)
        a.observe({'ams': {'tray_now': '255'}})
        self.assertEqual(sent, [])
        a.observe({'ams': {'tray_now': '0'}})
        self.assertEqual(len(sent), 1)       # auto-refill: unload (255), then the next spool
        self.assertIn('slot 2 to slot 1', sent[0][0])

    def test_runout_pause(self):
        a, sent, _ = make(); running(a)
        a.observe({'gcode_state': 'PAUSE', 'stg_cur': 6})
        self.assertIn('filament ran out', sent[0][0]); self.assertEqual(sent[0][1], 5)
        a.observe({'gcode_state': 'PAUSE'})
        self.assertEqual(len(sent), 1)

    def test_stall_once(self):
        a, sent, clock = make(); running(a)
        clock.t += 19 * 60; a.tick(); self.assertEqual(sent, [])
        clock.t += 2 * 60; a.tick(); a.tick()
        self.assertEqual(len(sent), 1); self.assertIn('no new layer', sent[0][0])
        a.observe({'layer_num': 6}); clock.t += 21 * 60; a.tick()
        self.assertEqual(len(sent), 2)

    def test_error_once(self):
        a, sent, _ = make(); running(a)
        a.observe({'print_error': 50348044}); a.observe({'print_error': 50348044})
        self.assertEqual(len(sent), 1)

    def test_finish(self):
        a, sent, _ = make(); running(a)
        a.observe({'gcode_state': 'FINISH'})
        self.assertIn('finished', sent[0][0])

    def test_idle_finish_report_is_quiet(self):
        a, sent, _ = make()
        a.observe({'gcode_state': 'FINISH', 'subtask_name': 'old'})
        self.assertEqual(sent, [])


if __name__ == '__main__':
    unittest.main()
