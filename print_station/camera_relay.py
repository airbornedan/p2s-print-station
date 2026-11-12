"""Shared on-demand camera readers with expiring viewer leases and bounded I/O."""
import os
import secrets
import selectors
import subprocess
import threading
import time
import urllib.request
import uuid


def split_jpegs(buf):
    frames = []
    while True:
        a = buf.find(b'\xff\xd8')
        b = buf.find(b'\xff\xd9', a + 2) if a >= 0 else -1
        if b < 0:
            # Bound corrupt input; a valid single JPEG here is well below 8 MiB.
            return frames, buf[-8 * 1024 * 1024:]
        frames.append(buf[a:b + 2])
        buf = buf[b + 2:]


def part(jpg):
    return b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ' + str(len(jpg)).encode() + b'\r\n\r\n' + jpg + b'\r\n'


class FFmpegReader:
    def __init__(self, command, log=None):
        self.proc = subprocess.Popen(command, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE if log else subprocess.DEVNULL, bufsize=0)
        if log:
            threading.Thread(target=log, args=(self.proc.stderr,), daemon=True).start()
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.proc.stdout, selectors.EVENT_READ)

    def read(self):
        if not self.selector.select(0.5):
            return None
        data = os.read(self.proc.stdout.fileno(), 65536)
        if not data:
            raise OSError('Camera connection ended')
        return data

    def close(self):
        self.selector.close()
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc.stdout.close()


class WebcamReader:
    skip_frames = 10   # the first frames after the camera opens are often corrupt

    def __init__(self, url):
        self.url, self.session = url, uuid.uuid4().hex
        self.stop = threading.Event()
        self.response = urllib.request.urlopen(f'{url}/stream?session={self.session}', timeout=2)
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.thread.start()

    def _heartbeat(self):
        while not self.stop.wait(5):
            try:
                urllib.request.urlopen(f'{self.url}/heartbeat?session={self.session}', timeout=2).close()
            except Exception:
                pass

    def read(self):
        data = self.response.read1(65536)
        if not data:
            raise OSError('Webcam connection ended')
        return data

    def close(self):
        self.stop.set()
        self.response.close()
        self.thread.join(timeout=3)
        try:
            urllib.request.urlopen(f'{self.url}/stop?session={self.session}', timeout=2).close()
        except Exception:
            pass


class CameraHub:
    TTL = 20
    MAX_VIEW = 600      # a viewer's stream ends after 10 minutes: long sessions are unattended and may wedge the printer camera
    GIVE_UP = 3         # failed connects in a row before the reader stops; a viewer's Reconnect starts it again

    def __init__(self, factories, clock=time.monotonic):
        self.factories, self.clock = factories, clock
        self.cond = threading.Condition(threading.RLock())
        self.leases = {}
        self.sources = {name: dict(frame=None, at=None, seq=0, running=False, error=None, stopped=False)
                        for name in factories}

    def _prune(self):
        now = self.clock()
        for key in list(self.leases):
            lease = self.leases[key]
            if lease['until'] <= now or now - lease['started'] >= self.MAX_VIEW:
                del self.leases[key]

    def acquire(self, owner, source):
        with self.cond:
            self._prune()
            if source not in self.sources:
                raise ValueError('Unknown camera')
            if len(self.leases) >= 12 or sum(v['owner'] == owner for v in self.leases.values()) >= 2:
                raise ValueError('Too many camera viewers')
            key = secrets.token_urlsafe(24)
            now = self.clock()
            self.leases[key] = dict(owner=owner, source=source, until=now + self.TTL, started=now)
            self.sources[source]['stopped'] = False   # a new viewer (Reconnect, refresh) tries again
            self._ensure(source)
            return key

    def _ensure(self, source):
        state = self.sources[source]
        if not state['running'] and not state['stopped']:
            state['running'] = True
            threading.Thread(target=self._run, args=(source,), daemon=True).start()

    def renew(self, key, owner):
        with self.cond:
            self._prune()
            lease = self.leases.get(key)
            if not lease or lease['owner'] != owner:
                return False
            lease['until'] = self.clock() + self.TTL
            self._ensure(lease['source'])
            return True

    def release(self, key, owner):
        with self.cond:
            if self.leases.get(key, {}).get('owner') == owner:
                del self.leases[key]
                self.cond.notify_all()

    def release_owner(self, owner):
        with self.cond:
            for key in list(self.leases):
                if self.leases[key]['owner'] == owner:
                    del self.leases[key]
            self.cond.notify_all()

    def _wanted(self, source):
        with self.cond:
            self._prune()
            return any(v['source'] == source for v in self.leases.values())

    def health(self, source):
        with self.cond:
            s = self.sources[source]
            age = None if s['at'] is None else max(0, self.clock() - s['at'])
            return dict(running=s['running'], frame_age_seconds=age,
                        state='live' if s['running'] and age is not None and age < 5 else
                        'stopped' if s['stopped'] else
                        'unavailable' if s['error'] else 'connecting' if s['running'] else 'off')

    def _run(self, source):
        state = self.sources[source]
        delay, failures = 1, 0
        try:
            while self._wanted(source):
                reader = None
                try:
                    reader = self.factories[source]()
                    buf = b''
                    skip = getattr(reader, 'skip_frames', 0)
                    last = self.clock()
                    while self._wanted(source):
                        chunk = reader.read()
                        if chunk:
                            frames, buf = split_jpegs(buf + chunk)
                            if skip:
                                dropped = min(skip, len(frames))
                                frames, skip = frames[dropped:], skip - dropped
                                if dropped:
                                    last = self.clock()
                            if frames:
                                last = self.clock()
                                delay, failures = 1, 0
                                with self.cond:
                                    state.update(frame=frames[-1], at=last, seq=state['seq'] + 1, error=None)
                                    self.cond.notify_all()
                        if self.clock() - last > 5:
                            raise TimeoutError('No fresh camera frames')
                except Exception as exc:
                    failures += 1
                    with self.cond:
                        state['error'] = type(exc).__name__
                        self.cond.notify_all()
                finally:
                    if reader:
                        reader.close()
                if failures >= self.GIVE_UP:
                    with self.cond:
                        state['stopped'] = True
                    break
                end = self.clock() + delay
                while self._wanted(source) and self.clock() < end:
                    time.sleep(0.2)
                delay = min(15, delay * 2)
        finally:
            with self.cond:
                state.update(running=False, frame=None, at=None)
                # A lease may have arrived while the reader was closing.
                if self._wanted(source):
                    self._ensure(source)
                self.cond.notify_all()

    def snapshot(self, path, timeout=12):
        """Notification snapshots share the live reader; require a post-request frame."""
        owner = "snapshot-" + secrets.token_hex(8)
        started = self.clock()
        key = self.acquire(owner, 'bambu')
        try:
            with self.cond:
                state = self.sources['bambu']
                ready = self.cond.wait_for(
                    lambda: state['frame'] is not None and state['at'] >= started,
                    timeout=timeout)
                if not ready:
                    raise TimeoutError('No fresh printer image')
                image = state['frame']
            path.write_bytes(image)
        finally:
            self.release(key, owner)

    def frames(self, key, owner, fps=1, valid=lambda: True, auto_renew=False):
        seen = -1
        with self.cond:
            lease = self.leases.get(key)
            if not lease or lease['owner'] != owner or lease.get('streaming'):
                return
            lease['streaming'] = True
        try:
            while valid():
                with self.cond:
                    self._prune()
                    lease = self.leases.get(key)
                    if not lease or lease['owner'] != owner:
                        return
                    if auto_renew:
                        lease['until'] = self.clock() + self.TTL
                    s = self.sources[lease['source']]
                    self.cond.wait_for(lambda: s['seq'] != seen or key not in self.leases, timeout=3)
                    if key not in self.leases or s['stopped']:
                        return
                    fresh = s['frame'] is not None and self.clock() - s['at'] < 5 and s['seq'] != seen
                    seen = s['seq']
                    frame = s['frame'] if fresh else None
                # Whitespace makes broken downstream connections detectable even during upstream stalls.
                yield part(frame) if frame else b'\r\n'
                time.sleep(1 / fps)
        finally:
            self.release(key, owner)
