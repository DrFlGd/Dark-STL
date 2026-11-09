"""How far each vertex can move before it meets another surface: the room around thin parts.

Displacement sizes relief from the texture alone, so a claw tip a few millimetres thick can be
given relief of ten: its two sides are pushed past each other. The room measured here is what
caps that (``pipeline.displace_resampled``), and what the 3D view's Thickness shading shows.

For every vertex, two balls are grown from it, both touching the surface at the vertex: one on
the inside (against its normal) and one on the outside (along it). Each shrinks until no other
part of the surface is inside it; its radius is the room on that side (the shrinking-ball
method, Ma, Bae, Choi and Kim 2012, which only needs nearest-point queries, so a KD-tree does it):

- **inward room** is half the local thickness: both sides of a blade moving in by it meet in
  the middle;
- **outward room** is half the gap to the nearest surface in front: two fingers side by side,
  each moving out by it, just touch.

The surface is stood for by sample points (the vertices of an evenly spaced mesh) with normals.
A sample only stops a ball when the vertex is clearly off the sample's tangent plane, so the
vertex's own neighbours on a flat or gently curved surface don't shrink it to nothing. For the
room relief may use, every other sample counts: at a sharp edge or a tight curl the side faces
are what relief would fold into. For the thickness of a wall, as printing sees it (``walls``),
a sample also has to face towards or away from the vertex (within 60 degrees), not run alongside
it, so the far side of a blade, the next finger and the body behind a cape count, and a box's
corner doesn't make its walls look thin.

Where a ball reaches ``reach`` without meeting anything the room is unbounded (``inf``). An
inward ball that ends against the back of a surface (inside a solid) measures a thickness; one
that ends against the front of another surface, or meets nothing, says the vertex is on a single
sheet (a cape with the body behind it, a banner), which has no thickness of its own.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from scipy.spatial import cKDTree

from .mesh import TriMesh

ProgressFn = Callable[[float, str], None]

OFF_PLANE = 0.5  # a sample stops a ball only if the vertex is off its tangent plane by this share of their distance
FACING = 0.5  # ... and its normal is within 60 degrees of the vertex's, or of the opposite
NEIGHBOURS = 8  # samples looked at around each ball's centre per round
ROUNDS = 32  # most rounds of shrinking once a ball has met something (most settle in a handful)
GROW = 3.0  # an empty ball grows this many times bigger each round
SETTLED = 0.01  # a ball that shrank by less than this share in a round is taken as settled


@dataclass
class Room:
    """Room around every vertex of a mesh (model units), on both sides of the surface."""

    inward: np.ndarray  # (V,) half the thickness behind the vertex; inf where nothing was met within ``reach``
    outward: np.ndarray  # (V,) half the gap in front of the vertex; inf likewise
    solid: np.ndarray  # (V,) bool: the inward ball ended inside a solid, so 2 x inward is a real thickness
    reach: np.ndarray  # (V,) how far each vertex's balls were grown
    seconds: float = 0.0

    def thickness(self) -> np.ndarray:
        """Thickness behind each vertex: 2 x the inward room inside a solid, 0 on a single sheet."""
        return np.where(self.solid, 2.0 * self.inward, 0.0)


class _Cloud:
    """The surface samples, and coarser copies of them (one sample per cell, the cells doubling) for bigger
    balls: a ball only needs samples a sixth of its radius apart, and a KD-tree answers a query from far away
    much faster when it holds fewer points."""

    RATIO = 6.0  # a ball is tested against samples at most this many times closer together than its radius

    def __init__(self, pts: np.ndarray, nrm: np.ndarray, spacing: float):
        self.base = spacing
        self.levels: list[tuple[cKDTree, np.ndarray, np.ndarray]] = []
        cell = spacing
        while True:
            # one blank sample at the end: what a query bounded by the ball finds where nothing is near
            self.levels.append((cKDTree(pts), np.vstack([pts, np.zeros((1, 3))]), np.vstack([nrm, np.zeros((1, 3))])))
            if len(pts) < 512 or len(self.levels) >= 16:
                break
            cell *= 2.0
            keys = np.floor((pts - pts.min(axis=0)) / cell).astype(np.int64)
            size = keys.max(axis=0) + 1
            flat = keys[:, 0] + size[0] * (keys[:, 1] + size[1] * keys[:, 2])
            _, first = np.unique(flat, return_index=True)
            pts, nrm = pts[first], nrm[first]

    def level(self, radius: np.ndarray) -> np.ndarray:
        with np.errstate(divide="ignore", invalid="ignore"):
            lv = np.floor(np.log2(np.maximum(radius, 1e-300) / (self.RATIO * self.base)))
        return np.clip(np.nan_to_num(lv, nan=0.0, posinf=0.0), 0, len(self.levels) - 1).astype(np.int64)


def _shrink(points: np.ndarray, normals: np.ndarray, side: float, cloud: _Cloud, reach: np.ndarray,
            facing_only: bool = False, chunk: int = 200_000,
            back_only: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The ball on one ``side`` of each point (+1 along its normal, -1 against it): its radius (inf: it reached
    the point's ``reach`` without meeting anything), and the position and normal of the sample it ends against
    (zero where none). ``back_only``: points whose ball only ends where the surface faces away from its centre
    (where the solid it is in ends), not against the front of another part pushed into it.

    Each ball starts at the sample spacing and grows while nothing that counts is inside it; once something
    is, it shrinks to pass through it, and again for whatever is inside the smaller ball, until nothing is.
    Starting small keeps the samples nearest the ball's centre the ones that matter: a big ball's nearest
    samples are often surfaces alongside that don't count, hiding the far side that does.
    """
    n = len(points)
    start = cloud.base
    radius = np.minimum(np.full(n, start), reach)
    hit_p = np.zeros((n, 3))
    hit_n = np.zeros((n, 3))
    met_any = np.zeros(n, bool)
    if n == 0:
        return np.full(n, np.inf), hit_p, hit_n
    floor = start * 1e-4
    growing = np.ones(n, bool)
    active = np.flatnonzero(reach > floor)  # a vertex not to move needs no room
    rounds = int(np.ceil(np.log(max(float(np.max(reach)) / start, 1.0)) / np.log(GROW))) + ROUNDS
    for _ in range(rounds):
        if not len(active):
            break
        keep = []
        lv = cloud.level(radius[active])
        for level in np.unique(lv):
            tree, spts, snrm = cloud.levels[level]
            k = min(NEIGHBOURS, tree.n)
            group = active[lv == level]
            group = group[np.argsort(radius[group], kind="stable")]  # similar balls together: one bound per chunk
            for s in range(0, len(group), chunk):
                ids = group[s:s + chunk]
                p = points[ids]
                d = normals[ids] * side
                r = radius[ids]
                # only samples inside the ball matter, so the search stops at its radius
                dist, cand = tree.query(p + d * r[:, None], k=k, workers=-1, distance_upper_bound=float(r.max()))
                dist = dist.reshape(len(p), k)
                cand = cand.reshape(len(p), k)
                new = np.full(len(p), np.inf)
                pick_p = np.zeros((len(p), 3))
                pick_n = np.zeros((len(p), 3))
                near = np.flatnonzero(dist[:, 0] < r)  # balls with any sample inside
                if len(near):
                    c = cand[near]
                    q = spts[c]
                    pq = q - p[near, None, :]  # vertex -> sample
                    len2 = np.einsum("ijk,ijk->ij", pq, pq)
                    qn = snrm[c]
                    dn = d[near]
                    off = np.einsum("ijk,ijk->ij", qn, pq)  # the vertex's distance off the sample's tangent plane
                    along = np.einsum("ik,ijk->ij", dn, pq)  # how far the sample lies on the ball's side
                    counts = ((len2 > floor * floor) & (off * off > (OFF_PLANE * OFF_PLANE) * len2)
                              & (along > 0) & (dist[near] < r[near, None] * (1.0 - 1e-9)))
                    if facing_only:  # walls: only surfaces facing towards or away from the vertex
                        counts &= np.abs(np.einsum("ik,ijk->ij", dn, qn)) > FACING
                    with np.errstate(divide="ignore", invalid="ignore"):
                        # the ball touching the vertex (centre on its side) that passes through the sample
                        fit = np.where(counts, len2 / (2.0 * along), np.inf)
                        if back_only is not None:
                            back = back_only[ids][near]
                            if back.any():  # the sample faces away from that ball's centre: the solid ends there
                                ahead = fit * np.einsum("ik,ijk->ij", dn, qn) - off
                                fit = np.where(back[:, None] & ~(ahead < 0), np.inf, fit)
                    j = np.argmin(fit, axis=1)
                    rows = np.arange(len(near))
                    new[near] = fit[rows, j]
                    pick_p[near] = q[rows, j]
                    pick_n[near] = qn[rows, j]
                met = new < r * (1.0 - 1e-6)
                was_growing = growing[ids]
                grow = was_growing & ~met
                m_ids = ids[met]
                radius[m_ids] = np.maximum(new[met], floor)
                hit_p[m_ids] = pick_p[met]
                hit_n[m_ids] = pick_n[met]
                met_any[m_ids] = True
                growing[m_ids] = False
                gone = grow & (r >= reach[ids])
                radius[ids[gone]] = np.inf
                more = grow & ~gone
                radius[ids[more]] = np.minimum(r[more] * GROW, reach[ids][more])
                # a shrinking ball goes on while it still shrinks noticeably (a per cent is plenty for room)
                go_on = met & (new > floor) & (was_growing | (new < r * (1.0 - SETTLED)))
                keep.append(ids[go_on | more])
        active = np.concatenate(keep) if keep else active[:0]
    radius[~met_any] = np.inf
    return radius, hit_p, hit_n


def _spacing(mesh: TriMesh) -> float:
    """A typical distance between neighbouring vertices (the median edge length)."""
    if mesh.n_faces == 0:
        return max(mesh.diagonal() * 1e-3, 1e-9)
    e = mesh.faces[:: max(1, mesh.n_faces // 200_000)]
    lengths = np.linalg.norm(mesh.vertices[e] - mesh.vertices[np.roll(e, 1, axis=1)], axis=2)
    return max(float(np.median(lengths)), mesh.diagonal() * 1e-6, 1e-12)


def neighbours(mesh: TriMesh) -> tuple[np.ndarray, np.ndarray]:
    """Each vertex's neighbours along edges, as (owner, neighbour) pairs sorted by owner."""
    edges, _ = mesh.edges_unique()
    a = np.concatenate([edges[:, 0], edges[:, 1]])
    b = np.concatenate([edges[:, 1], edges[:, 0]])
    order = np.argsort(a, kind="stable")
    return a[order], b[order]


def settle(n: int, room: np.ndarray, pairs: tuple[np.ndarray, np.ndarray], rings: int = 1,
           passes: int = 2) -> np.ndarray:
    """The room smoothed over a few vertex spacings, so the limit itself has no steps.

    Worked on the tightness (1 / room, 0 where unbounded): every vertex first takes the tightest of its
    neighbours ``rings`` deep (a tip or crease vertex, which no sample stops, takes its neighbours'
    room), then is averaged with its neighbours ``passes`` times.
    """
    owner, other = pairs
    if n == 0 or not len(owner):
        return room
    with np.errstate(divide="ignore"):
        tight = np.where(np.isfinite(room), 1.0 / np.maximum(room, 1e-30), 0.0)
    degree = np.bincount(owner, minlength=n)
    has = degree > 0
    starts = np.searchsorted(owner, np.arange(n))
    for _ in range(rings):
        top = np.zeros(n)
        top[has] = np.maximum.reduceat(tight[other], starts[has])
        tight = np.maximum(tight, top)
    for _ in range(passes):
        mean = np.bincount(owner, weights=tight[other], minlength=n) / np.maximum(degree, 1)
        tight = np.where(has, 0.5 * tight + 0.5 * mean, tight)
    with np.errstate(divide="ignore"):
        return np.where(tight > 0, 1.0 / np.where(tight > 0, tight, 1.0), np.inf)


def _vote(n: int, solid: np.ndarray, known: np.ndarray, pairs: tuple[np.ndarray, np.ndarray],
          passes: int = 2) -> np.ndarray:
    """Solid or sheet decided with the neighbours: a vertex whose ball met nothing it could tell by (a tip,
    a crease) goes with those around it; one still undecided counts as a sheet."""
    owner, other = pairs
    vote = np.where(known, np.where(solid, 1.0, -1.0), 0.0)
    if len(owner):
        degree = np.maximum(np.bincount(owner, minlength=n), 1)
        for _ in range(passes):
            vote = 0.5 * vote + 0.5 * np.bincount(owner, weights=vote[other], minlength=n) / degree
    return vote > 0


def _voxels(points: np.ndarray, cell: float) -> tuple[np.ndarray, np.ndarray]:
    """One point per cube of side ``cell``: the points picked, and which of them each point's cube has."""
    keys = np.floor((points - points.min(axis=0)) / cell).astype(np.int64)
    size = keys.max(axis=0) + 1
    flat = keys[:, 0] + size[0] * (keys[:, 1] + size[1] * keys[:, 2])
    _, first, inverse = np.unique(flat, return_index=True, return_inverse=True)
    return first, inverse.reshape(-1)


def _balls(points: np.ndarray, nrm: np.ndarray, cloud: _Cloud, reach: np.ndarray, sides: str, walls: bool,
           back_only: np.ndarray | None = None):
    """Both balls of every point: inward and outward room, whether the inward one ended inside a solid, and
    whether it ended against anything at all."""
    inward, hit_p, hit_n = _shrink(points, nrm, -1.0, cloud, reach, walls, back_only=back_only)
    outward = _shrink(points, nrm, 1.0, cloud, reach, walls)[0] if sides == "both" else np.full(len(points), np.inf)
    # inside a solid when the ball ended against the back of a surface: that surface's normal faces away from it
    hit = np.isfinite(inward)
    solid = np.zeros(len(points), bool)
    if hit.any():
        centre = points[hit] - nrm[hit] * inward[hit, None]
        solid[hit] = np.einsum("ij,ij->i", hit_n[hit], centre - hit_p[hit]) < 0
    return inward, outward, solid, hit


QUERIES = 150_000  # vertices whose room is measured; on denser meshes the others take it from those around them


def measure_room(mesh: TriMesh, reach: float | np.ndarray, normals: np.ndarray | None = None,
                 samples: TriMesh | None = None, sides: str = "both", walls: bool = False,
                 progress: ProgressFn | None = None, pairs: tuple[np.ndarray, np.ndarray] | None = None,
                 queries: int = QUERIES, hidden: np.ndarray | None = None,
                 solid: np.ndarray | None = None) -> Room:
    """Inward and outward room at every vertex of ``mesh``, grown up to ``reach`` (model units; one for all, or
    one per vertex: a vertex that is to move only a little needs to look only a little way).

    ``normals``: the directions the vertices move in (default: the mesh's own smooth normals).
    ``samples``: a mesh whose vertices stand for the surface (default: ``mesh`` itself, which should then
    be evenly spaced, as the pipeline's resampled meshes are; see :func:`surface_samples`).
    ``sides``: "both", or "in" for the inward room alone (thickness). ``walls``: only surfaces facing towards
    or away from a vertex count, not those alongside (the side faces at a sharp edge): the thickness of a
    wall, as printing sees it, rather than how far the surface can move without folding.
    ``pairs``: :func:`neighbours`, if made. ``queries``: on a mesh with more vertices than this, the room is
    measured at about this many, evenly spread, and every other vertex takes the tightest room of those
    near it that face its way (the room changes over the shape's size, not the vertex spacing).
    ``hidden``: samples that don't stop a ball (surfaces hidden inside another part, see :func:`buried`).
    ``solid``: vertices known to be on a solid (closed parts): their inward ball ends only where the solid does.
    """
    t0 = time.time()
    n = mesh.n_vertices
    reach_v = np.broadcast_to(np.asarray(reach, np.float64), (n,)).copy()
    if n == 0:
        z = np.zeros(0)
        return Room(z, z, np.zeros(0, bool), reach_v)
    nrm = mesh.vertex_normals() if normals is None else np.asarray(normals, np.float64)
    sm = mesh if samples is None else samples
    pts_s, nrm_s = sm.vertices, nrm if samples is None else sm.vertex_normals()
    if hidden is not None and not hidden.all():
        pts_s, nrm_s = pts_s[~hidden], nrm_s[~hidden]
    cloud = _Cloud(pts_s, nrm_s, _spacing(sm))
    if progress:
        progress(0.05, "Measuring the room around thin parts")
    pairs = pairs if pairs is not None else neighbours(mesh)
    rings = 1
    pick = None
    if n > queries * 1.3:
        cell = _spacing(mesh) * float(np.sqrt(n / queries))
        pick, cube = _voxels(mesh.vertices, cell)
        # each picked vertex looks as far as any vertex of its cube needs
        far = np.zeros(len(pick))
        np.maximum.at(far, cube, reach_v)
        inw_p, out_p, solid_p, known_p = _balls(mesh.vertices[pick], nrm[pick], cloud, far, sides, walls,
                                                None if solid is None else solid[pick])
        tree = cKDTree(mesh.vertices[pick])
        dist, j = tree.query(mesh.vertices, k=6, distance_upper_bound=2.0 * cell, workers=-1)
        ok = j < len(pick)
        j = np.where(ok, j, 0)
        ok &= np.einsum("ijk,ik->ij", nrm[pick][j], nrm) > 0.5  # the same side of a thin part
        with np.errstate(divide="ignore"):
            def tightest(room):
                t = np.where(ok, np.where(np.isfinite(room[j]), 1.0 / np.maximum(room[j], 1e-30), 0.0), 0.0).max(axis=1)
                return np.where(t > 0, 1.0 / np.where(t > 0, t, 1.0), np.inf)
            inward, outward = tightest(inw_p), tightest(out_p)
        votes = (np.where(ok & known_p[j], np.where(solid_p[j], 1.0, -1.0), 0.0)).sum(axis=1)
        known = votes != 0
        found = votes > 0
        alone = np.flatnonzero(~ok.any(axis=1))  # nothing picked near facing its way (a sliver): its own balls
        if len(alone):
            a_in, a_out, a_solid, a_known = _balls(mesh.vertices[alone], nrm[alone], cloud, reach_v[alone], sides, walls,
                                                   None if solid is None else solid[alone])
            inward[alone], outward[alone], found[alone], known[alone] = a_in, a_out, a_solid, a_known
        rings = 0  # taking the tightest around already widened the limit
    else:
        inward, outward, found, known = _balls(mesh.vertices, nrm, cloud, reach_v, sides, walls, solid)
    found = _vote(n, found, known, pairs)
    if solid is not None:
        found |= solid
    inward = settle(n, inward, pairs, rings=rings)
    if sides == "both":
        outward = settle(n, outward, pairs, rings=rings)
    if progress:
        progress(1.0, "Measured the room")
    return Room(inward, outward, found, reach_v, time.time() - t0)


def surface_samples(mesh: TriMesh, spacing: float, most: int = 300_000) -> TriMesh | None:
    """An evenly spaced stand-in for ``mesh``'s surface (positions and face groups) for :func:`measure_room` to test
    against, about ``spacing`` apart (at most ``most`` vertices); None when the mesh's own vertices
    already are."""
    from .subdivide import subdivide_to_vertex_count

    plain = TriMesh(mesh.vertices, mesh.faces, face_group=mesh.face_group)
    if plain.n_faces == 0 or float(np.percentile(plain.edge_lengths(), 95)) <= spacing * 1.5:
        return None
    want = int(min(most, 2 * plain.surface_area() / (np.sqrt(3) * spacing * spacing)))
    if want <= plain.n_vertices * 1.2:
        return None
    return subdivide_to_vertex_count(plain, want)


def _parts(mesh: TriMesh) -> tuple[np.ndarray, np.ndarray]:
    """Each vertex's part (its triangles joined where they share a position, as across UV seams), and whether
    each part is closed: no edge of it is open."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    n = mesh.n_vertices
    if n == 0 or mesh.n_faces == 0:
        return np.arange(n), np.zeros(n, bool)
    tol = max(mesh.diagonal() * 1e-7, 1e-12)
    _, at = np.unique(np.round(mesh.vertices / tol).astype(np.int64), axis=0, return_inverse=True)
    at = at.reshape(-1)
    f = at[mesh.faces]
    e = np.sort(np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]]), axis=1)
    e = e[e[:, 0] != e[:, 1]]
    m = int(at.max()) + 1
    key, count = np.unique(e[:, 0] * np.int64(m) + e[:, 1], return_counts=True)
    graph = coo_matrix((np.ones(len(e)), (e[:, 0], e[:, 1])), shape=(m, m))
    _, part = connected_components(graph, directed=False)
    open_part = np.zeros(part.max() + 1, bool)
    open_part[part[key[count == 1] // m]] = True
    return part[at], ~open_part


def closed_parts(mesh: TriMesh) -> np.ndarray:
    """Whether each vertex is on a closed part. Inside such a part is solid, whatever other parts poke into it."""
    part, closed = _parts(mesh)
    return closed[part] if len(part) else np.zeros(0, bool)


def buried(points: np.ndarray, normals: np.ndarray, part: np.ndarray, closed: np.ndarray, margin: float) -> np.ndarray:
    """Whether each point (on part ``part``, see :func:`_parts`) lies inside another closed part, by more than
    ``margin``: game parts overlap (an arm's end inside the body, a neck inside the head), and such a surface is
    hidden and won't be printed. Told by the nearest point of each closed part: the point is behind it."""
    out = np.zeros(len(points), bool)
    if len(points) == 0 or len(closed) < 2 or not closed.any():
        return out
    order = np.argsort(part, kind="stable")
    bounds = np.searchsorted(part[order], np.arange(len(closed) + 1))
    for p in np.flatnonzero(closed):
        ids = order[bounds[p]:bounds[p + 1]]
        if not len(ids):
            continue
        lo, hi = points[ids].min(axis=0) - margin, points[ids].max(axis=0) + margin
        cand = np.flatnonzero((part != p) & ~out & np.all((points >= lo) & (points <= hi), axis=1))
        if not len(cand):
            continue
        _, j = cKDTree(points[ids]).query(points[cand], workers=-1)
        q = ids[j]
        behind = np.einsum("ij,ij->i", normals[q], points[cand] - points[q]) < -margin
        out[cand[behind]] = True
    return out


def thickness(mesh: TriMesh, most: int = 300_000, progress: ProgressFn | None = None) -> np.ndarray:
    """Thickness behind every vertex of ``mesh`` (model units), as the 3D view's Thickness shading shows it:
    0 on single sheets, inf where the vertex is hidden inside another part (:func:`buried`). The balls are
    grown up to half the model's size, so a sheet with nothing behind it is told from a solid. On a closed
    part every vertex is on a solid, and its ball ends only where that solid does: not against another part
    pushed into it, as game parts overlap."""
    if mesh.n_vertices == 0:
        return np.zeros(0)
    diag = max(mesh.diagonal(), 1e-9)
    area = max(mesh.surface_area(), 1e-18)
    spacing = max(float(np.sqrt(2 * area / (np.sqrt(3) * most))), diag * 1e-4)
    part, closed = _parts(mesh)
    samples = surface_samples(TriMesh(mesh.vertices, mesh.faces, face_group=part[mesh.faces[:, 0]].astype(np.int32)),
                              spacing, most)
    if samples is None:
        sm, s_part = mesh, part
    else:
        sm, s_part = samples, np.zeros(samples.n_vertices, np.int64)
        s_part[samples.faces.ravel()] = np.repeat(samples.face_group, 3)
    # surfaces hidden inside another part are left out: they stop no ball, and need no thickness
    hidden = buried(sm.vertices, sm.vertex_normals(), s_part, closed, 0.1 * _spacing(sm))
    inside = hidden if samples is None else hidden[cKDTree(samples.vertices).query(mesh.vertices, workers=-1)[1]]
    reach = np.where(inside, 0.0, diag / 2)  # nor are they measured
    room = measure_room(mesh, reach, samples=samples, sides="in", walls=True, progress=progress, hidden=hidden,
                        solid=closed[part])
    out = room.thickness()
    out[inside] = np.inf
    return out


def cap_heights(heights: np.ndarray, room: Room, used: float = 0.8) -> tuple[np.ndarray, dict]:
    """Heights softly limited to the room on their side: ``R tanh(h / R)`` with ``R = used x room``.

    Small heights stay as they are (tanh is straight near 0), heights near the room bend away from
    it, and none reaches it. Where the room nears the reach (the balls met nothing close) the cap
    fades out, so open and thick areas are not changed at all.

    Returns the capped heights and what changed: how many vertices were capped (by more than 5 % of
    their height), the largest reduction, and the share of the relief kept (RMS).
    """
    h = np.asarray(heights, np.float64)
    used = float(min(max(used, 0.05), 1.0))
    side = np.where(h >= 0, room.outward, room.inward)
    out = h.copy()
    fin = np.isfinite(side)
    if fin.any():
        lim = np.maximum(used * side[fin], 1e-30)
        capped = lim * np.tanh(h[fin] / lim)
        # in full up to half the reach, none at the reach
        fade = np.clip(2.0 - 2.0 * side[fin] / np.maximum(room.reach[fin], 1e-30), 0.0, 1.0)
        out[fin] = h[fin] + (capped - h[fin]) * fade
    cut = np.abs(h) - np.abs(out)
    total = float(np.sqrt(np.mean(h * h))) if len(h) else 0.0
    return out, {"capped": int(np.count_nonzero(cut > 0.05 * np.abs(h))),
                 "max_reduction": float(cut.max()) if len(cut) else 0.0,
                 "kept": float(np.sqrt(np.mean(out * out))) / total if total > 0 else 1.0}


REACH = 8.0  # the balls look this many times further than the relief may go (then the cap changes it < 2 %)


def keep_thin_parts(mesh: TriMesh, heights: np.ndarray, normals: np.ndarray | None = None, used: float = 0.8,
                    progress: ProgressFn | None = None) -> tuple[np.ndarray, np.ndarray, dict]:
    """Heights and directions for displacing ``mesh`` (evenly spaced) that keep thin parts from being pushed
    through themselves and close parts into each other, in three steps:

    1. the heights are capped by the room on their side (:func:`cap_heights`); each vertex looks for room as
       far as the largest relief around it needs, so vertices that barely move cost next to nothing;
    2. where a vertex uses much of its room, its direction is smoothed with its neighbours' (:func:`calm`),
       so on a tip or an edge thinner than the vertex spacing neighbours move alike instead of crossing;
    3. triangles still crossing are found and the heights around them damped (:func:`untangle`).

    Returns the heights, the directions, and what was done: vertices capped, the largest reduction, the share
    of the relief kept, crossing triangle pairs found after steps 1 and 2 and left after 3, and the seconds."""
    h = np.asarray(heights, np.float64)
    pairs = neighbours(mesh)
    owner, other = pairs
    need = np.abs(h)
    if len(owner):  # the largest relief two rings around, so neighbours measure alike
        n = len(h)
        has = np.bincount(owner, minlength=n) > 0
        starts = np.searchsorted(owner, np.arange(n))
        for _ in range(2):
            top = np.zeros(n)
            top[has] = np.maximum.reduceat(need[other], starts[has])
            need = np.maximum(need, top)
    used = float(min(max(used, 0.05), 1.0))
    nrm = mesh.vertex_normals() if normals is None else normals
    room = measure_room(mesh, REACH * need / used, normals=nrm, progress=progress, pairs=pairs)
    out, info = cap_heights(h, room, used)
    t0 = time.time()
    side = np.where(out >= 0, room.outward, room.inward)
    with np.errstate(invalid="ignore", divide="ignore"):
        share = np.where(np.isfinite(side), np.abs(out) / np.maximum(side, 1e-30), 0.0)
    nrm = calm(nrm, np.clip(2.0 * share, 0.0, 1.0), pairs)
    out, info["crossings_found"], info["crossings_left"] = untangle(mesh, out, nrm, share, pairs)
    info["seconds"] = room.seconds + time.time() - t0
    return out, nrm, info


CALM_PASSES = 4


def calm(normals: np.ndarray, weight: np.ndarray, pairs: tuple[np.ndarray, np.ndarray],
         passes: int = CALM_PASSES) -> np.ndarray:
    """Directions blended towards their neighbours' average by ``weight`` (0: as they are, 1: the average),
    ``passes`` times."""
    if not np.any(weight > 0):
        return normals
    owner, other = pairs
    n = len(normals)
    degree = np.maximum(np.bincount(owner, minlength=n), 1)[:, None]
    w = weight[:, None]
    out = np.asarray(normals, np.float64).copy()
    for _ in range(passes):
        mean = np.stack([np.bincount(owner, weights=out[other, i], minlength=n) for i in range(3)], 1) / degree
        out = out * (1.0 - w) + mean * w
        out /= np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-12)
    return out


RISK = 0.3  # a vertex moving more than this share of its room is checked for crossings after the cap
UNTANGLE_ROUNDS = 6


def untangle(mesh: TriMesh, heights: np.ndarray, normals: np.ndarray, share: np.ndarray,
             pairs: tuple[np.ndarray, np.ndarray], rounds: int = UNTANGLE_ROUNDS) -> tuple[np.ndarray, int, int]:
    """What the cap leaves crossing, found and damped: where the surface is thinner than the vertices are
    apart (a blade's edge, the last millimetre of a point), neighbouring vertices moving along normals that
    disagree can still fold triangles through each other. Triangles that cross are found where vertices move
    a fair share of their room (``share``: the height over the room, per vertex); the heights around each are
    halved (their neighbours' by a quarter) and the check repeated, a few rounds at most.

    Returns the heights, how many crossing triangle pairs there were, and how many are left."""
    from .intersect import crossing_pairs_among

    h = np.asarray(heights, np.float64).copy()
    risky = share > RISK
    if not risky.any():
        return h, 0, 0
    owner, other = pairs
    near = risky.copy()
    near[owner[risky[other]]] = True  # and one ring around
    faces = np.flatnonzero(near[mesh.faces].any(axis=1))
    first, left = None, 0
    moved = mesh.copy()
    for done in range(rounds + 1):
        moved.vertices = mesh.vertices + normals * h[:, None]
        cross = crossing_pairs_among(moved, faces)
        if first is None:
            first = len(cross)
        left = len(cross)  # elsewhere nothing moved since the first check found none
        if not left or done == rounds:
            break
        hit = np.zeros(mesh.n_vertices, bool)
        hit[mesh.faces[cross.ravel()].ravel()] = True
        ring = np.zeros_like(hit)
        ring[owner[hit[other]]] = True
        h[ring & ~hit] *= 0.75
        h[hit] *= 0.5
        # next round: only around what changed (two rings)
        look = hit | ring
        look[owner[look[other]]] = True
        faces = faces[look[mesh.faces[faces]].any(axis=1)]
    return h, int(first or 0), int(left)
