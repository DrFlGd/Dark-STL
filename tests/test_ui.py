"""Headless smoke test of the main window: scan, open, enhance and export without any dialogs."""

import os
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["DARKSTL_NO_3D"] = "1"

from PySide6.QtCore import QModelIndex, QSettings, Qt, QThreadPool  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from darkstl import export  # noqa: E402

from test_pipeline import weapon_dir  # noqa: E402,F401 - fixture


@pytest.fixture
def window(tmp_path):
    for fmt in (QSettings.NativeFormat, QSettings.IniFormat):
        QSettings.setPath(fmt, QSettings.UserScope, str(tmp_path / "settings"))
    app = QApplication.instance() or QApplication([])
    from darkstl.ui.main_window import MainWindow

    win = MainWindow([])
    yield win
    QThreadPool.globalInstance().waitForDone(10_000)
    win.close()
    app.processEvents()


def wait_for(condition, timeout=30.0):
    app = QApplication.instance()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for the window")


def leaves(model, parent=QModelIndex()):
    for r in range(model.rowCount(parent)):
        index = model.index(r, 0, parent)
        if model.rowCount(index):
            yield from leaves(model, index)
        else:
            yield index


def test_window_scan_open_enhance_export(window, weapon_dir, tmp_path):  # noqa: F811
    window.add_root(str(weapon_dir))
    wait_for(lambda: window.tree.model().rowCount() > 0)
    assert window.tree.model().index(0, 0).data().startswith("Dark Souls III")
    labels = [i.data() for i in leaves(window.tree.model())]
    assert any("wp_a_0300" in s for s in labels) and not any("wp_a_0300_l" in s for s in labels)

    window.search.setText("hd_m")
    assert [i.data() for i in leaves(window.tree.model())] == ["Head 1000 (Male)  -  hd_m_1000"]
    window.search.setText("")

    leaf = next(i for i in leaves(window.tree.model()) if i.data().endswith("wp_a_0300"))
    window._on_tree_activated(leaf)
    wait_for(lambda: window.model is not None)
    assert window.source_mesh.n_faces == 32
    assert window.texture_list.count() == 2

    window.target.setValue(5_000)
    window.run_enhance()
    wait_for(lambda: window.result is not None)
    assert window.view_which.currentText() == "Enhanced"
    assert window.result.displaced.n_vertices > 3_000

    out = tmp_path / "wp.stl"
    window.export_to(str(out), window.result.displaced)
    wait_for(lambda: out.exists() and out.stat().st_size > 84)
    QThreadPool.globalInstance().waitForDone(10_000)
    assert export.read_stl(out).shape[0] == window.result.displaced.n_faces


def test_window_variants_and_placeholders(window, tmp_path):
    import synth
    from darkstl.importers.fromsoft.formats import dcx

    pos, nrm, uv, idx = synth.grid_plane(1)
    sword = synth.build_flver([synth.SynthMesh(pos, nrm, uv, idx)], [("m", "m.mtd", [])])
    sheath = synth.build_flver([synth.SynthMesh(pos * 2, nrm, uv, idx)] * 2, [("m", "m.mtd", [])])
    pair = tmp_path / "wp_a_0402.partsbnd.dcx"
    pair.write_bytes(dcx.compress_dflt(synth.build_bnd3([(200, "WP_A_0402.flver", sword),
                                                         (201, "WP_A_0402_1.flver", sheath)])))
    window.load(pair, None)
    wait_for(lambda: window.model is not None)
    assert not window.variant_row.isHidden() and window.variant.count() == 2
    assert window.mesh_table.rowCount() == 1 and list(window.option_boxes) == ["bones"]
    window.variant.setCurrentIndex(1)
    window._on_variant_chosen(1)
    wait_for(lambda: window.model.variant == "WP_A_0402_1.flver")
    assert window.mesh_table.rowCount() == 2 and window.source_mesh.n_faces == 4
    window.mesh_table.item(0, 0).setCheckState(Qt.Unchecked)
    assert window.source_mesh.n_faces == 2

    empty = tmp_path / "c2509.chrbnd.dcx"
    empty.write_bytes(dcx.compress_dflt(synth.build_bnd3([(200, "c2509.flver", synth.build_flver([], []))])))
    window.load(empty, None)
    wait_for(lambda: window.model.name == "c2509")
    assert window.variant_row.isHidden()
    assert "no geometry" in window.model_notes.text() and not window.model_notes.isHidden()
    assert not window.run_button.isEnabled() and not window.export_button.isEnabled()
