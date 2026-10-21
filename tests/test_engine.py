"""The engine the desktop app runs (app/engine.py): commands on stdin, answers on stdout."""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from test_pipeline import weapon_dir  # noqa: F401 - fixture

SRC = Path(__file__).resolve().parents[1] / "src"


class Engine:
    def __init__(self, home, *folders):
        # the log goes to a file (a pipe nobody reads fills up), with where each thread is every 20 s
        env = dict(os.environ, DARKSTL_HOME=str(home), PYTHONPATH=str(SRC), DARKSTL_ENGINE_TRACE="20")
        self.log = Path(home).parent / "engine.log"
        with open(self.log, "wb") as log:
            self.proc = subprocess.Popen([sys.executable, "-m", "darkstl", "engine", *map(str, folders)], env=env,
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log)
        self.next = 0

    def send(self, cmd, args=None, binary=False):
        self.next += 1
        request = {"id": self.next, "cmd": cmd, "args": args or {}, "bin": binary}
        self.proc.stdin.write(json.dumps(request).encode() + b"\n")
        self.proc.stdin.flush()
        return self.next

    def read(self):
        # an engine that doesn't answer is stopped, so the test fails (with its log) instead of hanging
        watchdog = threading.Timer(60, self.proc.kill)
        watchdog.start()
        try:
            line = self.proc.stdout.readline()
            body = None
            if line:
                header = json.loads(line)
                body = self.proc.stdout.read(header["size"]) if "size" in header else None
        finally:
            watchdog.cancel()
        assert line, "the engine didn't answer:\n" + self.log.read_text("utf-8", "replace")
        return header, body

    def call(self, cmd, args=None, binary=False):
        rid = self.send(cmd, args, binary)
        header, body = self.read()
        assert header["id"] == rid
        return header, body

    def close(self):
        self.proc.stdin.close()
        code = self.proc.wait(30)
        return code, self.log.read_text("utf-8", "replace")


@pytest.fixture
def engine(tmp_path, weapon_dir):  # noqa: F811
    e = Engine(tmp_path / "home", weapon_dir)
    yield e
    if e.proc.poll() is None:
        e.proc.kill()


def test_commands_and_pictures_over_the_pipe(engine):
    header, _ = engine.call("app_info")
    assert header["result"]["dialogs"] is True  # the window shows them
    end = time.time() + 60
    while True:
        overview = engine.call("overview")[0]["result"]
        if not overview["scanning"] and overview["total"] or time.time() > end:
            break
        time.sleep(0.1)
    assert [g["name"] for g in overview["games"]] == ["Dark Souls III"]
    items = engine.call("models_query", {"game": "Dark Souls III"})[0]["result"]["items"]
    model_id = next(i["id"] for i in items if i["name"].lower().startswith("wp_a_0300"))

    header, body = engine.call("thumb", {"id": model_id}, binary=True)
    assert header["type"] == "image/png" and body[:4] == b"\x89PNG" and len(body) == header["size"]

    # errors come back as answers, and the engine carries on
    assert engine.call("nope")[0] == {"id": engine.next, "error": "Unknown command 'nope'", "status": 400}
    assert engine.call("thumb", {"id": model_id})[0]["status"] == 400  # bytes asked for as JSON
    engine.proc.stdin.write(b"not json\n")
    engine.proc.stdin.flush()
    assert engine.read()[0] == {"id": None, "error": "Bad request", "status": 400}
    assert engine.call("app_info")[0]["result"]["version"]

    code, log = engine.close()
    assert code == 0 and "engine" in log and "ready" in log


def test_answers_come_as_they_finish(engine):
    ids = [engine.send("thumb", {"id": "missing"}, binary=True), engine.send("app_info")]
    got = {}
    for _ in ids:
        header, _ = engine.read()
        got[header["id"]] = header
    assert set(got) == set(ids)
    assert "result" in got[ids[1]]
