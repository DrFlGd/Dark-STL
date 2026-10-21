"""Smoke test of the built engine (build/engine): the command line reads a model and enhances it
both ways, the remesher included, and "darkstl engine" answers the app's requests on its pipe.

    python desktop/engine/smoke.py build/engine

The model is built byte by byte like the tests' (tests/synth.py), so it needs the package
installed and numpy, nothing else. drive.py, which tests the whole app, uses weapon() too.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
import synth  # noqa: E402

from darkstl.importers.fromsoft.formats import dcx  # noqa: E402

EXE = ".exe" if os.name == "nt" else ""


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
    return out.stdout + out.stderr


def check_engine(cli: Path, game: Path, env: dict) -> None:
    """The app's side of the pipe: requests in, answers (JSON, or a header and bytes) out."""
    print(f"$ {cli.name} engine {game}", flush=True)
    proc = subprocess.Popen([str(cli), "engine", str(game)], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    ids = iter(range(1, 1000))

    def call(cmd, args=None, binary=False):
        rid = next(ids)
        proc.stdin.write(json.dumps({"id": rid, "cmd": cmd, "args": args or {}, "bin": binary}).encode() + b"\n")
        proc.stdin.flush()
        header = json.loads(proc.stdout.readline())
        body = proc.stdout.read(header["size"]) if "size" in header else None
        if header.get("id") != rid or "error" in header:
            raise SystemExit(f"{cmd}: {header}")
        return header.get("result"), body

    try:
        info, _ = call("app_info")
        if not info["dialogs"] or not all(m["available"] for m in info["methods"]):
            raise SystemExit(f"app_info: {info}")
        deadline = time.time() + 120
        while True:
            overview, _ = call("overview")
            if not overview["scanning"] and overview["total"] or time.time() > deadline:
                break
            time.sleep(0.5)
        print("library:", [(g["name"], g["count"]) for g in overview["games"]], flush=True)
        if [(g["name"], g["count"]) for g in overview["games"]] != [("Dark Souls III", 1)]:
            raise SystemExit("the engine didn't list the game folder it was given")
        (item,) = call("models_query", {"game": "Dark Souls III"})[0]["items"]
        _, png = call("thumb", {"id": item["id"]}, binary=True)
        if png[:4] != b"\x89PNG":
            raise SystemExit("the preview isn't a PNG")
        print(f"engine: preview of {item['name']}, {len(png)} bytes", flush=True)
    finally:
        proc.stdin.close()
        code = proc.wait(60)
    if code:
        raise SystemExit(f"the engine ended with exit code {code}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("folder", type=Path, help="the built engine folder (build/engine)")
    args = p.parse_args()
    cli = args.folder / f"darkstl{EXE}"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        env = dict(os.environ, DARKSTL_HOME=str(tmp / "home"))
        game = weapon(tmp / "games")
        model = game / "parts" / "wp_a_0300.partsbnd.dcx"

        assert run([str(cli), "--version"], env).strip().startswith("darkstl ")
        info = run([str(cli), "info", str(model)], env)
        assert "WP_A_0300_n" in info, "info didn't list the normal map"
        for method in ("subdivide", "isotropic"):
            out = tmp / f"{method}.stl"
            log = run([str(cli), "enhance", str(model), "-n", "2000", "--method", method, "-o", str(out)], env)
            size = out.stat().st_size
            assert size > 84 + 50 * 2000, f"the STL is too small ({size} bytes)"
            assert "needs the 'pyacvd' package" not in log, "the remesher is missing"
            print(f"enhance --method {method}: {size} bytes of STL", flush=True)
        check_engine(cli, game, env)
    print("Smoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
