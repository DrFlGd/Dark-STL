"""Every texture of a game kept in its asset store, not only those its models use.

The importers list a game folder's textures (``Importer.list_textures``) and open them in bulk
(``Importer.open_textures``); each is stored still compressed as the game keeps it (DDS) where the
importer gives it so. Before that, :func:`estimate` works out how much it would add, from a sample of
the textures not yet stored. The store remembers which listed texture each stored file is, so a second
run only stores what's new.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .package import AssetStore, _encode_image, _raw_dds, texture_base

log = logging.getLogger(__name__)

SAMPLE = 16  # textures opened to estimate the size of the rest


@dataclass(frozen=True)
class Listed:
    root: Path
    importer: object  # the Importer that listed it
    name: str
    ref: str

    @property
    def source(self) -> str:
        """How the asset store knows it (the same wherever the game folder is)."""
        return f"{self.importer.key}|{self.ref}"


def listed(roots: list[Path], importers: list) -> list[Listed]:
    """Every texture the game's folders hold, as their importers list them. A name an earlier folder lists
    isn't listed again from a later one (a collected copy of some of the game's files)."""
    out: list[Listed] = []
    seen: set[str] = set()
    for root in roots:
        here: list[Listed] = []
        for imp in importers:
            try:
                found = imp.list_textures(Path(root), deep=True)
            except Exception:  # noqa: BLE001 - one importer's listing failing leaves the others
                log.debug("Texture list of %s by %s failed", root, imp.key, exc_info=True)
                continue
            here += [Listed(Path(root), imp, name, ref) for name, ref in found]
        out += [t for t in here if t.name.lower() not in seen]
        seen |= {t.name.lower() for t in here}
    return out


def missing(store: AssetStore, textures: list[Listed]) -> list[Listed]:
    """Those of ``textures`` the store doesn't hold yet: not stored from this listing before, nor (for a
    name only one texture has) stored from a model that uses it."""
    names = Counter(t.name.lower() for t in textures)
    under = store.stored_under()
    return [t for t in textures if store.stored_source(t.source) is None
            and not (names[t.name.lower()] == 1 and texture_base(t.name) in under)]


def stored_size(texture) -> int:
    """The bytes a texture takes in the asset store."""
    data = _raw_dds(texture)
    if data is not None:
        return len(data)
    return len(_encode_image(texture.decode()))


def _by_importer(textures: list[Listed]) -> dict[tuple, list[Listed]]:
    out: dict[tuple, list[Listed]] = {}
    for t in textures:
        out.setdefault((t.root, t.importer.key), []).append(t)
    return out


def estimate(textures: list[Listed], check: Callable[[], None] | None = None) -> dict:
    """How much storing ``textures`` would add: a sample spread through them is opened and measured.
    ``{"bytes", "sampled"}``."""
    if not textures:
        return {"bytes": 0, "sampled": 0}
    step = max(1, len(textures) // SAMPLE)
    sample = textures[::step][:SAMPLE]
    sizes = []
    for (root, _key), these in _by_importer(sample).items():
        imp = these[0].importer
        for _ref, tex in imp.open_textures(root, [t.ref for t in these]):
            if check:
                check()
            if isinstance(tex, Exception):
                continue
            try:
                sizes.append(stored_size(tex))
            except Exception:  # noqa: BLE001 - one that can't be read isn't counted
                continue
    if not sizes:
        return {"bytes": 0, "sampled": 0}
    return {"bytes": int(sum(sizes) / len(sizes) * len(textures)), "sampled": len(sizes)}


def keep(store: AssetStore, textures: list[Listed], report: Callable[[int, str], None] | None = None,
         check: Callable[[], None] | None = None) -> tuple[int, list[str]]:
    """Store each of ``textures``: (how many were stored, why others couldn't be)."""
    stored, failed, n = 0, [], 0
    for (root, _key), these in _by_importer(textures).items():
        imp = these[0].importer
        by_ref = {t.ref: t for t in these}
        for ref, tex in imp.open_textures(root, list(by_ref)):
            if check:
                check()
            t = by_ref[ref]
            n += 1
            if report:
                report(n, t.name)
            if isinstance(tex, Exception):
                failed.append(f"{t.name}: {tex}")
                continue
            try:
                store.add_texture(tex, t.source)
                stored += 1
            except Exception as exc:  # noqa: BLE001 - one that can't be read doesn't stop the rest
                log.debug("Keeping %s failed", t.name, exc_info=True)
                failed.append(f"{t.name}: {exc}")
            store.save(every=10.0)
    return stored, failed
