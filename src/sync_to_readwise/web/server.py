"""Stdlib HTTP glue around `StatusApp` — no third-party web dependency.

The daemon runs `BlockingScheduler` on the main thread, so the server runs on
a background daemon thread and exits with the process.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import structlog

from sync_to_readwise.web.app import StatusApp

log = structlog.get_logger(__name__)


class _Handler(BaseHTTPRequestHandler):
    server_version = "sync-to-readwise"

    # The owning server is a `_StatusServer`; declared for type clarity.
    server: _StatusServer

    def log_message(self, *args: object) -> None:
        # Suppress the default stderr access log — structlog already records
        # everything operationally interesting.
        pass

    def _respond(self, method: str) -> None:
        parsed = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        host = self.headers.get("Host", "localhost")
        resp = self.server.app.dispatch(method, parsed.path, query, host)
        self.send_response(resp.status)
        for key, value in resp.headers.items():
            self.send_header(key, value)
        self.send_header("Content-Type", resp.content_type)
        self.send_header("Content-Length", str(len(resp.body)))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(resp.body)

    def do_GET(self) -> None:
        self._respond("GET")

    def do_HEAD(self) -> None:
        self._respond("HEAD")


class _StatusServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: StatusApp) -> None:
        super().__init__(address, _Handler)
        self.app = app


def serve_in_thread(app: StatusApp, host: str, port: int) -> _StatusServer:
    """Bind the status server and serve it on a background daemon thread.

    Returns the server so callers can `shutdown()` it; the thread is a daemon,
    so it also dies with the process if shutdown is never called.
    """
    server = _StatusServer((host, port), app)
    thread = threading.Thread(target=server.serve_forever, name="status-web", daemon=True)
    thread.start()
    log.info("web_server_started", host=host, port=port)
    return server
