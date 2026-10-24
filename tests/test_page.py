"""The page itself in a real browser (Chromium through Playwright): add a game folder, browse it,
open a model in the 3D view, enhance and export it. Skipped when Playwright or its Chromium isn't
installed (pip install playwright && python -m playwright install chromium)."""

import os
from pathlib import Path

import pytest

from darkstl.app.config import Config
from darkstl.app.server import AppServer
from darkstl.app.service import Service

from test_pipeline import weapon_dir  # noqa: F401 - fixture

sync_api = pytest.importorskip("playwright.sync_api")

GL = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]


def launch(p):
    """Playwright's own Chromium, or the one DARKSTL_CHROMIUM / /opt/pw-browsers names; skips without one."""
    tries = [{}] + [{"executable_path": exe} for exe in (os.environ.get("DARKSTL_CHROMIUM"), "/opt/pw-browsers/chromium") if exe]
    for extra in tries:
        try:
            return p.chromium.launch(args=GL, **extra)
        except Exception:  # noqa: BLE001 - try the next, then skip
            continue
    pytest.skip("no Chromium for Playwright")


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        b = launch(p)
        yield b
        b.close()


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DARKSTL_HOME", str(tmp_path / "home"))
    server = AppServer(Service(Config.load(), tmp_path / "home" / "cache"), 0).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def page(browser, app):
    pg = browser.new_page(viewport={"width": 1400, "height": 900})
    pg.goto(app.url)
    yield pg
    assert pg.evaluate("window.__errors") == []
    pg.close()


def test_add_a_folder_browse_open_enhance_export(page, weapon_dir, tmp_path, monkeypatch):  # noqa: F811
    included = tmp_path / "included"
    included.mkdir()
    (included / "models.json").write_text('{"models": [{"name": "up", "codecs": ["*"]}]}')
    (included / "up.onnx").write_bytes(b"")
    monkeypatch.setenv("DARKSTL_BUNDLED_MODELS", str(included))
    page.wait_for_selector("#first-run")
    page.click("#first-add-folder")
    page.fill("#folder-path", str(weapon_dir))
    page.wait_for_selector("text=Recognised as Dark Souls III.")
    page.click("#add-folder-dialog-ok")
    # it goes to the game's page, with a tile for each kind of model
    page.wait_for_selector('#kind-tiles [data-sub="Weapons"]')
    assert page.locator("#game-list .nav-link").all_inner_texts()[0].startswith("DS3")
    assert page.inner_text("#browse-count") == "4 models"
    page.click('[data-sub="Weapons"]')
    page.wait_for_selector("#group-chips")
    page.wait_for_selector(".results [data-model]")
    assert page.locator(".results [data-model]").count() == 2
    page.wait_for_selector('.results [data-thumb="ok"]', timeout=30000)
    card = page.locator(".results [data-model]").first
    card.click()
    page.wait_for_selector("#model-summary")
    page.click("#summary-open")

    page.wait_for_selector("#stage-canvas:not([data-loading])", timeout=30000)
    assert page.locator("#stage-error").count() == 0
    assert page.evaluate("window.__viewer.mesh.geometry.index.count") > 0
    assert "1000 × 1000" in page.inner_text("#stage-size")
    page.click("#rail-textures")
    page.wait_for_function("document.querySelector('#texture-info')?.textContent.includes('128')")
    page.click("#rail-enhance")
    page.fill("#target-vertices", "3000")
    page.press("#target-vertices", "Tab")
    assert page.text_content("#rail-enhance").strip() == "Generate"
    assert page.text_content("#enhance-run") == "Generate (steps 1 + 2)"
    page.click("#enhance-run")
    page.wait_for_selector("#enhance-result", timeout=60000)
    page.wait_for_selector("#mesh-kept")  # step 1's mesh is kept, so the button now does step 2 alone
    assert page.text_content("#enhance-run") == "Displace (step 2)"
    page.wait_for_selector('#show-which [data-value="enhanced"][aria-pressed="true"]')
    page.wait_for_selector("#stage-canvas:not([data-loading])")
    # the original and the enhanced model in turn: both are kept, nothing is fetched or made again
    meshes = []
    page.on("request", lambda r: "model_mesh" in r.url and meshes.append(r.url))
    triangles = "window.__viewer.mesh.geometry.index.count / 3"
    enhanced = page.evaluate(triangles)
    for which in ("original", "enhanced", "original", "enhanced"):
        page.click(f'#show-which [data-value="{which}"]')
        page.wait_for_selector(f'#show-which [data-value="{which}"][aria-pressed="true"]')
        page.wait_for_selector("#stage-canvas:not([data-loading])")
        assert (page.evaluate(triangles) == enhanced) == (which == "enhanced")
    # generating again with the same settings gives the kept model at once
    page.click("#enhance-run")
    page.wait_for_selector("text=Already generated with these settings")
    assert meshes == []
    page.click('#shading [data-value="clay"]')
    # the same mesh drawn again stays where it was (on the floor, centred)
    page.wait_for_selector("#stage-canvas:not([data-loading])")

    # the displacement settings: per-material strength, upsampling, colour-texture detail, AI models
    page.click(".more-settings > summary")
    page.fill('[data-strength="0"]', "2")
    page.press('[data-strength="0"]', "Tab")
    page.select_option("#upsample", "ai")
    page.wait_for_selector("#ai-models #ai-model-add")
    page.wait_for_selector("#ai-models-folder")  # the list has come
    assert page.locator("#ai-model option").count() == 1  # nothing in the models folder yet
    assert page.locator("#ai-model option").inner_text().startswith("Included")  # the app's own, by default
    page.select_option("#upsample", "guided")
    assert page.locator("#ai-models").count() == 0
    page.fill("#albedo-detail", "0.1")
    page.press("#albedo-detail", "Tab")
    if os.environ.get("DARKSTL_SCREENSHOTS"):
        page.locator("#enhance-tab").screenshot(path=os.path.join(os.environ["DARKSTL_SCREENSHOTS"], "enhance-settings.png"))
    page.wait_for_selector("#mesh-kept")  # step 1 is the same: its mesh is used again
    assert "Step 1 is done" in page.text_content("#mesh-kept")
    page.click("#enhance-run")
    page.wait_for_selector("#enhance-result .field-list :text('albedo detail')", timeout=60000)
    page.wait_for_selector("text=the evened-out mesh was kept")
    page.fill("#target-vertices", "3500")  # a step-1 setting: no kept mesh for it
    page.press("#target-vertices", "Tab")
    page.wait_for_selector("#mesh-kept", state="detached")
    page.fill("#target-vertices", "3000")
    page.press("#target-vertices", "Tab")
    page.wait_for_selector("#mesh-kept")
    # step 1 on its own: the evened-out model, not displaced, is shown and kept; step 2 then starts from it
    page.fill("#target-vertices", "4000")
    page.press("#target-vertices", "Tab")
    page.wait_for_selector("#mesh-kept", state="detached")
    assert page.text_content("#mesh-run") == "Run step 1 only" and page.text_content("#enhance-run") == "Generate (steps 1 + 2)"
    page.click("#mesh-run")
    page.wait_for_selector("#enhance-result h3:text('Evened out (step 1)')", timeout=60000)
    assert "up to 0.00 mm" in page.text_content("#enhance-result")
    page.wait_for_selector("#mesh-kept")
    assert page.text_content("#enhance-run") == "Displace (step 2)" and page.text_content("#mesh-run") == "Show the evened-out mesh"
    page.click("#enhance-run")
    page.wait_for_selector("text=step 2 only")
    page.wait_for_selector("#enhance-result h3:text('Enhanced')", timeout=60000)

    page.click("#rail-export")
    page.fill("#export-folder", str(tmp_path / "out"))
    page.press("#export-folder", "Tab")
    page.click("#export-run")
    page.wait_for_selector("#export-done", timeout=60000)
    assert (tmp_path / "out" / "WP_A_0300_enhanced.stl").stat().st_size > 84
    # the view sits a model on the floor, centred, each time the same mesh is shown again (another shading)
    assert page.evaluate(SHOW_TWICE) == [[-5, 5, 0], [-5, 5, 0]]


SHOW_TWICE = """() => {
  let json = JSON.stringify({ vertices: 3, triangles: 1, uv: false, groups: [], min: [10, 10, 0], max: [20, 20, 5] });
  while (json.length % 4) json += " ";
  const head = new TextEncoder().encode(json);
  const buf = new ArrayBuffer(12 + head.length + 84);
  new Uint8Array(buf).set(new TextEncoder().encode("DSTLMESH"));
  new DataView(buf).setUint32(8, head.length, true);
  new Uint8Array(buf, 12, head.length).set(head);
  new Float32Array(buf, 12 + head.length, 9).set([10, 10, 0, 20, 10, 0, 15, 20, 5]);
  new Float32Array(buf, 12 + head.length + 36, 9).set([0, 0, 1, 0, 0, 1, 0, 0, 1]);
  new Uint32Array(buf, 12 + head.length + 72, 3).set([0, 1, 2]);
  const built = window.__viewer.build(buf);
  return [1, 2].map(() => {
    window.__viewer.show(built, { keepView: true });
    const b = window.__viewer.mesh.geometry.boundingBox;
    return [b.min.x, b.max.x, b.min.z];
  });
}"""


def test_settings_and_themes(page, app, weapon_dir):  # noqa: F811
    page.goto(app.url + "#/settings")
    page.wait_for_selector("#settings-games")
    page.click("#settings-add-folder")
    page.fill("#folder-path", str(weapon_dir / "nope"))
    page.wait_for_selector("#folder-missing")
    page.fill("#folder-path", str(weapon_dir))
    page.wait_for_selector("text=Recognised as Dark Souls III.")
    page.click("#add-folder-dialog-ok")
    page.wait_for_selector("#kind-tiles")
    page.goto(app.url + "#/settings")
    page.wait_for_selector(f'#folder-list [data-folder="{Path(weapon_dir)}"]')
    for skin in ("partsbin", "layerline", "classic", "drafting"):
        page.select_option("#skin-select", skin)
        assert page.evaluate("document.documentElement.dataset.skin") == skin
    page.select_option("#theme-select", "night")
    assert page.evaluate("document.documentElement.dataset.theme") == "night"
    page.goto(app.url + "#/about")
    page.wait_for_selector("#about-head")


def test_previews_stay_shown_and_a_job_stops_from_the_status_bar(page, app, weapon_dir):  # noqa: F811
    app.service.call("folder_add", {"path": str(weapon_dir)})
    page.goto(app.url + "#/browse/Dark%20Souls%20III/Weapons")
    page.wait_for_selector('.results [data-thumb="ok"]', timeout=30000)
    page.wait_for_function("document.querySelectorAll('.results [data-thumb=\"ok\"], .results [data-thumb=\"none\"]').length === 2")
    weapons = page.eval_on_selector_all(".results [data-model]", "els => els.map((e) => e.dataset.model)")
    # back to the game's page and into the kind again (or another view): shown at once, not asked for again
    asked = []
    page.on("request", lambda r: "/bin/thumb" in r.url and asked.append(r.url))
    page.goto(app.url + "#/browse/Dark%20Souls%20III")
    page.wait_for_selector('#kind-tiles [data-sub="Weapons"]')
    page.click('[data-sub="Weapons"]')
    page.wait_for_selector(".results [data-model]")
    assert page.locator('.results [data-thumb="wait"]').count() == 0
    page.click('#layout-switch [data-view="list"]')
    page.wait_for_selector(".results.list-view [data-model]")
    assert page.locator('.results [data-thumb="wait"]').count() == 0
    page.click('#layout-switch [data-view="grid"]')
    assert [u for u in asked if any(f"id={w}&" in u for w in weapons)] == []  # (the game's page shows others too)

    # a long Generate, stopped by clicking its status at the foot of the window
    page.click(".results [data-model]")
    page.click("#summary-open")
    page.wait_for_selector("#stage-canvas:not([data-loading])", timeout=30000)
    page.click("#rail-enhance")
    page.fill("#target-vertices", "20000000")
    page.press("#target-vertices", "Tab")
    page.click("#enhance-run")
    page.wait_for_selector("#status-job.can-stop .status-stop", timeout=10000)  # Stop is shown without hovering
    page.click("#status-job")
    page.wait_for_selector("text=Stopped generating", timeout=120000)
    assert page.locator("#enhance-error").count() == 0 and page.locator("#enhance-result").count() == 0
    assert page.text_content("#enhance-run") == "Generate (steps 1 + 2)"
