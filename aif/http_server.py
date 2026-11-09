"""MCP Streamable HTTP transport, standard library only.

One endpoint (`/mcp`) so several devices and models can share one store. Each client session gets its
own `Server` (and `Memory`, so writes are credited to that client); the store's file lock keeps
concurrent writers safe, and every session picks up the others' writes on its next read.

Replies are plain JSON. When a call emits notifications (tools/list_changed after a new topic or
person), the reply is a short SSE stream: the notifications, then the response.
"""
from __future__ import annotations

import hmac
import json
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from aif.mcp import Server
from aif.ops import Memory
from aif.store import Store

MAX_SESSIONS = 256
SESSION_IDLE = 7 * 24 * 3600
MAX_BODY = 4 * 1024 * 1024


class _Buffer:
    """Collects what a Server would have written to stdout during one request."""

    def __init__(self):
        self.lines: list[str] = []

    def write(self, text: str) -> None:
        self.lines.extend(line for line in text.splitlines() if line)

    def flush(self) -> None:
        pass


class Sessions:
    def __init__(self, store: Store, clearance: str, contributor: str):
        self.store, self.clearance, self.contributor = store, clearance, contributor
        self._sessions: dict[str, tuple[Server, threading.Lock, float]] = {}
        self._lock = threading.Lock()

    def open(self, sid: str | None) -> tuple[str, Server, threading.Lock]:
        """The session for `sid`, or a fresh one. Unknown ids (e.g. after a restart) get a fresh session
        under the same id rather than a 404, so clients keep working without re-initializing."""
        now = time.monotonic()
        with self._lock:
            if sid and sid in self._sessions:
                server, lock, _ = self._sessions[sid]
            else:
                sid = sid or secrets.token_hex(16)
                server = Server(Memory(self.store, clearance=self.clearance, contributor=self.contributor))
                lock = threading.Lock()
                if len(self._sessions) >= MAX_SESSIONS:
                    self._expire(now, force=True)
            self._sessions[sid] = (server, lock, now)
        return sid, server, lock

    def close(self, sid: str) -> bool:
        with self._lock:
            return self._sessions.pop(sid, None) is not None

    def _expire(self, now: float, force: bool = False) -> None:
        idle = sorted(self._sessions, key=lambda s: self._sessions[s][2])
        for sid in idle:
            if force or now - self._sessions[sid][2] > SESSION_IDLE:
                del self._sessions[sid]
                force = False


def make_handler(sessions: Sessions, token: str | None, path: str = "/mcp"):
    class Handler(BaseHTTPRequestHandler):
        server_version = "aif"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

        def _reply(self, status: int, body: bytes = b"", ctype: str = "application/json", headers=None):
            self.send_response(status)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            if body:
                self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _error(self, status: int, message: str, code: int = -32600):
            body = json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}})
            self._reply(status, body.encode())

        def _allowed(self) -> bool:
            if self.path.split("?", 1)[0] != path:
                self._reply(404)
                return False
            if token is None:
                return True
            got = self.headers.get("Authorization", "")
            if got.startswith("Bearer ") and hmac.compare_digest(got[7:].strip(), token):
                return True
            self._reply(401, headers={"WWW-Authenticate": 'Bearer realm="aif"'})
            return False

        def do_GET(self):
            if self.path == "/health":
                return self._reply(200, b'{"ok":true}')
            if self._allowed():
                # No server-initiated stream: notifications ride along with the call that caused them.
                self._reply(405, headers={"Allow": "POST, DELETE"})

        def do_DELETE(self):
            if self._allowed():
                sid = self.headers.get("Mcp-Session-Id")
                self._reply(200 if sid and sessions.close(sid) else 404)

        def do_POST(self):
            if not self._allowed():
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._error(413, "Request too large")
            try:
                msg = json.loads(self.rfile.read(length) or b"null")
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self._error(400, "Parse error", -32700)
            batch = msg if isinstance(msg, list) else [msg]
            if not batch or not all(isinstance(x, dict) for x in batch):
                return self._error(400, "Invalid request")

            sid = self.headers.get("Mcp-Session-Id")
            if any(x.get("method") == "initialize" for x in batch):
                sid = None
            sid, server, lock = sessions.open(sid)
            out = _Buffer()
            with lock:
                server.out = out
                replies = [r for r in (server.handle(x) for x in batch) if r is not None]
            headers = {"Mcp-Session-Id": sid}
            if not replies:
                return self._reply(202, headers=headers)
            payload = replies if isinstance(msg, list) else replies[0]
            if not out.lines:
                return self._reply(200, json.dumps(payload, ensure_ascii=False).encode(), headers=headers)
            events = [*out.lines, json.dumps(payload, ensure_ascii=False)]
            body = "".join(f"event: message\ndata: {e}\n\n" for e in events).encode()
            self._reply(200, body, "text/event-stream", {**headers, "Cache-Control": "no-cache"})

    return Handler


def serve_http(store: Store, host: str, port: int, *, token: str | None, clearance: str = "personal",
               contributor: str = "unknown") -> None:
    sessions = Sessions(store, clearance, contributor)
    httpd = ThreadingHTTPServer((host, port), make_handler(sessions, token))
    httpd.daemon_threads = True
    auth = "bearer token" if token else "NO AUTH"
    print(f"aif: MCP over HTTP at http://{host}:{port}/mcp ({auth}, store {store.home})", file=sys.stderr, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
