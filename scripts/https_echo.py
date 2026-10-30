"""HTTPS server that answers every request with what it received. CI uses it to check that the
dashboard, built the Vercel way, passes requests to the API intact (dashboard job in ci.yml).

Usage: python3 scripts/https_echo.py PORT CERT.pem KEY.pem
"""
import json
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Echo(BaseHTTPRequestHandler):
    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        out = json.dumps(
            {"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = do_PUT = do_DELETE = _answer

    def log_message(self, *args) -> None:
        pass  # quiet: the CI log shows the checks instead


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Echo)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(sys.argv[2], sys.argv[3])
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
