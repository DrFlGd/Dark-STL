"""Main window: library tree on the left, 3D view in the middle, tools on the right."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PySide6.QtCore import QSettings, QSortFilterProxyModel, Qt
from PySide6.QtGui import QAction, QImage, QKeySequence, QPixmap, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..assets import ModelAsset, load_model
from ..export import FORMATS, ExportOptions, export_mesh
from ..geometry.mesh import TriMesh
from ..geometry.model_mesh import MeshOptions, flver_to_trimesh
from ..geometry.remesh import acvd_available
from ..library import GAMES, CatalogEntry, Library
from ..materials import MaterialInfo, resolve_materials
from ..pipeline import EnhanceResult, EnhanceSettings, enhance
from . import workers
from .viewport import Viewport

ENTRY_ROLE = Qt.UserRole + 1


def numpy_to_pixmap(img: np.ndarray, max_size: int = 320) -> QPixmap:
    arr = img if img.dtype == np.uint8 else np.clip(img * 255, 0, 255).astype(np.uint8)
    if arr.ndim == 2:
        arr = np.dstack([arr, arr, arr, np.full_like(arr, 255)])
    arr = np.ascontiguousarray(arr[..., :4] if arr.shape[2] >= 4 else np.dstack([arr, np.full(arr.shape[:2], 255,
                                                                                            np.uint8)]))
    h, w = arr.shape[:2]
    qimg = QImage(arr.data, w, h, 4 * w, QImage.Format_RGBA8888).copy()
    pix = QPixmap.fromImage(qimg)
    return pix.scaled(max_size, max_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)


class MainWindow(QMainWindow):
    def __init__(self, folders: list[str] | None = None):
        super().__init__()
        self.setWindowTitle(f"Dark STL {__version__}")
        self.resize(1500, 900)
        self.settings = QSettings("DarkSTL", "DarkSTL")
        self.library = Library()
        self.asset: ModelAsset | None = None
        self.entry: CatalogEntry | None = None
        self.materials: list[MaterialInfo] = []
        self.source_mesh: TriMesh | None = None
        self.result: EnhanceResult | None = None
        self._jobs = []

        self.viewport = Viewport(self)
        self.setCentralWidget(self.viewport)
        self._build_library_dock()
        self._build_tools_dock()
        self._build_actions()
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(260)
        self.progress.setRange(0, 1000)
        self.progress.hide()
        self.statusBar().addPermanentWidget(self.progress)
        self.statusBar().showMessage("Add a folder of extracted game files to begin (File > Add game folder).")

        self._restore_settings()
        for folder in folders or []:
            self.add_root(folder)

    # ------------------------------------------------------------------ layout

    def _build_library_dock(self) -> None:
        dock = QDockWidget("Library", self)
        dock.setObjectName("library")
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(6, 6, 6, 6)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search models (e.g. wp_a_03, c2010, Head)")
        self.show_lod = QCheckBox("Show low-detail (_l) copies")
        self.tree_model = QStandardItemModel()
        self.proxy = QSortFilterProxyModel()
        self.proxy.setSourceModel(self.tree_model)
        self.proxy.setRecursiveFilteringEnabled(True)
        self.proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.tree = QTreeView()
        self.tree.setModel(self.proxy)
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.activated.connect(self._on_tree_activated)
        self.tree.clicked.connect(self._on_tree_activated)
        self.search.textChanged.connect(self._on_search)
        self.show_lod.toggled.connect(lambda _: self.rebuild_tree())
        add = QPushButton("Add game folder...")
        add.clicked.connect(self.choose_root)
        v.addWidget(add)
        v.addWidget(self.search)
        v.addWidget(self.tree, 1)
        v.addWidget(self.show_lod)
        dock.setWidget(w)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)
        self.library_dock = dock

    def _build_tools_dock(self) -> None:
        dock = QDockWidget("Tools", self)
        dock.setObjectName("tools")
        tabs = self.tools_tabs = QTabWidget()
        tabs.addTab(self._scroll(self._build_model_tab()), "Model")
        tabs.addTab(self._scroll(self._build_textures_tab()), "Textures")
        tabs.addTab(self._scroll(self._build_enhance_tab()), "Enhance")
        tabs.addTab(self._scroll(self._build_export_tab()), "Export")
        self.tabs = tabs
        dock.setWidget(tabs)
        dock.setMinimumWidth(380)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)

    @staticmethod
    def _scroll(widget: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(widget)
        return area

    def _build_model_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        self.model_info = QLabel("No model loaded")
        self.model_info.setWordWrap(True)
        self.model_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self.model_info)
        box = QGroupBox("Meshes (untick to leave out of preview and export)")
        bv = QVBoxLayout(box)
        self.mesh_table = QTableWidget(0, 4)
        self.mesh_table.setHorizontalHeaderLabels(["Mesh", "Material", "Vertices", "Triangles"])
        self.mesh_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.mesh_table.verticalHeader().hide()
        self.mesh_table.itemChanged.connect(self._on_mesh_toggled)
        bv.addWidget(self.mesh_table)
        self.bone_toggle = QCheckBox("Place rigid parts using their bones")
        self.bone_toggle.setChecked(True)
        self.bone_toggle.setToolTip("Non-skinned meshes are stored relative to a bone; untick if parts look misplaced.")
        self.bone_toggle.toggled.connect(lambda _: self.rebuild_source_mesh())
        bv.addWidget(self.bone_toggle)
        v.addWidget(box)
        view = QGroupBox("View")
        fv = QFormLayout(view)
        self.view_mode = QComboBox()
        self.view_mode.addItems(["Textured", "Clay", "Wireframe"])
        self.view_mode.currentIndexChanged.connect(lambda _: self.refresh_view(reset_camera=False))
        self.view_which = QComboBox()
        self.view_which.addItems(["Original", "Enhanced"])
        self.view_which.setEnabled(False)
        self.view_which.currentIndexChanged.connect(lambda _: self.refresh_view(reset_camera=False))
        fv.addRow("Shading", self.view_mode)
        fv.addRow("Show", self.view_which)
        v.addWidget(view)
        v.addStretch(1)
        return w

    def _build_textures_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        self.material_box = QGroupBox("Detail map used by each material")
        self.material_form = QFormLayout(self.material_box)
        v.addWidget(self.material_box)
        v.addWidget(QLabel("Textures found for this model:"))
        self.texture_list = QListWidget()
        self.texture_list.currentItemChanged.connect(self._on_texture_selected)
        v.addWidget(self.texture_list, 1)
        self.texture_preview = QLabel()
        self.texture_preview.setAlignment(Qt.AlignCenter)
        self.texture_preview.setMinimumHeight(240)
        v.addWidget(self.texture_preview)
        self.texture_meta = QLabel()
        v.addWidget(self.texture_meta)
        row = QHBoxLayout()
        save = QPushButton("Save PNG...")
        save.clicked.connect(self.save_texture_png)
        add_dir = QPushButton("Add texture folder...")
        add_dir.setToolTip("Folders searched for textures a model references but does not contain (map textures).")
        add_dir.clicked.connect(self.add_texture_dir)
        row.addWidget(save)
        row.addWidget(add_dir)
        v.addLayout(row)
        return w

    def _build_enhance_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        step1 = QGroupBox("1. Even out vertex spacing")
        f1 = QFormLayout(step1)
        self.target = QSpinBox()
        self.target.setRange(1_000, 50_000_000)
        self.target.setSingleStep(250_000)
        self.target.setGroupSeparatorShown(True)
        self.target.setValue(1_000_000)
        self.method = QComboBox()
        self.method.addItem("Adaptive subdivision (exact UVs, keeps sharp edges)", "subdivide")
        self.method.addItem("Isotropic remesh (exact count, uniform triangles)", "isotropic")
        if not acvd_available():
            self.method.model().item(1).setEnabled(False)
            self.method.setItemText(1, "Isotropic remesh (install pyacvd)")
        self.spacing_label = QLabel("-")
        self.target.valueChanged.connect(self._update_spacing_label)
        self.smooth = QCheckBox("Round off curved surfaces")
        self.smooth.setChecked(True)
        self.smooth.setToolTip("New vertices follow the curve the model's shading normals describe, so low-poly "
                               "cylinders and blades print round instead of faceted. Flat-shaded parts stay flat.")
        f1.addRow("Target vertices", self.target)
        f1.addRow("Method", self.method)
        f1.addRow("Vertex spacing", self.spacing_label)
        f1.addRow("", self.smooth)
        v.addWidget(step1)

        step2 = QGroupBox("2. Displace with texture detail")
        f2 = QFormLayout(step2)
        self.source = QComboBox()
        self.source.addItem("Normal map (integrated to height)", "normal")
        self.source.addItem("Height / grayscale map", "height")
        self.source.addItem("None (remesh only)", "none")
        self.source.currentIndexChanged.connect(lambda _: self._sync_detail_choices())
        self.strength = QDoubleSpinBox()
        self.strength.setRange(0.0, 50.0)
        self.strength.setSingleStep(0.25)
        self.strength.setValue(1.0)
        self.strength.setToolTip("1.0 reproduces the relief the normal map encodes; raise it to exaggerate.")
        self.height_range = QDoubleSpinBox()
        self.height_range.setRange(0.0, 100.0)
        self.height_range.setValue(1.0)
        self.height_range.setSuffix(" mm")
        self.green = QComboBox()
        self.green.addItem("Auto-detect", "auto")
        self.green.addItem("DirectX (Y-)", "directx")
        self.green.addItem("OpenGL (Y+)", "opengl")
        self.detail = QDoubleSpinBox()
        self.detail.setRange(0.0, 0.5)
        self.detail.setDecimals(3)
        self.detail.setSingleStep(0.01)
        self.detail.setValue(1 / 16)
        self.detail.setToolTip("High-pass radius as a fraction of texture size. Larger keeps broader shapes; "
                               "0 keeps everything (may bulge).")
        self.max_disp = QDoubleSpinBox()
        self.max_disp.setRange(0.0, 100.0)
        self.max_disp.setSpecialValueText("No limit")
        self.max_disp.setSuffix(" mm")
        f2.addRow("Source", self.source)
        f2.addRow("Strength", self.strength)
        f2.addRow("Height map range", self.height_range)
        f2.addRow("Normal map green", self.green)
        f2.addRow("Detail radius", self.detail)
        f2.addRow("Clamp displacement", self.max_disp)
        v.addWidget(step2)

        self.run_button = QPushButton("Enhance model")
        self.run_button.setEnabled(False)
        self.run_button.clicked.connect(self.run_enhance)
        v.addWidget(self.run_button)
        self.enhance_log = QTextEdit()
        self.enhance_log.setReadOnly(True)
        self.enhance_log.setMinimumHeight(140)
        v.addWidget(self.enhance_log)
        v.addStretch(1)
        return w

    def _build_export_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        box = QGroupBox("Export")
        f = QFormLayout(box)
        self.export_format = QComboBox()
        for ext, desc in FORMATS.items():
            self.export_format.addItem(f"{desc} ({ext})", ext)
        self.export_which = QComboBox()
        self.export_which.addItems(["Enhanced model", "Original model"])
        self.export_units = QComboBox()
        self.export_units.addItem("Millimetres (x1000)", 1000.0)
        self.export_units.addItem("Metres (game units)", 1.0)
        self.export_units.addItem("Centimetres (x100)", 100.0)
        self.export_units.addItem("Inches (x39.37)", 39.3701)
        self.export_textures = QCheckBox("Include albedo textures (OBJ / GLB)")
        self.export_textures.setChecked(True)
        f.addRow("Format", self.export_format)
        f.addRow("Model", self.export_which)
        f.addRow("Units", self.export_units)
        f.addRow(self.export_textures)
        v.addWidget(box)
        self.export_button = QPushButton("Export...")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self.export_current)
        v.addWidget(self.export_button)
        note = QLabel("STL and PLY are written Z-up for slicers. OBJ and GLB are Y-up. "
                      "Game models are often not watertight; check them in your slicer before printing.")
        note.setWordWrap(True)
        v.addWidget(note)
        v.addStretch(1)
        return w

    def _build_actions(self) -> None:
        menu = self.menuBar().addMenu("&File")
        act = QAction("Add game folder...", self, shortcut=QKeySequence.Open, triggered=self.choose_root)
        menu.addAction(act)
        menu.addAction(QAction("Open model file...", self, triggered=self.open_file))
        menu.addAction(QAction("Add texture folder...", self, triggered=self.add_texture_dir))
        menu.addAction(QAction("Set Oodle DLL (Sekiro / Elden Ring)...", self, triggered=self.choose_oodle))
        menu.addSeparator()
        menu.addAction(QAction("Remove all game folders", self, triggered=self.clear_roots))
        menu.addSeparator()
        menu.addAction(QAction("Quit", self, shortcut=QKeySequence.Quit, triggered=self.close))
        view = self.menuBar().addMenu("&View")
        view.addAction(self.library_dock.toggleViewAction())
        view.addAction(QAction("Save screenshot...", self, triggered=self.save_screenshot))
        help_menu = self.menuBar().addMenu("&Help")
        help_menu.addAction(QAction("About", self, triggered=self.about))

    # --------------------------------------------------------------- settings

    def _restore_settings(self) -> None:
        roots = self.settings.value("roots", [], type=list) or []
        games = self.settings.value("root_games", [], type=list) or []
        oodle_path = self.settings.value("oodle", "", type=str)
        if oodle_path:
            from ..formats import oodle

            oodle.add_search_dir(oodle_path)
        for root, game in zip(roots, games + [None] * len(roots)):
            if Path(root).is_dir():
                self.add_root(root, game or None, save=False)

    def _save_roots(self) -> None:
        self.settings.setValue("roots", [str(p) for p in self.library.roots])
        self.settings.setValue("root_games", list(self.library.roots.values()))

    def texture_dirs(self) -> tuple[str, ...]:
        return tuple(self.settings.value("texture_dirs", [], type=list) or [])

    # ---------------------------------------------------------------- library

    def choose_root(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Folder with extracted game files")
        if not folder:
            return
        from ..library import detect_game

        guess = detect_game(folder)
        options = list(GAMES)
        idx = options.index(guess) if guess in options else 0
        game, ok = QInputDialog.getItem(self, "Which game?", "These files are from:", options, idx, True)
        if ok:
            self.add_root(folder, game)

    def add_root(self, folder: str, game: str | None = None, save: bool = True) -> None:
        self.statusBar().showMessage(f"Scanning {folder} ...")

        def job(progress):
            lib = Library()
            lib.add_root(folder, game, lambda i, name: progress(0.0, f"Scanning: {name}"))
            return lib

        def done(lib: Library):
            self.library.merge(lib)
            self.rebuild_tree()
            n = len(lib.entries)
            self.statusBar().showMessage(f"Found {n:,} models in {folder}")
            if save:
                self._save_roots()

        self._run(job, done, busy=f"Scanning {folder}")

    def clear_roots(self) -> None:
        self.library = Library()
        self._save_roots()
        self.rebuild_tree()

    def rebuild_tree(self) -> None:
        self.tree_model.clear()
        root = self.tree_model.invisibleRootItem()
        for game, cats in self.library.grouped(include_lod=self.show_lod.isChecked()).items():
            g_item = QStandardItem(game)
            g_item.setEditable(False)
            total = 0
            for cat, groups in cats.items():
                c_item = QStandardItem(cat)
                c_item.setEditable(False)
                count = 0
                for group, entries in groups.items():
                    parent = c_item
                    if group and len(groups) > 1:
                        parent = QStandardItem(f"{group} ({len(entries)})")
                        parent.setEditable(False)
                        c_item.appendRow(parent)
                    for e in entries:
                        label = e.label if e.label == e.stem else f"{e.label}  -  {e.stem}"
                        item = QStandardItem(label + ("" if e.packed else "  (unpacked)"))
                        item.setEditable(False)
                        item.setData(e, ENTRY_ROLE)
                        item.setToolTip(str(e.path))
                        parent.appendRow(item)
                    count += len(entries)
                c_item.setText(f"{cat} ({count})")
                g_item.appendRow(c_item)
                total += count
            g_item.setText(f"{game} ({total})")
            root.appendRow(g_item)
        self.tree.expandToDepth(0)

    def _on_search(self, text: str) -> None:
        self.proxy.setFilterFixedString(text)
        if text:
            self.tree.expandAll()

    def _on_tree_activated(self, index) -> None:
        entry = self.proxy.data(index, ENTRY_ROLE)
        if isinstance(entry, CatalogEntry) and entry is not self.entry:
            self.load(entry.path, entry)

    def open_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open model", "",
                                              "Models (*.partsbnd *.chrbnd *.objbnd *.geombnd *.mapbnd *.flver *.dcx)"
                                              ";;All files (*)")
        if path:
            self.load(Path(path), None)

    # ----------------------------------------------------------------- loading

    def load(self, path: Path, entry: CatalogEntry | None) -> None:
        dirs = self.texture_dirs()

        def job(progress):
            progress(0.1, f"Reading {path.name}")
            asset = load_model(path, extra_texture_dirs=dirs)
            progress(0.6, "Building mesh")
            mesh = flver_to_trimesh(asset.flver, MeshOptions())
            mats = resolve_materials(asset)
            return asset, mesh, mats

        def done(res):
            self.asset, self.source_mesh, self.materials = res
            self.entry = entry
            self.result = None
            self.view_which.setCurrentIndex(0)
            self.view_which.setEnabled(False)
            self._populate_model_tab()
            self._populate_textures_tab()
            self._update_spacing_label()
            self.run_button.setEnabled(True)
            self.export_button.setEnabled(True)
            self.enhance_log.clear()
            for note in self.asset.notes:
                self.enhance_log.append(note)
            self.refresh_view(reset_camera=True)
            self.statusBar().showMessage(f"Loaded {self.asset.name}: {self.source_mesh.n_vertices:,} vertices, "
                                         f"{self.source_mesh.n_faces:,} triangles, {len(self.asset.textures)} textures")

        self._run(job, done, busy=f"Loading {path.name}")

    def _populate_model_tab(self) -> None:
        a = self.asset
        f = a.flver
        self.model_info.setText(
            f"<b>{a.name}</b><br>{a.path}<br>FLVER 0x{f.version:X} ({f.game_hint})<br>"
            f"{len(f.meshes)} meshes, {len(f.materials)} materials, {len(f.bones)} bones, "
            f"{len(a.textures)} textures found")
        self.mesh_table.blockSignals(True)
        self.mesh_table.setRowCount(len(f.meshes))
        for i, m in enumerate(f.meshes):
            chk = QTableWidgetItem(str(i))
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk.setCheckState(Qt.Checked)
            mat = f.materials[m.material_index].name if m.material_index < len(f.materials) else "?"
            self.mesh_table.setItem(i, 0, chk)
            self.mesh_table.setItem(i, 1, QTableWidgetItem(mat))
            self.mesh_table.setItem(i, 2, QTableWidgetItem(f"{m.vertices.count:,}"))
            self.mesh_table.setItem(i, 3, QTableWidgetItem(f"{len(m.triangles()):,}"))
        self.mesh_table.blockSignals(False)

    def _populate_textures_tab(self) -> None:
        while self.material_form.rowCount():
            self.material_form.removeRow(0)
        self.normal_choices: dict[int, QComboBox] = {}
        keys = sorted(self.asset.textures)
        for info in self.materials:
            combo = QComboBox()
            combo.addItem("(none)", None)
            for k in keys:
                combo.addItem(self.asset.textures[k].name, k)
            combo.setToolTip("Texture used for displacement (normal map, or height map if chosen as source)")
            self.normal_choices[info.index] = combo
            label = f"{info.name}" + (" *" if info.guessed else "")
            self.material_form.addRow(label, combo)
        self._sync_detail_choices()
        self.texture_list.clear()
        for k in keys:
            ref = self.asset.textures[k]
            item = QListWidgetItem(ref.name)
            item.setData(Qt.UserRole, k)
            item.setToolTip(ref.source)
            self.texture_list.addItem(item)
        self.texture_preview.clear()
        self.texture_meta.clear()
        if keys:
            self.texture_list.setCurrentRow(0)

    def _default_detail(self, info: MaterialInfo) -> str | None:
        return info.height if self.source.currentData() == "height" else info.normal

    def _sync_detail_choices(self) -> None:
        """Point each material's combo at the map the chosen source would use by default."""
        for mat, combo in getattr(self, "normal_choices", {}).items():
            i = combo.findData(self._default_detail(self.materials[mat]))
            combo.setCurrentIndex(max(i, 0))

    def _on_texture_selected(self, item, _prev=None) -> None:
        if item is None or self.asset is None:
            return
        ref = self.asset.textures[item.data(Qt.UserRole)]
        try:
            img = ref.decode()
            info = ref.info()
            self.texture_preview.setPixmap(numpy_to_pixmap(img))
            self.texture_meta.setText(f"{info.width}x{info.height} {info.codec}  -  {ref.source}")
        except Exception as exc:  # noqa: BLE001
            self.texture_preview.setText(f"Could not decode: {exc}")

    def save_texture_png(self) -> None:
        item = self.texture_list.currentItem()
        if item is None:
            return
        ref = self.asset.textures[item.data(Qt.UserRole)]
        path, _ = QFileDialog.getSaveFileName(self, "Save texture", f"{ref.name}.png", "PNG (*.png)")
        if path:
            from ..export import write_png

            write_png(ref.decode(), Path(path))

    def add_texture_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Folder with extra textures (e.g. map textures)")
        if folder:
            dirs = list(self.texture_dirs())
            if folder not in dirs:
                dirs.append(folder)
            self.settings.setValue("texture_dirs", dirs)
            if self.asset is not None:
                self.load(self.asset.path, self.entry)

    def choose_oodle(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Locate oo2core_6_win64.dll (in the Sekiro / Elden Ring folder)",
                                              "", "Oodle (oo2core*.dll liboo2core*);;All files (*)")
        if path:
            from ..formats import oodle

            oodle.add_search_dir(path)
            self.settings.setValue("oodle", path)

    # ------------------------------------------------------------------- mesh

    def included_meshes(self) -> set[int]:
        return {i for i in range(self.mesh_table.rowCount())
                if self.mesh_table.item(i, 0).checkState() == Qt.Checked}

    def _on_mesh_toggled(self, item) -> None:
        if item.column() == 0:
            self.rebuild_source_mesh()

    def mesh_options(self) -> MeshOptions:
        return MeshOptions(include_meshes=self.included_meshes(), apply_bone_transforms=self.bone_toggle.isChecked())

    def rebuild_source_mesh(self) -> None:
        if self.asset is None:
            return
        self.source_mesh = flver_to_trimesh(self.asset.flver, self.mesh_options())
        self.result = None
        self.view_which.setCurrentIndex(0)
        self.view_which.setEnabled(False)
        self._update_spacing_label()
        self.refresh_view(reset_camera=False)

    def _update_spacing_label(self) -> None:
        if self.source_mesh is None or self.source_mesh.n_faces == 0:
            self.spacing_label.setText("-")
            return
        from ..geometry.subdivide import edge_length_for_vertex_count

        s = edge_length_for_vertex_count(self.source_mesh.surface_area(), self.target.value())
        self.spacing_label.setText(f"about {s * 1000:.3f} mm (model has {self.source_mesh.n_vertices:,} vertices)")

    def albedo_textures(self, mesh: TriMesh) -> dict[int, np.ndarray]:
        out = {}
        for m in np.unique(mesh.face_material):
            m = int(m)
            if m < len(self.materials) and self.materials[m].albedo:
                try:
                    out[m] = self.asset.textures[self.materials[m].albedo].decode()
                except Exception:  # noqa: BLE001 - preview falls back to clay
                    pass
        return out

    def current_mesh(self) -> TriMesh | None:
        if self.view_which.currentIndex() == 1 and self.result is not None:
            return self.result.displaced
        return self.source_mesh

    def refresh_view(self, reset_camera: bool = False) -> None:
        mesh = self.current_mesh()
        if mesh is None:
            return
        mode = self.view_mode.currentText().lower()
        textures = self.albedo_textures(mesh) if mode == "textured" else None
        self.viewport.show_mesh(mesh, textures, mode, reset_camera=reset_camera)

    # ---------------------------------------------------------------- enhance

    def enhance_settings(self) -> EnhanceSettings:
        overrides = {}
        for mat, combo in getattr(self, "normal_choices", {}).items():
            key = combo.currentData()
            if key != self._default_detail(self.materials[mat]):
                overrides[mat] = key or ""
        return EnhanceSettings(
            target_vertices=self.target.value(),
            method=self.method.currentData(),
            smooth=0.5 if self.smooth.isChecked() else 0.0,
            source=self.source.currentData(),
            strength=self.strength.value(),
            height_range_mm=self.height_range.value(),
            green=self.green.currentData(),
            detail_fraction=self.detail.value(),
            max_displacement_mm=self.max_disp.value() or None,
            texture_overrides=overrides,
            mesh_options=self.mesh_options(),
        )

    def run_enhance(self) -> None:
        if self.asset is None:
            return
        settings = self.enhance_settings()
        asset = self.asset
        self.run_button.setEnabled(False)
        self.enhance_log.append(f"Enhancing to {settings.target_vertices:,} vertices ({settings.method})...")

        def done(result: EnhanceResult):
            self.run_button.setEnabled(True)
            if asset is not self.asset:  # another model was opened while this one was running
                return
            self.result = result
            st = result.stats()
            self.enhance_log.append(
                f"Done: {st['vertices']:,} vertices, {st['faces']:,} triangles; "
                f"largest displacement {st['max_displacement_mm']:.3f} mm")
            for mat, f in result.fields.items():
                self.enhance_log.append(f"  {self.materials[mat].name}: {asset.textures[f.texture].name}, "
                                        f"green {f.field.convention or 'n/a'}, texel {f.texel_size * 1000:.3f} mm")
            for w in result.warnings:
                self.enhance_log.append(f"  warning: {w}")
            self.view_which.setEnabled(True)
            self.view_which.setCurrentIndex(1)
            self.view_mode.setCurrentText("Clay")
            self.refresh_view(reset_camera=False)

        def failed(msg):
            self.run_button.setEnabled(True)
            self.enhance_log.append(f"Failed: {msg}")

        self._run(lambda progress: enhance(asset, settings, progress), done, failed, busy="Enhancing")

    # ----------------------------------------------------------------- export

    def export_current(self) -> None:
        if self.asset is None:
            return
        use_enhanced = self.export_which.currentIndex() == 0 and self.result is not None
        if self.export_which.currentIndex() == 0 and self.result is None:
            QMessageBox.information(self, "Export", "Run Enhance first, or choose 'Original model'.")
            return
        mesh = self.result.displaced if use_enhanced else self.source_mesh
        ext = self.export_format.currentData()
        start = Path(self.settings.value("export_dir", str(Path.home()), type=str)) / (
            f"{self.asset.name}{'_enhanced' if use_enhanced else ''}{ext}")
        path, _ = QFileDialog.getSaveFileName(self, "Export model", str(start), f"{FORMATS[ext]} (*{ext})")
        if not path:
            return
        if not path.lower().endswith(ext):
            path += ext
        self.settings.setValue("export_dir", str(Path(path).parent))
        self.export_to(path, mesh)

    def export_to(self, path: str, mesh: TriMesh) -> None:
        ext = Path(path).suffix.lower()
        opts = ExportOptions(scale=self.export_units.currentData(),
                             material_names=[m.name for m in self.materials])
        if self.export_textures.isChecked() and ext in (".obj", ".glb"):
            opts.textures = self.albedo_textures(mesh)

        def done(p):
            self.statusBar().showMessage(f"Exported {p}")

        self._run(lambda progress: export_mesh(mesh, path, opts), done, busy=f"Exporting {Path(path).name}")

    # ------------------------------------------------------------------- misc

    def save_screenshot(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save screenshot", "darkstl.png", "PNG (*.png)")
        if path:
            self.viewport.screenshot(path)

    def about(self) -> None:
        QMessageBox.about(self, "Dark STL",
                          f"Dark STL {__version__}\n\nBrowse FromSoftware models, add texture detail by "
                          "displacement and export print-ready meshes.\n\nFile formats follow the community "
                          "documentation behind SoulsFormats and Soulstruct.")

    def _run(self, fn, on_done, on_error=None, busy: str = "") -> None:
        self.progress.setValue(0)
        self.progress.show()
        if busy:
            self.statusBar().showMessage(busy + " ...")

        def finish(result):
            self.progress.hide()
            on_done(result)

        def fail(msg):
            self.progress.hide()
            self.statusBar().showMessage(f"Error: {msg}")
            if on_error:
                on_error(msg)
            else:
                QMessageBox.warning(self, "Dark STL", msg)

        def prog(frac, msg):
            self.progress.setValue(int(frac * 1000))
            if msg:
                self.statusBar().showMessage(msg)

        job = workers.start(fn, finish, fail, prog)
        self._jobs.append(job)
        self._jobs = self._jobs[-16:]
