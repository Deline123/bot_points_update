"""Render readiness endpoint for standalone Bot Points deployment.

Until the audited v3.9.9 source and persistent storage migration are present,
no Bot Points operations are exposed. Fail closed instead of exposing the
original localhost-only endpoints publicly.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = json.dumps({"service":"bot-points","status":"setup_required",
                               "ready":False}).encode()
            self.send_response(200)
        else:
            body = b'Bot Points is not yet configured for safe public deployment.'
            self.send_response(503)
        self.send_header("Content-Type", "application/json" if self.path=="/health" else "text/plain; charset=utf-8")
        self.send_header("Cache-Control","no-store")
        self.send_header("Content-Length",str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_POST(self):
        self.send_response(503)
        self.send_header("Content-Length","0")
        self.end_headers()
    def log_message(self, fmt, *args):
        pass

if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0",int(os.environ.get("PORT","10000"))),Handler).serve_forever()
