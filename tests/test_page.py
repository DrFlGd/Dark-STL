"""The page itself in a real browser (Chromium through Playwright): add a game folder, browse it,
open a model in the 3D view, even it out, upscale its textures, displace and export it. Skipped when Playwright or its Chromium isn't
installed (pip install playwright && python -m playwright install chromium)."""

import os
from pathlib import Path
from urllib.parse import quote


import pytest

from darkstl.app.config import Config
from darkstl.app.server import AppServer
from darkstl.app.service import Service

from test_pipeline import weapon_dir  # noqa: F401 - fixture
from test_unpack import keypair, packed_ds3  # noqa: F401 - fixtures

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


def test_add_a_folder_browse_open_enhance_export(page, app, weapon_dir, tmp_path, monkeypatch):  # noqa: F811
    included = tmp_path / "included"
    included.mkdir()
    (included / "models.json").write_text('{"models": [{"name": "up", "codecs": ["*"]}]}')
    (included / "up.onnx").write_bytes(b"")
    monkeypatch.setenv("DARKSTL_BUNDLED_MODELS", str(included))
    shots = os.environ.get("DARKSTL_SCREENSHOTS")
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

    # step 1 on the Mesh tab: the evened-out model, not displaced, is shown and kept
    page.click("#rail-mesh")
    assert page.text_content("#rail-mesh").strip() == "Mesh"
    page.fill("#target-vertices", "3000")
    page.press("#target-vertices", "Tab")
    assert page.text_content("#mesh-run") == "Even out the vertices"
    page.click("#mesh-run")
    page.wait_for_selector("#mesh-kept", timeout=60000)
    page.wait_for_selector("#mesh-shown")
    if shots:
        page.screenshot(path=os.path.join(shots, "mesh-tab.png"))
    page.wait_for_selector("text=Displace adds the texture detail")
    assert "Kept while you work" in page.text_content("#mesh-kept")
    assert page.text_content("#mesh-run") == "Show the evened-out model"
    page.wait_for_selector('#show-which [data-value="enhanced"][aria-pressed="true"]')
    page.wait_for_selector("#stage-canvas:not([data-loading])")
    # step 2 on the Displace tab starts from the kept mesh
    page.click("#rail-displace")
    page.wait_for_selector("#enhance-result h3:text('Evened out, not displaced yet')")
    assert "up to 0.00 mm" in page.text_content("#enhance-result")
    page.wait_for_function("document.querySelector('#displace-mesh-state')?.textContent.includes('kept')")
    assert page.text_content("#displace-run") == "Displace"
    page.click("#displace-run")
    page.wait_for_selector("#enhance-result h3:text('Enhanced')", timeout=60000)
    page.wait_for_selector("text=Displaced:")
    assert "evened out first" not in page.text_content("#toast")  # step 1 wasn't run again
    assert page.text_content("#displace-run") == "Displace again"
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
    # displacing again with the same settings gives the kept model at once
    page.click("#displace-run")
    page.wait_for_selector("text=Already generated with these settings")
    assert meshes == []
    page.click('#shading [data-value="clay"]')
    # the same mesh drawn again stays where it was (on the floor, centred)
    page.wait_for_selector("#stage-canvas:not([data-loading])")

    # the displacement settings: per-material strength and colour-texture detail; the mesh is kept through them
    page.click(".more-settings > summary")
    page.fill('[data-strength="0"]', "2")
    page.press('[data-strength="0"]', "Tab")
    page.fill("#albedo-detail", "0.1")
    page.press("#albedo-detail", "Tab")
    if shots:
        page.screenshot(path=os.path.join(shots, "displace-tab.png"))
    page.click("#displace-run")
    page.wait_for_selector("#enhance-result .field-list :text('albedo detail')", timeout=60000)
    page.wait_for_selector("text=Displaced:")
    assert "evened out first" not in page.text_content("#toast")

    # the Textures tab: upsampling settings, AI models, and the maps made ready with before and after to zoom into
    page.click("#rail-textures")
    page.select_option("#upsample", "ai")
    page.wait_for_selector("#ai-models #ai-model-add")
    page.wait_for_selector("#ai-models-folder")  # the list has come
    assert page.locator("#ai-model option").count() == 1  # nothing in the models folder yet
    assert page.locator("#ai-model option").inner_text().startswith("Included")  # the app's own, by default
    page.select_option("#upsample", "guided")
    assert page.locator("#ai-models").count() == 0
    page.select_option("#upsample-factor", "2")
    assert page.input_value("#edge-smoothing") == "1"  # stair-stepped edges smoothed by default
    page.select_option("#edge-smoothing", "0.5")
    page.wait_for_selector('#upscale-maps li[data-map][data-ready="0"]')  # not made with these settings yet
    assert "Upscale first" in page.text_content("#compare")
    assert page.text_content("#upscale-run") == "Upscale 1 map"
    page.click("#upscale-run")
    page.wait_for_selector('#upscale-maps li[data-map][data-ready="1"]', timeout=60000)
    page.wait_for_selector("text=1 normal map ready for Displace")
    assert "2× · 256 × 256" in page.text_content('#upscale-maps li[data-map]')
    assert "smoothed jagged edges" in page.text_content('#upscale-maps li[data-map]')
    loaded = "(img) => img.complete && img.naturalWidth > 0"
    for which in ("before", "after"):
        page.wait_for_selector(f'#compare img[data-which="{which}"]', timeout=30000)
        page.wait_for_function(f"document.querySelector('#compare img[data-which=\"{which}\"]').complete")
        assert page.eval_on_selector(f'#compare img[data-which="{which}"]', loaded)
    assert "2 × 2 texels for each" in page.text_content("#compare")
    scale = page.text_content("#compare-scale")
    before = page.get_attribute('#compare img[data-which="after"]', "src")
    page.click("#compare-zoom-in")
    assert page.text_content("#compare-scale") != scale
    page.wait_for_function(f"document.querySelector('#compare img[data-which=\"after\"]').src !== {before!r}", timeout=30000)
    page.click('#compare-mode [data-value="after"]')
    page.wait_for_selector('#compare img[data-which="before"]', state="detached")
    page.click('#compare-mode [data-value="split"]')
    page.wait_for_selector(".compare-divider")
    if shots:
        page.locator("#compare").scroll_into_view_if_needed()
        page.screenshot(path=os.path.join(shots, "textures-tab.png"))
    # Displace uses the map made here
    page.click("#rail-displace")
    page.wait_for_function("document.querySelector('#displace-maps-state')?.textContent === '1 normal map ready'")
    page.wait_for_function("document.querySelector('#displace-mesh-state')?.textContent.includes('kept')")
    page.click("#displace-run")
    page.wait_for_selector("#enhance-result .field-list :text('2x')", timeout=60000)
    page.wait_for_selector("text=Displaced:")
    assert "evened out first" not in page.text_content("#toast")

    # a step-1 setting changes: no kept mesh for it until the Mesh tab runs again
    page.click("#rail-mesh")
    page.wait_for_selector("#mesh-kept")
    page.fill("#target-vertices", "3500")
    page.press("#target-vertices", "Tab")
    page.wait_for_selector("#mesh-kept", state="detached")
    assert page.text_content("#mesh-run") == "Even out the vertices"
    page.fill("#target-vertices", "3000")
    page.press("#target-vertices", "Tab")
    page.wait_for_selector("#mesh-kept")
    page.click("#rail-displace")
    page.fill("#strength", "1.5")
    page.press("#strength", "Tab")
    page.wait_for_function("document.querySelector('#displace-mesh-state')?.textContent.includes('kept')")

    page.click("#rail-export")
    page.fill("#export-folder", str(tmp_path / "out"))
    page.press("#export-folder", "Tab")
    page.click("#export-run")
    page.wait_for_selector("#export-done", timeout=60000)
    assert (tmp_path / "out" / "WP_A_0300_enhanced.stl").stat().st_size > 84
    # the view sits a model on the floor, centred, each time the same mesh is shown again (another shading)
    assert page.evaluate(SHOW_TWICE) == [[-5, 5, 0], [-5, 5, 0]]

    # a normal map upscaled by another program, brought back in place of the Textures tab's settings
    from PIL import Image

    from darkstl.importers import open_model

    model = open_model(next(weapon_dir.rglob("*0300.partsbnd.dcx")))
    tex = next(t for t in model.textures.values() if t.name.endswith("_n"))
    mine = tmp_path / "WP_A_0300_n_4x.png"
    Image.fromarray(tex.decode()).resize((512, 512), Image.BICUBIC).save(mine)

    class Dialogs:
        dialogs = True

        def pick_file(self, title, patterns, start=None):
            return str(mine)

    app.service.host = Dialogs()
    page.evaluate("window.__darkstl.platform.info.dialogs = true")
    page.click("#rail-textures")
    page.click("#upscale-maps [data-import]")
    page.wait_for_selector('#upscale-maps li[data-map][data-ready="1"] :text("4x from WP_A_0300_n_4x.png")', timeout=60000)
    assert "Using WP_A_0300_n_4x.png" in page.text_content("#upscale-maps")
    assert "4× · 512 × 512" in page.text_content("#upscale-maps li[data-map]")
    if shots:
        page.locator("#upscale-maps").scroll_into_view_if_needed()
        page.screenshot(path=os.path.join(shots, "textures-imported.png"))
    page.click("#upscale-maps [data-import-stop]")
    page.wait_for_selector("#upscale-maps [data-import]")


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

    # a long step 1, stopped by clicking its status at the foot of the window
    page.click(".results [data-model]")
    page.click("#summary-open")
    page.wait_for_selector("#stage-canvas:not([data-loading])", timeout=30000)
    page.click("#rail-mesh")
    page.fill("#target-vertices", "20000000")
    page.press("#target-vertices", "Tab")
    page.click("#mesh-run")
    page.wait_for_selector("#status-job.can-stop .status-stop", timeout=10000)  # Stop is shown without hovering
    page.click("#status-job")
    page.wait_for_selector("text=Stopped evening out", timeout=120000)
    assert page.locator("#mesh-error").count() == 0 and page.locator("#mesh-kept").count() == 0
    assert page.text_content("#mesh-run") == "Even out the vertices"


def test_cut_a_model_into_pieces_with_joints_and_export_them(page, app, tmp_path):
    pytest.importorskip("manifold3d")
    from test_cut import figure_obj

    shots = os.environ.get("DARKSTL_SCREENSHOTS")
    path = figure_obj(tmp_path)
    opened = app.service.call("model_open", {"path": str(path)})
    page.goto(f"{app.url}#/model/{quote(opened['id'], safe='')}")
    page.wait_for_selector("#stage-canvas:not([data-loading])", timeout=30000)
    page.click("#rail-cut")
    page.wait_for_selector("#cut-none")
    # the original is cut: nothing has been generated
    assert page.get_attribute('#cut-from button:has-text("Enhanced")', "disabled") is not None
    page.click('#cut-add [data-axis="z"]')
    page.wait_for_selector('#cut-list li[data-cut="1"][aria-selected="true"]')
    page.wait_for_function("window.__viewer.cutGroup.children.length === 1")  # drawn after the list shows
    assert "across z · 0.00 mm from the middle · Pegs" in page.text_content("#cut-list")
    # moved to the waist with the number box, then dragged in the 3D view a little way up
    page.fill("#cut-position-mm", "-10")
    page.press("#cut-position-mm", "Tab")
    page.wait_for_selector("#cut-list :text('-10.00 mm from the middle')")
    page.wait_for_timeout(400)  # the view settles (it eases to a stop)
    corner = page.evaluate(PLANE_CORNER)
    page.mouse.move(*corner)
    page.mouse.down()
    page.mouse.move(corner[0], corner[1] - 6, steps=4)
    page.mouse.up()
    page.wait_for_function("!document.querySelector('#cut-list').textContent.includes('-10.00 mm')")
    page.fill("#cut-position-mm", "-12")
    page.press("#cut-position-mm", "Tab")
    # a second cut through the neck, with magnets and a looser clearance
    page.click('#cut-add [data-axis="z"]')
    page.fill("#cut-position-mm", "13")
    page.press("#cut-position-mm", "Tab")
    page.click('#cut-joint [data-value="magnets"]')
    page.wait_for_selector("#cut-magnet-d")
    page.fill("#cut-magnet-d", "5")
    page.press("#cut-magnet-d", "Tab")
    page.fill("#cut-clearance", "0.3")
    page.press("#cut-clearance", "Tab")
    page.wait_for_selector("#cut-list :text('Magnets, 0.30 mm clearance')")
    page.wait_for_function("window.__viewer.cutGroup.children.length === 2")  # drawn after the list shows
    if shots:
        page.screenshot(path=os.path.join(shots, "cut-planes.png"))
    page.click("#cut-run")
    page.wait_for_selector("#cut-result", timeout=60000)
    page.wait_for_selector("text=Cut into 3 pieces")
    page.wait_for_selector('#show-which [data-value="pieces"][aria-pressed="true"]')
    page.wait_for_selector("#stage-canvas:not([data-loading])")
    assert page.locator("#piece-list li").count() == 3
    page.wait_for_function("window.__viewer.mesh.children.length === 3")
    page.wait_for_function("window.__viewer.cutGroup.children.length === 0")  # the planes aren't drawn over the pieces
    page.fill("#cut-explode", "0.6")
    page.dispatch_event("#cut-explode", "input")
    assert page.evaluate("window.__viewer.mesh.children[0].position.length()") > 0
    if shots:
        page.screenshot(path=os.path.join(shots, "cut-pieces.png"))
    # cutting again with the same planes is instant
    page.click("#cut-run")
    page.wait_for_selector("text=Cut with these planes already: shown")
    # each piece in its own file
    page.click("#rail-export")
    page.wait_for_selector('#export-which button[aria-pressed="true"]:has-text("Pieces")')
    page.fill("#export-folder", str(tmp_path / "out"))
    page.press("#export-folder", "Tab")
    page.click("#export-run")
    page.wait_for_selector("#export-done", timeout=60000)
    assert "3 files in figure_pieces" in page.text_content("#export-done")
    files = sorted(p.name for p in (tmp_path / "out" / "figure_pieces").iterdir())
    assert files == ["figure_piece1.stl", "figure_piece2.stl", "figure_piece3.stl"]
    # the cuts are remembered for the model (in the settings, saved as they change)
    for _ in range(100):
        plans = (app.service.call("prefs_get", {}).get("ds-ui") or {}).get("cutPlans") or {}
        if len(plans.get(opened["id"], [])) == 2:
            break
        page.wait_for_timeout(50)
    page.reload()
    page.wait_for_selector("#stage-canvas:not([data-loading])", timeout=30000)
    page.wait_for_selector("#rail-cut")
    if not page.locator("#cut-tab").count():  # (it may be open already: the tab last saved; clicking it would fold it)
        page.click("#rail-cut")
    page.wait_for_selector("#cut-list")
    assert page.locator("#cut-list li").count() == 2


def test_make_a_model_watertight_then_export_and_cut_it(page, app, tmp_path):
    pytest.importorskip("manifold3d")
    from test_cut import figure_obj

    shots = os.environ.get("DARKSTL_SCREENSHOTS")
    opened = app.service.call("model_open", {"path": str(figure_obj(tmp_path))})
    page.goto(f"{app.url}#/model/{quote(opened['id'], safe='')}")
    page.wait_for_selector("#stage-canvas:not([data-loading])", timeout=30000)
    assert page.get_attribute('#show-which [data-value="finished"]', "disabled") is not None
    page.click("#rail-finish")
    page.wait_for_selector("#finish-tab")
    assert page.get_attribute('#finish-from button:has-text("Enhanced")', "disabled") is not None
    assert page.is_checked("#finish-hollows") and not page.is_checked("#finish-gaps")
    page.check("#finish-gaps")
    page.wait_for_selector("#finish-gap-mm")
    page.uncheck("#finish-gaps")
    page.click("#finish-run")
    page.wait_for_selector("#finish-result", timeout=60000)
    page.wait_for_selector("text=Made watertight")
    page.wait_for_selector('#show-which [data-value="finished"][aria-pressed="true"]')
    page.wait_for_selector("#stage-canvas:not([data-loading])")
    assert "3 parts joined into one" in page.text_content("#finish-result")
    assert page.text_content("#finish-stats").count("1") >= 1
    if shots:
        page.screenshot(path=os.path.join(shots, "finish-tab.png"))
    # other settings: shown as made with other ones until made again
    page.check("#finish-loose")
    page.wait_for_selector("#finish-stale")
    page.uncheck("#finish-loose")
    page.wait_for_selector("#finish-stale", state="detached")
    # Export and Cut can use it
    page.click("#rail-export")
    page.wait_for_selector('#export-which button[aria-pressed="true"]:has-text("Finished")')
    assert page.get_attribute("#export-name", "placeholder") == "figure_watertight"
    page.fill("#export-folder", str(tmp_path / "out"))
    page.press("#export-folder", "Tab")
    page.click("#export-run")
    page.wait_for_selector("#export-done", timeout=60000)
    assert (tmp_path / "out" / "figure_watertight.stl").exists()
    page.click("#rail-cut")
    page.wait_for_selector("#cut-tab")
    assert page.get_attribute('#cut-from button:has-text("Finished")', "disabled") is None


PLANE_CORNER = """() => {
  const v = window.__viewer;
  const g = v.cutGroup.children[0];
  const size = g.children[0].geometry.parameters.width;
  const at = g.localToWorld(new g.position.constructor(size * 0.42, size * 0.42, 0)).project(v.camera);
  const r = v.canvas.getBoundingClientRect();
  return [r.left + (at.x + 1) / 2 * r.width, r.top + (1 - at.y) / 2 * r.height];
}"""


def test_unpack_a_packed_game(page, packed_ds3):  # noqa: F811
    """Adding a game's install folder whose models are in its archives goes on to the Unpack dialog;
    unpacking lists the models from the app's library."""
    import shutil

    game, lists = packed_ds3
    home = Path(os.environ["DARKSTL_HOME"])
    shutil.copytree(lists, home / "filelists")
    shots = os.environ.get("DARKSTL_SCREENSHOTS")
    page.wait_for_selector("#first-run")
    page.click("#first-add-folder")
    page.fill("#folder-path", str(game.parent))
    page.wait_for_selector("text=Recognised as Dark Souls III.")
    page.click("#add-folder-dialog-ok")
    page.wait_for_selector("#unpack-dialog")
    page.wait_for_selector("#unpack-groups")
    assert "No models were found loose" in page.inner_text("#unpack-why")
    assert page.is_checked('[data-group="parts"]') and page.is_checked('[data-group="characters"]')
    assert not page.is_checked('[data-group="maps"]') and page.is_disabled('[data-group="maps"]')
    assert page.input_value("#unpack-dest").endswith("Dark Souls III")
    assert "1 other file" in page.inner_text("#unpack-names")
    if shots:
        page.locator("#unpack-dialog").screenshot(path=str(Path(shots) / "unpack-dialog.png"))
    page.click("#unpack-dialog-ok")
    page.wait_for_selector("#unpack-dialog", state="detached", timeout=30000)
    page.wait_for_selector('#kind-tiles [data-sub="Weapons"]')
    assert page.inner_text("#browse-count") == "4 models"
    page.goto(page.url.split("#")[0] + "#/settings")
    page.wait_for_selector("#settings-archives")
    page.wait_for_selector("#filelists-status >> text=Downloaded lists for Dark Souls III")  # the copied list counts as one
    if shots:
        page.locator("#settings-archives").screenshot(path=str(Path(shots) / "settings-archives.png"))


def test_many_long_groups_stay_two_rows_of_chips(page, tmp_path):
    """Cyberpunk's depot folders give a kind hundreds of groups, some with long names: the chips
    keep to two rows (names cut short, full name on hover) and the rest open in a short scrolling box."""
    import synth_red as S
    from test_redengine import write_loose

    pos, nrm, uv, idx = S.grid_plane(2, 1.0)
    mesh = S.build_mesh([S.SynthChunk(pos, nrm, uv, idx)], [("m", r"base\surfaces\m.mi")])
    long = "a_very_long_folder_name_from_the_depot_that_goes_on_and_on_and_on"
    files = {fr"base\environment\{long}_{i:03d}\prop_{i:03d}.mesh" if i % 3 == 0 else fr"base\environment\kind_{i:03d}\prop_{i:03d}.mesh": mesh
             for i in range(120)}
    root = tmp_path / "exported"
    write_loose(root, files)
    page.wait_for_selector("#first-run")
    page.click("#first-add-folder")
    page.fill("#folder-path", str(root))
    page.wait_for_selector("text=Recognised as Cyberpunk 2077.")
    page.click("#add-folder-dialog-ok")
    page.wait_for_selector('#kind-tiles [data-sub="Objects"]')
    page.click('[data-sub="Objects"]')
    page.wait_for_selector("#group-chips-more")
    box = page.locator("#group-chips").bounding_box()
    assert box["height"] < 80
    assert page.locator(".results [data-model]").first.bounding_box()["y"] < 300
    chip = page.locator("#group-chips .chip-btn").nth(1)
    assert chip.bounding_box()["width"] <= 201
    assert chip.get_attribute("title").startswith("A Very Long Folder Name")
    # a group far down the list comes up front when it is the one shown
    page.click("#group-chips-more")
    assert page.get_attribute("#group-chips-more", "aria-expanded") == "true"
    assert page.locator("#group-chips").bounding_box()["height"] <= 270
    last = page.locator('#group-chips [data-group="Kind 119"]')
    last.scroll_into_view_if_needed()
    last.click()
    page.wait_for_selector('#group-chips [data-group="Kind 119"].on')
    assert page.get_attribute("#group-chips-more", "aria-expanded") == "false"
    assert page.locator("#group-chips .chip-btn").nth(1).get_attribute("data-group") == "Kind 119"
    assert page.locator('#group-chips [data-group="Kind 119"]').is_visible()
    # the model page's bar keeps a long group and file name inside the window
    page.click('#group-chips [data-group^="A Very Long"]')
    page.locator(".results [data-model]").first.dblclick()
    page.wait_for_selector("#mp-name")
    assert page.evaluate("document.querySelector('#mp-head').scrollWidth <= document.querySelector('#mp-head').clientWidth + 1")
    assert page.locator("#mp-name").bounding_box()["width"] > 60


def test_configure_a_weapon_from_its_parts(page, app, tmp_path):
    """A configurable model is listed with its slots, opens on its Configure tab, puts itself together
    again when a part is chosen, and its configuration can be edited and kept."""
    import json
    import time

    from test_configs import cube_obj

    root = tmp_path / "game"
    w = root / "weapons" / "rifle"
    cube_obj(w / "rifle_body.obj", 0.2)
    cube_obj(w / "rifle_mag_std.obj", 0.05, (0, 0, -0.15))
    cube_obj(w / "rifle_mag_drum.obj", 0.08, (0, 0, -0.16))
    cube_obj(w / "rifle_scope.obj", 0.04, (0, 0.12, 0))
    (root / "guns.darkstl-configs.json").write_text(json.dumps({"format": "darkstl-configurations", "game": "Test game",
        "configurations": [{"id": "rifle", "label": "Test rifle", "base": "weapons/rifle/rifle_body.obj", "slots": [
            {"id": "mag", "label": "Magazine", "required": True, "options": [
                {"id": "std", "label": "Standard", "model": "rifle_mag_std"}, {"id": "drum", "label": "Drum", "model": "rifle_mag_drum"}]},
            {"id": "sight", "label": "Sight", "options": [{"id": "scope", "label": "Scope", "model": "rifle_scope"}]}],
            "presets": {"Heavy": {"mag": "drum", "sight": "scope"}}}]}))
    s = app.service
    job = s.call("folder_add", {"path": str(root), "game": "Test game"})["job"]
    end = time.time() + 30
    while s.call("job", {"id": job})["state"] == "running" and time.time() < end:
        time.sleep(0.05)
    shots = os.environ.get("DARKSTL_SCREENSHOTS")
    page.goto(f"{app.url}#/browse/{quote('Test game')}/Weapons")
    page.wait_for_selector(".card:has-text('Configurable: 2 slots, 3 parts')")
    page.dblclick(".card:has-text('Test rifle')")
    page.wait_for_selector("#configure-tab")
    page.wait_for_selector("#stage-canvas:not([data-loading])", timeout=30000)
    assert page.input_value("#slot-mag") == "std" and page.input_value("#slot-sight") == ""
    tris = lambda: page.text_content("#stage-size")  # noqa: E731
    assert "24 triangles" in tris()
    page.select_option("#preset-select", "Heavy")
    page.wait_for_function("document.querySelector('#slot-sight').value === 'scope'")
    page.wait_for_function("document.querySelector('#stage-size').textContent.includes('36 triangles')", timeout=30000)
    assert page.input_value("#slot-mag") == "drum"
    if shots:
        page.wait_for_timeout(500)
        page.screenshot(path=str(Path(shots) / "configure-tab.png"))
    # the parts show on the Model tab, each of which can be left out
    page.click("#rail-model")
    assert page.locator(".part-row").count() == 3 and "Sight: Scope" in page.text_content("#parts")
    # edit: rename it, lift the scope 10 mm, save; it's kept and put together again
    page.click("#rail-configure")
    page.click("#config-edit")
    page.fill("#config-label", "My rifle")
    page.fill('[data-slot="sight"] [aria-label="Move Z (mm)"]', "10")
    page.press('[data-slot="sight"] [aria-label="Move Z (mm)"]', "Tab")
    # and add a part from the library to a new slot
    page.click("#config-add-slot")
    page.click('[data-slot="slot3"] .add-part')
    page.fill(".part-search", "rifle_scope")
    page.click(".part-result[data-file='rifle_scope.obj']")
    page.click('[data-slot="slot3"] button:has-text("Done")')
    if shots:
        page.screenshot(path=str(Path(shots) / "configure-edit.png"))
    page.click("#config-save")
    page.wait_for_selector("#configure-tab h3:has-text('My rifle')")
    page.wait_for_selector("#slot-slot3")
    saved = json.loads((Path(os.environ["DARKSTL_HOME"]) / "configurations" / "Test game.json").read_text())
    (c,) = saved["configurations"]
    assert c["label"] == "My rifle" and c["slots"][1]["options"][0]["attach"]["offset"] == [0, 0, 0.01]
    assert c["slots"][2]["options"][0]["model"] == {"file": "weapons/rifle/rifle_scope.obj"}


def test_collect_dialog_works_out_its_plan_in_a_job(page, app, weapon_dir):  # noqa: F811
    app.service.call("folder_add", {"path": str(weapon_dir)})
    page.goto(app.url + "#/browse/Dark%20Souls%20III")
    page.wait_for_selector("#place-collect", timeout=30000)
    page.click("#place-collect")
    page.wait_for_selector("#collect-plan-text", timeout=30000)
    assert "model" in page.text_content("#collect-plan-text")
    jobs = [j for j in app.service.jobs.running()]
    page.click('[data-collect="textures"]')  # another plan: the one before is let go
    page.wait_for_selector("#collect-plan-text", timeout=30000)
    assert not jobs or all(j.state != "running" for j in jobs)
    assert any(j.label.startswith("Working out what to copy") for j in app.service.jobs._jobs.values())
