"""Stand-in for Slack/Discord/webhook endpoints in end-to-end tests (standard library only).

POST to any path records {path, body}; GET /messages returns everything recorded as JSON.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MESSAGES = []
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {"raw": raw.decode("utf-8", "replace")}
        with LOCK:
            MESSAGES.append({"path": self.path, "body": body})
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_GET(self):
        with LOCK:
            data = json.dumps(MESSAGES).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8099), Handler).serve_forever()
