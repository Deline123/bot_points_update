"""Service-to-service authentication for Bot Points.

Only StaffFlow should possess the shared secret. Reverse proxy forwards signed
requests; no browser-side JavaScript should ever receive the key.
"""
import hashlib
import hmac
import os
import threading
import time
from collections import OrderedDict
from flask import jsonify, request

_LOCK = threading.Lock()
_SEEN = OrderedDict()
_WINDOW = 60
_MAX_NONCES = 10000


def _signature(secret, method, path, query, timestamp, nonce, body):
    digest = hashlib.sha256(body).hexdigest()
    message = "\n".join((method.upper(), path, query, timestamp, nonce, digest))
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def install_gateway_auth(app):
    """Fail closed for every request when BOT_POINTS_GATEWAY_SECRET is absent."""
    @app.before_request
    def verify_gateway():
        secret = os.environ.get("BOT_POINTS_GATEWAY_SECRET", "")
        if len(secret) < 32:
            return jsonify(error="Gateway not configured"), 503
        stamp = request.headers.get("X-Points-Timestamp", "")
        nonce = request.headers.get("X-Points-Nonce", "")
        signature = request.headers.get("X-Points-Signature", "")
        if not stamp.isdecimal() or not 16 <= len(nonce) <= 128 or len(signature) != 64:
            return jsonify(error="Unauthorized"), 401
        now = int(time.time())
        if abs(now - int(stamp)) > _WINDOW:
            return jsonify(error="Unauthorized"), 401
        expected = _signature(secret, request.method, request.path,
                              request.query_string.decode("latin-1"),
                              stamp, nonce, request.get_data(cache=True))
        if not hmac.compare_digest(signature, expected):
            return jsonify(error="Unauthorized"), 401
        with _LOCK:
            while _SEEN:
                key, expiry = next(iter(_SEEN.items()))
                if expiry >= now and len(_SEEN) < _MAX_NONCES:
                    break
                _SEEN.popitem(last=False)
            if nonce in _SEEN:
                return jsonify(error="Replay rejected"), 401
            _SEEN[nonce] = now + _WINDOW


def sign_gateway_request(secret, method, path, query, body, *, nonce, timestamp):
    """For StaffFlow server-side proxy only; never call in browser."""
    return {
        "X-Points-Timestamp": str(timestamp),
        "X-Points-Nonce": nonce,
        "X-Points-Signature": _signature(
            secret, method, path, query, str(timestamp), nonce, body),
    }
