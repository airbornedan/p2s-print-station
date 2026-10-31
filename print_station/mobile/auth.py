"""Opaque, hashed device credentials stored outside the repository."""
import hashlib
import secrets
import sqlite3
import time
import os
from pathlib import Path

DEFAULT_DB = Path.home() / '.local/state/print_station/mobile/devices.sqlite'
COOKIE = '__Host-print-device'


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Devices:
    def __init__(self, path=DEFAULT_DB, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS enroll (hash TEXT PRIMARY KEY, expires REAL);
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY, name TEXT, hash TEXT UNIQUE, csrf TEXT,
                    created REAL, used REAL, revoked INTEGER DEFAULT 0);
            ''')

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def code(self):
        code = f'{secrets.randbelow(100_000_000):08d}'
        with self.connect() as db:
            # Only the newest locally requested enrollment code is valid.
            db.execute('DELETE FROM enroll')
            db.execute('INSERT INTO enroll VALUES (?, ?)', (digest(code), self.clock() + 600))
        return code

    def enroll(self, code, name):
        now = self.clock()
        token, csrf, ident = secrets.token_urlsafe(32), secrets.token_urlsafe(24), secrets.token_hex(8)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT expires FROM enroll WHERE hash=?', (digest(code),)).fetchone()
            if not row or row[0] < now:
                return None
            db.execute('DELETE FROM enroll WHERE hash=?', (digest(code),))
            db.execute('INSERT INTO devices VALUES (?, ?, ?, ?, ?, ?, 0)',
                       (ident, name[:60], digest(token), csrf, now, now))
        return token

    def validate(self, token):
        if not token or len(token) > 100:
            return None
        now = self.clock()
        with self.connect() as db:
            row = db.execute('SELECT id, csrf, created, used, revoked FROM devices WHERE hash=?',
                             (digest(token),)).fetchone()
            if not row or row[4] or now - row[2] >= 365 * 86400 or now - row[3] >= 90 * 86400:
                return None
            # Avoid SQLite writes on every frame/heartbeat.
            if now - row[3] >= 3600:
                db.execute('UPDATE devices SET used=? WHERE id=?', (now, row[0]))
            return dict(id=row[0], csrf=row[1], expires=row[2] + 365 * 86400)

    def list(self):
        with self.connect() as db:
            return db.execute('SELECT id, name, created, used, revoked FROM devices ORDER BY created').fetchall()

    def revoke(self, ident):
        with self.connect() as db:
            return db.execute('UPDATE devices SET revoked=1 WHERE id=?', (ident,)).rowcount
