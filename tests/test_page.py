"""The page itself in a real browser (Chromium through Playwright): add a game folder, browse it,
open a model in the 3D view, even it out, upscale its textures, displace and export it. Skipped when Playwright or its Chromium isn't
installed (pip install playwright && python -m playwright install chromium)."""

import os
import struct
import time
from pathlib import Path
from urllib.parse import quote


import numpy as np
import pytest

from darkstl.app.config import Config
from darkstl.app.server import AppServer
from darkstl.app.service import Service

from test_pipeline import weapon_dir  # noqa: F401 - fixture
from test_posing import game  # noqa: F401 - fixture
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
    shade(page, "clay")
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

    # thin parts are kept from clipping, at 80 % of their room, unless switched off; nothing is thin on a plate
    assert page.is_checked("#thin-parts") and page.input_value("#thin-room") == "80"
    assert page.text_content("#thin-result") == "nothing needed lowering"
    page.uncheck("#thin-parts")
    assert page.locator("#thin-room").count() == 0
    page.click("#displace-run")
    page.wait_for_selector("#thin-result", state="detached", timeout=60000)
    page.check("#thin-parts")
    page.fill("#thin-room", "60")
    page.press("#thin-room", "Tab")
    assert page.input_value("#thin-room") == "60"

    # the Textures tab: a chosen texture can be draped over the whole model (to compare the maps on it)
    page.click("#rail-textures")
    tiles = page.locator(".tex-tile")
    assert tiles.count() >= 2
    tiles.nth(1).click()
    second = page.get_attribute(".tex-tile[aria-selected='true']", "data-texture")
    page.check("#texture-on-model")
    page.wait_for_function(f"document.querySelector('#stage-canvas').dataset.texture === {second!r}")
    tiles.nth(0).click()  # the drape follows the selection
    first = page.get_attribute(".tex-tile[aria-selected='true']", "data-texture")
    assert first != second
    page.wait_for_function(f"document.querySelector('#stage-canvas').dataset.texture === {first!r}")
    page.uncheck("#texture-on-model")
    page.wait_for_function("!document.querySelector('#stage-canvas').dataset.texture")

    # upsampling settings, AI models, and the maps made ready with before and after to zoom into
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


def shade(page, how):
    """Choose the 3D view's shading: from its buttons, or from the View menu the footer folds them into when it's
    narrow."""
    if page.locator("#view-menu").count():
        page.click("#view-menu")
        page.click(f'#context-menu [data-action="shading-{how}"]')
        page.wait_for_selector(f'#view-menu[data-shading="{how}"]')
    else:
        page.click(f'#shading [data-value="{how}"]')


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
    # Settings has three pages: General, Library and Game tools, each with its own sections
    page.goto(app.url + "#/settings")
    page.wait_for_selector("#settings-nav")
    assert page.locator("#settings-nav [data-page]").all_inner_texts()[0].startswith("General")
    for section in ("#settings-exports", "#settings-previews", "#settings-look", "#settings-painter"):
        page.wait_for_selector(section)
    assert "Export for Model Painter writes a GLB" in page.text_content("#settings-painter")  # the space after </i> is kept
    assert page.locator("#settings-games").count() == 0 and page.locator("#settings-oodle").count() == 0
    page.click('#settings-nav [data-page="tools"]')
    for section in ("#settings-oodle", "#settings-unreal", "#settings-archives"):
        page.wait_for_selector(section)
    assert page.url.endswith("#/settings/tools") and page.locator("#settings-exports").count() == 0
    page.click('#settings-nav [data-page="library"]')
    for section in ("#settings-games", "#settings-library", "#settings-textures"):
        page.wait_for_selector(section)
    assert page.get_attribute('#settings-nav [data-page="library"]', "aria-current") == "page"
    page.click("#settings-add-folder")
    page.fill("#folder-path", str(weapon_dir / "nope"))
    page.wait_for_selector("#folder-missing")
    page.fill("#folder-path", str(weapon_dir))
    page.wait_for_selector("text=Recognised as Dark Souls III.")
    page.click("#add-folder-dialog-ok")
    page.wait_for_selector("#kind-tiles")
    page.goto(app.url + "#/settings/library")
    page.wait_for_selector(f'#folder-list [data-folder="{Path(weapon_dir)}"]')
    # the previews' switch: characters posed (on by default)
    page.goto(app.url + "#/settings")
    page.wait_for_selector("#posed-previews")
    assert page.is_checked("#posed-previews")
    page.click("#posed-previews")  # (saved, then shown: a controlled box)
    page.wait_for_function("document.querySelector('#posed-previews') && !document.querySelector('#posed-previews').checked")
    page.wait_for_selector("#toast >> text=bind pose")
    assert app.service.config.posed_previews is False
    for skin in ("partsbin", "layerline", "classic", "drafting"):
        page.select_option("#skin-select", skin)
        assert page.evaluate("document.documentElement.dataset.skin") == skin
    page.select_option("#theme-select", "night")
    assert page.evaluate("document.documentElement.dataset.theme") == "night"
    page.goto(app.url + "#/about")
    page.wait_for_selector("#about-head")


def test_an_unreal_game_without_a_profile_is_added_by_name_and_its_profile_shown(page, app, tmp_path):
    from test_profiles import _unreal_game

    opened = []
    app.service.host.open_path = opened.append
    root = _unreal_game(tmp_path, "Moonfall", folder="Moonfall Deluxe")
    page.goto(app.url + "#/settings/tools")
    page.wait_for_selector("#settings-profiles")
    assert page.locator("#profiles-none").count() == 1
    page.goto(app.url + "#/settings/library")
    page.click("#settings-add-folder")
    page.fill("#folder-path", str(root))
    page.wait_for_selector("#folder-unreal")
    assert "its project is Moonfall" in page.inner_text("#folder-unreal")
    # the install folder's name is suggested, and can be changed
    page.wait_for_selector("#folder-game-name")
    assert page.input_value("#folder-game-name") == "Moonfall Deluxe"
    page.fill("#folder-game-name", "Moon Fall")
    page.click("#add-folder-dialog-ok")
    page.wait_for_selector('#kind-tiles [data-sub="Weapons"]', timeout=60000)
    assert page.locator('#kind-tiles [data-sub="Characters"]').count() == 1
    # its profile is listed in Settings (Game tools), and shown when asked
    page.goto(app.url + "#/settings/tools")
    page.wait_for_selector("#profiles-user")
    assert "Moon Fall" in page.inner_text("#profiles-user")
    page.click("#profiles-user button")
    page.wait_for_function("document.querySelector('#profiles-user button') && !document.querySelector('#profiles-user button').disabled")
    assert opened and opened[-1].endswith("moon-fall.json")
    # a shipped profile copied to be changed: listed as the user's, replacing Dark STL's
    page.click("#profiles-shipped summary")
    row = page.locator("#profiles-shipped li", has_text="Returnal")
    row.locator(".profile-copy").click()
    page.wait_for_selector("#profiles-user li:has-text('Returnal')")
    assert "replaces Dark STL's" in page.inner_text("#profiles-user")
    assert opened[-1].endswith("returnal.json")
    page.click("#profiles-open")
    page.wait_for_function("document.querySelector('#profiles-open') && !document.querySelector('#profiles-open').disabled")
    assert opened[-1].endswith("profiles")


def test_an_fbx_model_opens_with_its_textures_and_poses(page, app, tmp_path):
    import synth_fbx

    folder = tmp_path / "export"
    (folder / "textures").mkdir(parents=True)
    (folder / "textures" / "Skin_D.png").write_bytes(synth_fbx.png((200, 50, 50, 255)))
    (folder / "textures" / "Cloth_N.png").write_bytes(synth_fbx.png((128, 128, 255, 255)))
    (folder / "strip.fbx").write_bytes(synth_fbx.skinned_quad_scene(animated=True))
    opened = app.service.call("model_open", {"path": str(folder / "strip.fbx")})
    page.goto(f"{app.url}#/model/{quote(opened['id'], safe='')}")
    page.wait_for_selector("#stage-canvas:not([data-loading])", timeout=30000)
    assert page.locator("#stage-error").count() == 0
    page.click("#rail-textures")
    page.wait_for_selector("text=Skin_D")
    page.click("#rail-pose")
    page.wait_for_selector("#pose-anim")
    assert page.locator("#pose-anim option").all_inner_texts()[0].startswith("Wave")
    page.select_option("#pose-anim", "Wave")
    page.wait_for_selector("#pose-frame")
    page.wait_for_selector("#stage-canvas:not([data-loading])", timeout=30000)
    assert page.locator("#stage-error").count() == 0


def test_previews_stay_shown_and_a_job_stops_from_the_status_bar(page, app, weapon_dir):  # noqa: F811
    app.service.call("folder_add", {"path": str(weapon_dir)})
    page.goto(app.url + "#/browse/Dark%20Souls%20III/Weapons")
    page.wait_for_selector('.results [data-thumb="ok"]', timeout=30000)
    page.wait_for_function("document.querySelectorAll('.results [data-thumb=\"ok\"], .results [data-thumb=\"none\"]').length === 2")
    weapons = page.eval_on_selector_all(".results [data-model]", "els => els.map((e) => e.dataset.model)")
    # back to the game's page and into the kind again (or another view): shown at once, not asked for again
    asked = []
    # (only previews: the mouse rests on a card after the clicks, and a hovered card asks for its turntable, turn=1)
    page.on("request", lambda r: "/bin/thumb" in r.url and "turn=1" not in r.url and asked.append(r.url))
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

    # Redraw preview (a card's menu): only that model's picture is drawn again, the others stay shown
    first = page.locator(f'.results [data-model="{weapons[0]}"]').first
    first.click(button="right")
    # waited for by its request: the card may not have shown "wait" yet when the click returns
    with page.expect_response(lambda r: f"id={weapons[0]}&" in r.url and "cached" not in r.url, timeout=30000):
        page.click('[data-action="redraw-preview"]')
    page.wait_for_selector("text=Drawing its preview again.")
    page.wait_for_function(f"document.querySelector('.results [data-model=\"{weapons[0]}\"] [data-thumb]').dataset.thumb !== 'wait'", timeout=30000)
    assert [u for u in asked if f"id={weapons[0]}&" in u and "cached" not in u]  # drawn, not read from the cache
    assert [u for u in asked if f"id={weapons[1]}&" in u] == []
    # and a whole kind, from the page's ⋯ menu
    page.click("#browse-more")
    page.click('[data-action="game-redraw"]')
    page.wait_for_selector("text=previews in Weapons again")

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


def figure_obj(folder: Path) -> Path:
    """A small figure 66 mm tall (a body, a head and arms 8 mm thick, overlapping as game parts do) as an OBJ."""
    from test_clean import box, sphere, write_obj

    body = box((-0.012, -0.012, 0.0), (0.012, 0.012, 0.050), n=6)
    head = sphere((0, 0, 0.057), 0.009, nu=32, nv=16)
    arms = box((-0.028, -0.004, 0.038), (0.028, 0.004, 0.046), n=6)
    return write_obj(folder / "figure.obj", [("body", body), ("head", head), ("arms", arms)])


THICK_RED = """() => {
  const c = window.__viewer.mesh.geometry.getAttribute("color").array;
  let red = 0;
  for (let i = 0; i < c.length; i += 3) if (c[i] > 0.7 && c[i + 1] < 0.4) red++;
  return red;
}"""


def test_the_thickness_view_shows_what_is_too_thin_to_print(page, app, tmp_path):
    shots = os.environ.get("DARKSTL_SCREENSHOTS")
    opened = app.service.call("model_open", {"path": str(figure_obj(tmp_path))})
    page.goto(f"{app.url}#/model/{quote(opened['id'], safe='')}")
    page.wait_for_selector("#stage-canvas:not([data-loading])", timeout=30000)
    meshes = []
    page.on("request", lambda r: "model_mesh" in r.url and meshes.append(r.url))
    shade(page, "thickness")
    page.wait_for_selector("#thickness-panel")
    page.wait_for_selector("#thin-share", timeout=60000)
    assert meshes and "thickness=1" in meshes[-1]
    assert page.evaluate("window.__viewer.mesh.material.vertexColors") is True
    # at game size (66 mm tall) the 8 mm arms are thick enough for a 0.8 mm wall
    assert page.get_attribute("#print-height", "placeholder") == "game size"
    assert page.text_content("#thin-share") == "Nothing is too thin at game size."
    assert page.evaluate(THICK_RED) == 0
    # printed 5 mm tall, the arms are 0.6 mm: too thin, and coloured red at once, without fetching again
    fetched = len(meshes)
    page.fill("#print-height", "5")
    page.press("#print-height", "Tab")
    page.wait_for_function("document.querySelector('#thin-share').textContent.includes('at 5 mm tall')")
    assert "% of the model's points are too thin at 5 mm tall" in page.text_content("#thin-share")
    share = page.evaluate("window.__viewer.thinShare")
    assert 0 < share < 1 and page.evaluate(THICK_RED) > 0
    if shots:
        page.screenshot(path=os.path.join(shots, "thickness.png"))
    # a printer that makes thinner walls: less of it is too thin
    page.fill("#min-wall", "0.3")
    page.press("#min-wall", "Tab")
    page.wait_for_function(f"window.__viewer.thinShare < {share}")
    assert len(meshes) == fetched
    # another shading: the panel goes, the plain mesh comes back; the print height is remembered
    shade(page, "clay")
    page.wait_for_selector("#thickness-panel", state="detached")
    page.wait_for_selector("#stage-canvas:not([data-loading])")
    assert page.evaluate("window.__viewer.mesh.material[0]?.vertexColors ?? window.__viewer.mesh.material.vertexColors") is False
    shade(page, "thickness")
    page.wait_for_selector("#thin-share")
    assert page.input_value("#print-height") == "5" and page.input_value("#min-wall") == "0.3"
    assert page.evaluate("window.__errors") == []


MODEL_POINT = """([x, y, z]) => {
  const v = window.__viewer;
  const p = v.offset.clone().set(x + v.offset.x, y + v.offset.y, z + v.offset.z).project(v.camera);
  const r = v.canvas.getBoundingClientRect();
  return [(p.x + 1) / 2 * r.width, (1 - p.y) / 2 * r.height];
}"""


def _count(page, selector):
    return int("".join(ch for ch in page.text_content(selector) if ch.isdigit()) or 0)


def test_clean_a_model_up_step_by_step(page, app, tmp_path):
    """The Clean tab: a step added shows what it did and can be taken away; thin sheets are listed, highlighted in
    the 3D view as they're pointed at and ticked, and removed; a click picks faces up to the creases (Shift adds,
    Alt takes away) and removes them; a print height scales the model. The steps are kept for the model, and the
    enhanced model and the exported file are made from the cleaned one."""
    from test_clean import figure

    shots = os.environ.get("DARKSTL_SCREENSHOTS")
    path, counts = figure(tmp_path)
    opened = app.service.call("model_open", {"path": str(path)})
    total = opened["stats"]["triangles"]
    page.goto(f"{app.url}#/model/{quote(opened['id'], safe='')}")
    page.wait_for_selector("#stage-canvas:not([data-loading])", timeout=30000)
    triangles = lambda: _count(page, "#stage-tris")  # noqa: E731
    assert triangles() == total
    # the Model tab lists the parts and sends you to Clean to change them
    assert page.locator("#parts .part-row").count() == 4 and page.locator("#parts input").count() == 0
    page.click("#parts-to-clean")
    page.wait_for_selector('#inspector[data-tab="clean"] #clean-tab')
    page.wait_for_selector("#clean-none")
    assert page.locator("#clean-parts .part-row input").count() == 4

    # a step: the lining, hidden inside the body, removed; its row says what it did; taken away, it's back
    page.click("#clean-hidden-parts")
    page.wait_for_selector('#clean-steps li[data-kind="hidden_parts"] .clean-step-text:text("removed 1 part")', timeout=60000)
    assert f"{counts['lining']} faces" in page.text_content("#clean-steps")
    page.wait_for_function(f"document.querySelector('#stage-tris').textContent.replace(/\\D/g, '') === '{total - counts['lining']}'", timeout=30000)
    page.click('#clean-steps li[data-step="0"] button')
    page.wait_for_selector("#clean-none")
    page.wait_for_function(f"document.querySelector('#stage-tris').textContent.replace(/\\D/g, '') === '{total}'", timeout=30000)

    # thin sheets: the cape and the single-layer cuirass are listed; pointed at and ticked, they're drawn over the model
    page.click("#clean-find-sheets")
    page.wait_for_selector("#clean-sheet-list li", timeout=60000)
    assert page.locator("#clean-sheet-list li").count() == 2
    cape = page.locator(f"#clean-sheet-list li:has-text('{counts['cape']} faces')")
    assert cape.get_attribute("data-sheet-kind") == "open"
    cape.hover()
    page.wait_for_function(f"window.__viewer.marks.get('hover')?.count === {counts['cape']}")
    cape.locator("input").check()
    page.wait_for_function(f"window.__viewer.marks.get('sheets')?.count === {counts['cape']}")
    if shots:
        page.screenshot(path=os.path.join(shots, "clean-sheets.png"))
    page.click("#clean-remove-sheets")
    page.wait_for_selector('#clean-steps li[data-kind="sheets"] .clean-step-text:text("removed 1 sheet")', timeout=30000)
    assert f"{counts['cape']} faces" in page.text_content("#clean-steps")
    page.wait_for_function(f"document.querySelector('#stage-tris').textContent.replace(/\\D/g, '') === '{total - counts['cape']}'", timeout=30000)
    assert page.locator("#clean-sheet-list").count() == 0
    page.wait_for_function("!window.__viewer.marks.get('sheets')")

    # pick: a click on the body's side picks that side, up to its edges; Shift+click adds nothing new; Alt+click takes it away
    page.click("#clean-pick")
    page.wait_for_selector('#clean-pick[aria-pressed="true"]')
    page.wait_for_timeout(400)  # the view settles (it eases to a stop)
    at = page.evaluate(MODEL_POINT, [0, 0, 400])
    canvas = "#stage-canvas canvas"
    page.click(canvas, position={"x": at[0], "y": at[1]})
    page.wait_for_selector("#clean-selection-count:text-matches('[0-9,]+ faces? picked')", timeout=30000)
    picked = _count(page, "#clean-selection-count")
    assert 0 < picked < total
    page.wait_for_function(f"window.__viewer.marks.get('selection')?.count === {picked}")
    page.click(canvas, position={"x": at[0], "y": at[1]}, modifiers=["Shift"])
    page.wait_for_timeout(300)
    assert _count(page, "#clean-selection-count") == picked
    page.click(canvas, position={"x": at[0], "y": at[1]}, modifiers=["Alt"])
    page.wait_for_selector("#clean-selection-count:text('Nothing picked')")
    page.click(canvas, position={"x": at[0], "y": at[1]})
    page.wait_for_selector(f"#clean-selection-count:text('{picked:,} faces picked')")
    if shots:
        page.screenshot(path=os.path.join(shots, "clean-pick.png"))
    page.click("#clean-remove-selection")
    page.wait_for_selector('#clean-steps li[data-kind="selection"] .clean-step-text:text("removed")', timeout=30000)
    assert f"removed {picked:,} faces" in page.text_content('#clean-steps li[data-kind="selection"]')
    left = total - counts["cape"] - picked
    page.wait_for_function(f"document.querySelector('#stage-tris').textContent.replace(/\\D/g, '') === '{left}'", timeout=30000)
    page.click("#clean-pick")  # clicks turn the view again

    # a print height: 150 mm tall, the game's size beside it
    page.fill("#clean-height", "150")
    page.press("#clean-height", "Tab")
    page.wait_for_selector('#clean-steps li[data-op="transform"] .clean-step-text:text("150 mm tall")', timeout=30000)
    page.wait_for_selector("#stage-game:text('game 1800 mm')", timeout=30000)
    page.wait_for_function("document.querySelector('#stage-size .stage-dims').textContent.endsWith('× 150 mm')", timeout=30000)
    assert page.locator("#clean-steps li").count() == 3
    if shots:
        page.screenshot(path=os.path.join(shots, "clean-tab.png"))

    # kept for the model: the page opened again has the same steps
    for _ in range(100):
        plans = (app.service.call("prefs_get", {}).get("ds-ui") or {}).get("cleanPlans") or {}
        if len(plans.get(opened["id"], [])) == 3:
            break
        page.wait_for_timeout(50)
    page.reload()
    page.wait_for_selector("#stage-canvas:not([data-loading])", timeout=30000)
    if not page.locator("#clean-tab").count():
        page.click("#rail-clean")
    page.wait_for_selector('#clean-steps li[data-op="transform"] .clean-step-text:text("150 mm tall")', timeout=30000)
    assert page.locator("#clean-steps li").count() == 3
    page.wait_for_function(f"document.querySelector('#stage-tris').textContent.replace(/\\D/g, '') === '{left}'", timeout=30000)

    # the enhanced model is made from the cleaned one, and so is the file exported
    page.click('#clean-next [data-next="mesh"]')
    page.fill("#target-vertices", "12000")
    page.press("#target-vertices", "Tab")
    page.click("#mesh-run")
    page.wait_for_selector("#mesh-next", timeout=120000)
    page.wait_for_selector('#show-which [data-value="enhanced"][aria-pressed="true"]')
    page.wait_for_selector("#stage-canvas:not([data-loading])", timeout=30000)
    assert page.text_content("#stage-size .stage-dims").endswith("× 150 mm")
    page.click("#rail-export")
    page.wait_for_selector('#export-working[data-which="enhanced"]')
    page.wait_for_selector("#export-scaled")
    page.fill("#export-folder", str(tmp_path / "out"))
    page.press("#export-folder", "Tab")
    page.click("#export-run")
    page.wait_for_selector("#export-done", timeout=60000)
    data = (tmp_path / "out" / "figure_enhanced.stl").read_bytes()
    n = struct.unpack_from("<I", data, 80)[0]
    tri = np.frombuffer(data, dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]), count=n, offset=84)["v"].reshape(-1, 3)
    size = tri.max(0) - tri.min(0)
    assert abs(size[2] - 150) < 0.5 and tri[:, 2].min() > -0.01  # 150 mm tall, on the floor
    assert size[1] < 30  # 25 mm across the cuirass: the cape behind the body (37.5 mm with it) isn't there


def test_the_model_page_reads_in_the_order_the_work_happens(browser, app, weapon_dir, tmp_path):  # noqa: F811
    """On a 1280 x 720 window: the rail's tabs in their groups (Edit is Clean, Mesh, Displace: Finish and Cut moved to
    Model Painter), one place to choose the model worked on (Original or Enhanced), explanations folded once a tab's
    job has run, the Next strip, toasts clear of the footer, the game's tree folded beside the model, and the Export
    tab, which leads with the handoff to Model Painter (a stand-in program that writes down what it was started
    with)."""
    from darkstl.app import painter

    shots = os.environ.get("DARKSTL_SCREENSHOTS")
    app.service.call("folder_add", {"path": str(weapon_dir)})
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    page.goto(app.url + "#/browse/Dark%20Souls%20III/Weapons")
    page.wait_for_selector(".results [data-model]", timeout=30000)
    page.wait_for_selector("#category-panel:not(.folded)")  # the game's tree, beside a game's models at 1280 px
    page.locator(".results [data-model]").first.click()
    page.click("#summary-open")
    page.wait_for_selector("#stage-canvas:not([data-loading])", timeout=30000)
    page.wait_for_selector("#category-panel.folded")  # folded beside the model, for the 3D view's room

    # the rail: Model, Edit and Export groups in the order the work happens, each tab's name upright under its icon
    groups = page.eval_on_selector_all("#inspector .rail-group", "els => els.map((e) => e.dataset.group)")
    assert groups == ["Model", "Edit", "Export"]
    tabs = page.eval_on_selector_all("#inspector .rail-tab", "els => els.map((e) => [e.dataset.tab, e.dataset.group])")
    assert tabs == [["model", "Model"], ["textures", "Model"], ["clean", "Edit"], ["mesh", "Edit"], ["displace", "Edit"],
                    ["export", "Export"]]
    assert page.locator("#rail-finish, #rail-cut").count() == 0
    # the Show buttons under the 3D view: the Original and the Enhanced model, nothing else
    shows = page.eval_on_selector_all("#show-which button", "els => els.map((e) => [e.dataset.value, e.textContent, e.disabled])")
    assert shows == [["original", "Original", False], ["enhanced", "Enhanced", True]]
    assert page.eval_on_selector("#rail-mesh .rail-name", "e => getComputedStyle(e).writingMode") == "horizontal-tb"
    assert page.eval_on_selector("#inspector .rail", "e => e.getBoundingClientRect().width") == 64

    # the footer under the 3D view is one row: its view settings are in a menu
    assert page.locator("#shading").count() == 0
    bar = page.eval_on_selector("#stage-bar", "e => e.getBoundingClientRect().height")
    assert bar < 50
    shade(page, "clay")
    shade(page, "textured")

    # Mesh: its explanation is open until it has run once; the Next strip goes on to Displace
    page.click("#rail-mesh")
    page.wait_for_selector("#tip-mesh[open]")
    page.fill("#target-vertices", "3000")
    page.press("#target-vertices", "Tab")
    page.click("#mesh-run")
    page.wait_for_selector("#mesh-next", timeout=60000)
    page.wait_for_selector("#tip-mesh:not([open])")
    # the toast sits at the top right of the 3D view, clear of the footer's buttons
    page.wait_for_selector("#toast:not([hidden])")
    if shots:
        page.screenshot(path=os.path.join(shots, "mesh-1280.png"))
    box = "e => { const r = e.getBoundingClientRect(); return [r.top, r.right, r.bottom]; }"
    toast, stage, footer = (page.eval_on_selector(sel, box) for sel in ("#toast", "#stage-canvas", "#stage-bar"))
    assert toast[0] < stage[0] + 40 and abs(toast[1] - (stage[1] - 12)) <= 2 and toast[2] < footer[0]
    # folded to its first sentence, wrapped and whole (not cut short), and nothing runs under the rail
    assert page.eval_on_selector("#mesh-tab", "e => e.scrollWidth - e.clientWidth") <= 0
    summary = "#tip-mesh > summary"
    assert page.inner_text(summary).endswith("remade at one even size.")
    assert page.eval_on_selector(summary, "e => e.scrollWidth - e.clientWidth") <= 0
    # the bar under the view, one row: the triangle count whole, and the size beside it
    whole = "e => e.scrollWidth <= e.clientWidth && e.getBoundingClientRect().right <= e.parentNode.getBoundingClientRect().right + 0.5"
    assert page.eval_on_selector("#stage-tris", whole), page.inner_text("#stage-size")
    assert page.eval_on_selector("#stage-size .stage-dims", "e => e.scrollWidth <= e.clientWidth")
    page.click("#tip-btn-mesh")  # ⓘ opens it again
    page.wait_for_selector("#tip-mesh[open]")
    page.click("#tip-btn-mesh")
    page.wait_for_selector("#tip-mesh:not([open])")
    page.click('#mesh-next [data-next="displace"]')
    page.wait_for_selector('#inspector[data-tab="displace"] #displace-tab')
    page.wait_for_selector("#tip-displace[open]")  # not run yet
    heads = page.locator("#detail-maps-head span").all_inner_texts()
    assert heads == ["Material", "Texture", "Strength"]
    page.click("#displace-run")
    page.wait_for_selector("#displace-next", timeout=60000)
    page.wait_for_selector("#tip-displace:not([open])")
    if shots:
        page.screenshot(path=os.path.join(shots, "displace-1280.png"))

    # Displace's Next strip goes on to Export, and nowhere else
    assert page.eval_on_selector_all("#displace-next button", "els => els.map((e) => e.dataset.next)") == ["export"]
    assert page.locator("#show-which [data-value]").count() == 2 and not page.is_disabled('#show-which [data-value="enhanced"]')

    # Export works on the model shown: "change" goes to the footer's Show buttons
    page.click('#displace-next [data-next="export"]')
    page.wait_for_selector('#inspector[data-tab="export"] #export-working[data-which="enhanced"]')
    assert page.locator("#export-which").count() == 0
    page.click("#export-working .working-change")
    assert page.evaluate("!!document.activeElement.closest('#show-which')")
    page.wait_for_selector("#show-which.flash")
    page.click('#show-which [data-value="original"]')
    page.wait_for_selector('#export-working[data-which="original"]')
    page.click('#show-which [data-value="enhanced"]')
    page.wait_for_selector('#export-working[data-which="enhanced"]')
    # it leads with For Model Painter, which says where watertight, cutting and painting are now; then Save a file
    sections = page.eval_on_selector_all("#export-tab > section", "els => els.map((e) => e.id)")
    assert sections == ["export-painter", "export-file"]
    assert page.inner_text("#painter-why") == ("To make it watertight, cut it into pieces or paint it for a colour printer, "
                                               "export it for Model Painter.")
    formats = page.eval_on_selector_all("#export-format option", "els => els.map((e) => e.value)")
    assert formats == [".stl", ".obj", ".ply", ".glb"]
    assert page.locator("#export-lay-flat, #paint-summary").count() == 0
    if shots:
        page.screenshot(path=os.path.join(shots, "export-tab-1280.png"))

    # Export: the folder shown short, edited whole; the For Model Painter preset; then Open in Model Painter
    out = tmp_path / "exports" / "for printing" / "weapons"
    page.fill("#export-folder", str(out))
    page.press("#export-folder", "Tab")
    assert page.input_value("#export-folder") == f"…{os.sep}for printing{os.sep}weapons"
    assert page.get_attribute("#export-folder", "title") == str(out)
    page.focus("#export-folder")
    assert page.input_value("#export-folder") == str(out)
    page.press("#export-folder", "Tab")
    page.click("#export-painter-run")
    page.wait_for_selector('#export-done[data-preset="painter"]', timeout=60000)
    assert (out / "WP_A_0300_for_painter.glb").read_bytes()[:4] == b"glTF"
    if not painter.find(""):  # (unless this computer has Model Painter)
        assert page.locator("#painter-open").count() == 0 and page.locator("#painter-hint").count() == 1
    if shots:
        page.screenshot(path=os.path.join(shots, "export-1280.png"))
    # Model Painter set in Settings (here a stand-in that writes down the file it was started with)
    said = tmp_path / "said.txt"
    stub = tmp_path / "Model Painter" / ("Model Painter.cmd" if os.name == "nt" else "Model Painter")
    stub.parent.mkdir()
    stub.write_text(f'@echo %~1> "{said}"\n' if os.name == "nt" else f'#!/bin/sh\nprintf "%s" "$1" > "{said}"\n')
    stub.chmod(0o755)
    app.service.call("settings_set", {"model_painter": str(stub)})
    page.click("#rail-mesh")
    page.click("#rail-export")  # (the tab reads where Model Painter is as it opens)
    page.click("#export-painter-run")
    page.wait_for_selector("#painter-open", timeout=60000)
    assert page.locator("#painter-hint").count() == 0
    # the Next strip after the export: Open in Model Painter first (the main step), Show in folder beside it
    nexts = page.eval_on_selector_all("#export-next button", "els => els.map((e) => [e.id, e.classList.contains('primary')])")
    assert nexts == [["painter-open", True], ["export-show", False]]
    if shots:
        page.screenshot(path=os.path.join(shots, "export-painter-1280.png"))
    page.click("#painter-open")
    page.wait_for_selector("#toast:not([hidden]):has-text('Opening WP_A_0300_for_painter')")
    for _ in range(200):
        if said.exists() and said.read_text().strip():
            break
        page.wait_for_timeout(50)
    assert said.read_text().strip() == str(out / "WP_A_0300_for_painter (2).glb")

    # back to the game's models: its tree as it was; folded again beside a model, unless opened there by hand
    page.click("#mp-back")
    page.wait_for_selector("#category-panel:not(.folded)")
    page.locator(".results [data-model]").first.click()
    page.click("#summary-open")
    page.wait_for_selector("#category-panel.folded")
    page.click("#category-panel-open")
    page.wait_for_selector("#category-panel:not(.folded)")
    page.click("#mp-back")
    page.locator(".results [data-model]").first.click()
    page.click("#summary-open")
    page.wait_for_selector("#stage-canvas")
    page.wait_for_selector("#category-panel:not(.folded)")
    assert page.evaluate("window.__errors") == []
    page.close()


def test_preferences_saved_before_finish_and_cut_moved_still_open_the_model_page(browser, app, weapon_dir):  # noqa: F811
    """Preferences an earlier version saved with the model page left on Finish, a painted 3MF chosen as the export
    format with its paint settings, and the Cut and paint tabs' own settings: the page opens on Export with STL
    chosen, nothing fails, and the next save leaves those out."""
    from test_app import wait

    wait(app.service, app.service.call("folder_add", {"path": str(weapon_dir)})["job"])
    item = next(i for i in app.service.call("models_query", {"game": "Dark Souls III", "limit": 100})["items"]
                if i["stem"] == "wp_a_0300")
    # (put there as they were, past the engine's own clean-up of a settings file it reads)
    app.service.config.prefs = {"ds-ui": {
        "inspector": {"model": "open"}, "inspectorTab": {"model": "finish"},
        "exportOpts": {"format": ".3mf", "units": "mm", "textures": True, "folder": "",
                       "paint": {"colours": 4, "feature": 1.5}},
        "tipsOpen": {"finish": False, "cut": False, "paint": True, "mesh": False},
        "cutOpts": {"joint": "dowel", "clearance": 0.2}, "cutPlans": {item["id"]: [{"axis": "z", "at": 0.5}]},
        "paintOpts": {"colours": 6}, "paintSources": {item["id"]: "albedo"}, "paintPreview": True, "paintShow": True,
        "finishOpts": {"watertight": True}}}
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.goto(app.url + f"#/model/{item['id']}")
    page.wait_for_selector("#stage-canvas:not([data-loading])", timeout=30000)
    page.wait_for_selector('#inspector[data-tab="export"] #export-tab')
    assert page.input_value("#export-format") == ".stl"
    assert page.locator("#rail-finish, #rail-cut, #paint-summary").count() == 0
    # the next save of the page's preferences leaves the retired ones out
    page.click("#rail-mesh")
    page.wait_for_selector('#inspector[data-tab="mesh"]')
    for _ in range(100):
        saved = app.service.config.prefs.get("ds-ui") or {}
        if (saved.get("inspectorTab") or {}).get("model") == "mesh":
            break
        page.wait_for_timeout(50)
    assert saved["inspectorTab"]["model"] == "mesh"
    assert not {"cutOpts", "cutPlans", "paintOpts", "paintSources", "paintPreview", "paintShow", "finishOpts"} & set(saved)
    assert "paint" not in saved["exportOpts"] and saved["exportOpts"].get("format") != ".3mf"
    assert not {"finish", "cut", "paint"} & set(saved.get("tipsOpen") or {})
    assert page.evaluate("window.__errors") == []
    page.close()


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
    page.goto(page.url.split("#")[0] + "#/settings/tools")
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
    assert page.is_checked("#collect-as-packages")  # models are imported as packages unless asked otherwise
    page.click("#collect-as-files")
    page.wait_for_selector('[data-collect="textures"]')
    page.wait_for_selector("#collect-plan-text", timeout=30000)
    jobs = [j for j in app.service.jobs.running()]
    page.click('[data-collect="textures"]')  # another plan: the one before is let go
    page.wait_for_selector("#collect-plan-text", timeout=30000)
    assert not jobs or all(j.state != "running" for j in jobs)
    assert any(j.label.startswith("Working out what to copy") for j in app.service.jobs._jobs.values())


def test_cancel_stops_an_import_and_it_can_run_in_the_background(page, app, weapon_dir, monkeypatch):  # noqa: F811
    from darkstl import package
    from darkstl.app.service import Service

    real = package.write_package

    def slow(*args, **kw):  # each model takes a while, so there's time to press the buttons
        time.sleep(0.8)
        return real(*args, **kw)

    monkeypatch.setattr(package, "write_package", slow)
    monkeypatch.setattr(Service, "IMPORT_THREADS", 1)
    app.service.call("folder_add", {"path": str(weapon_dir)})
    root = Path(os.environ["DARKSTL_HOME"])

    def start():
        page.goto(app.url + "#/browse/Dark%20Souls%20III")
        page.wait_for_selector("#place-collect", timeout=30000)
        page.click("#place-collect")
        page.wait_for_selector("#collect-plan-text", timeout=30000)
        page.click("#collect-dialog-ok")
        page.wait_for_selector("#collect-dialog-background")

    # Cancel: the import stops (what was written is kept), and the dialog closes
    start()
    page.click("#collect-dialog-cancel")
    page.wait_for_selector("#collect-dialog", state="detached", timeout=30000)
    page.wait_for_selector("#toast :text('Import stopped')")
    (job,) = [j for j in app.service.jobs._jobs.values() if j.label.startswith("Importing")]
    assert job.state == "stopped"
    written = list(root.rglob("package.json"))
    assert len(written) < 4
    listed = [e for e in app.service.library.entries if e.importer == "package"]
    assert len(listed) == len(written)  # those written before it stopped are listed
    # Run in background: the dialog closes, the import goes on to the end
    start()
    page.click("#collect-dialog-background")
    page.wait_for_selector("#collect-dialog", state="detached")
    page.wait_for_selector("#status-job")
    page.wait_for_selector("#toast :text('as packages')", timeout=60000)
    jobs = [j for j in app.service.jobs._jobs.values() if j.label.startswith("Importing")]
    assert jobs[-1].state == "done" and len(list(root.rglob("package.json"))) > len(written)


def test_export_model_files_for_review(page, app, weapon_dir, tmp_path):  # noqa: F811
    """A model's files go into a zip in the export folder, from its page's menu."""
    import zipfile

    from test_app import wait

    app.service.config.export_dir = str(tmp_path / "exports")
    wait(app.service, app.service.call("folder_add", {"path": str(weapon_dir)})["job"])
    item = app.service.call("models_query", {"game": "Dark Souls III", "limit": 100})["items"][0]
    page.goto(app.url + f"#/model/{item['id']}")
    page.wait_for_selector("#stage-canvas:not([data-loading])", timeout=30000)
    page.click("#mp-more")
    page.click('[data-action="model-files"]')
    page.wait_for_selector("#model-files-plan-text", timeout=30000)
    assert "file" in page.text_content("#model-files-plan-text")
    page.click('[data-files="package"]')
    page.click("#model-files-dialog-ok")
    page.wait_for_selector("#model-files-dialog", state="detached", timeout=60000)
    (zpath,) = (tmp_path / "exports").glob("*.zip")
    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()
    assert "report.txt" in names and any(n.startswith("Dark Souls III/") for n in names)
    assert any(n.startswith("Dark Souls III packages/") for n in names)


def test_collect_keeps_every_texture_of_the_game_after_saying_how_much(page, app, weapon_dir):  # noqa: F811
    app.service.call("folder_add", {"path": str(weapon_dir)})
    page.goto(app.url + "#/browse/Dark%20Souls%20III")
    page.wait_for_selector("#place-collect", timeout=30000)
    page.click("#place-collect")
    page.wait_for_selector("#collect-plan-text", timeout=30000)
    if page.query_selector("#collect-scope"):
        page.select_option("#collect-scope", "game")
        page.wait_for_selector("#collect-plan-text", timeout=30000)
    assert not page.query_selector("#collect-plan-textures")
    page.check('[data-collect="all_textures"]')
    page.wait_for_selector("#collect-plan-textures", timeout=30000)
    text = page.text_content("#collect-plan-textures")
    assert "2 textures to keep, about" in text, text
    page.click('[data-collect="all_textures"]')  # off again: the plan is only the models'
    page.wait_for_selector("#collect-plan-textures", state="detached", timeout=30000)
    page.check('[data-collect="all_textures"]')
    page.wait_for_selector("#collect-plan-textures", timeout=30000)
    page.click("#collect-dialog-ok")
    page.wait_for_selector("#toast :text('as packages')", timeout=60000)
    assert "kept" in page.text_content("#toast")
    page.click("#place-collect")
    page.wait_for_selector("#collect-plan-text", timeout=30000)
    if page.query_selector("#collect-scope"):
        page.select_option("#collect-scope", "game")
    page.check('[data-collect="all_textures"]')
    page.wait_for_selector("#collect-plan-textures :text('kept already')", timeout=30000)


def test_the_texture_picker_assigns_a_texture_by_name(page, app, weapon_dir):  # noqa: F811
    """Materials on the Textures tab: Change… finds a texture of the game by name, shows it, and uses it."""
    from test_overlay import _game_with_a_wall

    game = _game_with_a_wall(weapon_dir)
    shots = os.environ.get("DARKSTL_SCREENSHOTS")
    from test_app import wait

    wait(app.service, app.service.call("folder_add", {"path": str(game)})["job"])
    items = app.service.call("models_query", {"game": "Dark Souls III", "limit": 100})["items"]
    item = next(i for i in items if i["label"] == "Weapon 0300")
    page.goto(app.url + f"#/model/{item['id']}")
    page.wait_for_selector("#stage-canvas:not([data-loading])", timeout=30000)
    page.click("#rail-textures")
    page.click('[data-pick="0:albedo"]')
    page.wait_for_selector("#texture-picker")
    page.fill("#texture-search", "mossy")
    page.wait_for_selector('#texture-results [data-texture="m30_wall_mossy_a"]')
    assert page.locator("#texture-results li").count() == 1
    page.click('#texture-results [data-texture="m30_wall_mossy_a"]')
    page.wait_for_selector("#texture-chosen img")
    if shots:
        page.screenshot(path=os.path.join(shots, "texture-picker.png"))
    page.click("#texture-picker-ok")
    page.wait_for_function("!document.querySelector('#texture-picker')")
    page.wait_for_selector("#materials >> text=(chosen)", timeout=30000)
    assert "m30_wall_mossy_a" in page.inner_text("#materials")
    if shots:
        page.screenshot(path=os.path.join(shots, "texture-chosen.png"))
    # and back to the model's own
    page.click('[data-pick="0:albedo"]')
    page.wait_for_selector("#texture-reset")
    page.click("#texture-reset")
    page.wait_for_function("!document.querySelector('#materials')?.textContent.includes('(chosen)')", timeout=30000)


def test_models_are_renamed_moved_and_combined(page, app, weapon_dir):  # noqa: F811
    """Rename or move… gives a model a name and a folder of one's own, a whole kind is renamed from its
    summary, and Combine with other models… makes one model of two."""
    from test_app import wait

    shots = os.environ.get("DARKSTL_SCREENSHOTS")
    wait(app.service, app.service.call("folder_add", {"path": str(weapon_dir)})["job"])
    items = {i["stem"]: i for i in app.service.call("models_query", {"game": "Dark Souls III", "limit": 100})["items"]}
    page.goto(app.url + "#/browse/Dark%20Souls%20III/Weapons")
    page.reload()  # the game was added behind the page's back
    page.click(f'.results [data-model="{items["wp_a_0300"]["id"]}"]')
    page.click("#summary-arrange")
    page.wait_for_selector("#arrange-dialog")
    page.fill("#arrange-name", "Longsword")
    page.fill("#arrange-folder", "Weapons/Long swords")
    if shots:
        page.screenshot(path=os.path.join(shots, "rename-or-move.png"))
    page.click("#arrange-dialog-ok")
    page.wait_for_function("!document.querySelector('#arrange-dialog')")
    page.wait_for_selector('#group-chips >> text=Long swords')
    page.click(f'.results [data-model="{items["wp_a_0300"]["id"]}"]')
    page.wait_for_selector("#summary-original")
    assert "Weapon 0300" in page.inner_text("#summary-original")
    assert "Longsword" in page.inner_text("#model-summary")
    # the characters, as a whole, become "Bosses"
    page.goto(app.url + "#/browse/Dark%20Souls%20III/Characters")
    page.wait_for_selector("#place-rename")
    page.click("#place-rename")
    page.fill("#arrange-folder-to", "Bosses")
    page.click("#arrange-folder-dialog-ok")
    page.wait_for_function("location.hash.endsWith('/Bosses')")  # it goes to the folder under its new name
    page.wait_for_selector(f'.results [data-model="{items["c1100"]["id"]}"]')
    assert app.service.call("models_query", {"game": "Dark Souls III", "category": "Bosses"})["total"] >= 1
    # combine the character with the sword, from the model's page
    page.goto(app.url + f"#/model/{items['c1100']['id']}")
    page.wait_for_selector("#stage-canvas:not([data-loading])", timeout=30000)
    page.click("#mp-more")
    page.click('[data-action="combine"]')
    page.wait_for_selector("#combine-dialog")
    page.fill("#combine-search", "longsword")
    page.wait_for_selector('#combine-results [data-model="wp_a_0300"]')
    page.click('#combine-results [data-model="wp_a_0300"] input')
    page.fill("#combine-name", "Knight with sword")
    if shots:
        page.screenshot(path=os.path.join(shots, "combine.png"))
    page.click("#combine-dialog-ok")
    page.wait_for_function("!document.querySelector('#combine-dialog')")
    page.wait_for_selector("#mp-name >> text=Knight with sword", timeout=30000)
    page.wait_for_selector("#stage-canvas:not([data-loading])", timeout=30000)
    assert "Longsword" in page.inner_text("#inspector")


def test_an_enhanced_model_is_saved_as_a_variation(page, app, weapon_dir):  # noqa: F811
    """Displace tab: the enhanced model saved under a name, listed with its picture, shown again after another
    Generate, its settings chosen again, renamed and deleted."""
    from test_app import wait

    shots = os.environ.get("DARKSTL_SCREENSHOTS")
    wait(app.service, app.service.call("folder_add", {"path": str(weapon_dir)})["job"])
    item = next(i for i in app.service.call("models_query", {"game": "Dark Souls III", "limit": 100})["items"]
                if i["stem"] == "wp_a_0300")
    page.goto(app.url + f"#/model/{item['id']}")
    page.wait_for_selector("#stage-canvas:not([data-loading])", timeout=30000)
    page.click("#rail-mesh")
    page.fill("#target-vertices", "3000")
    page.press("#target-vertices", "Tab")
    page.click("#rail-displace")
    page.wait_for_selector("#variation-none")
    page.fill("#strength", "3")
    page.press("#strength", "Tab")
    page.click("#displace-run")
    page.wait_for_selector("#enhance-result h3:text('Enhanced')", timeout=60000)
    first = page.inner_text("#enhance-result")
    page.fill("#variation-name", "Deep relief")
    page.click("#variation-save")
    page.wait_for_selector("#variation-shown >> text=Deep relief", timeout=30000)
    page.wait_for_selector('#variation-list li.shown img')
    assert page.locator("#variation-list li").count() == 1
    # another Generate replaces the enhanced model; the saved one comes back, and its settings too
    page.fill("#strength", "0.5")
    page.press("#strength", "Tab")
    page.click("#displace-run")
    page.wait_for_selector("text=Displaced:", timeout=60000)
    page.wait_for_selector("#variation-save")
    assert page.locator("#variation-list li.shown").count() == 0
    page.click('#variation-list [data-act="show"]')
    page.wait_for_selector("#variation-shown")
    page.wait_for_selector("#stage-canvas:not([data-loading])", timeout=30000)
    assert page.inner_text("#enhance-result").split("Relief")[1] == first.split("Relief")[1]
    page.click('#variation-list [data-act="settings"]')
    assert page.input_value("#strength") == "3"
    if shots:
        page.screenshot(path=os.path.join(shots, "variations.png"))
    # renamed, then deleted
    page.click('#variation-list [data-act="more"]')
    page.click('[data-action="rename"]')
    page.fill(".variation-rename", "Best so far")
    page.press(".variation-rename", "Enter")
    page.wait_for_selector("#variation-list >> text=Best so far")
    page.click('#variation-list [data-act="more"]')
    page.click('[data-action="delete"]')
    page.click("#confirm-dialog-ok")
    page.wait_for_function("!document.querySelector('#variation-list')")
    page.wait_for_selector("#variation-save")


def test_a_card_shows_the_name_the_model_has_in_the_game(page, app, weapon_dir):  # noqa: F811
    """The game's own item text names its models: the card shows that name, its file's name muted under it, and
    the summary both, with the model's size and counts once they're read."""
    import synth_names
    from test_app import wait

    synth_names.add_names(weapon_dir, "Dark Souls III")
    wait(app.service, app.service.call("folder_add", {"path": str(weapon_dir)})["job"])
    page.goto(app.url + "#/browse/Dark%20Souls%20III/Weapons")
    page.reload()  # the game was added behind the page's back
    dagger = page.locator('.results .card:has(.card-file:text-is("wp_a_0300"))')
    dagger.wait_for()
    assert dagger.locator(".card-name").inner_text() == "Dagger"
    page.wait_for_selector('.results .card:has(.card-file:text-is("wp_a_0300")) .card-stats', timeout=30000)
    assert "triangles" in dagger.locator(".card-stats").inner_text()
    dagger.click()
    page.wait_for_selector("#summary-file-label")
    assert page.inner_text("#summary-name") == "Dagger"
    assert page.inner_text("#summary-file-label") == "Weapon 0300"
    assert page.locator("#summary-game-name").count() == 0  # shown only once it's renamed: it's the title
    # the menu shows the name too, and the characters are named as the game names them
    page.goto(app.url + "#/browse/Dark%20Souls%20III/Characters")
    page.wait_for_selector('.results .card-name:text-is("Hollow Soldier")')


def test_search_from_home_finds_a_model_in_another_game(page, app, weapon_dir, tmp_path):  # noqa: F811
    """The search box on Home looks through every game: a model in another game is found by its name, the
    results are grouped by game and narrowed by game, and "/" comes back to the box. Opened, the model is
    then in Home's Recent row."""
    import synth_names
    from test_app import wait

    synth_names.add_names(weapon_dir, "Dark Souls III")
    statues = tmp_path / "statues"
    statues.mkdir()
    (statues / "dagger_statue.obj").write_text("v 0 0 0\nv 0.1 0 0\nv 0 0.2 0\nv 0 0 0.3\nf 1 2 3\nf 1 2 4\nf 1 3 4\nf 2 3 4\n")
    for folder in (weapon_dir, statues):
        wait(app.service, app.service.call("folder_add", {"path": str(folder)})["job"])
    page.reload()
    page.wait_for_selector("#game-tiles")
    page.fill("#home-search", "dagger")
    page.press("#home-search", "Enter")
    page.wait_for_function("location.hash.startsWith('#/search')")
    page.wait_for_selector('.search-group[data-game="statues"] [data-model]')
    page.wait_for_selector('.search-group[data-game="Dark Souls III"] .card-name:text-is("Dagger")')
    assert page.input_value("#search") == "dagger"
    assert page.inner_text("#search-count") == "2 models"
    page.click('#filter-games button:has-text("statues")')
    page.wait_for_function("document.querySelectorAll('#search-results [data-model]').length === 1")
    page.click("#filter-clear")
    page.wait_for_function("document.querySelectorAll('#search-results [data-model]').length === 2")
    # "/" from a page without its own box goes to the menu's, which searches every game too
    statue = page.locator('.search-group[data-game="statues"] [data-model]')
    statue.dblclick()
    page.wait_for_selector("#stage-canvas:not([data-loading])", timeout=30000)
    page.keyboard.press("/")
    assert page.evaluate("document.activeElement.id") == "nav-search-box"
    page.keyboard.type("knight")
    page.keyboard.press("Enter")
    page.wait_for_selector('#search-results .card-name:text-is("Knight Helm")')
    page.goto(app.url + "#/")
    page.wait_for_selector("#recent-models [data-model]")
    assert "dagger_statue" in page.inner_text("#recent-models")


def _tetrahedra(n):
    """An OBJ file of ``n`` tetrahedra side by side (4 triangles each)."""
    lines = []
    for k in range(n):
        lines += [f"v {k} 0 0", f"v {k + 0.5} 0 0", f"v {k} 0.5 0", f"v {k} 0 0.5"]
    for k in range(n):
        a = 4 * k + 1
        lines += [f"f {a} {a + 1} {a + 2}", f"f {a} {a + 1} {a + 3}", f"f {a} {a + 2} {a + 3}", f"f {a + 1} {a + 2} {a + 3}"]
    return "\n".join(lines) + "\n"


def test_the_list_view_sorts_by_its_columns(page, app, tmp_path):
    """The list view has a column for each thing known of a model; a header sorts by it (again: the other
    way), and the sort is kept for that page."""
    from test_app import wait

    statues = tmp_path / "statues"
    statues.mkdir()
    for name, n in (("knight", 3), ("dragon", 9), ("rat", 1)):
        (statues / f"{name}.obj").write_text(_tetrahedra(n))
    wait(app.service, app.service.call("folder_add", {"path": str(statues)})["job"])
    for item in app.service.call("models_query", {"limit": 100})["items"]:
        app.service.call("model_open", {"id": item["id"]})  # their sizes and counts are learnt when opened
    page.goto(app.url + "#/browse/statues/Models")
    page.reload()
    page.wait_for_selector(".results [data-model]")
    page.click('#layout-switch [data-view="list"]')
    page.wait_for_selector("#list-head")
    heads = page.locator("#list-head .sort-head").all_inner_texts()
    assert heads[1] == "Kind ↑"  # sorted by kind until a header is clicked
    assert [h.rstrip(" ↑↓") for h in heads] == ["Name", "Kind", "Size", "Triangles", "Parts", "Textures", "Animations", "Collected"]
    tris = "Array.from(document.querySelectorAll('.lib-row [data-col=\"triangles\"]')).map((e) => e.textContent).join(' ')"
    page.click('#list-head [data-sort="triangles"]')
    page.wait_for_selector('#list-head [data-sort="triangles"][aria-pressed="true"]')
    page.wait_for_function(f"{tris} === '4 12 36'")
    page.click('#list-head [data-sort="triangles"]')  # again: the other way
    page.wait_for_function(f"{tris} === '36 12 4'")
    assert "↓" in page.inner_text('#list-head [data-sort="triangles"]')
    page.click('#list-head [data-sort="name"]')
    page.wait_for_function("Array.from(document.querySelectorAll('.lib-row .lc-name-text')).map((e) => e.textContent).join(' ') === 'dragon knight rat'")
    page.click('#list-head [data-sort="size"]')
    page.wait_for_function(f"{tris} === '4 12 36'")  # the longest side grows with the count here
    # kept for this page, not the others
    page.goto(app.url + "#/search?q=a")
    page.wait_for_selector("#search-results .lib-row")
    assert page.locator('#list-head [data-sort="size"][aria-pressed="true"]').count() == 0
    page.goto(app.url + "#/browse/statues/Models")
    page.wait_for_selector('#list-head [data-sort="size"][aria-pressed="true"]')
    # and in the preferences, so it's there when the app is opened again (saved a moment after the click)
    # (an earlier sort may be saved already: wait for this one, the page saves 250 ms after the last change)
    want = {"browse:statues/Models": {"by": "size", "desc": False}}
    end = time.time() + 10
    while time.time() < end and app.service.call("prefs_get", {}).get("ds-ui", {}).get("sorts") != want:
        time.sleep(0.05)
    assert app.service.call("prefs_get", {})["ds-ui"]["sorts"] == want
    page.reload()
    page.wait_for_selector('#list-head [data-sort="size"][aria-pressed="true"]')


def _triangles(page) -> int:
    """The triangle count the stage's footer shows."""
    return int("".join(ch for ch in page.inner_text("#stage-tris") if ch.isdigit()))


def test_turn_a_bone_save_the_pose_and_attach_a_model(page, app, game):  # noqa: F811
    """On the Pose tab: choose a bone, turn it with a slider's box, save the pose, open the model again and apply
    it; then attach another model to a bone and see the triangle count rise."""
    from test_app import wait

    shots = os.environ.get("DARKSTL_SCREENSHOTS")
    wait(app.service, app.service.call("folder_add", {"path": str(game)})["job"])
    items = {i["stem"]: i for i in app.service.call("models_query", {"game": "Dark Souls III", "limit": 100})["items"]}
    page.goto(app.url + f"#/model/{items['c2000']['id']}")
    page.reload()  # the game was added behind the page's back
    page.wait_for_selector("#stage-canvas:not([data-loading])", timeout=30000)
    before = _triangles(page)
    page.click("#rail-pose")
    page.wait_for_selector('#bone-list .bone-row[data-bone="hand"]')
    page.click('#bone-list .bone-row[data-bone="hand"]')
    page.wait_for_selector('#bone-edit[data-bone="hand"]')
    assert page.is_enabled("#bone-mirror") is False  # "hand" has no partner on the other side
    page.fill("#bone-z-number", "45")
    page.press("#bone-z-number", "Enter")
    page.wait_for_function("document.querySelector('#stage-pose')?.textContent.includes('1 bone turned')")
    page.wait_for_selector('#bone-list .bone-row[data-bone="hand"] .bone-turned')
    page.wait_for_selector("#stage-canvas:not([data-loading])", timeout=30000)
    # a drag of the gizmo's blue ring: another 45° about the bone's own Z axis, added to the turn there
    page.evaluate("""() => { const v = window.__viewer;
      v.gizmo.dispatchEvent({ type: "dragging-changed", value: true });
      v.boneHandle.rotateZ(Math.PI / 4);
      v.gizmo.dispatchEvent({ type: "dragging-changed", value: false }); }""")
    page.wait_for_function("document.querySelector('#bone-z-number').value === '90'")
    page.wait_for_selector("#stage-canvas:not([data-loading])", timeout=30000)
    if shots:
        page.screenshot(path=os.path.join(shots, "pose-bones.png"))
    page.fill("#pose-save-name", "Wave")
    page.click("#pose-save")
    page.wait_for_selector('#pose-saved-own .saved-pose[data-pose="Wave"]')
    page.wait_for_function("document.querySelector('#stage-pose')?.textContent.includes('Wave')")
    # open the model again, back in its bind pose, and apply the saved pose
    page.goto(app.url + "#/browse/Dark%20Souls%20III")
    page.reload()
    page.goto(app.url + f"#/model/{items['c2000']['id']}")
    page.wait_for_selector("#stage-canvas:not([data-loading])", timeout=30000)
    assert page.locator("#stage-pose").count() == 0
    if page.get_attribute("#rail-pose", "aria-pressed") != "true":  # the page opens on the tab it was left on
        page.click("#rail-pose")
    page.wait_for_selector('#pose-saved-own .saved-pose[data-pose="Wave"]')
    page.click('#pose-saved-own .saved-pose[data-pose="Wave"] .pose-apply')
    page.wait_for_function("document.querySelector('#stage-pose')?.textContent.includes('Wave, 1 bone turned')")
    page.wait_for_selector('#bone-list .bone-row[data-bone="hand"] .bone-turned')
    # attach the model without animations to the hand
    page.click("#attach-add")
    page.wait_for_selector("#attach-dialog")
    page.fill("#attach-search", "c2001")
    page.wait_for_selector('#attach-results [data-model="c2001"]')
    page.click('#attach-results [data-model="c2001"] input')
    page.select_option("#attach-dialog-holder", "bone:hand")
    if shots:
        page.screenshot(path=os.path.join(shots, "attach.png"))
    page.click("#attach-dialog-ok")
    page.wait_for_function("!document.querySelector('#attach-dialog')")
    page.wait_for_selector("#attach-list .attach-row", timeout=30000)
    page.wait_for_selector("#stage-canvas:not([data-loading])", timeout=30000)
    page.wait_for_function(f"parseInt((document.querySelector('#stage-tris')?.textContent || '').replace(/\\D/g, '')) > {before}",
                           timeout=30000)
    assert _triangles(page) == 2 * before
    assert "Wave" in page.inner_text("#stage-pose")  # the pose stays as the model opens again with it
    # moved 50 mm out along the hand, kept with the model
    page.wait_for_selector("#attach-edit")  # the one just attached, to place
    page.fill("#attach-offset-x", "50")
    page.press("#attach-offset-x", "Enter")
    from darkstl import overlay

    kept_in = app.service._overlay("Dark Souls III").path
    deadline = time.time() + 15
    kept = []
    while time.time() < deadline:
        kept = overlay.Overlay(kept_in, "Dark Souls III").attachments("chr/c2000.chrbnd.dcx")
        if kept and kept[0]["place"].get("offset_mm", [0])[0] == 50:
            break
        time.sleep(0.2)
    assert kept and kept[0]["place"]["bone"] == "hand" and kept[0]["place"]["offset_mm"][0] == 50
    page.click("#rail-export")
    assert page.get_attribute("#export-name", "placeholder").endswith("_Wave")


# ----------------------------------------------------------------- Library II (0.48)


def test_collections_tags_and_stars_from_the_page(page, app, weapon_dir):  # noqa: F811
    """A card's right-click menu: Add to collection… (it shows in the menu, with its models on its page), Tag… (a
    filter when searching every game) and Star (Starred in the menu); a card dragged onto a collection joins it."""
    from test_app import wait

    wait(app.service, app.service.call("folder_add", {"path": str(weapon_dir)})["job"])
    page.goto(app.url + "#/browse/Dark%20Souls%20III/Weapons")
    page.wait_for_selector(".results [data-model]")
    cards = page.locator(".results [data-model]")
    first, second = cards.nth(0), cards.nth(1)
    first_id, second_id = first.get_attribute("data-model"), second.get_attribute("data-model")
    first.click(button="right")
    page.click('[data-action="add-collection"]')
    page.wait_for_selector("#collection-add-dialog")
    page.fill("#collection-new-name", "To print")
    page.click("#collection-add-dialog-ok")
    page.wait_for_selector('#collection-list [data-collection] >> text=To print')
    link = page.locator("#collection-list [data-collection]").first
    assert link.locator(".nav-count").inner_text() == "1"
    # dragged onto it in the menu: added too
    second.drag_to(link)
    page.wait_for_function("document.querySelector('#collection-list [data-collection] .nav-count')?.textContent === '2'")
    # tagged and starred from the menu
    first.click(button="right")
    page.click('[data-action="tag"]')
    page.wait_for_selector("#tag-dialog")
    page.fill("#tag-input", "needs supports")
    page.click("#tag-dialog-ok")
    page.wait_for_selector(f'.results [data-model="{first_id}"] .card-tag >> text=needs supports')
    second.click(button="right")
    page.click('[data-action="star"]')
    page.wait_for_selector(f'.results [data-model="{second_id}"] .card-star')
    page.wait_for_function("document.querySelector('#nav-starred .nav-count')?.textContent === '1'")
    # the collection's page lists them in its order
    link.click()
    page.wait_for_selector("#collection-head")
    page.wait_for_function("document.querySelectorAll('#collection-results [data-model]').length === 2")
    assert page.locator("#collection-results [data-model]").first.get_attribute("data-model") == first_id
    assert page.inner_text("#collection-name-title") == "To print"
    # Starred
    page.click("#nav-starred")
    page.wait_for_function(f"document.querySelectorAll('#collection-results [data-model]').length === 1")
    assert page.locator("#collection-results [data-model]").first.get_attribute("data-model") == second_id
    # the tag is a filter when searching every game
    page.goto(app.url + "#/search")
    page.wait_for_selector("#filter-tags .chip-btn")
    page.click("#filter-tags .chip-btn")
    page.wait_for_function("document.querySelectorAll('#search-results [data-model]').length === 1")
    assert page.locator("#search-results [data-model]").first.get_attribute("data-model") == first_id
    # Home shows the collections
    page.goto(app.url + "#/")
    page.wait_for_selector('#collection-tiles [data-collection="starred"]')
    assert page.locator("#collection-tiles [data-collection]").count() == 2


def test_choose_several_and_export_them_through_the_queue(page, app, weapon_dir, tmp_path):  # noqa: F811
    """Shift+click chooses a run of cards; their right-click menu queues Export as GLB, which writes one file each;
    the Queue page lists it. A card held under the pointer turns round its model."""
    from test_app import wait

    out = tmp_path / "exports"
    app.service.call("settings_set", {"export_dir": str(out)})
    wait(app.service, app.service.call("folder_add", {"path": str(weapon_dir)})["job"])
    page.goto(app.url + "#/browse/Dark%20Souls%20III/Weapons")
    page.wait_for_selector('.results [data-thumb="ok"]', timeout=30000)
    cards = page.locator(".results [data-model]")
    # hovered a moment: the picture turns (its turntable drawn in the background, or now)
    cards.nth(0).hover()
    page.wait_for_selector(".results .thumb-turn img", timeout=30000)
    page.mouse.move(0, 0)
    page.wait_for_selector(".results .thumb-turn", state="detached")
    cards.nth(0).click()
    cards.nth(1).click(modifiers=["Shift"])
    page.wait_for_selector("#browse-head[data-selecting]")
    assert page.inner_text("#bar-count") == "2 models chosen"
    page.wait_for_selector("#selection-summary")
    page.keyboard.press("Escape")
    page.wait_for_selector("#browse-head[data-selecting]", state="detached")
    page.keyboard.press("Control+a")
    page.wait_for_selector("#browse-head[data-selecting]")
    cards.nth(1).click(button="right")
    page.click('[data-action="bulk-glb"]')
    page.wait_for_selector("#toast >> text=Queued: Exporting 2 models as GLB")
    page.wait_for_selector("#toast >> text=Exported 2 models as GLB", timeout=120000)
    assert len(list(out.rglob("*.glb"))) == 2
    page.click("#nav-queue")
    page.wait_for_selector('#queue-list [data-state="done"]')
    assert "2 models exported" in page.inner_text("#queue-list")


def test_first_run_offers_a_tile_for_each_game(page, app):
    page.wait_for_selector("#first-games")
    assert page.locator("#first-games [data-game]").count() >= 20
    page.click('#first-games [data-game="Elden Ring"]')
    page.wait_for_selector("#first-game-note")
    assert "unpack" in page.inner_text("#first-game-note") and "Oodle" in page.inner_text("#first-game-note")
    page.click('#first-games [data-game="Cyberpunk 2077"]')
    page.wait_for_selector('#first-game-note[data-game="Cyberpunk 2077"]')
    page.click("#first-add-game")
    page.wait_for_selector("#add-folder-dialog")
    assert page.input_value("#folder-game") == "Cyberpunk 2077"
