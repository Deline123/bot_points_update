import os
import secrets
import time
import unittest
from unittest.mock import patch
from flask import Flask
from gateway_auth import install_gateway_auth, sign_gateway_request


class GatewayAuthTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        install_gateway_auth(app)
        app.add_url_rule("/api/points", "points", lambda: "ok", methods=["GET", "POST"])
        self.client = app.test_client()
        self.secret = "S" * 40

    def test_no_secret_fail_closed(self):
        with patch.dict(os.environ, {"BOT_POINTS_GATEWAY_SECRET": ""}):
            self.assertEqual(self.client.get("/api/points").status_code, 503)

    def test_unsigned_request_denied(self):
        with patch.dict(os.environ, {"BOT_POINTS_GATEWAY_SECRET": self.secret}):
            self.assertEqual(self.client.get("/api/points").status_code, 401)

    def test_signed_request_and_replay(self):
        nonce = secrets.token_hex(16)
        headers = sign_gateway_request(self.secret, "POST", "/api/points",
                                       "", b"{}", nonce=nonce, timestamp=int(time.time()))
        with patch.dict(os.environ, {"BOT_POINTS_GATEWAY_SECRET": self.secret}):
            self.assertEqual(self.client.post("/api/points", data=b"{}", headers=headers).status_code, 200)
            self.assertEqual(self.client.post("/api/points", data=b"{}", headers=headers).status_code, 401)

    def test_body_tamper_denied(self):
        headers = sign_gateway_request(self.secret, "POST", "/api/points",
                                       "", b"{}", nonce=secrets.token_hex(16),
                                       timestamp=int(time.time()))
        with patch.dict(os.environ, {"BOT_POINTS_GATEWAY_SECRET": self.secret}):
            self.assertEqual(self.client.post("/api/points", data=b"{ }", headers=headers).status_code, 401)


if __name__ == "__main__":
    unittest.main()
