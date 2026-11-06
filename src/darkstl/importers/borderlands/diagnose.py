"""Find where an object is in a Borderlands (Unreal 3) game folder: every package that holds it, or names it.

For tracking down a material or texture the importer can't find: ``darkstl find <game folder> <name>``
lists each export and import whose name contains ``<name>`` in every package under the folder, with its
class, its path in that package, the path the importer looks it up by, and its flags, plus the packages
that couldn't be read and why.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ...errors import FormatError
from . import files
from .formats import upk

EF_FORCED_EXPORT = 0x01


@dataclass
class Match:
    package: Path
    kind: str  # "export" or "import"
    class_name: str
    path: str  # inside the package (an import's starts with the package it's imported from)
    canonical: str = ""  # exports: the path the importer's object index keys it by
    flags: str = ""

    def line(self, root: Path) -> str:
        try:
            where = self.package.relative_to(root)
        except ValueError:
            where = self.package
        extra = f"  [looked up as {self.canonical}]" if self.canonical and self.canonical != self.path else ""
        flags = f"  ({self.flags})" if self.flags else ""
        return f"{where}: {self.kind} {self.class_name} {self.path}{extra}{flags}"


@dataclass
class Report:
    root: Path
    needles: list[str]
    packages: int = 0
    matches: list[Match] = field(default_factory=list)
    failed: list[tuple[Path, str]] = field(default_factory=list)

    def lines(self, limit: int = 400) -> list[str]:
        exports = [m for m in self.matches if m.kind == "export"]
        imports = [m for m in self.matches if m.kind == "import"]
        out = [f"Searched {self.packages:,} packages under {self.root} for {', '.join(self.needles)}",
               f"{len(exports)} exports (objects stored in a package), {len(imports)} imports (references to "
               f"objects stored elsewhere)", ""]
        if exports:
            out.append("Stored in:")
            out += ["  " + m.line(self.root) for m in exports[:limit]]
            if len(exports) > limit:
                out.append(f"  ... and {len(exports) - limit} more")
            out.append("")
        if imports:
            out.append("Referenced by:")
            shown = imports[:max(20, limit - len(exports))]
            out += ["  " + m.line(self.root) for m in shown]
            if len(imports) > len(shown):
                out.append(f"  ... and {len(imports) - len(shown)} more")
            out.append("")
        if self.failed:
            out.append(f"{len(self.failed)} packages couldn't be read:")
            out += [f"  {p.name}: {err}" for p, err in self.failed[:30]]
            if len(self.failed) > 30:
                out.append(f"  ... and {len(self.failed) - 30} more")
        return out


def _flags(pkg: upk.Package, e: upk.Export) -> str:
    out = []
    if e.export_flags & EF_FORCED_EXPORT:
        out.append("forced export")
    top, seen = e, 0
    while top.outer > 0 and seen < 64:
        top = pkg.exports[top.outer - 1]
        seen += 1
    if top is not e:
        out.append(f"outer {top.name} is a {top.class_name}" + (" (forced export)"
                                                                  if top.export_flags & EF_FORCED_EXPORT else ""))
    if e.size <= 0:
        out.append("no data")
    return ", ".join(out)


def find_objects(root: Path, needles: list[str], progress: Callable[[int, int], None] | None = None) -> Report:
    """Every export and import under ``root`` whose name contains one of ``needles`` (any case)."""
    root = Path(root)
    low = [n.lower() for n in needles if n]
    report = Report(root, list(needles))
    packages = list(files.iter_packages(root))
    for i, path in enumerate(packages):
        if progress is not None:
            progress(i, len(packages))
        try:
            pkg = upk.open_package(path)
        except (FormatError, OSError, ValueError, IndexError) as exc:
            report.failed.append((path, str(exc) or type(exc).__name__))
            continue
        report.packages += 1
        for e in pkg.exports:
            if any(n in e.name.lower() for n in low):
                try:
                    canonical = files.canonical_path(pkg, e)
                except IndexError:
                    canonical = ""
                report.matches.append(Match(path, "export", e.class_name, e.path, canonical, _flags(pkg, e)))
        for k, imp in enumerate(pkg.imports):
            if any(n in imp.name.lower() for n in low):
                try:
                    where = pkg.object_path(-k - 1)
                except (IndexError, KeyError):
                    where = imp.name
                report.matches.append(Match(path, "import", imp.class_name, where))
    return report


__all__ = ["Match", "Report", "find_objects"]
