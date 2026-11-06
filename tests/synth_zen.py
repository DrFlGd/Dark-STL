"""Byte-built Unreal 5 IoStore containers holding zen packages, shaped like Borderlands 4's.

:func:`zen_package` turns a :class:`synth_ue4.Ue4Package` into a zen package (with versioning
info, so the UE4-layout test data still reads with its own layouts); :func:`build_utoc` packs
chunks into a ``.utoc``/``.ucas`` pair with a directory index, zlib-compressed blocks and, with a
key, AES encryption; :func:`build_game` writes a whole game folder (``global.utoc`` with the
script objects, and one content container with its package store header).
"""

from __future__ import annotations

import hashlib
import struct
import zlib

from synth_ue4 import Ue4Package, _aes

EXPORT_BUNDLE_DATA, BULK_DATA, SCRIPT_OBJECTS, CONTAINER_HEADER = 1, 2, 5, 6
NULL = (1 << 64) - 1


def h64(text: str) -> int:
    return int.from_bytes(hashlib.sha1(text.lower().encode()).digest()[:8], "little")


def package_id(name: str) -> int:
    return h64("package:" + name)


def name_batch(names: list[str]) -> bytes:
    if not names:
        return struct.pack("<i", 0)
    strings = b"".join(n.encode() for n in names)
    out = struct.pack("<iIQ", len(names), len(strings), 0xC1640000) + bytes(8 * len(names))
    for n in names:
        b = len(n.encode())
        out += bytes([(b >> 8) & 0x7F, b & 0xFF])
    return out + strings


def mapped(names: list[str], s: str, global_=False) -> int:
    return names.index(s) | ((2 if global_ else 0) << 30)


class Script:
    """The engine's script objects (``/Script/Engine`` and its classes) as ``global.utoc`` lists them."""

    def __init__(self):
        self.names: list[str] = []
        self.entries: list[tuple[str, int, int, int]] = []  # name, global index, outer, cdo class
        self.index: dict[str, int] = {}

    def add(self, path: str) -> int:
        if path in self.index:
            return self.index[path]
        outer = NULL
        if "." in path:
            outer = self.add(path.rsplit(".", 1)[0])
        name = path.rsplit(".", 1)[-1]
        if name not in self.names:
            self.names.append(name)
        gidx = (1 << 62) | (len(self.entries) + 1)
        self.entries.append((name, gidx, outer, 1 if outer != NULL else NULL))
        self.index[path] = gidx
        return gidx

    def chunk(self) -> bytes:
        out = name_batch(self.names) + struct.pack("<i", len(self.entries))
        for name, gidx, outer, cdo in self.entries:
            out += struct.pack("<QQQQ", mapped(self.names, name, True), gidx, outer, cdo)
        return out


def zen_package(b: Ue4Package, script: Script, unversioned: bool = False) -> tuple[bytes, list[str], bytes]:
    """(the package chunk, the names of the packages it imports from, its bulk data)."""
    uasset, uexp, ubulk = b.build()
    names = list(b.names)
    if b.pkg_name not in names:
        names.append(b.pkg_name)
    imported_pkgs: list[str] = []
    hashes: list[int] = []
    import_map: list[int] = []
    for cp, cn, outer, nm in b.imports:
        if cn == "Package":
            if nm.startswith("/Script/"):
                import_map.append(script.add(nm))
            else:
                import_map.append(NULL)  # zen packages list no package objects
            continue
        opkg = b.imports[-outer - 1][3]
        if opkg.startswith("/Script/"):
            import_map.append(script.add(f"{opkg}.{nm}"))
            continue
        if opkg not in imported_pkgs:
            imported_pkgs.append(opkg)
        hashes.append(h64(f"{opkg}.{nm}"))
        import_map.append((2 << 62) | (imported_pkgs.index(opkg) << 32) | (len(hashes) - 1))
    exports = b""
    off = 0
    for e in b.exports:
        cls = import_map[-e["class"] - 1]
        outer = NULL if e["outer"] == 0 else e["outer"] - 1
        exports += struct.pack("<QQQQQQQQIB3x", off, len(e["data"]), mapped(names, e["name"]), outer, cls, NULL, NULL,
                               h64(f"{b.pkg_name}.{e['name']}"), 0, 0)
        off += len(e["data"])
    custom = struct.pack("<i", len(b.custom)) + b"".join(struct.pack("<4Ii", *g, n) for g, n in b.custom.items())
    versioning = struct.pack("<Iiii", 3, b.ue4, 0, 0) + custom
    flags = 0x80000000 | (0x2000 if unversioned else 0)

    def header(hsize, offsets) -> bytes:
        h = struct.pack("<IIQII", 1, hsize, mapped(names, b.pkg_name), flags, len(uasset))
        h += struct.pack("<7i", *offsets)
        return h + versioning + name_batch(names) + struct.pack("<Qq", 0, 0)

    pre = header(0, [0] * 7)
    hashes_off = len(pre)
    imports_off = hashes_off + 8 * len(hashes)
    exports_off = imports_off + 8 * len(import_map)
    bundles_off = exports_off + len(exports)
    bundles = b"".join(struct.pack("<II", i, c) for i in range(len(b.exports)) for c in (0, 1))
    end = bundles_off + len(bundles)
    body = (struct.pack(f"<{len(hashes)}Q", *hashes) + struct.pack(f"<{len(import_map)}Q", *import_map)
            + exports + bundles)
    head = header(end, [hashes_off, imports_off, exports_off, bundles_off, end, end, end])
    assert len(head) == hashes_off
    data = b"".join(e["data"] for e in b.exports)
    return head + body + data, imported_pkgs, ubulk


def _dir_index(mount: str, files: list[tuple[str, int]]) -> bytes:
    strings: list[str] = []

    def sid(s: str) -> int:
        if s not in strings:
            strings.append(s)
        return strings.index(s)

    dirs = [[0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF]]  # name, first child, next sibling, first file
    paths = {"": 0}
    fentries: list[list[int]] = []

    def dir_of(path: str) -> int:
        if path in paths:
            return paths[path]
        parent, _, name = path.rpartition("/")
        p = dir_of(parent)
        dirs.append([sid(name), 0xFFFFFFFF, dirs[p][1], 0xFFFFFFFF])
        dirs[p][1] = len(dirs) - 1
        paths[path] = len(dirs) - 1
        return paths[path]

    for path, entry in files:
        d, _, name = path.rpartition("/")
        di = dir_of(d)
        fentries.append([sid(name), dirs[di][3], entry])
        dirs[di][3] = len(fentries) - 1

    def fs(s: str) -> bytes:
        bs = s.encode() + b"\0"
        return struct.pack("<i", len(bs)) + bs

    out = fs(mount) + struct.pack("<I", len(dirs)) + b"".join(struct.pack("<4I", *d) for d in dirs)
    out += struct.pack("<I", len(fentries)) + b"".join(struct.pack("<3I", *f) for f in fentries)
    out += struct.pack("<I", len(strings)) + b"".join(fs(s) for s in strings)
    return out


def build_utoc(chunks: list[tuple[tuple[int, int, int], bytes, str | None]], container_id: int, key=None,
               compress=True, block_size=0x4000, mount="../../../", indexed=True) -> tuple[bytes, bytes]:
    """(.utoc, .ucas) for chunks given as ((id, index, type), data, file path or None)."""
    address = bytearray()
    offsets = []
    for _cid, data, _path in chunks:
        start = len(address)
        offsets.append((start, len(data)))
        address += data
        address += bytes((-len(address)) % block_size)
    ucas = bytearray()
    blocks = []
    for i in range(0, len(address), block_size):
        raw = bytes(address[i:i + block_size])
        method = 0
        if compress:
            c = zlib.compress(raw)
            if len(c) < len(raw):
                raw, method = c, 1
        stored = raw + bytes((-len(raw)) % 16)
        if key is not None:
            stored = _aes(key, stored)
        blocks.append((len(ucas), len(raw), min(block_size, len(address) - i), method))
        ucas += stored
    files = [(p, i) for i, (_c, _d, p) in enumerate(chunks) if p]
    index = _dir_index(mount, files) if indexed else b""
    if key is not None and index:
        index += bytes((-len(index)) % 16)
        index = _aes(key, index)
    flags = (1 if compress else 0) | (2 if key is not None else 0) | (8 if indexed else 0)
    methods = [b"Zlib"] if compress else []
    method_len = 32
    head = b"-==--==--==--==-" + struct.pack("<BBH", 8, 0, 0)
    head += struct.pack("<9I", 144, len(chunks), len(blocks), 12, len(methods), method_len, block_size, len(index), 1)
    head += struct.pack("<Q", container_id) + bytes(16) + struct.pack("<BBH", flags, 0, 0)
    head += struct.pack("<IQI", 0, 1 << 40, 0) + struct.pack("<I", 0) + bytes(40)
    assert len(head) == 144
    body = b""
    for (cid, cidx, ctype), _d, _p in chunks:
        body += struct.pack("<Q", cid) + struct.pack(">H", cidx) + bytes([0, ctype])
    for off, length in offsets:
        body += off.to_bytes(5, "big") + length.to_bytes(5, "big")
    for off, csize, usize, method in blocks:
        body += off.to_bytes(5, "little") + csize.to_bytes(3, "little") + usize.to_bytes(3, "little") + bytes([method])
    body += b"".join(m.ljust(method_len, b"\0") for m in methods)
    return head + body + index, bytes(ucas)


def container_header(container_id: int, store: list[tuple[int, list[int]]]) -> bytes:
    out = struct.pack("<IIQ", 0x496F436E, 5, container_id)
    ids = struct.pack("<i", len(store)) + b"".join(struct.pack("<Q", pid) for pid, _ in store)
    entries = bytearray()
    tail = bytearray()
    n = len(store)
    for i, (_pid, imported) in enumerate(store):
        here = 16 * i
        if imported:
            off = 16 * n + len(tail) - here
            tail += b"".join(struct.pack("<Q", x) for x in imported)
            entries += struct.pack("<ii", len(imported), off)
        else:
            entries += struct.pack("<ii", 0, 0)
        entries += struct.pack("<ii", 0, 0)
    seg = ids + struct.pack("<i", len(entries) + len(tail)) + bytes(entries) + bytes(tail)
    out += seg + struct.pack("<ii", 0, 0)  # optional segment: no packages
    return out + struct.pack("<i", 0) + struct.pack("<i", 0) + struct.pack("<i", 0) + bytes(16)


def build_game(paks_dir, packages: dict[str, Ue4Package], key=None, compress=True,
               prefix: str = "OakGame/Content/", container: str = "pakchunk0-Windows",
               unversioned: set[str] | None = None) -> None:
    """``packages``: path below ``prefix`` (without the suffix) -> package. Writes global and one container."""
    script = Script()
    chunks = []
    store = []
    cid = 0x1234ABCD
    for path, b in packages.items():
        data, imported, ubulk = zen_package(b, script, unversioned=bool(unversioned and path in unversioned))
        pid = package_id(b.pkg_name)
        chunks.append(((pid, 0, EXPORT_BUNDLE_DATA), data, prefix + path + ".uasset"))
        if ubulk:
            chunks.append(((pid, 0, BULK_DATA), ubulk, prefix + path + ".ubulk"))
        store.append((pid, [package_id(n) for n in imported]))
    chunks.append(((cid, 0, CONTAINER_HEADER), container_header(cid, store), None))
    utoc, ucas = build_utoc(chunks, cid, key=key, compress=compress)
    (paks_dir / f"{container}.utoc").write_bytes(utoc)
    (paks_dir / f"{container}.ucas").write_bytes(ucas)
    gutoc, gucas = build_utoc([((0, 0, SCRIPT_OBJECTS), script.chunk(), None)], 0x1, compress=False, indexed=False)
    (paks_dir / "global.utoc").write_bytes(gutoc)
    (paks_dir / "global.ucas").write_bytes(gucas)
