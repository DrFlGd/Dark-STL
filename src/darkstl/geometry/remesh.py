"""Isotropic remeshing to an exact vertex count (ACVD clustering).

ACVD (Valette & Chassery) groups a densely sampled surface into ``n`` clusters
of equal area and connects neighbouring clusters, which gives the evenly
spaced vertices of a "remesh" in tools like ZBrush. It is provided by the
optional ``pyacvd`` package.

The remeshed vertices do not carry UVs, so every vertex is projected back onto
the (UV-exact) source surface to look up its texture coordinate and material.
"""

from __future__ import annotations

from typing import Callable

import numpy as np

from .mesh import TriMesh
from .proximity import SurfaceLocator
from .subdivide import edge_length_for_vertex_count, subdivide_max_edge

ProgressFn = Callable[[float, str], None]


def acvd_available() -> bool:
    try:
        import pyacvd  # noqa: F401
        import pyvista  # noqa: F401
    except Exception:
        return False
    return True


def clusters_to_mesh(dense: TriMesh, labels: np.ndarray) -> TriMesh:
    """Dual mesh of a surface clustering: one vertex per cluster, one triangle per
    sample triangle whose corners fall in three different clusters."""
    n_clusters = int(labels.max()) + 1
    # area-weighted cluster centroids
    w = np.zeros(dense.n_vertices)
    areas = dense.face_areas() / 3.0
    for k in range(3):
        np.add.at(w, dense.faces[:, k], areas)
    wsum = np.bincount(labels, weights=w, minlength=n_clusters)
    centroids = np.stack([np.bincount(labels, weights=w * dense.vertices[:, i], minlength=n_clusters)
                          for i in range(3)], axis=1) / np.maximum(wsum, 1e-30)[:, None]
    tri = labels[dense.faces]
    ok = (tri[:, 0] != tri[:, 1]) & (tri[:, 1] != tri[:, 2]) & (tri[:, 0] != tri[:, 2])
    tri = tri[ok]
    rot = np.argmin(tri, axis=1)
    idx = (rot[:, None] + np.arange(3)[None, :]) % 3
    tri = np.take_along_axis(tri, idx, axis=1)
    tri = np.unique(tri, axis=0)
    # drop the second of any pair of opposite-orientation duplicates
    canon = np.sort(tri, axis=1)
    _, first = np.unique(canon, axis=0, return_index=True)
    tri = tri[np.sort(first)]
    return TriMesh(centroids, tri).subset(np.ones(len(tri), bool))


def isotropic_remesh(mesh: TriMesh, target_vertices: int, snap_to_surface: bool = True,
                     progress: ProgressFn | None = None, smooth: float = 0.0) -> TriMesh:
    import pyacvd
    import pyvista as pv

    area = mesh.surface_area()
    spacing = edge_length_for_vertex_count(area, target_vertices)
    if progress:
        progress(0.05, "Sampling the surface densely")
    # ACVD needs several samples per cluster; edge midpoint subdivision keeps UVs exact.
    dense = subdivide_max_edge(mesh, spacing * 0.75, max_vertices=max(target_vertices * 40, 1_000_000),
                               smooth=smooth)

    faces_pv = np.hstack([np.full((dense.n_faces, 1), 3, np.int64), dense.faces]).ravel()
    poly = pv.PolyData(dense.vertices, faces_pv)
    if progress:
        progress(0.25, f"Clustering {dense.n_vertices:,} samples into {target_vertices:,} vertices")
    clus = pyacvd.Clustering(poly)
    labels = np.asarray(clus.cluster(int(target_vertices)), np.int64)
    remeshed = clusters_to_mesh(dense, labels)
    if progress:
        progress(0.7, "Projecting new vertices onto the original surface")
    locator = SurfaceLocator(dense)
    vn = remeshed.vertex_normals()
    hits = locator.query(remeshed.vertices, k=8, normals=vn, normal_penalty=spacing)
    if snap_to_surface:
        remeshed.vertices = hits.points.copy()

    # Orient faces to agree with the source surface
    src_n = dense.face_normals()[hits.face]
    corner_n = src_n[remeshed.faces].sum(axis=1)
    flip = np.einsum("ij,ij->i", remeshed.face_cross(), corner_n) < 0
    if flip.any():
        remeshed.faces[flip] = remeshed.faces[flip][:, [0, 2, 1]]

    # Per-vertex UV / material from the projection, stored per corner for export
    if dense.corner_uvs is not None:
        vuv = np.einsum("ij,ijk->ik", hits.bary, dense.corner_uvs[hits.face])
        remeshed.corner_uvs = vuv[remeshed.faces].astype(np.float32)
    vmat = dense.face_material[hits.face]
    vgrp = dense.face_group[hits.face]
    remeshed.face_material = vmat[remeshed.faces[:, 0]]
    remeshed.face_group = vgrp[remeshed.faces[:, 0]]
    remeshed.metadata["vertex_material"] = vmat
    remeshed.metadata["spacing"] = spacing
    return remeshed
