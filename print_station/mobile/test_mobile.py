import ast
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
from mobile.app import create_app
from mobile.auth import Devices, COOKIE, digest
from status_cache import StatusCache
from camera_relay import CameraHub, split_jpegs

ORIGIN = 'https://print.example.com'


class MobileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.devices = Devices(Path(self.temp.name) / 'devices.sqlite')
        self.cache = StatusCache()
        self.cameras = Mock()
        self.cameras.health.return_value = dict(state='off', running=False, frame_age_seconds=None)
        self.app = create_app(self.cache, self.cameras, self.devices)
        self.app.testing = True
        self.client = self.app.test_client()
        self.csrf = None

    def request(self, path, method='GET', body=None, csrf=True, origin=True):
        headers = {'Origin': ORIGIN} if origin else {}
        if csrf and self.csrf:
            headers['X-CSRF-Token'] = self.csrf
        return self.client.open(path, method=method, json=body, base_url=ORIGIN, headers=headers)

    def enroll(self):
        code = self.devices.code()
        response = self.request('/api/enroll', 'POST', dict(code=code, name='test phone'))
        self.assertEqual(response.status_code, 200)
        self.csrf = self.request('/api/session').json['csrf']
        return response

    def test_enrollment_cookie_and_single_use(self):
        code = self.devices.code()
        response = self.request('/api/enroll', 'POST', dict(code=code, name='phone'))
        cookie = response.headers['Set-Cookie']
        for flag in ['Secure', 'HttpOnly', 'SameSite=Strict', 'Path=/']:
            self.assertIn(flag, cookie)
        self.assertEqual(self.request('/api/enroll', 'POST', dict(code=code)).status_code, 403)
        with self.devices.connect() as db:
            hashed = db.execute('SELECT hash FROM devices').fetchone()[0]
        self.assertNotEqual(hashed, self.client.get_cookie(COOKIE, domain='print.example.com').value)

    def test_eight_digit_code_and_previous_code_invalidated(self):
        from unittest.mock import patch
        with patch('mobile.auth.secrets.randbelow', side_effect=[123, 456]):
            first = self.devices.code()
            second = self.devices.code()
        self.assertEqual(first, '00000123')
        self.assertEqual(second, '00000456')
        self.assertIsNone(self.devices.enroll(first, 'old'))
        self.assertIsNotNone(self.devices.enroll(second, 'phone'))

    def test_anonymous_status_camera_and_authentication_routes(self):
        for path in ['/api/status', '/api/camera/a', '/api/camera-health', '/api/session']:
            self.assertEqual(self.request(path).status_code, 401)
        self.cameras.acquire.assert_not_called()

    def test_read_only_even_when_authenticated(self):
        self.enroll()
        for path in ['/api/send', '/api/slice', '/api/control/stop', '/api/speed/3', '/api/code',
                     '/api/remote/extend', '/api/inbox', '/api/../api/control/pause']:
            for method in ['GET', 'POST', 'PUT', 'DELETE']:
                self.assertIn(self.request(path, method, {}).status_code, (404, 405))

    def test_csrf_origin_host(self):
        self.enroll()
        self.assertEqual(self.request('/api/viewer', 'POST', {'source': 'bambu'}, csrf=False).status_code, 403)
        self.assertEqual(self.request('/api/viewer', 'POST', {'source': 'bambu'}, origin=False).status_code, 403)
        response = self.client.post('/api/enroll', base_url=ORIGIN,
                                    headers={'Origin': 'https://evil.example'}, json={})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get('/api/status', base_url='https://wrong.example').status_code, 400)

    def test_revocation(self):
        self.enroll()
        ident = self.devices.list()[0][0]
        self.devices.revoke(ident)
        self.assertEqual(self.request('/api/status').status_code, 401)

    def test_expiry_and_enrollment_rate_limit(self):
        now = [1000]
        other = Devices(Path(self.temp.name) / 'other.sqlite', lambda: now[0])
        code = other.code()
        now[0] += 601
        self.assertIsNone(other.enroll(code, 'late'))
        token = other.enroll(other.code(), 'valid')
        self.assertIsNotNone(other.validate(token))
        now[0] += 91 * 86400
        self.assertIsNone(other.validate(token))
        for _ in range(10):
            self.assertEqual(self.request('/api/enroll', 'POST', {'code': 'bad'}).status_code, 403)
        self.assertEqual(self.request('/api/enroll', 'POST', {'code': 'bad'}).status_code, 429)

    def test_lease_ownership_and_source_validation(self):
        self.enroll()
        self.assertEqual(self.request('/api/viewer', 'POST', {'source': 'file:///etc/passwd'}).status_code, 400)
        self.cameras.acquire.return_value = 'viewer-identifier'
        self.assertEqual(self.request('/api/viewer', 'POST', {'source': 'bambu'}).json['viewer'], 'viewer-identifier')
        self.cameras.renew.return_value = False
        self.assertEqual(self.request('/api/camera/someone-elses-viewer').status_code, 410)
        self.cameras.frames.assert_not_called()

    def test_public_shell_has_no_secrets_and_security_headers(self):
        response = self.request('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertNotIn('rtsps://', response.text)
        response = self.request('/static/manifest.webmanifest')
        self.assertEqual(response.status_code, 200)
        response.close()

    def test_mobile_filename_without_credentials_or_negative_remaining(self):
        self.enroll()
        self.cache.observe(dict(gcode_state='RUNNING', subtask_name='private-file', access_code='secret',
            mc_percent=17, ams={'ams': [dict(id='0', tray=[dict(id='0', remain=-1, tray_color='ABCDEF00')])]}))
        response = self.request('/api/status')
        self.assertEqual(response.json['filename'], 'private-file')
        self.assertNotIn('secret', response.text)
        self.assertIsNone(response.json['ams'][0]['remaining_percent'])


class CacheTests(unittest.TestCase):
    def test_clear_spool_flagged_privately(self):
        from json_status.schema import clean
        cache = StatusCache(lambda: 100)
        cache.observe(dict(gcode_state='IDLE', ams={'ams': [dict(id='0', tray=[
            dict(id='0', tray_type='PETG', tray_sub_brands='PETG Translucent', tray_color='00000000'),
            dict(id='3', tray_type='PLA', tray_sub_brands='PLA Basic', tray_color='000000FF')])]}))
        slots = cache.snapshot()['ams']
        self.assertEqual([s['clear'] for s in slots], [True, False])
        self.assertEqual([s['color'] for s in slots], ['#000000', '#000000'])
        self.assertNotIn('clear', clean(dict(cache.data))['ams'][0])   # public JSON schema unchanged

    def test_independent_freshness_and_unknowns(self):
        now = [100]
        cache = StatusCache(lambda: now[0])
        cache.observe(dict(gcode_state='RUNNING', mc_percent=12, ams={'ams': []}))
        now[0] = 200
        cache.observe(dict(nozzle_temper=220))
        self.assertEqual(cache.snapshot()['observed_at_unix'], 100)
        self.assertEqual(cache.snapshot()['ams_observed_at_unix'], 100)
        cache.observe(dict(gcode_state='RUNNING', mc_percent=15))
        self.assertEqual(cache.snapshot()['observed_at_unix'], 200)
        self.assertEqual(cache.snapshot()['ams_observed_at_unix'], 100)
        self.assertIsNone(cache.snapshot()['remaining_minutes'])
        cache.observe(dict(gcode_state='RUNNING', subtask_name='next-job'))
        self.assertEqual(cache.snapshot()['filename'], 'next-job')
        cache.observe(dict(gcode_state='IDLE'))
        self.assertIsNone(cache.snapshot()['filename'])


class CameraTests(unittest.TestCase):
    def test_parser_and_bounded_corrupt_input(self):
        frames, tail = split_jpegs(b'noise\xff\xd8one\xff\xd9\xff\xd8two')
        self.assertEqual(frames, [b'\xff\xd8one\xff\xd9'])
        self.assertEqual(tail, b'\xff\xd8two')
        self.assertLessEqual(len(split_jpegs(b'x' * 9000000)[1]), 8 * 1024 * 1024)

    def test_shared_reader_release_and_expiry(self):
        opened, closed = [], []
        class Reader:
            def __init__(self): opened.append(1)
            def read(self):
                time.sleep(.01)
                return b'\xff\xd8image\xff\xd9'
            def close(self): closed.append(1)
        now = [0]
        hub = CameraHub({'bambu': Reader}, lambda: now[0])
        a = hub.acquire('phone-a', 'bambu')
        b = hub.acquire('phone-b', 'bambu')
        self.addCleanup(hub.release, a, 'phone-a')
        self.addCleanup(hub.release, b, 'phone-b')
        time.sleep(.05)
        self.assertEqual(len(opened), 1)
        self.assertEqual(hub.health('bambu')['state'], 'live')
        self.assertFalse(hub.renew(a, 'attacker'))
        hub.release(a, 'phone-a')
        self.assertEqual(len(closed), 0)
        now[0] = 21
        deadline = time.monotonic() + 2
        while not closed and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(len(closed), 1)
        self.assertFalse(hub.health('bambu')['running'])
        self.assertFalse(hub.renew(b, 'phone-b'))

    def test_view_limit_ends_renewed_lease(self):
        class Reader:
            def read(self):
                time.sleep(.01)
                return b'\xff\xd8image\xff\xd9'
            def close(self): pass
        now = [0]
        hub = CameraHub({'bambu': Reader}, lambda: now[0])
        key = hub.acquire('viewer', 'bambu')
        self.addCleanup(hub.release, key, 'viewer')
        for t in range(15, CameraHub.MAX_VIEW, 15):
            now[0] = t
            self.assertTrue(hub.renew(key, 'viewer'))
        now[0] = CameraHub.MAX_VIEW
        self.assertFalse(hub.renew(key, 'viewer'))
        self.assertEqual(list(hub.frames(key, 'viewer')), [])

    def test_reader_stops_after_three_failures_until_new_viewer(self):
        opens = []
        class Reader:
            def __init__(self):
                opens.append(1)
                raise OSError('port closed')
        now = [0]
        hub = CameraHub({'bambu': Reader}, lambda: now[0])
        a = hub.acquire('a', 'bambu')
        self.addCleanup(hub.release, a, 'a')
        deadline = time.monotonic() + 3
        while hub.health('bambu')['state'] != 'stopped' and time.monotonic() < deadline:
            now[0] += 1
            hub.renew(a, 'a')
            time.sleep(.05)
        self.assertEqual(hub.health('bambu')['state'], 'stopped')
        self.assertEqual(len(opens), CameraHub.GIVE_UP)
        self.assertTrue(hub.renew(a, 'a'))          # renewing does not retry
        time.sleep(.3)
        self.assertEqual(len(opens), CameraHub.GIVE_UP)
        self.assertEqual(list(hub.frames(a, 'a')), [])   # open streams end
        b = hub.acquire('b', 'bambu')                # Reconnect / refresh
        self.addCleanup(hub.release, b, 'b')
        deadline = time.monotonic() + 2
        while len(opens) <= CameraHub.GIVE_UP and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertGreater(len(opens), CameraHub.GIVE_UP)

    def test_snapshot_shares_reader_and_requires_new_frame(self):
        closed = threading.Event()
        opens = []
        class Reader:
            def __init__(self): opens.append(1)
            def read(self):
                time.sleep(.02)
                return b'\xff\xd8fresh\xff\xd9'
            def close(self): closed.set()
        hub = CameraHub({'bambu': Reader})
        key = hub.acquire('viewer', 'bambu')
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'photo.jpg'
                hub.snapshot(path, timeout=1)
                self.assertEqual(path.read_bytes(), b'\xff\xd8fresh\xff\xd9')
                self.assertEqual(len(opens), 1)
                self.assertEqual(len(hub.leases), 1)
        finally:
            hub.release(key, 'viewer')
        self.assertTrue(closed.wait(2))

    def test_warmup_frames_skipped(self):
        class Reader:
            skip_frames = 2
            n = 0
            def read(self):
                time.sleep(.01)
                Reader.n += 1
                return b'\xff\xd8f%d\xff\xd9' % Reader.n
            def close(self): pass
        hub = CameraHub({'webcam': Reader})
        key = hub.acquire('viewer', 'webcam')
        try:
            deadline = time.monotonic() + 2
            while hub.sources['webcam']['frame'] is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(hub.sources['webcam']['frame'], b'\xff\xd8f3\xff\xd9')
        finally:
            hub.release(key, 'viewer')

    def test_stalled_reader_is_closed(self):
        now = [0]
        closed = threading.Event()
        class Reader:
            def read(self):
                time.sleep(.01)
                now[0] += 6
                return None
            def close(self): closed.set()
        hub = CameraHub({'bambu': Reader}, lambda: now[0])
        key = hub.acquire('viewer', 'bambu')
        try:
            self.assertTrue(closed.wait(2))
            self.assertEqual(hub.sources['bambu']['error'], 'TimeoutError')
            self.assertNotEqual(hub.health('bambu')['state'], 'live')
        finally:
            hub.release(key, 'viewer')

    def test_revoked_stream_terminates_and_releases(self):
        hub = CameraHub({'bambu': Mock()})
        # Disable worker so this test has no background work.
        hub._ensure = Mock()
        key = hub.acquire('phone', 'bambu')
        self.assertEqual(list(hub.frames(key, 'phone', valid=lambda: False)), [])
        self.assertNotIn(key, hub.leases)


if __name__ == '__main__':
    unittest.main()
