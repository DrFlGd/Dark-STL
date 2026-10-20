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


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        tries = [{}] + [{"executable_path": exe} for exe in (os.environ.get("DARKSTL_CHROMIUM"), "/opt/pw-browsers/chromium") if exe]
        for extra in tries:
            try:
                b = p.chromium.launch(args=GL, **extra)
                break
            except Exception:  # noqa: BLE001 - try the next, then skip
                continue
        else:
            pytest.skip("no Chromium for Playwright")
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


def test_add_a_folder_browse_open_enhance_export(page, weapon_dir, tmp_path):  # noqa: F811
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
    page.click("#enhance-run")
    page.wait_for_selector("#enhance-result", timeout=60000)
    page.wait_for_selector('#show-which [data-value="enhanced"][aria-pressed="true"]')
    page.wait_for_selector("#stage-canvas:not([data-loading])")
    page.click('#shading [data-value="clay"]')
    # the same mesh drawn again stays where it was (on the floor, centred)
    page.wait_for_selector("#stage-canvas:not([data-loading])")

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
  return [1, 2].map(() => {
    window.__viewer.show(buf, { keepView: true });
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
