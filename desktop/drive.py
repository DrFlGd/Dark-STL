"""Drive the built desktop app through tauri-driver (WebDriver), the way someone would use it.

    tauri-driver &          # needs msedgedriver (Windows) or WebKitWebDriver (Linux) to hand
    python desktop/drive.py --app "desktop/target/release/Dark STL.exe" --out shots

The app gets a game folder (built byte by byte, as in smoke.py) on its command line. Checks: the
page runs in the app (the desktop platform, no local server), the game is listed with previews,
a model opens in the 3D view with its textures, the isotropic remesher enhances it, and the
result exports. Screenshots go to --out. Exits non-zero on any failure.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.options import ArgOptions
from selenium.webdriver.support.select import Select

sys.path.insert(0, str(Path(__file__).resolve().parent / "engine"))
from smoke import weapon  # noqa: E402

ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
ap.add_argument("--app", required=True, help="the built app")
ap.add_argument("--out", default="shots", help="folder for screenshots")
ap.add_argument("--driver", default="http://127.0.0.1:4444")
a = ap.parse_args()
out = Path(a.out)
out.mkdir(parents=True, exist_ok=True)
tmp = Path(tempfile.mkdtemp(prefix="darkstl-drive-"))
game = weapon(tmp / "games")
exports = tmp / "exports"
# the app (and its engine) inherit tauri-driver's environment: DARKSTL_HOME keeps this run's
# settings apart, when tauri-driver was started with it
print("settings in", os.environ.get("DARKSTL_HOME") or "the user's own folders", flush=True)

opts = ArgOptions()
opts.set_capability("browserName", "wry")
# a switch as well as the folder: WebDriver starts the app with its own (msedgedriver does on Windows),
# and the app gives its engine only the folders
opts.set_capability("tauri:options", {"application": str(Path(a.app).resolve()), "args": [str(game), "--a-switch"]})
d = webdriver.Remote(command_executor=a.driver, options=opts)
d.set_script_timeout(30)
failures = []


def shot(name):
    d.save_screenshot(str(out / f"{name}.png"))


def wait(js, timeout=60, what=""):
    print(f"waiting for {what or js[:70]}…", flush=True)
    end = time.time() + timeout
    while time.time() < end:
        try:
            if d.execute_script(f"return !!({js})"):
                return True
        except Exception:  # noqa: BLE001 - not there yet
            pass
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for {what or js}")


def click(css):
    d.find_element(By.CSS_SELECTOR, css).click()


try:
    wait("window.__errors && document.querySelector('#game-list .nav-link')", 120, "the game in the menu")
    kind = d.execute_script("return [!!window.__TAURI_INTERNALS__, location.protocol, location.host]")
    print("page:", kind, flush=True)
    assert kind[0], "the page isn't running in the app"
    shot("01-home")
    click("#game-list .nav-link")
    wait("document.querySelector('#kind-tiles [data-sub=\"Weapons\"]')", what="the game's kinds")
    click('[data-sub="Weapons"]')
    wait("document.querySelector('.results [data-thumb=\"ok\"]')", 60, "a preview (engine:// picture)")
    shot("02-weapons")
    click(".results [data-model]")
    wait("document.querySelector('#summary-open')", what="the model's summary")
    click("#summary-open")
    wait("document.querySelector('#stage-canvas:not([data-loading])') && window.__viewer?.mesh", 60,
         "the model in the 3D view (engine:// mesh)")
    assert not d.find_elements(By.CSS_SELECTOR, "#stage-error"), d.find_element(By.CSS_SELECTOR, "#stage-error").text
    shot("03-model")
    click("#rail-textures")
    wait("document.querySelector('#texture-info')?.textContent.includes('128')", what="the texture's details")
    click("#rail-enhance")
    method = Select(d.find_element(By.CSS_SELECTOR, "#method"))
    options = {o.get_attribute("value"): o.is_enabled() for o in method.options}
    assert options.get("isotropic"), f"the remesher isn't offered: {options}"
    method.select_by_value("isotropic")
    field = d.find_element(By.CSS_SELECTOR, "#target-vertices")
    field.send_keys(Keys.CONTROL, "a")
    field.send_keys("3000", Keys.TAB)
    click("#enhance-run")
    wait("document.querySelector('#enhance-result')", 120, "the enhanced model")
    result = d.find_element(By.CSS_SELECTOR, "#enhance-result").text
    print("enhance:", " ".join(result.split()), flush=True)
    assert "subdivision instead" not in result, result
    wait("document.querySelector('#stage-canvas:not([data-loading])')", what="the enhanced model drawn")
    shot("04-enhanced")
    click("#rail-export")
    folder = d.find_element(By.CSS_SELECTOR, "#export-folder")
    folder.send_keys(Keys.CONTROL, "a")
    folder.send_keys(str(exports), Keys.TAB)
    click("#export-run")
    wait("document.querySelector('#export-done')", 120, "the export")
    stls = list(exports.glob("*.stl"))
    assert stls and stls[0].stat().st_size > 84 + 50 * 3000, f"no STL in {exports}: {list(exports.iterdir())}"
    print(f"export: {stls[0].name}, {stls[0].stat().st_size} bytes", flush=True)
    shot("05-exported")
    errors = d.execute_script("return window.__errors")
    assert not errors, f"page errors: {errors}"
except Exception as exc:  # noqa: BLE001 - reported with a screenshot and what the page says
    failures.append(f"{type(exc).__name__}: {exc}")
    shot("failed")
    try:
        print("page says:", d.execute_script("return document.body.innerText.slice(0, 600)"), flush=True)
        print("page errors:", d.execute_script("return window.__errors"), flush=True)
    except Exception:  # noqa: BLE001
        pass
finally:
    d.quit()

if failures:
    print("FAILED:", *failures, sep="\n  ", flush=True)
    sys.exit(1)
print("The app works.", flush=True)
