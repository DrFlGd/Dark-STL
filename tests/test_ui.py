"""Headless smoke test of the main window: scan, open, enhance and export without any dialogs."""

import os
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["DARKSTL_NO_3D"] = "1"

from PySide6.QtCore import QModelIndex, QSettings, QThreadPool  # noqa: E402
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
    wait_for(lambda: window.asset is not None)
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
