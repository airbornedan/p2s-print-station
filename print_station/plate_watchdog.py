"""Station-sent print notifications; no evaluator, cron or printer controls."""
import json
import os
import smtplib
import subprocess
import sys
import threading
import time
import tomllib
import uuid
from email.message import EmailMessage
from pathlib import Path

STATE = Path.home() / '.local/state/print_station/monitor'
ACTIVE = {'PREPARE', 'SLICING', 'RUNNING', 'PAUSE'}
TERMINAL = {'FINISH', 'FAILED', 'STOP', 'CANCEL', 'IDLE'}


def email(subject, body, shots):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools/bambu'))
    import siteconf
    with open(siteconf.path('mail_config'), 'rb') as config:
        m = tomllib.load(config)['mail']
    msg = EmailMessage()
    msg['From'], msg['To'], msg['Subject'] = m['from_address'], m['to_address'], subject
    msg.set_content(body)
    for shot in shots:
        msg.add_attachment(shot.read_bytes(), maintype='image', subtype='jpeg', filename=shot.name)
    with smtplib.SMTP(m['smtp_host'], m['smtp_port'], timeout=30) as smtp:
        smtp.starttls()
        # Some relays log in with a technical user separate from the From address.
        smtp.login(m.get('smtp_user', m['from_address']), m.get('smtp_password', m.get('app_password')))
        smtp.send_message(msg)


def capture(path):
    script = Path(__file__).resolve().parent.parent / 'tools/bambu/cam.py'
    subprocess.run([sys.executable, str(script), 'snap', str(path)],
                   check=True, capture_output=True, timeout=15)
    if not path.is_file() or not path.stat().st_size:
        raise RuntimeError('No camera image')


class PrintMonitor:
    def __init__(self, state_file=None, clock=time.time, schedule=True,
                 camera=capture, mail=email, notify=None):
        self.file = Path(state_file) if state_file else STATE / 'state.json'
        self.clock, self.schedule, self.camera, self.mail = clock, schedule, camera, mail
        self.notify = notify
        self.lock = threading.RLock()
        self.timer = None
        self.busy = False
        try:
            self.state = json.loads(self.file.read_text())
        except (OSError, ValueError):
            self.state = {'job': None, 'outbox': []}
        # Events that exhausted their retries before give-up existed would poll forever.
        self.state['outbox'] = [e for e in self.state['outbox'] if e['attempts'] < 3]
        self._queue()

    def _save(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        stage = self.file.with_suffix('.tmp')
        stage.write_text(json.dumps(self.state))
        os.replace(stage, self.file)

    def _queue(self):
        work = self.state.get('job') or self.state['outbox']
        if self.schedule and work and self.timer is None and not self.busy:
            self.timer = threading.Timer(5, self.check)
            self.timer.daemon = True
            self.timer.start()

    def expect(self, name):
        """Called after upload, before start: only this station submission may arm."""
        with self.lock:
            now = self.clock()
            current = self.state.get('job')
            if current and current['phase'] == 'pending' and now < current['deadline']:
                raise RuntimeError('Another print submission is starting')
            token = uuid.uuid4().hex
            # An active job stays as 'previous' so a refused start can restore it.
            previous = current if current and current['phase'] == 'active' and now < current['deadline'] else None
            self.state['job'] = dict(token=token, name=name.replace('.gcode.3mf', ''),
                                     phase='pending', deadline=now + 120, seen=[], previous=previous)
            self._save()
            self._queue()
            return token

    def cancel(self, token):
        """The printer refused the start: drop the pending job and resume any previous one."""
        with self.lock:
            job = self.state.get('job')
            if job and job['token'] == token and job['phase'] == 'pending':
                self.state['job'] = job.get('previous')
                self._save()

    def _unobserved_end(self, job, why):
        self._event(job, 'end', f'ended; outcome not observed ({why}).')

    def _event(self, job, kind, detail, delay=0):
        if kind in job['seen']:
            return
        job['seen'].append(kind)
        now = self.clock()
        self.state['outbox'].append(dict(id=uuid.uuid4().hex, name=job['name'],
            kind=kind, detail=detail, token=job['token'], time=now, due=now + delay, attempts=0))

    def observe(self, report):
        with self.lock:
            job = self.state.get('job')
            if not job or self.clock() >= job['deadline']:
                return
            name = report.get('subtask_name')
            if (job['phase'] == 'active' and report.get('gcode_state') in ACTIVE and name
                    and name != job['name'] and report.get('task_id') is not None):
                # The printer runs one job at a time: another job running means ours ended unseen.
                self._unobserved_end(job, 'another print started')
                self.state['job'] = None
                self._save()
                self._queue()
                return
            # Raw, identified report only: merged temperatures cannot freshen old errors.
            if (report.get('subtask_name') != job['name'] or report.get('task_id') is None
                    or 'gcode_state' not in report):
                return
            state = report['gcode_state']
            task = str(report['task_id'])
            if job['phase'] == 'pending':
                if state not in ACTIVE:
                    return
                job.update(phase='active', task=task, deadline=self.clock() + 86400)
                previous = job.pop('previous', None)
                if previous:
                    self._unobserved_end(previous, 'a new print started')
            if task != job['task']:
                return
            progress = report.get('mc_percent', 'unknown')
            if state in TERMINAL:
                outcome = 'completed' if state == 'FINISH' else 'ended without confirmed success'
                self._event(job, 'end', f'{outcome}; printer state {state}; progress {progress}%; '
                            f'print_error {report.get("print_error", "not reported")}',
                            5 if state == 'FINISH' else 0)
                self.state['job'] = None
            else:
                error = report.get('print_error')
                hms = report.get('hms')
                if error not in (None, 0, '0', '') or hms:
                    signature = json.dumps([error, hms], sort_keys=True)
                    self._event(job, 'error:' + signature,
                                f'Printer state {state}; progress {progress}%; error/HMS {signature}')
            self._save()
            self._queue()

    def check(self):
        with self.lock:
            self.timer = None
            if self.busy:
                return
            self.busy = True
        try:
            self._check()
        finally:
            with self.lock:
                self.busy = False
                self._queue()

    def _check(self):
        with self.lock:
            now = self.clock()
            job = self.state.get('job')
            if job and now >= job['deadline']:
                if job['phase'] == 'active':
                    self._event(job, 'expired', 'Monitoring expired after 24 hours; completion unknown.')
                self.state['job'] = None
                self._save()
            event = next((e for e in self.state['outbox'] if e['attempts'] < 3 and e['due'] <= now), None)
            if event is None:
                return
            event['attempts'] += 1
            event['due'] = now + 60
            self._save()
        path = self.file.parent / (event['id'] + '.jpg')
        # Never retry a capture later against a potentially different job/cleared bed.
        if 'capture' not in event:
            captured = time.strftime('%Y-%m-%d %H:%M:%S %z', time.localtime(self.clock()))
            try:
                with self.lock:
                    current = self.state.get('job')
                    if self.clock() - event['time'] > 60 or (current and current['token'] != event['token']):
                        raise RuntimeError('Event photo no longer current')
                self.camera(path)
                event['capture'] = f'Camera captured at {captured}.'
                event['photo'] = str(path)
            except Exception as exc:
                event['capture'] = f'Photo unavailable at {captured} ({type(exc).__name__}).'
            with self.lock:
                self._save()
        try:
            shots = [Path(event['photo'])] if event.get('photo') and Path(event['photo']).is_file() else []
            pushed = False
            if self.notify is not None:
                try:
                    self.notify(event, shots)
                    pushed = True
                except Exception as exc:
                    print('Push delivery failed; falling back to email:', type(exc).__name__, flush=True)
            if not pushed:
                self.mail(f'Printer {"error" if event["kind"].startswith("error:") else event["kind"]}: {event["name"]}',
                          f'Job: {event["name"]}\nEvent time: {event["time"]}\n{event["detail"]}\n{event["capture"]}', shots)
        except Exception as exc:
            with self.lock:
                event['delivery_error'] = type(exc).__name__
                gave_up = event['attempts'] >= 3
                if gave_up:
                    self.state['outbox'].remove(event)
                self._save()
            print('Print notification delivery failed:', type(exc).__name__,
                  '(giving up)' if gave_up else '', flush=True)
            if gave_up:
                path.unlink(missing_ok=True)
        else:
            with self.lock:
                self.state['outbox'].remove(event)
                self._save()
            path.unlink(missing_ok=True)
