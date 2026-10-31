import ast
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock
from plate_watchdog import PrintMonitor


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / 'state.json'
        self.now = 1000
        self.mail = Mock()
        self.camera = Mock(side_effect=lambda p: p.write_bytes(b'jpeg'))
        self.watch = self.make()
        self.report = dict(task_id='123', subtask_name='mask', gcode_state='RUNNING', mc_percent=10)

    def make(self):
        return PrintMonitor(self.file, lambda: self.now, False, self.camera, self.mail)

    def start(self):
        self.watch.expect('mask.gcode.3mf')
        self.watch.observe(self.report)

    def test_external_print_does_not_arm(self):
        self.watch.observe(self.report)
        self.assertIsNone(self.watch.state['job'])
        self.watch.expect('mask.gcode.3mf')
        self.watch.observe(dict(self.report, subtask_name='other'))
        self.assertEqual(self.watch.state['job']['phase'], 'pending')
        self.now += 121
        self.watch.check()
        self.assertIsNone(self.watch.state['job'])
        self.mail.assert_not_called()

    def test_raw_identity_and_state_required(self):
        self.start()
        for p in [{'print_error': 42}, {'gcode_state': 'FINISH'},
                  dict(self.report, task_id='456', print_error=42),
                  {'task_id': '123', 'subtask_name': 'mask', 'print_error': 42}]:
            self.watch.observe(p)
        self.assertFalse(self.watch.state['outbox'])
        self.watch.check()
        self.mail.assert_not_called()

    def test_error_dedup_and_resume_then_finish(self):
        self.start()
        error = dict(self.report, gcode_state='PAUSE', print_error=42)
        self.watch.observe(error)
        self.watch.observe(error)
        self.watch.check()
        self.mail.assert_called_once()
        self.assertIsNotNone(self.watch.state['job'])
        self.watch.observe(self.report)
        self.watch.observe(dict(error, print_error=43))
        self.watch.check()
        self.assertEqual(self.mail.call_count, 2)
        self.watch.observe(dict(self.report, gcode_state='FINISH'))
        self.assertIsNone(self.watch.state['job'])
        self.watch.check()
        self.assertEqual(self.mail.call_count, 2)
        self.now += 5
        self.watch.check()
        self.assertEqual(self.mail.call_count, 3)
        self.watch.observe(dict(self.report, gcode_state='FINISH'))
        self.watch.check()
        self.assertEqual(self.mail.call_count, 3)

    def test_terminal_outcomes(self):
        for state in ['FAILED', 'STOP', 'CANCEL', 'IDLE']:
            with self.subTest(state=state):
                self.start()
                self.watch.observe(dict(self.report, gcode_state=state))
                self.watch.check()
                self.assertIn('without confirmed success', self.mail.call_args.args[1])
                self.assertIsNone(self.watch.state['job'])

    def test_camera_failure_still_emails(self):
        self.start()
        self.camera.side_effect = TimeoutError()
        self.watch.observe(dict(self.report, gcode_state='FAILED'))
        self.watch.check()
        self.mail.assert_called_once()
        self.assertIn('Photo unavailable', self.mail.call_args.args[1])
        self.assertEqual(self.mail.call_args.args[2], [])

    def test_restart_and_bounded_retry_keeps_original_photo(self):
        self.start()
        deadline = self.watch.state['job']['deadline']
        self.watch = self.make()
        self.assertEqual(self.watch.state['job']['deadline'], deadline)
        self.watch.observe(dict(self.report, gcode_state='FAILED'))
        self.mail.side_effect = OSError()
        self.watch.check()
        self.watch = self.make()
        self.watch.check()
        self.assertEqual(self.mail.call_count, 1)
        for _ in range(4):
            self.now += 60
            self.watch.check()
        self.assertEqual(self.mail.call_count, 3)
        self.assertEqual(self.camera.call_count, 1)
        self.assertFalse(self.watch.state['outbox'])
        self.assertFalse(list(self.file.parent.glob('*.jpg')))

    def test_refused_start_restores_monitored_print(self):
        self.start()
        token = self.watch.expect('next.gcode.3mf')
        with self.assertRaises(RuntimeError):
            self.watch.expect('third.gcode.3mf')
        self.watch.cancel(token)
        self.assertEqual(self.watch.state['job']['name'], 'mask')
        self.watch.observe(dict(self.report, gcode_state='FINISH'))
        self.now += 5
        self.watch.check()
        self.assertIn('completed', self.mail.call_args.args[1])

    def test_new_station_print_closes_unfinished_job(self):
        self.start()
        self.watch.expect('next.gcode.3mf')
        self.watch.observe(dict(self.report, subtask_name='next', task_id='9'))
        self.assertEqual(self.watch.state['job']['name'], 'next')
        self.watch.check()
        self.assertIn('mask', self.mail.call_args.args[0])
        self.assertIn('outcome not observed', self.mail.call_args.args[1])

    def test_outside_print_closes_unfinished_job(self):
        self.start()
        self.watch.observe(dict(self.report, subtask_name='studio_job', task_id='9'))
        self.assertIsNone(self.watch.state['job'])
        self.watch.check()
        self.assertIn('another print started', self.mail.call_args.args[1])

    def test_push_failure_falls_back_to_email(self):
        self.watch.notify = Mock(side_effect=RuntimeError('No enrolled device'))
        self.start()
        self.watch.observe(dict(self.report, gcode_state='FAILED'))
        self.watch.check()
        self.mail.assert_called_once()
        self.assertFalse(self.watch.state['outbox'])

    def test_expiry_does_not_extend_or_claim_completion(self):
        self.start()
        deadline = self.watch.state['job']['deadline']
        self.now += 500
        self.watch.observe(self.report)
        self.assertEqual(self.watch.state['job']['deadline'], deadline)
        self.now = deadline
        self.watch.check()
        self.assertIsNone(self.watch.state['job'])
        self.assertIn('completion unknown', self.mail.call_args.args[1])
        self.watch.observe(self.report)
        self.assertIsNone(self.watch.state['job'])

    def test_late_photo_not_taken_against_next_print(self):
        self.start()
        self.watch.observe(dict(self.report, gcode_state='FAILED'))
        self.watch.expect('other.gcode.3mf')
        self.watch.check()
        self.camera.assert_not_called()
        self.assertIn('Photo unavailable', self.mail.call_args.args[1])

    def test_push_channel_bypasses_email_and_receives_original_event(self):
        notify = Mock()
        self.watch.notify = notify
        self.start()
        self.watch.observe(dict(self.report, gcode_state='FAILED'))
        self.watch.check()
        notify.assert_called_once()
        event, shots = notify.call_args.args
        self.assertEqual(event['kind'], 'end')
        self.assertTrue(shots)
        self.mail.assert_not_called()
        self.assertFalse(self.watch.state['outbox'])

    def test_send_integration_order_without_station_import(self):
        tree = ast.parse(Path(__file__).with_name('station.py').read_text())
        send = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'api_send')
        source = ast.unparse(send)
        self.assertLess(source.index('upload.py'), source.index('_print_monitor.expect'))
        self.assertLess(source.index('_print_monitor.expect'), source.index('start.py'))
        callback = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_on_message')
        self.assertIn('_print_monitor.observe(p)', ast.unparse(callback))


if __name__ == '__main__':
    unittest.main()
