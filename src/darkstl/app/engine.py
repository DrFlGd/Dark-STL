"""The engine as the desktop app (``desktop/``, Tauri) runs it: the commands the local server
answers (service.py), read from stdin and answered on stdout, so the app opens no port.

A request is one line of JSON, ``{"id": 7, "cmd": "overview", "args": {...}}``, with
``"bin": true`` for the commands that answer with bytes (meshes and pictures). The answer is
one line of JSON, ``{"id": 7, "result": ...}`` or ``{"id": 7, "error": "...", "status": 400}``,
and for bytes ``{"id": 7, "size": n, "type": "image/png"}`` followed by the n bytes. Answers
come as each request finishes, not in order. The engine stops when stdin closes.

The app's window shows the open dialogs itself, so here they only need to be said to exist.
"""

from __future__ import annotations

import faulthandler
import json
import os
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import BinaryIO

from .config import Config, cache_dir
from .service import AppError, Host, Service


class WindowHost(Host):
    dialogs = True  # pick_folder and pick_file are answered by the window (desktop/src/main.rs)


def answer(service: Service, request: dict) -> tuple[dict, bytes | None]:
    rid = request.get("id")
    cmd = str(request.get("cmd") or "")
    binary = bool(request.get("bin"))
    try:
        if binary != (cmd in Service.BYTES):
            raise AppError(f"{cmd} is asked for {'as JSON' if cmd in Service.BYTES else 'as bytes'}")
        result = service.call(cmd, request.get("args") or {})
    except AppError as exc:
        return {"id": rid, "error": str(exc), "status": 400}, None
    except Exception as exc:  # noqa: BLE001 - reported to the page, the engine carries on
        traceback.print_exc()
        return {"id": rid, "error": f"{type(exc).__name__}: {exc}", "status": 500}, None
    if not binary:
        return {"id": rid, "result": result}, None
    if result is None:
        return {"id": rid, "error": "Nothing to show", "status": 404}, None
    ctype = "image/png" if result[:4] == b"\x89PNG" else "application/octet-stream"
    return {"id": rid, "size": len(result), "type": ctype}, bytes(result)


def serve(service: Service, inp: BinaryIO, out: BinaryIO, workers: int = 8) -> None:
    """Answer requests from ``inp`` on ``out`` until ``inp`` ends."""
    lock = threading.Lock()

    def handle(line: bytes) -> None:
        try:
            request = json.loads(line)
        except ValueError:
            header, body = {"id": None, "error": "Bad request", "status": 400}, None
        else:
            header, body = answer(service, request)
        data = json.dumps(header).encode("utf-8") + b"\n"
        with lock:
            out.write(data + (body or b""))
            out.flush()

    with ThreadPoolExecutor(workers, thread_name_prefix="engine") as pool:
        for line in inp:
            if line.strip():
                pool.submit(handle, line)


def main(folders: list[str] | None = None) -> int:
    # The requests get stdin to themselves, and stdin becomes NUL. On Windows, while this thread
    # waits on the pipe, anything else that asks about it waits too: libgfortran (scipy, loaded
    # on the first remesher import in a worker) stats stdin as it loads, which stalled the engine.
    inp = os.fdopen(os.dup(0), "rb")
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)  # also the process's standard input handle, on Windows
    os.close(null)
    # the answers get stdout to themselves: anything else printed goes to stderr (the app's log)
    out = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    sys.stdout.flush()
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    if trace := os.environ.get("DARKSTL_ENGINE_TRACE"):  # every n seconds, where each thread is
        faulthandler.dump_traceback_later(float(trace), repeat=True)
    service = Service(Config.load(), cache_dir(), WindowHost())
    service.load_folders()
    for folder in folders or []:
        try:
            service.call("folder_add", {"path": folder})
        except Exception as exc:  # noqa: BLE001 - reported, the engine still starts
            print(f"Dark STL: can't add {folder}: {exc}", file=sys.stderr)
    print(f"Dark STL engine {os.getpid()} ready", file=sys.stderr, flush=True)
    serve(service, inp, out)
    return 0  # jobs still running (an export, say) end with the window: their threads are daemons
