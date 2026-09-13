"""Minimal HTTP health server for Kubernetes liveness/readiness probes."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer


class _HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return
        self.send_response(404)
        self.end_headers()


def main() -> None:
    HTTPServer(("0.0.0.0", 8000), _HealthHandler).serve_forever()


if __name__ == "__main__":
    main()
