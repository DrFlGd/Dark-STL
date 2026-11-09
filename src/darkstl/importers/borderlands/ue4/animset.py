"""Posing Unreal 4/5 skeletal meshes with the game's animations.

A skeletal mesh is animated through its ``Skeleton`` asset: every ``AnimSequence`` made for that skeleton
can pose it. They are found by reading the header of each package that may be an animation (its name or
folder says so) and keeping those whose ``Skeleton`` import is the mesh's; in a game that names them
otherwise, every package whose name doesn't say it is something else is looked through, once. What that
finds, and each animation's length, is kept in the importer's cache per game, so the list comes back at once
next time. When nothing is found, the Pose tab says what was looked through and what was found instead.

Animations key the skeleton's bones; a mesh's bones are matched to them by name. Rotations come from the
animation; translations are the mesh's bone offsets moved by the animation's change from the skeleton's
reference pose, so a mesh that shares a skeleton with others of other proportions keeps its own. An animation
made on the mesh itself, where the skeleton's reference pose holds the bones turned another way (a skeleton
kept from an earlier version of the model), is measured from the mesh's own bones instead. The root
bone's travel across the floor (root motion) is left out, as for the other games: the model stays where its
bind pose puts it, rising and falling with the animation. Additive animations (offsets meant to be layered
on another animation) are shown on the bind pose.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ....errors import FormatError
from ...fromsoft.animation import AnimationInfo, compose_hierarchy
from . import vfs
from .anim import AnimSequence, Skeleton, read_anim, read_skeleton, sample, transforms_to_matrices
from .asset import Package

log = logging.getLogger(__name__)

INDEX_VERSION = 2
INFO_VERSION = 3  # 2: animations read past the skeleton's GUID; 3: tracks for virtual bones allowed (rows kept
# before said those animations couldn't be read)
ANIM_CLASSES = ("AnimSequence",)
# names an animation package goes by: Epic's style guide (A_, AS_) and others' (Anim_, ANIM_, AN_...)
ANIM_NAME = re.compile(r"^(a|as|an|am|anm|ani|anim|anims|animation|seq)_", re.I)
NOT_ANIM_NAME = re.compile(r"^(t|tex|m|mi|mic|mf|mat|sk|skm|sm|sk_|bp|abp|wbp|phys|pa|pha|ps|p|fx|ns|niagara|s|sw|"
                           r"sc|snd|cue|ak|dt|da|curve|ao|bs|blendspace|mont|am_mont|cr|ctrl|ik|rig|lv|map|l|wb|ui)_",
                           re.I)
# folders that hold no animations, left out when every package is looked through
NOT_ANIM_FOLDERS = {"audio", "sound", "sounds", "wwise", "wwiseaudio", "wwiseassets", "music", "ui", "hud", "umg",
                    "movies", "localization", "fonts", "textures", "texture", "materials", "material",
                    "materiallibrary", "niagara", "particles", "shaders", "splash", "icons", "slate", "lighting"}
WIDE_PEEKS = 250000  # at most this many packages are looked through when the names find nothing
WIDE_FAILURES = 300  # stop looking through when this many in a row can't be read (an Oodle pak with no DLL)
WIDE_THREADS = 4
UNIT = 0.01  # centimetres -> metres
_TO_EXPORT = np.diag([UNIT, -UNIT, UNIT, 1.0])  # Unreal (left-handed, cm) -> export frame (right-handed, m)
_FROM_EXPORT = np.linalg.inv(_TO_EXPORT)


def to_export(m: np.ndarray) -> np.ndarray:
    """Bone matrices (..., 4, 4) in Unreal's frame and units carried into the export frame."""
    return _TO_EXPORT @ np.asarray(m, np.float64) @ _FROM_EXPORT


def maybe_animation(virtual: str) -> bool:
    """Whether a package's name or folder says it may be an animation."""
    low = virtual.lower()
    if not low.endswith(".uasset"):
        return False
    stem = virtual.rsplit("/", 1)[-1][:-7]
    if ANIM_NAME.match(stem):
        return True
    if NOT_ANIM_NAME.match(stem):
        return False
    return any("anim" in part or part in ("mocap", "sequences", "motions") for part in low.split("/")[:-1])


def could_be_animation(virtual: str) -> bool:
    """Whether a package might be an animation at all: its name and folders don't say it is something else."""
    low = virtual.lower()
    if not low.endswith(".uasset"):
        return False
    stem = virtual.rsplit("/", 1)[-1][:-7]
    if NOT_ANIM_NAME.match(stem):
        return False
    return not any(part in NOT_ANIM_FOLDERS for part in low.split("/")[:-1])


def is_animation_class(cls: str) -> bool:
    """An ``AnimSequence``, or a game's own class made from it (``...AnimSequence``)."""
    return cls in ANIM_CLASSES or cls.endswith("AnimSequence")


def _skeleton_package(pkg: Package) -> str | None:
    """The package of the Skeleton a package imports, if it imports exactly one."""
    found = {pkg.import_package(-(i + 1)) for i, imp in enumerate(pkg.imports) if imp.class_name == "Skeleton"}
    if not found:
        found = {pkg.name for e in pkg.exports if e.class_name == "Skeleton"}
    return next(iter(found)) if len({f.lower() for f in found}) == 1 else None


def _main_class(pkg: Package, virtual: str) -> str:
    stem = virtual.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    best = ""
    for e in pkg.exports:
        if e.outer == 0:
            if e.name.lower() == stem:
                return e.class_name
            best = best or e.class_name
    return best


# ---------------------------------------------------------------------------------------------------------
# Which packages are animations, and of which skeleton (kept per game)


@dataclass
class Survey:
    """What looking for a skeleton's animations went through, to say why none were found."""
    looked: int  # packages whose header was read
    wide: bool  # every package that may be an animation was looked through, not only those named like one
    animations: int  # animations found, of any skeleton
    skeletons: list[tuple[str, int]]  # the other skeletons animations were made for, with how many, most first
    users: list[tuple[str, int]]  # classes of the other packages that use this skeleton, with how many
    anim_like: list[tuple[str, int]]  # other classes seen whose name says animation (AnimMontage...)
    unreadable: int  # packages whose header couldn't be read
    first_error: str
    stopped: str  # why looking through stopped early, if it did
    locked: int  # paks no key opened: their files weren't looked at


class AnimIndex:
    """For one game folder: each animation package's skeleton, and what listing an animation read from it.

    Packages named like animations (or in animation folders) are looked at first. When none of them is made
    for the skeleton asked about, in a game that may name its animations otherwise (or where nothing named like
    one is), every package whose name and folders don't say it is something else is looked through, once."""

    def __init__(self, gf: vfs.GameFiles, cache: Path | None):
        self.gf = gf
        self.cache = cache
        self._lock = threading.Lock()
        self._files: dict[str, list] | None = None  # virtual (lower) -> [class, skeleton package, virtual, error]
        self._info: dict[str, list] = {}  # virtual (lower) -> [frames, length, additive, codec, error, moved]
        self._fingerprint = ""
        self._wide = False  # every package that may be an animation has been looked through
        self._stopped = ""  # why that stopped early, if it did
        self._dirty = False

    def _file(self) -> Path | None:
        if self.cache is None:
            return None
        tag = hashlib.sha1(os.path.normcase(str(self.gf.root)).encode()).hexdigest()[:16]
        return Path(self.cache) / f"unreal-animations-{tag}.json"

    def _every(self) -> list[str]:
        return [v for v in self.gf.assets() + (self.gf.zen.assets() if self.gf.zen is not None else [])
                if v.lower().endswith(".uasset")]

    def _make_fingerprint(self, names: list[str]) -> str:
        h = hashlib.sha1(f"{INDEX_VERSION}\n".encode())
        packed = vfs.find_paks(self.gf.root)
        try:
            from .iostore import find_tocs
            packed += find_tocs(self.gf.root)
        except Exception:  # noqa: BLE001
            pass
        for p in packed:
            try:
                st = p.stat()
            except OSError:
                continue
            h.update(f"{p.name}|{st.st_size}|{int(st.st_mtime)}\n".encode())
        if not packed:  # loose files: their names
            for n in sorted(names):
                h.update(n.lower().encode() + b"\n")
        return h.hexdigest()

    def _load(self) -> None:
        if self._files is not None:
            return
        every = self._every()
        self._fingerprint = self._make_fingerprint(every)
        files: dict[str, list] = {}
        f = self._file()
        if f is not None and f.is_file():
            try:
                data = json.loads(f.read_text("utf-8"))
                if data.get("fingerprint") == self._fingerprint:
                    files = data.get("files", {})
                    if data.get("info_version") == INFO_VERSION:
                        self._info = data.get("info", {})
                    self._wide = bool(data.get("wide"))
                    self._stopped = str(data.get("stopped") or "")
            except (OSError, ValueError):
                files = {}
        self._files = files
        self._peek_many([v for v in every if maybe_animation(v) and v.lower() not in files])
        self.save()

    def _peek(self, virtual: str) -> list:
        try:
            if self.gf.zen is not None and not self.gf.exists(virtual):
                pkg = self.gf.zen.open_package(virtual)
            else:
                from . import asset

                pkg = asset.read_package(vfs.package_name(virtual) or virtual, self.gf.read(virtual),
                                         assume_engine=self.gf.assumed_engine)
            return [_main_class(pkg, virtual), _skeleton_package(pkg) or "", virtual, ""]
        except Exception as e:  # noqa: BLE001 - an unreadable package is just not an animation
            log.debug("Couldn't read the header of %s: %s", virtual, e)
            return ["", "", virtual, (str(e) or type(e).__name__)[:300]]

    def _peek_many(self, todo: list[str]) -> str:
        """Read the headers of ``todo`` into the index; why it stopped early (``""`` if it didn't): when that
        many in a row can't be read, the rest won't be either, and those read so far are left out."""
        if not todo:
            return ""
        threads = 1 if self.gf.zen is not None else WIDE_THREADS
        failures, run = 0, []
        with ThreadPoolExecutor(threads) as pool:
            for start in range(0, len(todo), 512):
                chunk = todo[start:start + 512]
                for v, row in zip(chunk, pool.map(self._peek, chunk)):
                    self._files[v.lower()] = row
                    run.append(v.lower())
                    failures = failures + 1 if row[3] else 0
                self._dirty = True
                if failures >= WIDE_FAILURES:
                    for k in run:  # tried again next time (an Oodle library may be found by then)
                        if self._files[k][3]:
                            del self._files[k]
                    return f"the game's packages stopped being readable ({row[3]})"
        return ""

    def _widen(self) -> None:
        """Look through every package that may be an animation."""
        todo = [v for v in self._every() if could_be_animation(v) and v.lower() not in self._files]
        stopped = ""
        if len(todo) > WIDE_PEEKS:
            stopped = f"only the first {WIDE_PEEKS:,} of {len(todo):,} packages were looked through"
            todo = todo[:WIDE_PEEKS]
        log.info("Looking through %d packages for animations", len(todo))
        failed = self._peek_many(todo)
        self._stopped = failed or stopped
        self._wide = not failed
        self._dirty = True
        self.save()

    def save(self) -> None:
        f = self._file()
        if f is None or not self._dirty:
            return
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps({"fingerprint": self._fingerprint, "files": self._files, "info": self._info,
                                       "info_version": INFO_VERSION,
                                       "wide": self._wide, "stopped": self._stopped}), "utf-8")
            tmp.replace(f)
            self._dirty = False
        except OSError:
            log.warning("Couldn't keep the animation index in %s", f, exc_info=True)

    def _of(self, want: str) -> list[str]:
        return sorted((row[2] for row in self._files.values()
                       if is_animation_class(row[0]) and row[1].lower() == want), key=str.lower)

    def of_skeleton(self, skeleton_package: str) -> list[str]:
        """The virtual paths of the animations made for a skeleton (its package, ``/Game/...``)."""
        with self._lock:
            self._load()
            want = skeleton_package.lower()
            found = self._of(want)
            if not found and not self._wide and self._may_widen():
                self._widen()
                found = self._of(want)
            return found

    def _may_widen(self) -> bool:
        """Whether to look through every package: for a game whose importer asks for it, or one none of whose
        packages named like animations is one."""
        return vfs.looks_through_for_animations(self.gf.game) or \
            not any(is_animation_class(row[0]) for row in self._files.values())

    def survey(self, skeleton_package: str) -> Survey:
        """What the index went through, for a skeleton none of whose animations it found."""
        with self._lock:
            self._load()
            want = skeleton_package.lower()
            rows = list(self._files.values())
            anims = [r for r in rows if is_animation_class(r[0])]
            skeletons = Counter(r[1] for r in anims if r[1].lower() != want)
            users = Counter(r[0] for r in rows if r[1].lower() == want and r[0] and r[0] != "Skeleton"
                            and not is_animation_class(r[0]))
            anim_like = Counter(r[0] for r in rows if "anim" in r[0].lower() and not is_animation_class(r[0]))
            bad = [r for r in rows if r[3]]
            return Survey(len(rows), self._wide, len(anims), skeletons.most_common(), users.most_common(),
                          anim_like.most_common(), len(bad), bad[0][3] if bad else "", self._stopped,
                          len(self.gf.locked))

    def info(self, virtual: str) -> list | None:
        with self._lock:
            return self._info.get(virtual.lower())

    def set_info(self, virtual: str, row: list) -> None:
        with self._lock:
            self._info[virtual.lower()] = row
            self._dirty = True


_INDEXES: dict[str, AnimIndex] = {}
_INDEX_LOCK = threading.Lock()


def index_for(gf: vfs.GameFiles, cache: Path | None) -> AnimIndex:
    key = os.path.normcase(str(gf.root))
    with _INDEX_LOCK:
        got = _INDEXES.get(key)
        if got is None or got.gf is not gf:
            got = _INDEXES[key] = AnimIndex(gf, cache)
        return got


# ---------------------------------------------------------------------------------------------------------
# The animations of one mesh


@dataclass
class _Entry:
    name: str
    virtual: str
    frames: int = 0
    length: float = 0.0
    additive: bool | None = None
    error: str = ""
    moves: int | None = None


class UEAnimations:
    """The animations that can pose one Unreal skeletal mesh, with the interface the Pose tab uses (``list``,
    ``info``, ``clamp``, ``skeleton``) and per-frame local poses for the importer and model packages."""

    NOTE = ("Animations come from the game's AnimSequence assets made for this model's Skeleton, read from "
            "the game's paks.")

    def __init__(self, bones: list[tuple[str, int]], ref_pose: np.ndarray, skeleton_package: str,
                 gf: vfs.GameFiles, cache: Path | None = None, used_bones=None):
        self.names_ = [b[0] for b in bones]
        self.parents = np.array([b[1] for b in bones], np.int64)
        self.ref_pose = np.asarray(ref_pose, np.float64)
        self.local = transforms_to_matrices(self.ref_pose[:, :4], self.ref_pose[:, 4:7], self.ref_pose[:, 7:10])
        self.bind_world = compose_hierarchy(self.local, self.parents)
        self.skeleton_package = skeleton_package
        self.gf = gf
        self.index = index_for(gf, cache)
        self.used_bones = sorted(int(b) for b in used_bones) if used_bones is not None else None
        self._skeleton: Skeleton | None = None
        self._map: np.ndarray | None = None  # mesh bone -> skeleton bone (-1)
        self._entries: dict[str, _Entry] | None = None
        self._decoded: OrderedDict[str, AnimSequence] = OrderedDict()
        self._made_on: dict[str, bool] = {}
        self._lock = threading.RLock()

    @property
    def note(self) -> str:
        """What the Pose tab says under the list: where the animations come from, and those it can't show."""
        text = self.NOTE
        if self._entries is None:
            return text
        if not self._entries:
            return self._none_found()
        bad = self.unreadable()
        if bad:
            acl = [n for n, why in bad if "ACL" in why]
            if acl:
                text += (f" {len(acl)} of its skeleton's animations are compressed with the ACL plugin, which Dark "
                         "STL can't read yet, so they aren't listed.")
            other = Counter(why for _n, why in bad if "ACL" not in why)
            if other:
                n = sum(other.values())
                (first, k), *more = other.most_common()
                text += f" {n} couldn't be read ({first}" + (
                    f"; {k} of them, and {n - k} for {len(more)} other reason{'s' if len(more) > 1 else ''})." if more
                    else ").")
        return text

    def _none_found(self) -> str:
        """Why no animations are listed: what was looked through, and what was found instead."""
        s = self.index.survey(self.skeleton_package)
        mine = self.skeleton_package.rsplit("/", 1)[-1]

        def short(package: str) -> str:
            if not package:
                return "no single skeleton"
            stem = package.rsplit("/", 1)[-1]
            return package if stem.lower() == mine.lower() else stem  # another folder's skeleton of that name

        def counted(pairs, most=4) -> str:
            text = ", ".join(f"{name}: {n:,}" for name, n in pairs[:most])
            return text + (f" and {len(pairs) - most} more" if len(pairs) > most else "")

        how = "every package that may be one" if s.wide else "those named like animations"
        text = (f"No animations made for its skeleton ({mine}) were found in the game's files. Dark STL looked "
                f"through {s.looked:,} {'package' if s.looked == 1 else 'packages'} ({how})")
        if s.animations:
            text += (f": {s.animations:,} {'is an animation' if s.animations == 1 else 'are animations'}, made "
                     f"for other skeletons ({counted([(short(k), n) for k, n in s.skeletons])}).")
        else:
            text += ", and none of them is an animation (AnimSequence)."
        if s.anim_like:
            text += f" Other animation assets found: {counted(s.anim_like)}."
        if s.users:
            text += f" Other assets made for its skeleton: {counted(s.users)}."
        if self.gf.virtual_of_package(self.skeleton_package) is None and (
                self.gf.zen is None or self.gf.zen.by_name.get(self.skeleton_package.lower()) is None):
            text += f" Its skeleton ({self.skeleton_package}) isn't in the game's files either."
        if s.unreadable:
            text += (f" {s.unreadable:,} {'package' if s.unreadable == 1 else 'packages'} couldn't be read "
                     f"({s.first_error}).")
        if s.stopped:
            text += f" Looking through stopped early: {s.stopped}."
        if s.locked:
            text += (f" {s.locked} of the game's paks couldn't be opened (they need another AES key), so their "
                     "files weren't looked at.")
        return text

    # -- the skeleton
    @property
    def skeleton(self) -> list[str]:
        return self.names_

    def _skel(self) -> Skeleton:
        if self._skeleton is None:
            pkg = self.gf.open_named(self.skeleton_package)
            if pkg is None:
                raise FormatError(f"the model's skeleton {self.skeleton_package} isn't in the game's files")
            e = pkg.main_export(("Skeleton",))
            if e is None:
                raise FormatError(f"{self.skeleton_package} isn't a Skeleton")
            self._skeleton = read_skeleton(pkg, e)
            index = {n.lower(): i for i, n in enumerate(self._skeleton.names)}
            self._map = np.array([index.get(n.lower(), -1) for n in self.names_], np.int64)
        return self._skeleton

    def debug(self) -> dict:
        """The model's bones beside its skeleton's, for a review of a model that poses wrongly."""
        skel = self._skel()
        names = skel.names
        matched = [(i, int(s)) for i, s in enumerate(self._map) if s >= 0]
        diffs = []
        for i, s in matched:
            dt = float(np.linalg.norm(self.ref_pose[i, 4:7] - skel.ref_pose[s, 4:7]))
            dq = float(abs(np.dot(self.ref_pose[i, :4], skel.ref_pose[s, :4])))
            diffs.append((dt, 2 * np.degrees(np.arccos(min(dq, 1.0))), self.names_[i]))
        diffs.sort(reverse=True)
        return {"package": self.skeleton_package, "skeleton_bones": len(names), "mesh_bones": len(self.names_),
                "virtual_bones": [{"name": n, "source": a, "target": b} for n, a, b in skel.virtual],
                "matched": len(matched), "mesh_only": [n for n, s in zip(self.names_, self._map) if s < 0][:40],
                "skeleton_only": [n for j, n in enumerate(names) if j not in {s for _i, s in matched}][:40],
                "bind_differences": [{"bone": n, "translation": round(dt, 3), "degrees": round(float(da), 2)}
                                     for dt, da, n in diffs[:12]],
                "skeleton_names": names, "skeleton_ref_pose": np.round(skel.ref_pose, 4).tolist(),
                "mesh_names": self.names_, "mesh_ref_pose": np.round(self.ref_pose, 4).tolist(),
                "other_skeletons": self.index.survey(self.skeleton_package).skeletons[:40]}

    def files_used(self) -> list[str]:
        """The virtual paths of the skeleton and the animations listed (for Collect)."""
        out = []
        v = self.gf.virtual_of_package(self.skeleton_package)
        if v is None and self.gf.zen is not None:
            v = self.gf.zen.by_name.get(self.skeleton_package.lower())
        if v:
            out.append(v)
        return out + [e.virtual for e in self.entries().values()]

    # -- the list
    def entries(self) -> dict[str, _Entry]:
        with self._lock:
            if self._entries is None:
                found = self.index.of_skeleton(self.skeleton_package)
                stems: dict[str, int] = {}
                for v in found:
                    stem = v.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                    stems[stem.lower()] = stems.get(stem.lower(), 0) + 1
                out: dict[str, _Entry] = {}
                for v in found:
                    stem = v.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                    name = stem if stems[stem.lower()] == 1 else f"{stem} ({v.rsplit('/', 2)[-2]})"
                    while name in out:
                        name += "'"
                    out[name] = _Entry(name, v)
                self._entries = dict(sorted(out.items(), key=lambda kv: kv[0].lower()))
                if self._entries:
                    try:  # first, so every animation is checked against it as posing will
                        self._skel()
                    except FormatError:
                        pass
                for e in self._entries.values():
                    self._summarise(e)
                self.index.save()
            return self._entries

    def _summarise(self, e: _Entry) -> None:
        row = self.index.info(e.virtual)
        if row is None:
            try:
                anim = self._read(e.virtual)
                moved = sorted(anim.tracks)
                row = [anim.frames, anim.length, anim.additive, anim.codec, "", moved]
            except (FormatError, ValueError, IndexError, KeyError) as exc:
                row = [0, 0.0, None, "", str(exc) or type(exc).__name__, []]
            self.index.set_info(e.virtual, row)
        e.frames, e.length, e.additive, _codec, e.error = int(row[0]), float(row[1]), row[2], row[3], row[4]
        if not e.error:
            try:
                self._skel()
                moved = set(int(b) for b in row[5])
                shows = self._shown_bones()
                e.moves = sum(1 for b in shows if self._map[b] in moved)
            except FormatError:
                e.moves = None

    def _shown_bones(self) -> list[int]:
        bones = self.used_bones if self.used_bones is not None else list(range(len(self.names_)))
        out: set[int] = set()
        for b in bones:
            while 0 <= b < len(self.parents) and b not in out:
                out.add(b)
                b = int(self.parents[b])
        return sorted(out)

    @property
    def names(self) -> list[str]:
        return [n for n, e in self.entries().items() if not e.error]

    def __len__(self) -> int:
        return len(self.names)

    def __contains__(self, name: str) -> bool:
        e = self.entries().get(name)
        return e is not None and not e.error

    def unreadable(self) -> list[tuple[str, str]]:
        """Animations of the skeleton that couldn't be read, with why."""
        return [(n, e.error) for n, e in self.entries().items() if e.error]

    def _entry(self, name: str) -> _Entry:
        e = self.entries().get(name)
        if e is None or e.error:
            raise FormatError(f"No animation named {name!r}" if e is None else f"{name}: {e.error}")
        return e

    def _info(self, e: _Entry) -> AnimationInfo:
        fps = (e.frames - 1) / e.length if e.length > 0 and e.frames > 1 else 30.0
        where = e.virtual.rsplit(".", 1)[0]
        label = where + (" (additive: shown on the bind pose)" if e.additive else "")
        return AnimationInfo(e.name, max(e.frames, 1), e.length, fps, label, e.moves, e.frames <= 1)

    def info(self, name: str) -> AnimationInfo:
        return self._info(self._entry(name))

    def list(self) -> list[AnimationInfo]:
        return [self._info(e) for e in self.entries().values() if not e.error]

    def clamp(self, name: str, frame: int) -> int:
        return min(max(int(frame), 0), self.info(name).frames - 1)

    # -- poses
    def _read(self, virtual: str) -> AnimSequence:
        pkg = self.gf.open_package(virtual)
        e = pkg.main_export(ANIM_CLASSES) or next(
            (x for x in pkg.exports if x.outer == 0 and is_animation_class(x.class_name)), None)
        if e is None:
            raise FormatError(f"{virtual} isn't an animation")
        bones = self._skeleton.tracks if self._skeleton is not None else None
        return read_anim(pkg, e, bones)

    def _anim(self, name: str) -> AnimSequence:
        with self._lock:
            if name in self._decoded:
                self._decoded.move_to_end(name)
                return self._decoded[name]
            e = self._entry(name)
            self._skel()
            anim = self._read(e.virtual)
            self._decoded[name] = anim
            while len(self._decoded) > 4:
                self._decoded.popitem(last=False)
            return anim

    def local_poses(self, name: str, frames: np.ndarray | None = None) -> np.ndarray:
        """(F, B, 4, 4) local matrices of the mesh's bones at ``frames`` (default every frame), in Unreal's
        frame and units."""
        anim = self._anim(name)
        skel = self._skel()
        at = np.arange(max(anim.frames, 1), dtype=np.float64) if frames is None else \
            np.asarray(frames, np.float64).reshape(-1)
        n, b = len(at), len(self.names_)
        rot = np.repeat(self.ref_pose[None, :, :4], n, axis=0)
        trans = np.repeat(self.ref_pose[None, :, 4:7], n, axis=0)
        scale = np.repeat(self.ref_pose[None, :, 7:10], n, axis=0)
        additive = bool(anim.additive)
        on_mesh = not additive and self.made_on_the_mesh(name)
        for i in range(b):
            s = int(self._map[i])
            if s < 0:
                continue
            t, q, sc = sample(anim, s, at)
            # the pose its offsets are measured from: the skeleton's, or the mesh's own when it was made on it
            ref = self.ref_pose[i] if on_mesh else skel.ref_pose[s]
            if q is not None:
                if additive:
                    q = _quat_mul(q, rot[:, i])
                rot[:, i] = q
            if t is not None:
                delta = t if additive else t - ref[4:7]
                moved = trans[:, i] + delta
                if self.parents[i] < 0:  # root motion left out: only the rise and fall
                    moved[:, :2] = trans[:, i, :2]
                trans[:, i] = moved
            if sc is not None:
                scale[:, i] = scale[:, i] * (1 + sc) if additive else sc
        return transforms_to_matrices(rot, trans, scale)

    def made_on_the_mesh(self, name: str) -> bool:
        """Whether the animation was made on this mesh's own bones rather than on its skeleton's reference
        pose: when the two differ (a skeleton kept from an earlier version of the model, with its bones
        turned another way), the animation's keys sit near one of them, and that is the pose they are
        offsets from. With the same bones in both it makes no difference."""
        with self._lock:
            known = self._made_on.get(name)
            if known is not None:
                return known
            anim = self._anim(name)
            skel = self._skel()
            at = np.unique(np.linspace(0, max(anim.frames - 1, 0), 8).round())
            mesh_votes = skel_votes = 0
            for i, s in enumerate(self._map):
                if s < 0:
                    continue
                mine, theirs = self.ref_pose[i], skel.ref_pose[s]
                t, q, _sc = sample(anim, int(s), at)
                apart = float(np.linalg.norm(mine[4:7] - theirs[4:7]))
                if t is not None and apart > 1e-3 * max(1.0, float(np.linalg.norm(mine[4:7]))):
                    near = np.linalg.norm(t - mine[4:7], axis=1) < np.linalg.norm(t - theirs[4:7], axis=1)
                    mesh_votes += int(near.sum())
                    skel_votes += int((~near).sum())
                if q is not None and _angle(mine[:4], theirs[:4]) > 2.0:
                    near = _angle(q, mine[:4]) < _angle(q, theirs[:4])
                    mesh_votes += int(near.sum())
                    skel_votes += int((~near).sum())
            self._made_on[name] = found = mesh_votes > skel_votes
            if found:
                log.info("%s was made on the mesh's own bones, not its skeleton's reference pose", name)
            return found

    def local_pose(self, name: str, frame: float) -> np.ndarray:
        return self.local_poses(name, np.array([float(frame)]))[0]

    def skin_matrices(self, name: str, frame: float) -> np.ndarray:
        """(B, 4, 4) matrices carrying a vertex from the bind pose into the pose (Unreal's frame and units)."""
        world = compose_hierarchy(self.local_pose(name, frame), self.parents)
        return world @ np.linalg.pinv(self.bind_world)


def _angle(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Degrees between quaternions (x, y, z, w), either sign of each being the same turn."""
    a = np.asarray(a, np.float64)
    b = np.asarray(b, np.float64)
    a = a / np.maximum(np.linalg.norm(a, axis=-1, keepdims=True), 1e-12)
    b = b / np.maximum(np.linalg.norm(b, axis=-1, keepdims=True), 1e-12)
    return 2 * np.degrees(np.arccos(np.clip(np.abs(np.sum(a * b, axis=-1)), 0.0, 1.0)))


def _quat_mul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Hamilton products a * b of (n, 4) quaternions (x, y, z, w)."""
    ax, ay, az, aw = np.moveaxis(np.asarray(a, np.float64), -1, 0)
    bx, by, bz, bw = np.moveaxis(np.asarray(b, np.float64), -1, 0)
    return np.stack([aw * bx + ax * bw + ay * bz - az * by,
                     aw * by - ax * bz + ay * bw + az * bx,
                     aw * bz + ax * by - ay * bx + az * bw,
                     aw * bw - ax * bx - ay * by - az * bz], axis=-1)


def skin(positions: np.ndarray, normals: np.ndarray | None, joints: np.ndarray, weights: np.ndarray,
         matrices: np.ndarray) -> tuple[np.ndarray, np.ndarray | None]:
    """Linear blend skinning in Unreal's frame; vertices without weights stay where they are."""
    from ....posing import blend

    m, _ = blend(joints, weights, matrices)
    p = np.einsum("nij,nj->ni", m[:, :3, :3], positions) + m[:, :3, 3]
    n = None
    if normals is not None:
        n = np.einsum("nij,nj->ni", m[:, :3, :3], np.asarray(normals, np.float64))
        length = np.linalg.norm(n, axis=1, keepdims=True)
        n = n / np.where(length > 0, length, 1)
    return p, n


__all__ = ["AnimIndex", "Survey", "UEAnimations", "could_be_animation", "index_for", "is_animation_class",
           "maybe_animation", "skin", "to_export"]
