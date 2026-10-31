import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import requests
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from pywebpush import WebPushException
from mobile.auth import Devices, COOKIE
from mobile.push import PushService, validate_subscription, NoRedirectSession
from mobile.app import create_app
from status_cache import StatusCache


def subscription(endpoint='https://web.push.apple.com/test-endpoint'):
    key = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    encode = lambda b: base64.urlsafe_b64encode(b).decode().rstrip('=')
    return dict(endpoint=endpoint, keys=dict(p256dh=encode(key), auth=encode(b'1234567890abcdef')))


class PushTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.devices = Devices(self.path / 'devices.sqlite')
        self.sender = Mock()
        self.push = PushService(self.devices, self.path, sender=self.sender)
        self.token = self.devices.enroll(self.devices.code(), 'test phone')
        self.device = self.devices.validate(self.token)['id']
        self.event = dict(id='a' * 32, name='mask', kind='end', time=time.time(),
                          detail='completed; printer state FINISH', capture='Camera unavailable')

    def test_vapid_key_is_persistent_private_and_valid_public_point(self):
        again = PushService(self.devices, self.path, sender=self.sender)
        self.assertEqual(self.push.public_key, again.public_key)
        self.assertEqual(self.push.key.stat().st_mode & 0o777, 0o600)
        key = base64.urlsafe_b64decode(self.push.public_key + '=')
        self.assertEqual(len(key), 65)
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), key)

    def test_endpoint_allowlist_and_key_validation(self):
        self.assertEqual(validate_subscription(subscription())['endpoint'], 'https://web.push.apple.com/test-endpoint')
        for endpoint in ['http://web.push.apple.com/a', 'https://127.0.0.1/a',
                         'https://web.push.apple.com.evil.example/a', 'https://evil.example/a',
                         'https://web.push.apple.com:8443/a', 'https://user:pass@web.push.apple.com/a',
                         'https://web.push.apple.com/a#fragment', 'https://[::1]/a']:
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                validate_subscription(subscription(endpoint))
        value = subscription()
        value['keys']['p256dh'] = 'x' * 87
        with self.assertRaises(ValueError): validate_subscription(value)
        with self.assertRaises(ValueError): validate_subscription(None)

    def test_no_http_redirects(self):
        with patch.object(requests.Session, 'request') as request:
            NoRedirectSession().request('POST', 'https://web.push.apple.com/a', allow_redirects=True)
            self.assertFalse(request.call_args.kwargs['allow_redirects'])

    def test_send_and_retry_deduplication_with_private_photo(self):
        self.push.subscribe(self.device, subscription())
        photo = self.path / 'source.jpg'
        photo.write_bytes(b'jpeg')
        self.push.deliver(self.event, [photo])
        self.push.deliver(self.event, [photo])
        self.sender.assert_called_once()
        call = self.sender.call_args.kwargs
        self.assertEqual(call['ttl'], 3600)
        self.assertEqual(call['timeout'], 10)
        self.assertEqual(call['vapid_claims'], {'sub': 'https://print.example.com'})
        data = json.loads(call['data'])
        self.assertEqual(data['title'], 'Print completed')
        self.assertEqual(data['url'], '/?event=' + 'a' * 32)
        self.assertNotIn('image', data)
        stored = self.push.get('a' * 32)
        self.assertTrue(stored['image'])
        self.assertEqual((self.push.photos / ('a' * 32 + '.jpg')).read_bytes(), b'jpeg')

    def test_actual_webpush_encrypts_and_signs_without_network(self):
        from pywebpush import webpush
        self.push.sender = webpush
        self.push.subscribe(self.device, subscription())
        response = requests.Response()
        response.status_code = 201
        response._content = b''
        with patch('mobile.push.NoRedirectSession') as factory:
            session = factory.return_value.__enter__.return_value
            session.post.return_value = response
            self.push.deliver(self.event, [])
            sent = session.post.call_args.kwargs
            self.assertIsInstance(sent['data'], bytes)
            self.assertNotIn(b'printer state FINISH', sent['data'])
            self.assertIn('vapid', sent['headers']['Authorization'])
            self.assertEqual(sent['headers']['Content-Encoding'], 'aes128gcm')

    def test_test_notification_is_rate_limited_and_does_not_touch_print_state(self):
        import threading
        sent = threading.Event()
        self.push.subscribe(self.device, subscription())
        self.sender.side_effect = lambda **kw: sent.set()
        def photo(path): path.write_bytes(b'jpeg')
        self.push.test(self.device, photo)
        with self.assertRaises(ValueError): self.push.test(self.device, photo)
        self.assertTrue(sent.wait(3))
        # Let the worker finish storing receipt/cleaning its temporary capture.
        deadline = time.monotonic() + 3
        while list(self.push.photos.glob('*-capture.jpg')) and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.push.get()['title'], 'Printer notification test')
        self.assertEqual(list(self.push.photos.glob('*-capture.jpg')), [])

    def test_revoked_device_does_not_receive(self):
        self.push.subscribe(self.device, subscription())
        self.devices.revoke(self.device)
        with self.assertRaises(RuntimeError): self.push.deliver(self.event, [])
        self.sender.assert_not_called()

    def test_expired_subscription_removed(self):
        self.push.subscribe(self.device, subscription())
        response = requests.Response()
        response.status_code = 410
        self.sender.side_effect = WebPushException('Gone', response=response)
        with self.assertRaises(RuntimeError): self.push.deliver(self.event, [])
        self.assertFalse(self.push.enabled(self.device))

    def test_transient_failure_retains_subscription(self):
        self.push.subscribe(self.device, subscription())
        self.sender.side_effect = requests.Timeout()
        with self.assertRaises(RuntimeError): self.push.deliver(self.event, [])
        self.assertTrue(self.push.enabled(self.device))
        self.sender.side_effect = None
        self.push.deliver(self.event, [])
        self.assertEqual(self.sender.call_count, 2)

    def test_partial_delivery_retries_only_failed_device(self):
        self.push.subscribe(self.device, subscription())
        token = self.devices.enroll(self.devices.code(), 'second phone')
        device2 = self.devices.validate(token)['id']
        self.push.subscribe(device2, subscription('https://web.push.apple.com/second'))
        failed = set()
        def send(**kw):
            endpoint = kw['subscription_info']['endpoint']
            if endpoint.endswith('/second') and endpoint not in failed:
                failed.add(endpoint)
                raise requests.Timeout()
        self.sender.side_effect = send
        with self.assertRaises(RuntimeError): self.push.deliver(self.event, [])
        self.push.deliver(self.event, [])
        self.assertEqual(self.sender.call_count, 3)

    def test_private_photos_expire_and_cannot_be_path_traversed(self):
        self.push.subscribe(self.device, subscription())
        photo = self.path / 'source.jpg'; photo.write_bytes(b'jpeg')
        self.push.deliver(self.event, [photo])
        self.assertIsNone(self.push.get('../../devices.sqlite'))
        with self.devices.connect() as db:
            db.execute('UPDATE notifications SET created=?', (time.time() - 86401,))
        self.assertIsNone(self.push.get())
        self.assertFalse((self.push.photos / ('a' * 32 + '.jpg')).exists())

    def test_subscription_ownership(self):
        value = subscription()
        self.push.subscribe(self.device, value)
        with self.assertRaises(ValueError): self.push.subscribe('another-device', value)

    def test_authenticated_csrf_protected_routes_and_public_worker(self):
        app = create_app(StatusCache(), Mock(), self.devices, push=self.push)
        app.testing = True
        client = app.test_client()
        origin = 'https://print.example.com'
        response = client.get('/sw.js', base_url=origin)
        self.assertEqual(response.status_code, 200)
        response.close()
        for path in ['/api/push', '/api/notifications/latest', '/api/notifications/' + 'a' * 32 + '/photo']:
            self.assertEqual(client.get(path, base_url=origin).status_code, 401)
        client.set_cookie(COOKIE, self.token, domain='print.example.com', secure=True)
        self.assertEqual(client.get('/api/push', base_url=origin).json['public_key'], self.push.public_key)
        response = client.post('/api/push/subscribe', base_url=origin, json=subscription(), headers={'Origin': origin})
        self.assertEqual(response.status_code, 403)
        csrf = self.devices.validate(self.token)['csrf']
        response = client.post('/api/push/subscribe', base_url=origin, json=subscription(),
                               headers={'Origin': origin, 'X-CSRF-Token': csrf})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.push.enabled(self.device))
        self.push.deliver(self.event, [])
        self.assertEqual(client.get('/api/notifications/latest', base_url=origin).json['id'], 'a' * 32)
        self.assertEqual(client.get('/api/notifications/' + 'a' * 32 + '/photo', base_url=origin).status_code, 404)
        response = client.post('/api/push/unsubscribe', base_url=origin, json={},
                               headers={'Origin': origin, 'X-CSRF-Token': csrf})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.push.enabled(self.device))


if __name__ == '__main__': unittest.main()
