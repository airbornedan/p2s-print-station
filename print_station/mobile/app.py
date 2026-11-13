"""A separate Flask application: no routes can control the printer."""
import secrets
import threading
import time
from collections import defaultdict
from flask import Flask, request, jsonify, render_template, Response, g, send_from_directory, abort
from .auth import Devices, COOKIE


def create_app(cache, cameras, devices=None, origin='https://print.example.com', push=None, on_view=None):
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 4096
    devices = devices or Devices()
    attempts = defaultdict(list)
    rate_lock = threading.Lock()
    public = {'index', 'static', 'enroll', 'worker'}

    @app.before_request
    def authorize():
        if request.host != origin.split('://', 1)[1]:
            return jsonify(error='Invalid host'), 400
        if request.headers.get('Origin') not in (None, origin):
            return jsonify(error='Cross-origin request refused'), 403
        if request.method == 'POST' and request.headers.get('Origin') != origin:
            return jsonify(error='Same-origin request required'), 403
        if request.headers.get('Sec-Fetch-Site') == 'cross-site':
            return jsonify(error='Cross-site request refused'), 403
        if request.endpoint is None:
            return jsonify(error='Not found'), 404
        if request.endpoint in public:
            return None
        g.device = devices.validate(request.cookies.get(COOKIE))
        if not g.device:
            return jsonify(error='Device enrollment required'), 401
        if request.method == 'POST' and not secrets.compare_digest(
                request.headers.get('X-CSRF-Token', ''), g.device['csrf']):
            return jsonify(error='Invalid request token'), 403

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "connect-src 'self'; worker-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        return response

    @app.get('/')
    def index():
        return render_template('index.html')

    @app.post('/api/enroll')
    def enroll():
        now = time.monotonic()
        # REMOTE_ADDR is deliberately used, not an untrusted forwarded header.
        with rate_lock:
            for key in list(attempts):
                attempts[key] = [t for t in attempts[key] if now - t < 600]
                if not attempts[key]:
                    del attempts[key]
            key = request.remote_addr
            if len(attempts[key]) >= 10 or sum(map(len, attempts.values())) >= 50:
                return jsonify(error='Too many attempts; wait ten minutes'), 429
            attempts[key].append(now)
        data = request.get_json(silent=True) or {}
        code, name = data.get('code'), data.get('name', 'iPhone')
        if not isinstance(code, str) or not isinstance(name, str) or len(code) > 100:
            return jsonify(error='Invalid enrollment'), 400
        token = devices.enroll(code, name)
        if not token:
            return jsonify(error='Invalid or expired enrollment code'), 403
        response = jsonify(ok=True)
        response.set_cookie(COOKIE, token, secure=True, httponly=True, samesite='Strict', max_age=90 * 86400, path='/')
        return response

    @app.get('/api/session')
    def session():
        response = jsonify(csrf=g.device['csrf'])
        age = max(0, min(90 * 86400, int(g.device['expires'] - time.time())))
        response.set_cookie(COOKIE, request.cookies[COOKIE], secure=True, httponly=True,
                            samesite='Strict', max_age=age, path='/')
        return response

    @app.post('/api/logout')
    def logout():
        if push:
            push.unsubscribe(g.device['id'])
        devices.revoke(g.device['id'])
        cameras.release_owner(g.device['id'])
        response = jsonify(ok=True)
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite='Strict', path='/')
        return response

    @app.get('/api/status')
    def status():
        if on_view:
            on_view()   # someone is looking: the station may fetch a fresh report
        return jsonify(cache.snapshot())

    @app.post('/api/viewer')
    def viewer():
        data = request.get_json(silent=True) or {}
        source = data.get('source')
        if source not in ('bambu', 'webcam'):
            return jsonify(error='Unknown camera'), 400
        try:
            key = cameras.acquire(g.device['id'], source)
        except ValueError:
            return jsonify(error='Too many camera viewers'), 429
        return jsonify(viewer=key)

    @app.post('/api/viewer/<key>/renew')
    def renew(key):
        if not cameras.renew(key, g.device['id']):
            return jsonify(error='Viewer expired'), 410
        return jsonify(ok=True)

    @app.post('/api/viewer/<key>/release')
    def release(key):
        cameras.release(key, g.device['id'])
        return jsonify(ok=True)

    @app.get('/api/camera/<key>')
    def camera(key):
        # Identifier alone grants no access: device session and ownership are required.
        if not cameras.renew(key, g.device['id']):
            return jsonify(error='Viewer expired'), 410
        owner, token = g.device['id'], request.cookies[COOKIE]
        return Response(cameras.frames(key, owner, fps=1, valid=lambda: devices.validate(token) is not None),
                        mimetype='multipart/x-mixed-replace; boundary=frame',
                        headers={'X-Accel-Buffering': 'no'})

    @app.get('/api/camera-health')
    def camera_health():
        return jsonify({source: cameras.health(source) for source in ('bambu', 'webcam')})

    @app.get('/sw.js')
    def worker():
        return send_from_directory(app.static_folder, 'sw.js', mimetype='text/javascript')

    @app.get('/api/push')
    def push_info():
        if not push:
            return jsonify(error='Push is not configured'), 503
        return jsonify(public_key=push.public_key, enabled=push.enabled(g.device['id']))

    @app.post('/api/push/subscribe')
    def subscribe():
        if not push:
            return jsonify(error='Push is not configured'), 503
        try:
            push.subscribe(g.device['id'], request.get_json(silent=True))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(ok=True)

    @app.post('/api/push/unsubscribe')
    def unsubscribe():
        if push:
            push.unsubscribe(g.device['id'])
        return jsonify(ok=True)

    @app.post('/api/push/test')
    def test_push():
        if not push:
            return jsonify(error='Push is not configured'), 503
        try:
            push.test(g.device['id'], cameras.snapshot)
        except ValueError as exc:
            return jsonify(error=str(exc)), 429
        return jsonify(ok=True), 202

    @app.get('/api/notifications/latest')
    def latest_notification():
        return jsonify(push.get() if push else None)

    @app.get('/api/notifications/<ident>')
    def notification(ident):
        value = push.get(ident) if push else None
        if not value:
            abort(404)
        return jsonify(value)

    @app.get('/api/notifications/<ident>/photo')
    def notification_photo(ident):
        value = push.get(ident) if push else None
        if not value or not value['image']:
            abort(404)
        return send_from_directory(push.photos, ident + '.jpg', mimetype='image/jpeg')

    return app
