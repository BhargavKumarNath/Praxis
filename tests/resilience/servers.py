"""A real local HTTP server standing in for an external data source (Phase 15).

The fetcher, httpx, sockets and timeouts are all real; only the remote host is local. Modes:

* ``ok``: answers 200 with ``body_for(path)``;
* ``stall``: accepts the request and never answers, so the client hits its read timeout
  (the handler waits on an event that ``stop`` sets; nothing in the TEST sleeps);
* ``drift``: answers 200 with ``drift_for(path)`` (a body that breaks the source contract).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Literal

Mode = Literal["ok", "stall", "drift"]
_STALL_LIMIT_S = 10.0  # upper bound on how long a stalled handler thread can linger


class SourceServer:
    def __init__(self, body_for: Callable[[str], bytes], drift_for: Callable[[str], bytes]) -> None:
        self.body_for = body_for
        self.drift_for = drift_for
        self.mode: Mode = "ok"
        self.requests: list[tuple[Mode, str]] = []
        self.release = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                mode = owner.mode
                owner.requests.append((mode, self.path))
                if mode == "stall":
                    owner.release.wait(_STALL_LIMIT_S)
                    return  # connection closed without a response
                body = owner.body_for(self.path) if mode == "ok" else owner.drift_for(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host!s}:{port}"

    def count(self, mode: Mode) -> int:
        return sum(1 for m, _ in self.requests if m == mode)


@contextmanager
def source_server(
    body_for: Callable[[str], bytes], drift_for: Callable[[str], bytes]
) -> Iterator[SourceServer]:
    server = SourceServer(body_for, drift_for)
    server.thread.start()
    try:
        yield server
    finally:
        server.release.set()
        server.httpd.shutdown()
        server.httpd.server_close()
        server.thread.join(timeout=_STALL_LIMIT_S)
