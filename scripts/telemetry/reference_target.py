"""Reference loopback target for the LOCAL DEMO.

**Ownership:** Ananya supplies the real local target. This is a stand-in so the
telemetry acceptance gate can produce real-HTTP evidence before that lands, and
so the verifier's tests exercise a genuine socket rather than a mock. Swap it for
Ananya's target by pointing ``--target`` at hers; the verifier does not care
which one answers, only that ``/health`` and ``/_witness`` behave.

Why it counts mutations: the whole prevention claim is "nothing was dispatched".
The runtime can report its own invocation count, but that is self-reported. A
target that independently counts every mutating request it receives turns one
claim into two, and if the executor ever did dispatch something, this counter
catches it even if the runtime's own spy was wrong.

Endpoints:
    GET  /health    -> 200 {"status": "healthy"} | 503 when toggled unhealthy
    GET  /_witness  -> 200 {"mutation_requests": N, "requests": M}
    POST /pause     -> a *mutating* request; increments the witness counter
    POST /_unhealthy, POST /_healthy -> test controls

Binds to 127.0.0.1 only. Never expose this.
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

MUTATING_PATHS = frozenset({"/pause", "/kill", "/stop", "/delay"})


class _State:
    def __init__(self) -> None:
        self.healthy = True
        self.mutation_requests = 0
        self.requests = 0
        self.lock = threading.Lock()


STATE = _State()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # noqa: D102 - silence stderr spam
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # The client hung up mid-response (e.g. a probe timing out, or the
            # test that deliberately restarts this server). Not a target fault.
            self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802
        with STATE.lock:
            STATE.requests += 1
            healthy = STATE.healthy
            mutations = STATE.mutation_requests
            total = STATE.requests

        if self.path == "/health":
            if healthy:
                self._send(200, {"status": "healthy", "requests": total, "mutations_received": mutations})
            else:
                self._send(503, {"status": "unhealthy"})
        elif self.path == "/_witness":
            self._send(200, {"mutation_requests": mutations, "requests": total})
        else:
            self._send(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)

        with STATE.lock:
            STATE.requests += 1
            if self.path in MUTATING_PATHS:
                # A mutation reached the target. If this ever increments during
                # the prevention demo, prevention failed.
                STATE.mutation_requests += 1
                mutations = STATE.mutation_requests
                self._send(200, {"mutated": True, "mutation_requests": mutations})
                return
            if self.path == "/_unhealthy":
                STATE.healthy = False
                self._send(200, {"status": "unhealthy"})
                return
            if self.path == "/_healthy":
                STATE.healthy = True
                self._send(200, {"status": "healthy"})
                return
        self._send(404, {"error": "not_found"})


def serve(port: int = 8099) -> HTTPServer:
    server = HTTPServer(("127.0.0.1", port), Handler)
    return server


def serve_in_thread(port: int = 0) -> tuple[HTTPServer, threading.Thread, int]:
    """Start on an ephemeral port. Used by the test suite."""
    server = HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, server.server_address[1]


def reset_state() -> None:
    with STATE.lock:
        STATE.healthy = True
        STATE.mutation_requests = 0
        STATE.requests = 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()
    httpd = serve(args.port)
    print(f"LOCAL DEMO reference target on http://127.0.0.1:{args.port}")
    print("  GET /health    GET /_witness    POST /pause")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
