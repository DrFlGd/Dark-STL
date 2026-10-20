"""3D viewport (VTK via pyvistaqt), with a plain fallback when OpenGL/VTK is unavailable."""

from __future__ import annotations

import os

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ..geometry.mesh import TriMesh

CLAY = (0.78, 0.55, 0.38)
BACKGROUND_TOP = (0.20, 0.20, 0.22)
BACKGROUND_BOTTOM = (0.06, 0.06, 0.07)


def _split_by_uv(mesh: TriMesh, sel: np.ndarray):
    """Vertices duplicated per distinct (vertex, uv) pair, for per-point texture coordinates."""
    faces = mesh.faces[sel]
    corner_v = faces.reshape(-1)
    cuv = mesh.corner_uvs[sel].reshape(-1, 2)
    keys = np.column_stack([corner_v, np.round(cuv * 1e5).astype(np.int64)])
    _, first, inv = np.unique(keys, axis=0, return_index=True, return_inverse=True)
    return corner_v[first], cuv[first], inv.reshape(-1, 3)


class Viewport(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.plotter = None
        self.error = None
        if os.environ.get("DARKSTL_NO_3D"):
            self.error = "3D view disabled (DARKSTL_NO_3D)"
        else:
            try:
                import pyvista as pv
                from pyvistaqt import QtInteractor

                pv.global_theme.smooth_shading = True
                self.plotter = QtInteractor(self)
                self.plotter.set_background(BACKGROUND_BOTTOM, top=BACKGROUND_TOP)
                try:
                    self.plotter.enable_anti_aliasing("ssaa")
                except Exception:  # noqa: BLE001 - older VTK builds lack SSAA
                    pass
                self.plotter.add_axes(line_width=2, labels_off=False)
                layout.addWidget(self.plotter.interactor)
            except Exception as exc:  # noqa: BLE001 - keep the app usable without 3D
                self.plotter = None
                self.error = f"3D view unavailable: {exc}"
        if self.plotter is None:
            self.placeholder = QLabel(self.error or "3D view unavailable")
            self.placeholder.setAlignment(Qt.AlignCenter)
            self.placeholder.setWordWrap(True)
            layout.addWidget(self.placeholder)

    @property
    def available(self) -> bool:
        return self.plotter is not None

    def clear(self) -> None:
        if self.plotter is not None:
            self.plotter.clear_actors()

    def show_mesh(self, mesh: TriMesh, textures: dict[int, np.ndarray] | None = None, mode: str = "clay",
                  reset_camera: bool = True, edges: bool = False) -> None:
        if self.plotter is None or mesh is None:
            if self.plotter is None and mesh is not None:
                self.placeholder.setText(f"{self.error}\n\n{mesh.n_vertices:,} vertices, {mesh.n_faces:,} triangles")
            return
        import pyvista as pv

        self.plotter.clear_actors()
        show_edges = edges or mode == "wireframe"
        if mode == "textured" and textures and mesh.corner_uvs is not None:
            normals = mesh.vertex_normals()
            for mat in np.unique(mesh.face_material):
                sel = mesh.face_material == mat
                src, uv, faces = _split_by_uv(mesh, sel)
                poly = pv.PolyData(mesh.vertices[src], np.hstack([np.full((len(faces), 1), 3), faces]).ravel())
                poly.point_data["Normals"] = normals[src]
                img = textures.get(int(mat))
                if img is None:
                    self.plotter.add_mesh(poly, color=CLAY, smooth_shading=True, show_edges=show_edges)
                    continue
                tc = np.column_stack([uv[:, 0], 1.0 - uv[:, 1]]).astype(np.float32)  # VTK: origin bottom-left
                poly.active_texture_coordinates = tc
                rgb = img if img.dtype == np.uint8 else np.clip(img * 255, 0, 255).astype(np.uint8)
                tex = pv.Texture(np.ascontiguousarray(rgb[::-1, :, :3]))
                tex.repeat = True
                self.plotter.add_mesh(poly, texture=tex, smooth_shading=True, show_edges=show_edges)
        else:
            poly = pv.PolyData(mesh.vertices, np.hstack([np.full((mesh.n_faces, 1), 3), mesh.faces]).ravel())
            poly.point_data["Normals"] = mesh.vertex_normals()
            if mode == "wireframe":
                self.plotter.add_mesh(poly, color=(0.85, 0.85, 0.85), show_edges=True, edge_color=(0.15, 0.15, 0.15),
                                      smooth_shading=False)
            else:
                self.plotter.add_mesh(poly, color=CLAY, smooth_shading=True, specular=0.35, specular_power=18,
                                      show_edges=show_edges, edge_color=(0.3, 0.2, 0.15))
        if reset_camera:
            self.plotter.view_isometric()
            self.plotter.reset_camera()
        self.plotter.render()

    def screenshot(self, path: str) -> None:
        if self.plotter is not None:
            self.plotter.screenshot(path)
