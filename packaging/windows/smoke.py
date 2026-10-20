"""Smoke test of the built app folder: the command line reads and enhances a model, and the app
starts, lists a game folder given to it and (on Windows) opens its window.

    python packaging/windows/smoke.py "build/windows/dist/Dark STL"

The model is built byte by byte like the tests' (tests/synth.py), so it needs the package
installed and numpy, nothing else.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
import synth  # noqa: E402

from darkstl.importers.fromsoft.formats import dcx  # noqa: E402

EXE = ".exe" if os.name == "nt" else ""
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def weapon(folder: Path) -> Path:
    """A Dark Souls III weapon: a 1 m plane with a bump in its normal map."""
    game = folder / "DARK SOULS III" / "Game"
    (game / "parts").mkdir(parents=True)
    (game / "DarkSoulsIII.exe").write_bytes(b"")
    pos, nrm, uv, idx = synth.grid_plane(4, size=1.0)
    mats = [("Blade", "N:\\mtd\\C[DSB].mtd", [("N:\\tex\\WP_A_0300_n.tga", "g_BumpmapTexture")])]
    flver = synth.build_flver([synth.SynthMesh(pos, nrm, uv, idx)], mats, uv_float=True)
    y, x = np.mgrid[0:128, 0:128] / 128
    h = np.exp(-((x - 0.5) ** 2 + (y - 0.5) ** 2) / 0.01) * 8.0
    gy, gx = np.gradient(h)
    n = np.stack([-gx, -gy, np.ones_like(h)], -1)
    n /= np.linalg.norm(n, axis=-1, keepdims=True)
    normal = np.full(h.shape + (4,), 255, np.uint8)
    normal[..., :3] = np.rint((n * 0.5 + 0.5) * 255)
    tpf = synth.build_tpf([("WP_A_0300_n", synth.rgba_dds(normal))])
    bnd = synth.build_bnd4([(200, "N:\\parts\\Weapon\\WP_A_0300\\WP_A_0300.flver", flver),
                            (100, "N:\\parts\\Weapon\\WP_A_0300\\WP_A_0300.tpf", tpf)])
    path = game / "parts" / "wp_a_0300.partsbnd.dcx"
    path.write_bytes(dcx.compress_dflt(bnd))
    return game


def run(cmd: list[str], env: dict) -> str:
    print("$", " ".join(cmd), flush=True)
    out = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600, check=False)
    print(out.stdout + out.stderr, flush=True)
    if out.returncode:
        raise SystemExit(f"exit code {out.returncode}")
    return out.stdout


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fetch(url: str, data: dict | None = None, token: str = "") -> bytes:
    req = urllib.request.Request(url, json.dumps(data).encode() if data is not None else None,
                                 {"Content-Type": "application/json", "X-DarkSTL-Token": token})
    with NO_PROXY.open(req, timeout=10) as r:
        return r.read()


def window_titles() -> list[str]:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    titles = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if buf.value:
                titles.append(buf.value)
        return True

    user32.EnumWindows(each, 0)
    return titles


def check_app(app: Path, game: Path, home: Path, env: dict) -> None:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    print(f"$ {app.name} {game} --port {port}", flush=True)
    proc = subprocess.Popen([str(app), str(game), "--port", str(port)], env=env)
    try:
        deadline = time.time() + 120
        page = b""
        while time.time() < deadline and proc.poll() is None:
            try:
                page = fetch(base + "/")
                break
            except OSError:
                time.sleep(1)
        if b"Dark STL" not in page:
            raise SystemExit(f"the app didn't serve its page (exit code {proc.poll()})")
        token = re.search(rb'name="darkstl-token" content="([^"]+)"', page)
        if not token:
            raise SystemExit("the page has no token")
        token = token.group(1).decode()
        for part in ("/ui/shell.js", "/styles.css"):  # the page's files came along
            fetch(base + part)
        while time.time() < deadline:
            overview = json.loads(fetch(base + "/api/overview", {}, token))["result"]
            if not overview["scanning"] and overview["total"]:
                break
            time.sleep(1)
        print("library:", overview, flush=True)
        if [(g["name"], g["count"]) for g in overview["games"]] != [("Dark Souls III", 1)]:
            raise SystemExit("the app didn't list the game folder it was given")
        if os.name == "nt":
            while time.time() < deadline and "Dark STL" not in window_titles():
                time.sleep(1)
            if "Dark STL" not in window_titles():
                raise SystemExit(f"no Dark STL window; open windows: {window_titles()}")
            print("window: Dark STL", flush=True)
    finally:
        proc.kill()
        proc.wait()
        log = home / "darkstl.log"
        if log.exists():
            print("--- darkstl.log\n" + log.read_text(encoding="utf-8", errors="replace"), flush=True)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("folder", type=Path, help="the built app folder (Dark STL)")
    args = p.parse_args()
    cli = args.folder / f"darkstl{EXE}"
    app = args.folder / f"Dark STL{EXE}"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        home = tmp / "home"
        env = dict(os.environ, DARKSTL_HOME=str(home))
        game = weapon(tmp / "games")
        model = game / "parts" / "wp_a_0300.partsbnd.dcx"

        assert run([str(cli), "--version"], env).strip().startswith("darkstl ")
        info = run([str(cli), "info", str(model)], env)
        assert "WP_A_0300_n" in info, "info didn't list the normal map"
        out = tmp / "out.stl"
        run([str(cli), "enhance", str(model), "-n", "2000", "-o", str(out)], env)
        size = out.stat().st_size
        assert size > 84 + 50 * 2000, f"the STL is too small ({size} bytes)"
        print(f"enhance: {size} bytes of STL", flush=True)

        check_app(app, game, home, env)
    print("Smoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
