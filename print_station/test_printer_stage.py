import unittest

from printer_stage import stage_label
from status_cache import StatusCache


class StageTests(unittest.TestCase):
    def test_startup_and_unknown_codes(self):
        for code, label in [(0, 'Printing'), (1, 'Auto bed leveling'),
                            (4, 'Changing filament'), (14, 'Cleaning nozzle tip'),
                            (54, 'Waiting for heatbed to reach target temperature')]:
            self.assertEqual(stage_label(code), label)
        self.assertEqual(stage_label(999), 'Printer stage 999')
        for code in [None, True, '14', -1, 255, [], {}]:
            self.assertIsNone(stage_label(code))

    def test_incremental_stage_freshness_and_job_boundaries(self):
        now = [100]
        cache = StatusCache(lambda: now[0])
        cache.connection(True)
        cache.observe(dict(gcode_state='RUNNING', subtask_name='one', stg_cur=14))
        self.assertEqual(cache.snapshot()['stage'], 'Cleaning nozzle tip')
        now[0] = 110
        cache.observe(dict(stg_cur=1))
        self.assertEqual(cache.snapshot()['stage'], 'Auto bed leveling')
        now[0] = 141
        cache.observe(dict(gcode_state='RUNNING', subtask_name='one', nozzle_temper=200))
        self.assertIsNone(cache.snapshot()['stage'])
        cache.observe(dict(stg_cur=0))
        self.assertEqual(cache.snapshot()['stage'], 'Printing')
        cache.connection(False)
        self.assertIsNone(cache.snapshot()['stage'])
        cache.connection(True)
        cache.observe(dict(gcode_state='FINISH'))
        self.assertIsNone(cache.snapshot()['stage'])
        cache.observe(dict(gcode_state='RUNNING', subtask_name='two', stg_cur=2))
        self.assertEqual(cache.snapshot()['stage'], 'Heatbed preheating')
        cache.observe(dict(gcode_state='PREPARE', subtask_name='three'))
        self.assertIsNone(cache.snapshot()['stage'])


if __name__ == '__main__':
    unittest.main()
