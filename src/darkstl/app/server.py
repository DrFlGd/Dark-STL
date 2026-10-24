"""A small web server on this computer that the app's window (or a browser) talks to.

It serves the interface in ``darkstl/web`` and answers its commands: POST
``/api/<command>`` with JSON arguments, and GET ``/bin/<command>?...`` for
meshes and pictures. It listens on 127.0.0.1 only and every command needs the
token written into the page, so other web pages can't use it.
"""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from .service import AppError, Service

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
TYPES = {".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml",
         ".html": "text/html; charset=utf-8", ".json": "application/json", ".png": "image/png",
         ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8"}


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, service: Service, port: int = 0, web_root: Path = WEB_ROOT):
        self.service = service
        self.token = secrets.token_urlsafe(24)
        self.web_root = web_root.resolve()
        super().__init__(("127.0.0.1", port), Handler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/"

    def start(self) -> "AppServer":
        threading.Thread(target=self.serve_forever, name="darkstl-server", daemon=True).start()
        return self


class Handler(BaseHTTPRequestHandler):
    server: AppServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet unless asked
        if os.environ.get("DARKSTL_DEBUG"):
            super().log_message(fmt, *args)

    # ----------------------------------------------------------------- checks

    def _allowed_host(self) -> bool:
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _token_ok(self, query: dict) -> bool:
        given = self.headers.get("X-DarkSTL-Token") or query.get("t", "")
        return secrets.compare_digest(given, self.server.token)

    # ----------------------------------------------------------------- replies

    def _send(self, status: int, body: bytes, content_type: str, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data) -> None:
        self._send(status, json.dumps(data).encode("utf-8"), "application/json")

    # ----------------------------------------------------------------- GET

    def do_GET(self):
        if not self._allowed_host():
            return self._send(403, b"Forbidden", "text/plain")
        url = urlsplit(self.path)
        path = unquote(url.path)
        query = dict(parse_qsl(url.query))
        if path.startswith("/bin/"):
            return self._bytes(path[5:], query)
        if path in ("/", "/index.html"):
            page = (self.server.web_root / "index.html").read_text("utf-8")
            page = page.replace('content="TOKEN"', f'content="{self.server.token}"', 1)
            return self._send(200, page.encode("utf-8"), TYPES[".html"])
        return self._static(path)

    do_HEAD = do_GET

    def _static(self, path: str) -> None:
        root = self.server.web_root
        target = (root / path.lstrip("/")).resolve()
        if root not in target.parents or not target.is_file():
            return self._send(404, b"Not found", "text/plain")
        ctype = TYPES.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype, cache="no-cache")

    def _bytes(self, cmd: str, query: dict) -> None:
        if not self._token_ok(query):
            return self._send(403, b"Forbidden", "text/plain")
        if cmd not in Service.BYTES:
            return self._send(404, b"Not found", "text/plain")
        try:
            data = self.server.service.call(cmd, query)
        except AppError as exc:
            return self._send(400, str(exc).encode("utf-8"), "text/plain; charset=utf-8")
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._send(500, f"{type(exc).__name__}: {exc}".encode("utf-8"), "text/plain; charset=utf-8")
        if data is None:
            return self._send(404, b"Nothing to show", "text/plain")
        ctype = "image/png" if data[:4] == b"\x89PNG" else "application/octet-stream"
        # previews are keyed by the file's date, so the page may keep them a while (not "not drawn yet")
        self._send(200, data, ctype, cache="private, max-age=3600" if cmd == "thumb" and data else "no-store")

    # ----------------------------------------------------------------- POST

    def do_POST(self):
        if not self._allowed_host():
            return self._send(403, b"Forbidden", "text/plain")
        url = urlsplit(self.path)
        if not url.path.startswith("/api/"):
            return self._send(404, b"Not found", "text/plain")
        if not self._token_ok({}):
            return self._json(403, {"error": "This page isn't connected to Dark STL any more. Reload it."})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            args = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except ValueError:
            return self._json(400, {"error": "Bad request"})
        cmd = url.path[5:]
        if cmd in Service.BYTES:
            return self._json(400, {"error": f"{cmd} is read with GET /bin/{cmd}"})
        try:
            result = self.server.service.call(cmd, args)
        except AppError as exc:
            return self._json(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
        self._json(200, {"result": result})
