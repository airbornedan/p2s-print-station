import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import receive
from schema import clean


class StatusTests(unittest.TestCase):
    def test_allowlist_and_unknowns(self):
        d = clean({'password': 'secret', 'file': 'private', 'state': 'RUNNING',
                   'progress_percent': float('nan'), 'remaining_minutes': -1,
                   'ams': [{'unit': '0', 'slot_index': '1', 'material': 'PLA',
                            'remaining_percent': -1, 'color': '#8E9089', 'secret': 'private'}]})
        self.assertNotIn('secret', json.dumps(d))
        self.assertNotIn('private', json.dumps(d))
        self.assertIsNone(d['progress_percent'])
        self.assertIsNone(d['remaining_minutes'])
        self.assertIsNone(d['ams'][0]['remaining_percent'])
        self.assertEqual(d['ams'][0]['slot_number'], 2)

    def test_empty_slot_and_absent_report(self):
        d = clean({'ams': [{'unit': 0, 'slot_index': 0}]})
        self.assertIsNone(d['observed_at_unix'])
        self.assertIsNone(d['ams'][0]['material'])
        self.assertIsNone(d['state'])

    def test_receiver_keeps_previous_on_rejection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / 'snapshot.json'
            now = time.time()
            expires = root / 'expires'
            expires.write_text(str(int(now + 600)))
            payload = {'generated_at_unix': now, 'observed_at_unix': now, 'state': 'RUNNING'}
            with patch.object(receive, 'ROOT', root), patch.object(receive, 'EXPIRES', expires), patch.object(receive.sys, 'stdin', io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))):
                receive.receive()
            previous = target.read_bytes()
            for raw in (b'{bad', b'x' * 32769, json.dumps({'generated_at_unix': now - 1000}).encode()):
                with patch.object(receive, 'ROOT', root), patch.object(receive, 'EXPIRES', expires), patch.object(receive.sys, 'stdin', io.TextIOWrapper(io.BytesIO(raw))):
                    with self.assertRaises((ValueError, TypeError)):
                        receive.receive()
                self.assertEqual(target.read_bytes(), previous)
            expires.write_text('0')
            with patch.object(receive, 'ROOT', root), patch.object(receive, 'EXPIRES', expires), patch.object(receive.sys, 'stdin', io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))):
                with self.assertRaises(ValueError):
                    receive.receive()
            self.assertEqual(set(root.iterdir()), {target, expires})


if __name__ == '__main__':
    unittest.main()
