"""Posing Borderlands, Borderlands 2 and The Pre-Sequel (Unreal 3) skeletal meshes with the game's AnimSets.

Unreal 3 keeps animations in ``AnimSet`` objects: a list of bone names (one track each) and the
``AnimSequence`` objects that key those tracks. Nothing ties an AnimSet to a mesh in the packages (the
game's code pairs them), so a mesh is offered every AnimSet whose bones are the mesh's: most of its tracks
name bones the mesh has. Cooked packages carry copies of the AnimSets they use; each AnimSet is read once,
from the package it belongs to when that one is there (else the smallest that holds a copy), and what it
holds is kept in the importer's cache, so later models list theirs at once.

Keys are decoded with Unreal's own codecs (shared with the Unreal 4 reader) plus Borderlands 2's
polar-encoded rotations. Unreal 3 stores the rotation keys of every bone but the root the other way round
from the mesh's bones (UE Viewer conjugates the mesh's instead), and an AnimSet may take only rotations
from its animations (``bAnimRotationOnly``, the default), the mesh keeping its own bone lengths except for
the root and the bones it names in ``UseTranslationBoneNames``. As for the other games, the root's travel
across the floor is left out. Layouts follow UE Viewer (``UnAnim3.cpp``, ``UnMeshTypes.h``; MIT).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import struct
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ...errors import FormatError
from ..fromsoft.animation import AnimationInfo, compose_hierarchy
from . import files
from .formats import upk
from .formats.props import ArrayValue, Props, object_props
from .ue4 import anim as codecs
from .ue4.anim import AnimSequence, Track, sample, transforms_to_matrices

log = logging.getLogger(__name__)

INDEX_VERSION = 1
FORMATS = {"ACF_None": codecs.ACF_NONE, "ACF_Float96NoW": codecs.ACF_FLOAT96, "ACF_Fixed48NoW": codecs.ACF_FIXED48,
           "ACF_IntervalFixed32NoW": codecs.ACF_INTERVAL32, "ACF_Fixed32NoW": codecs.ACF_FIXED32,
           "ACF_Float32NoW": codecs.ACF_FLOAT32, "ACF_Identity": codecs.ACF_IDENTITY,
           "ACF_Delta40NoW": codecs.ACF_DELTA40, "ACF_Delta48NoW": codecs.ACF_DELTA48,
           "ACF_PolarEncoded32": codecs.ACF_POLAR32, "ACF_PolarEncoded48": codecs.ACF_POLAR48}
KEY_FORMATS = {"AKF_ConstantKeyLerp": codecs.AKF_CONSTANT, "AKF_VariableKeyLerp": codecs.AKF_VARIABLE,
               "AKF_PerTrackCompression": codecs.AKF_PER_TRACK}
MATCH = 0.8  # an AnimSet fits a mesh when this share of its tracks name the mesh's bones
HEADER_READ = 1 << 15  # bytes of an AnimSequence read to list it (its properties), before reading it all


# ---------------------------------------------------------------------------------------------------------
# Reading AnimSets and AnimSequences


@dataclass
class AnimSetInfo:
    canonical: str  # its path with its own package first (the same for every copy)
    package: str  # the package file it is read from
    path: str  # its path inside that package
    name: str
    tracks: list[str]  # bone names, one per track
    sequences: list[str]  # its AnimSequences' paths inside the package
    rotation_only: bool = True
    use_translation: list[str] = field(default_factory=list)
    force_mesh_translation: list[str] = field(default_factory=list)
    error: str = ""

    def as_json(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_json(cls, data: dict) -> "AnimSetInfo":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


def _names(props: Props, name: str) -> list[str]:
    v = props.get(name)
    return v.names() if isinstance(v, ArrayValue) else []


def read_anim_set(pkg: upk.Package, e: upk.Export, canonical: str = "") -> AnimSetInfo:
    props = object_props(pkg, pkg.export_data(e))
    seqs = props.get("Sequences")
    sequences = []
    if isinstance(seqs, ArrayValue):
        for ref in seqs.objects():
            if ref.ref > 0:
                sequences.append(pkg.exports[ref.ref - 1].path)
    rotation_only = props.get("bAnimRotationOnly", True)
    return AnimSetInfo(canonical or files.canonical_path(pkg, e), str(pkg.path), e.path, e.name,
                       _names(props, "TrackBoneNames"), sequences, bool(rotation_only),
                       _names(props, "UseTranslationBoneNames"), _names(props, "ForceMeshTranslationBoneNames"))


def _sequence_props(pkg: upk.Package, e: upk.Export, whole: bool = False) -> tuple[Props, upk.Reader]:
    if not whole and e.size > HEADER_READ:
        try:
            r = upk.Reader(pkg.read(e.offset, HEADER_READ), pkg, base=e.offset)
            return object_props(pkg, r), r
        except (FormatError, struct.error, IndexError):
            pass  # the properties run past what was read
    r = pkg.export_data(e)
    return object_props(pkg, r), r


def _number(props: Props, name: str, default: float = 0.0) -> float:
    v = props.get(name)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def sequence_summary(pkg: upk.Package, e: upk.Export) -> tuple[str, int, float, bool]:
    """(name, frames, length in seconds, additive) of an AnimSequence, from its properties alone."""
    props, _r = _sequence_props(pkg, e)
    return _summary(props, e)


def _summary(props: Props, e: upk.Export) -> tuple[str, int, float, bool]:
    name = props.get("SequenceName") or e.name
    frames = int(_number(props, "NumFrames"))
    length = _number(props, "SequenceLength")
    rate = _number(props, "RateScale", 1.0)
    if rate > 0 and length > 0:
        length /= rate
    return str(name), max(frames, 1), length, bool(props.get("bIsAdditive", False))


def _raw_tracks(r: upk.Reader, v: int) -> list[Track]:
    """``RawAnimData`` (uncooked keys; cooked games usually leave it empty)."""
    out = []
    for _ in range(r.count(1 << 16)):
        _size, n, raw = r.bulk_array()
        pos = np.frombuffer(raw, "<f4").astype(np.float64).reshape(-1, 3)[:n]
        _size, n, raw = r.bulk_array()
        rot = np.frombuffer(raw, "<f4").astype(np.float64).reshape(-1, 4)[:n]
        if v < 604:
            r.bulk_array()  # key times
        out.append(Track(translation=pos, rotation=rot))
    return out


def read_sequence(pkg: upk.Package, e: upk.Export, tracks: int) -> AnimSequence:
    """An AnimSequence's keys, by track (the AnimSet's ``TrackBoneNames`` order), as stored."""
    v = pkg.summary.version
    props, r = _sequence_props(pkg, e, whole=True)
    name, frames, length, additive = _summary(props, e)
    raw: list[Track] = []
    if v >= 577:
        raw = _raw_tracks(r, v)
    n = r.count(1 << 30)
    stream = r.bytes(n)
    offsets_v = props.get("CompressedTrackOffsets")
    offsets = np.asarray(offsets_v.ints()[:offsets_v.count] if isinstance(offsets_v, ArrayValue) else [], np.int64)
    key = KEY_FORMATS.get(str(props.get("KeyEncodingFormat", "AKF_ConstantKeyLerp")), codecs.AKF_CONSTANT)
    t_fmt = FORMATS.get(str(props.get("TranslationCompressionFormat", "ACF_None")))
    q_fmt = FORMATS.get(str(props.get("RotationCompressionFormat", "ACF_None")))
    if t_fmt is None or q_fmt is None:
        unknown = props.get("TranslationCompressionFormat") if t_fmt is None else props.get("RotationCompressionFormat")
        raise FormatError(f"its keys are compressed as {unknown}, which Dark STL can't read")
    out = AnimSequence(name, frames, length, {}, None, additive, f"{key}/{t_fmt}/{q_fmt}")
    if not len(offsets):
        if len(raw) != tracks:
            raise FormatError("it holds no keys")
        out.tracks = dict(enumerate(raw))
        return out
    per = 2 if key == codecs.AKF_PER_TRACK else 4
    if len(offsets) < per * tracks:
        raise FormatError(f"it keys {len(offsets) // per} tracks, its AnimSet {tracks}")
    b = codecs._Bytes(stream)
    for i in range(tracks):
        tr = Track()
        if key == codecs.AKF_PER_TRACK:
            t_off, q_off = int(offsets[2 * i]), int(offsets[2 * i + 1])
            # a track left out (-1) is still or turned nowhere: Unreal 3 plays it as no move and no turn
            tr.translation, tr.rotation = np.zeros((1, 3)), np.array([[0.0, 0.0, 0.0, 1.0]])
            if t_off >= 0:
                b.seek(t_off)
                tr.translation, tr.translation_frames = codecs._per_track(b, frames, False,
                                                                          codecs.BL2_PER_TRACK_TRANSLATION)
            if q_off >= 0:
                b.seek(q_off)
                tr.rotation, tr.rotation_frames = codecs._per_track(b, frames, True, codecs.BL2_PER_TRACK_ROTATION)
        else:
            t_off, t_keys, q_off, q_keys = (int(x) for x in offsets[4 * i:4 * i + 4])
            variable = key == codecs.AKF_VARIABLE
            if t_keys > 0:
                b.seek(t_off)
                tr.translation = codecs._uniform_vector(b, t_fmt, t_keys)
                b.align()
                if variable:
                    tr.translation_frames = codecs._times(b, t_keys, frames)
            if q_keys > 0:
                b.seek(q_off)
                fmt = q_fmt
                if fmt in (codecs.ACF_DELTA40, codecs.ACF_DELTA48) and q_keys > 1:
                    raise FormatError("its rotations are delta-compressed, which Dark STL can't read")
                tr.rotation = codecs._uniform_quat(b, fmt, q_keys)
                b.align()
                if variable:
                    tr.rotation_frames = codecs._times(b, q_keys, frames)
        out.tracks[i] = tr
    return out


# ---------------------------------------------------------------------------------------------------------
# Every AnimSet of a game, and which fit a mesh


class AnimSetIndex:
    """For one game folder: every AnimSet (its tracks and sequences), and each listed sequence's length."""

    RECHECK = 30.0  # seconds before a missing index is looked for again

    def __init__(self, root: Path, cache: Path | None):
        self.root = Path(root)
        self.cache = cache
        self._lock = threading.RLock()
        self._sets: dict[str, AnimSetInfo] | None = None  # canonical (lower) -> AnimSet
        self._seqs: dict[str, list] = {}  # "<package>|<path>" (lower) -> [name, frames, length, additive, error]
        self._fingerprint = ""
        self._dirty = False
        self._packages: OrderedDict[str, upk.Package] = OrderedDict()
        self._missed = -1e9  # when cached() last found no index (each model opened asks)

    def _file(self) -> Path | None:
        if self.cache is None:
            return None
        tag = hashlib.sha1(os.path.normcase(str(self.root)).encode()).hexdigest()[:16]
        return Path(self.cache) / f"borderlands-animsets-{tag}.json"

    def _make_fingerprint(self, packages: list[Path]) -> str:
        h = hashlib.sha1(f"{INDEX_VERSION}\n".encode())
        for p in sorted(packages):
            try:
                st = p.stat()
            except OSError:
                continue
            h.update(f"{p}|{st.st_size}|{int(st.st_mtime)}\n".encode())
        return h.hexdigest()

    def cached(self) -> bool:
        """Whether the index is kept and still matches the game's packages (so asking it is quick)."""
        with self._lock:
            if self._sets is not None:
                return True
            if time.monotonic() - self._missed < self.RECHECK:
                return False
            data = self._read_file(self._make_fingerprint(list(files.iter_packages(self.root))))
            if data is None:
                self._missed = time.monotonic()
                return False
            self._take(data)
            return True

    def _read_file(self, fingerprint: str) -> dict | None:
        self._fingerprint = fingerprint
        f = self._file()
        if f is None or not f.is_file():
            return None
        try:
            data = json.loads(f.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        return data if data.get("fingerprint") == fingerprint else None

    def _take(self, data: dict) -> None:
        self._sets = {k: AnimSetInfo.from_json(v) for k, v in data.get("sets", {}).items()}
        self._seqs = data.get("sequences", {})

    def _load(self) -> None:
        if self._sets is not None:
            return
        from .catalog import package_contents

        packages = list(files.iter_packages(self.root))
        data = self._read_file(self._make_fingerprint(packages))
        if data is not None:
            self._take(data)
            return
        found: dict[str, tuple[Path, str, str, int]] = {}  # canonical (lower) -> (package, path, name, size)
        for path in packages:
            try:
                objects = package_contents(path, self.cache, anim_sets=True)
                size = path.stat().st_size
            except (FormatError, OSError, ValueError):
                continue
            for o in objects:
                if o["class"] != "AnimSet":
                    continue
                key = o["canonical"].lower()
                home = key.split(".", 1)[0] == path.stem.lower()
                prev = found.get(key)
                prev_home = prev is not None and key.split(".", 1)[0] == prev[0].stem.lower()
                if prev is None or (home and not prev_home) or (home == prev_home and size < prev[3]):
                    found[key] = (path, o["path"], o["canonical"], size)
        by_package: dict[Path, list[tuple[str, str, str]]] = {}
        for key, (path, inner, canonical, _size) in found.items():
            by_package.setdefault(path, []).append((key, inner, canonical))
        sets: dict[str, AnimSetInfo] = {}
        for path, wanted in by_package.items():
            try:
                pkg = upk.open_package(path)
            except (FormatError, OSError) as exc:
                for key, inner, canonical in wanted:
                    sets[key] = AnimSetInfo(canonical, str(path), inner, inner.rsplit(".", 1)[-1], [], [],
                                            error=str(exc))
                continue
            for key, inner, canonical in wanted:
                e = pkg.find_export(inner, "AnimSet")
                try:
                    if e is None:
                        raise FormatError(f"{inner} isn't in {path.name}")
                    sets[key] = read_anim_set(pkg, e, canonical)
                except (FormatError, struct.error, IndexError, ValueError) as exc:
                    sets[key] = AnimSetInfo(canonical, str(path), inner, inner.rsplit(".", 1)[-1], [], [],
                                            error=str(exc) or type(exc).__name__)
        self._sets = sets
        self._dirty = True
        self.save()

    def save(self) -> None:
        f = self._file()
        if f is None or not self._dirty or self._sets is None:
            return
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps({"fingerprint": self._fingerprint,
                                       "sets": {k: v.as_json() for k, v in self._sets.items()},
                                       "sequences": self._seqs}), "utf-8")
            tmp.replace(f)
            self._dirty = False
        except OSError:
            log.warning("Couldn't keep the AnimSet index in %s", f, exc_info=True)

    def all(self) -> list[AnimSetInfo]:
        with self._lock:
            self._load()
            return list(self._sets.values())

    def fitting(self, bones: list[str], moving: set[str] | None = None) -> list[AnimSetInfo]:
        """The AnimSets whose tracks are the bones of a mesh with ``bones`` (most of their tracks name one of
        its bones, and at least one that moves its vertices, of ``moving``)."""
        have = {b.lower() for b in bones}
        moves = {b.lower() for b in (moving if moving is not None else bones)}
        out = []
        for s in self.all():
            if s.error or not s.tracks or not s.sequences:
                continue
            tracks = {t.lower() for t in s.tracks}
            shared = tracks & have
            if len(shared) >= min(2, len(tracks)) and len(shared) >= MATCH * len(tracks) and shared & moves:
                out.append(s)
        return sorted(out, key=lambda s: s.name.lower())

    def package(self, path: str) -> upk.Package:
        with self._lock:
            pkg = self._packages.get(path)
            if pkg is None:
                pkg = self._packages[path] = upk.open_package(path)
                while len(self._packages) > 6:
                    self._packages.popitem(last=False)
            else:
                self._packages.move_to_end(path)
            return pkg

    def sequence_info(self, s: AnimSetInfo, path: str) -> list:
        """[name, frames, length, additive, error] of one of an AnimSet's sequences."""
        key = f"{s.package}|{path}".lower()
        with self._lock:
            row = self._seqs.get(key)
            if row is not None:
                return row
            try:
                pkg = self.package(s.package)
                e = pkg.find_export(path, "AnimSequence")
                if e is None:
                    raise FormatError(f"{path} isn't in {Path(s.package).name}")
                name, frames, length, additive = sequence_summary(pkg, e)
                row = [name, frames, length, additive, ""]
            except (FormatError, OSError, struct.error, IndexError, ValueError) as exc:
                row = [path.rsplit(".", 1)[-1], 0, 0.0, False, str(exc) or type(exc).__name__]
            self._seqs[key] = row
            self._dirty = True
            return row

    def read(self, s: AnimSetInfo, path: str) -> AnimSequence:
        with self._lock:
            pkg = self.package(s.package)
            e = pkg.find_export(path, "AnimSequence")
            if e is None:
                raise FormatError(f"{path} isn't in {Path(s.package).name}")
            return read_sequence(pkg, e, len(s.tracks))


_INDEXES: dict[str, AnimSetIndex] = {}
_INDEX_LOCK = threading.Lock()


def index_for(root: Path, cache: Path | None) -> AnimSetIndex:
    key = os.path.normcase(str(root))
    with _INDEX_LOCK:
        got = _INDEXES.get(key)
        if got is None or (cache is not None and got.cache is None):
            got = _INDEXES[key] = AnimSetIndex(Path(root), cache)
        return got


def forget(root: Path | None = None) -> None:
    with _INDEX_LOCK:
        if root is None:
            _INDEXES.clear()
        else:
            _INDEXES.pop(os.path.normcase(str(root)), None)


# ---------------------------------------------------------------------------------------------------------
# The animations of one mesh


@dataclass
class _Entry:
    name: str
    set: AnimSetInfo
    path: str  # the sequence's path inside the AnimSet's package
    frames: int = 0
    length: float = 0.0
    additive: bool = False
    error: str = ""


def _conjugate(q: np.ndarray) -> np.ndarray:
    return q * np.array([-1.0, -1.0, -1.0, 1.0])


def _quat_mul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    from .ue4.animset import _quat_mul as mul

    return mul(a, b)


class UE3Animations:
    """The AnimSets' sequences that can pose one Unreal 3 skeletal mesh, with the interface the Pose tab uses
    (``list``, ``info``, ``clamp``, ``skeleton``) and per-frame local poses for the importer and model
    packages (as :class:`darkstl.importers.borderlands.ue4.animset.UEAnimations`)."""

    NOTE = ("Animations come from the game's AnimSets whose bones are this model's, read from its packages "
            "(each listed as AnimSet: sequence).")

    def __init__(self, bones, index: AnimSetIndex, used_bones=None):
        self.names_ = [b.name for b in bones]
        self.parents = np.array([b.parent if i else -1 for i, b in enumerate(bones)], np.int64)
        pose = np.zeros((len(bones), 10))
        for i, b in enumerate(bones):
            pose[i, :4] = b.rotation
            pose[i, 4:7] = b.position
            pose[i, 7:10] = 1.0
        self.ref_pose = pose
        self.local = transforms_to_matrices(pose[:, :4], pose[:, 4:7], pose[:, 7:10])
        self.bind_world = compose_hierarchy(self.local, self.parents)
        self.index = index
        self.used_bones = sorted(int(b) for b in used_bones) if used_bones is not None else None
        self._entries: dict[str, _Entry] | None = None
        self._decoded: OrderedDict[str, AnimSequence] = OrderedDict()
        self._lock = threading.RLock()
        self._bone_of = {n.lower(): i for i, n in enumerate(self.names_)}

    @property
    def skeleton(self) -> list[str]:
        return self.names_

    def moving_bones(self) -> set[str]:
        bones = self.used_bones if self.used_bones is not None else range(len(self.names_))
        return {self.names_[b] for b in bones if 0 <= b < len(self.names_)}

    def sets(self) -> list[AnimSetInfo]:
        return self.index.fitting(self.names_, self.moving_bones())

    @property
    def note(self) -> str:
        if self._entries is not None and not self._entries:
            return ("No AnimSet in the game's packages has this model's bones, so there is nothing to pose it "
                    "with.")
        text = self.NOTE
        bad = self.unreadable()
        if bad:
            text += f" {len(bad)} couldn't be read ({bad[0][1]})."
        return text

    # -- the list
    def entries(self) -> dict[str, _Entry]:
        with self._lock:
            if self._entries is None:
                out: dict[str, _Entry] = {}
                for s in self.sets():
                    for path in s.sequences:
                        name, frames, length, additive, error = self.index.sequence_info(s, path)
                        label = f"{s.name}: {name}"
                        while label in out:
                            label += "'"
                        out[label] = _Entry(label, s, path, int(frames), float(length), bool(additive), error)
                self.index.save()
                self._entries = dict(sorted(out.items(), key=lambda kv: kv[0].lower()))
            return self._entries

    @property
    def names(self) -> list[str]:
        return [n for n, e in self.entries().items() if not e.error]

    def __len__(self) -> int:
        return len(self.names)

    def __contains__(self, name: str) -> bool:
        e = self.entries().get(name)
        return e is not None and not e.error

    def unreadable(self) -> list[tuple[str, str]]:
        if self._entries is None:
            return []
        return [(n, e.error) for n, e in self._entries.items() if e.error]

    def _entry(self, name: str) -> _Entry:
        e = self.entries().get(name)
        if e is None or e.error:
            raise FormatError(f"No animation named {name!r}" if e is None else f"{name}: {e.error}")
        return e

    def _info(self, e: _Entry) -> AnimationInfo:
        fps = (e.frames - 1) / e.length if e.length > 0 and e.frames > 1 else 30.0
        label = f"{e.set.canonical} ({Path(e.set.package).name})" + \
            (" (additive: shown on the bind pose)" if e.additive else "")
        moves = sum(1 for t in e.set.tracks if t.lower() in self._bone_of)
        return AnimationInfo(e.name, max(e.frames, 1), e.length, fps, label, moves, e.frames <= 1)

    def info(self, name: str) -> AnimationInfo:
        return self._info(self._entry(name))

    def list(self) -> list[AnimationInfo]:
        return [self._info(e) for e in self.entries().values() if not e.error]

    def clamp(self, name: str, frame: int) -> int:
        return min(max(int(frame), 0), self.info(name).frames - 1)

    def files_used(self) -> list[Path]:
        """The packages the listed animations are read from (for Collect)."""
        return sorted({Path(e.set.package) for e in self.entries().values()})

    # -- poses
    def _anim(self, name: str) -> tuple[_Entry, AnimSequence]:
        with self._lock:
            e = self._entry(name)
            anim = self._decoded.get(name)
            if anim is None:
                try:
                    anim = self.index.read(e.set, e.path)
                except (struct.error, IndexError, ValueError) as exc:
                    raise FormatError(f"{name} couldn't be read ({exc})") from exc
                self._decoded[name] = anim
                while len(self._decoded) > 4:
                    self._decoded.popitem(last=False)
            else:
                self._decoded.move_to_end(name)
            return e, anim

    def local_poses(self, name: str, frames: np.ndarray | None = None) -> np.ndarray:
        """(F, B, 4, 4) local matrices of the mesh's bones at ``frames`` (default every frame), in Unreal's
        frame and units."""
        e, anim = self._anim(name)
        s = e.set
        at = np.arange(max(anim.frames, 1), dtype=np.float64) if frames is None else \
            np.asarray(frames, np.float64).reshape(-1)
        n = len(at)
        rot = np.repeat(self.ref_pose[None, :, :4], n, axis=0)
        trans = np.repeat(self.ref_pose[None, :, 4:7], n, axis=0)
        scale = np.repeat(self.ref_pose[None, :, 7:10], n, axis=0)
        translated = {b.lower() for b in s.use_translation}
        mesh_only = {b.lower() for b in s.force_mesh_translation}
        for track, bone_name in enumerate(s.tracks):
            i = self._bone_of.get(bone_name.lower())
            if i is None:
                continue
            t, q, _sc = sample(anim, track, at)
            if q is not None:
                if i != 0:  # Unreal 3 keys every bone but the root the other way round from the mesh
                    q = _conjugate(q)
                if anim.additive:
                    q = _quat_mul(q, rot[:, i])
                rot[:, i] = q
            low = bone_name.lower()
            use = i == 0 or ((not s.rotation_only or low in translated) and low not in mesh_only)
            if t is not None and use:
                moved = trans[:, i] + t if anim.additive else t
                if i == 0:  # root motion left out: only the rise and fall
                    moved = moved.copy()
                    moved[:, :2] = trans[:, i, :2]
                trans[:, i] = moved
        return transforms_to_matrices(rot, trans, scale)

    def local_pose(self, name: str, frame: float) -> np.ndarray:
        return self.local_poses(name, np.array([float(frame)]))[0]

    def skin_matrices(self, name: str, frame: float) -> np.ndarray:
        """(B, 4, 4) matrices carrying a vertex from the bind pose into the pose (Unreal's frame and units)."""
        world = compose_hierarchy(self.local_pose(name, frame), self.parents)
        return world @ np.linalg.pinv(self.bind_world)


__all__ = ["AnimSetIndex", "AnimSetInfo", "UE3Animations", "forget", "index_for", "read_anim_set",
           "read_sequence", "sequence_summary"]
