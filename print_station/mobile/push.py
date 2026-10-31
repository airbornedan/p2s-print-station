"""Encrypted Web Push and private, short-lived notification photos. No SMTP."""
import base64
import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import webpush, WebPushException

STATE = Path.home() / '.local/state/print_station/mobile'


class NoRedirectSession(requests.Session):
    def request(self, method, url, **kwargs):
        kwargs['allow_redirects'] = False
        return super().request(method, url, **kwargs)


def validate_subscription(value):
    if not isinstance(value, dict):
        raise ValueError('Invalid push subscription')
    endpoint = value.get('endpoint')
    if not isinstance(endpoint, str) or len(endpoint) > 2048:
        raise ValueError('Invalid push endpoint')
    try:
        url = urlsplit(endpoint)
        host = url.hostname or ''
        allowed = (host.endswith('.push.apple.com') or host == 'updates.push.services.mozilla.com'
                   or host == 'fcm.googleapis.com')
        if (url.scheme != 'https' or not allowed or url.port not in (None, 443)
                or url.username or url.password or url.fragment):
            raise ValueError('Unrecognized push service')
    except (ValueError, TypeError):
        raise ValueError('Unrecognized push service') from None
    keys = value.get('keys')
    if not isinstance(keys, dict):
        raise ValueError('Missing push keys')
    for name, length in [('p256dh', 65), ('auth', 16)]:
        text = keys.get(name)
        if not isinstance(text, str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,100}={0,2}', text):
            raise ValueError('Invalid push key')
        try:
            raw = base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))
            if len(raw) != length:
                raise ValueError()
            if name == 'p256dh':
                ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
        except Exception:
            raise ValueError('Invalid push key') from None
    return dict(endpoint=endpoint, keys={k: keys[k] for k in ('p256dh', 'auth')})


class PushService:
    def __init__(self, devices, directory=STATE, sender=webpush, clock=time.time, origin='https://print.example.com'):
        self.origin = origin
        self.devices, self.directory, self.sender, self.clock = devices, Path(directory), sender, clock
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.photos = self.directory / 'notification-photos'
        self.photos.mkdir(exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.test_times = {}
        self.key = self.directory / 'vapid-private.pem'
        if not self.key.exists():
            private = ec.generate_private_key(ec.SECP256R1())
            pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption())
            try:
                fd = os.open(self.key, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(pem)
            except FileExistsError:
                pass
        private = serialization.load_pem_private_key(self.key.read_bytes(), password=None)
        public = private.public_key().public_bytes(serialization.Encoding.X962,
                                                   serialization.PublicFormat.UncompressedPoint)
        self.public_key = base64.urlsafe_b64encode(public).decode().rstrip('=')
        with self.devices.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS push_subscriptions (
                    device TEXT PRIMARY KEY, endpoint_hash TEXT UNIQUE, subscription TEXT);
                CREATE TABLE IF NOT EXISTS notifications (
                    id TEXT PRIMARY KEY, created REAL, title TEXT, body TEXT, image INTEGER);
                CREATE TABLE IF NOT EXISTS push_deliveries (
                    event TEXT, device TEXT, endpoint_hash TEXT, status TEXT,
                    PRIMARY KEY (event, device, endpoint_hash));
            ''')

    def subscribe(self, device, value):
        subscription = validate_subscription(value)
        hashed = hashlib.sha256(subscription['endpoint'].encode()).hexdigest()
        with self.lock, self.devices.connect() as db:
            row = db.execute('SELECT device FROM push_subscriptions WHERE endpoint_hash=?', (hashed,)).fetchone()
            if row and row[0] != device:
                raise ValueError('Subscription belongs to another enrolled device')
            db.execute('INSERT OR REPLACE INTO push_subscriptions VALUES (?, ?, ?)',
                       (device, hashed, json.dumps(subscription)))

    def unsubscribe(self, device):
        with self.lock, self.devices.connect() as db:
            db.execute('DELETE FROM push_subscriptions WHERE device=?', (device,))

    def enabled(self, device):
        with self.devices.connect() as db:
            return db.execute('SELECT 1 FROM push_subscriptions WHERE device=?', (device,)).fetchone() is not None

    def _subscribers(self, only=None):
        now = self.clock()
        with self.devices.connect() as db:
            rows = db.execute('''SELECT s.device, s.endpoint_hash, s.subscription
                FROM push_subscriptions s JOIN devices d ON d.id=s.device
                WHERE d.revoked=0 AND d.created>? AND d.used>?''',
                (now - 365 * 86400, now - 90 * 86400)).fetchall()
        return [(d, h, json.loads(s)) for d, h, s in rows if only is None or d == only]

    def _prune(self):
        with self.devices.connect() as db:
            rows = db.execute('SELECT id,created FROM notifications ORDER BY created DESC').fetchall()
            for index, (ident, created) in enumerate(rows):
                if index >= 20 or self.clock() - created > 86400:
                    db.execute('DELETE FROM notifications WHERE id=?', (ident,))
                    db.execute('DELETE FROM push_deliveries WHERE event=?', (ident,))
                    (self.photos / (ident + '.jpg')).unlink(missing_ok=True)

    def _remember(self, event, shots):
        ident = event['id']
        if not re.fullmatch('[a-f0-9]{32}', ident):
            raise ValueError('Invalid event ID')
        with self.lock:
            self._prune()
            with self.devices.connect() as db:
                if db.execute('SELECT 1 FROM notifications WHERE id=?', (ident,)).fetchone():
                    return
                image = False
                if shots and shots[0].is_file() and shots[0].stat().st_size <= 5 * 1024 * 1024:
                    target = self.photos / (ident + '.jpg')
                    shutil.copyfile(shots[0], target)
                    target.chmod(0o600)
                    image = True
                db.execute('INSERT INTO notifications VALUES (?, ?, ?, ?, ?)',
                           (ident, event['time'], event['title'][:180], event['body'][:4000], image))
            self._prune()

    def get(self, ident=None):
        with self.lock:
            self._prune()
            with self.devices.connect() as db:
                if ident is None:
                    row = db.execute('SELECT * FROM notifications ORDER BY created DESC LIMIT 1').fetchone()
                else:
                    row = db.execute('SELECT * FROM notifications WHERE id=?', (ident,)).fetchone()
            if not row:
                return None
            return dict(id=row[0], created=row[1], title=row[2], body=row[3], image=bool(row[4]))

    def deliver(self, event, shots, only=None):
        title = ('Printer error' if event['kind'].startswith('error:') else
                 'Printer notification test' if event['kind'] == 'test' else
                 'Print monitor expired' if event['kind'] == 'expired' else
                 'Print completed' if event['detail'].startswith('completed;') else 'Print ended')
        body = f'{event["name"]}: {event["detail"]}'
        stored = dict(event, title=title, body=body + '\n' + event.get('capture', ''))
        self._remember(stored, shots)
        targets = self._subscribers(only)
        if not targets:
            raise RuntimeError('No enrolled device has push notifications enabled')
        failures = 0
        for device, hashed, subscription in targets:
            with self.devices.connect() as db:
                row = db.execute('SELECT status FROM push_deliveries WHERE event=? AND device=? AND endpoint_hash=?',
                                 (event['id'], device, hashed)).fetchone()
            if row and row[0] == 'accepted':
                continue
            try:
                # Push destinations are allowlisted and redirects are never followed.
                with NoRedirectSession() as session:
                    self.sender(subscription_info=subscription,
                        data=json.dumps(dict(title=title, body=body[:650], tag=event['id'],
                                             url='/?event=' + event['id'])),
                        vapid_private_key=str(self.key),
                        vapid_claims={'sub': self.origin}, timeout=10,
                        ttl=3600, headers={'Urgency': 'high' if event['kind'].startswith('error:') else 'normal'},
                        requests_session=session)
            except WebPushException as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status in (404, 410):
                    # Drop only the exact endpoint that expired, never a concurrently replaced one.
                    with self.devices.connect() as db:
                        db.execute('DELETE FROM push_subscriptions WHERE device=? AND endpoint_hash=?', (device, hashed))
                failures += 1
                print('Web Push delivery failed:', status or 'transport', flush=True)
            except Exception as exc:
                failures += 1
                print('Web Push delivery failed:', type(exc).__name__, flush=True)
            else:
                with self.devices.connect() as db:
                    db.execute('INSERT OR REPLACE INTO push_deliveries VALUES (?, ?, ?, ?)',
                               (event['id'], device, hashed, 'accepted'))
        if failures:
            raise RuntimeError('One or more push deliveries failed')

    def test(self, device, camera):
        with self.lock:
            now = self.clock()
            self.test_times = {key: value for key, value in self.test_times.items() if now - value < 60}
            if device in self.test_times:
                raise ValueError('Please wait a minute before another test')
            if not self.enabled(device):
                raise ValueError('Enable notifications first')
            self.test_times[device] = now
        def send():
            ident = uuid.uuid4().hex
            event = dict(id=ident, name='Test only', kind='test', time=now,
                         detail='Requested test notification. No printer error or completion is implied.')
            photo = self.photos / (ident + '-capture.jpg')
            shots = []
            try:
                camera(photo)
                shots = [photo]
                event['capture'] = 'Camera captured at ' + time.strftime('%Y-%m-%d %H:%M:%S %z')
            except Exception:
                event['capture'] = 'Photo unavailable; notification still sent.'
            try:
                self.deliver(event, shots, only=device)
            except Exception:
                print('Web Push test failed; check subscription/delivery status', flush=True)
            finally:
                photo.unlink(missing_ok=True)
        threading.Thread(target=send, daemon=True).start()
