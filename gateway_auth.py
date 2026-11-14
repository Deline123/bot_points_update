"""Fail-closed HMAC gateway with shared PostgreSQL replay protection."""
import hashlib
import hmac
import os
import time
from flask import jsonify, request

WINDOW = 60

def signature(secret, method, path, query, timestamp, nonce, body):
    digest = hashlib.sha256(body).hexdigest()
    msg = '\n'.join((method.upper(), path, query, timestamp, nonce, digest))
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()

def install_gateway_auth(app):
    @app.before_request
    def verify():
        # Public liveness only; no user data, configuration, or mutations.
        if request.path == '/healthz' and request.method == 'GET':
            return None
        secret = os.environ.get('BOT_POINTS_GATEWAY_SECRET', '')
        if len(secret) < 32:
            return jsonify(error='Gateway not configured'), 503
        timestamp = request.headers.get('X-Points-Timestamp', '')
        nonce = request.headers.get('X-Points-Nonce', '')
        signed = request.headers.get('X-Points-Signature', '')
        if not timestamp.isdecimal() or len(timestamp) > 15 or not 16 <= len(nonce) <= 128 or len(signed) != 64:
            return jsonify(error='Unauthorized'), 401
        now = int(time.time())
        if abs(now - int(timestamp)) > WINDOW:
            return jsonify(error='Unauthorized'), 401
        expected = signature(secret, request.method, request.path, request.query_string.decode('latin-1'), timestamp, nonce, request.get_data(cache=True))
        if not hmac.compare_digest(expected, signed):
            return jsonify(error='Unauthorized'), 401
        if not os.environ.get('DATABASE_URL'):
            return jsonify(error='Replay protection unavailable'), 503
        try:
            from neon_store import connect
            with connect() as db:
                db.execute('DELETE FROM gateway_nonces WHERE expires_at < %s', (now,))
                row = db.execute('INSERT INTO gateway_nonces(nonce, expires_at) VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING nonce', (nonce, now + WINDOW)).fetchone()
                if row is None:
                    return jsonify(error='Replay rejected'), 401
        except Exception:
            app.logger.exception('Gateway nonce verification failed')
            return jsonify(error='Gateway unavailable'), 503

def sign_gateway_request(secret, method, path, query, body, *, nonce, timestamp):
    return {'X-Points-Timestamp': str(timestamp), 'X-Points-Nonce': nonce,
            'X-Points-Signature': signature(secret, method, path, query, str(timestamp), nonce, body)}
