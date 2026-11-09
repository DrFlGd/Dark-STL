"""id Tech decl files (``generated/decls/...``): text in id's brace syntax, read into nested dicts.

``{ inherit = "parent"; edit = { RenderLayers = { item[0] = { parms = { albedo = { filePath = "x.tga"; } } } } } }``.
Keys keep their case; ``item[n]`` keys stay as they are written. :func:`merge` lays a decl over the one it inherits.
"""

from __future__ import annotations

import re

from ....errors import FormatError

_TOKEN = re.compile(r'''\s*(?:(//[^\n]*)|(/\*.*?\*/)|("(?:[^"\\]|\\.)*")|([{}=;,])|([^\s{}=;,"]+))''', re.S)


def _tokens(text: str):
    pos, n = 0, len(text)
    while pos < n:
        m = _TOKEN.match(text, pos)
        if m is None:
            if text[pos:].strip():
                raise FormatError(f"decl: can't read near {text[pos:pos + 30]!r}")
            return
        pos = m.end()
        if m.group(1) or m.group(2):
            continue
        if m.group(3) is not None:
            yield ("str", m.group(3)[1:-1].replace('\\"', '"'))
        elif m.group(4):
            yield ("p", m.group(4))
        elif m.group(5):
            yield ("w", m.group(5))


def _value(word: str):
    low = word.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(word)
    except ValueError:
        pass
    try:
        return float(word.rstrip("f"))
    except ValueError:
        return word


def parse(text: str) -> dict:
    toks = list(_tokens(text))
    i = 0

    def obj() -> dict:
        nonlocal i
        out: dict = {}
        while i < len(toks):
            kind, val = toks[i]
            if kind == "p" and val == "}":
                i += 1
                return out
            if kind == "p" and val in ";,":
                i += 1
                continue
            key = val
            i += 1
            if i < len(toks) and toks[i] == ("p", "="):
                i += 1
            if i >= len(toks):
                out[key] = None
                break
            kind, val = toks[i]
            if kind == "p" and val == "{":
                i += 1
                out[key] = obj()
            elif kind == "p":
                out[key] = None
                continue
            else:
                out[key] = val if kind == "str" else _value(val)
                i += 1
        return out

    # the decl's body is its first brace block (some start with a type and name: "material2 x {")
    while i < len(toks) and toks[i] != ("p", "{"):
        i += 1
    if i >= len(toks):
        raise FormatError("decl: no { ... } block")
    i += 1
    return obj()


def merge(parent: dict, child: dict) -> dict:
    """``child`` laid over ``parent``: objects merge key by key (keys compared without case), an empty
    ``filePath`` keeps the parent's."""
    out = dict(parent)
    lower = {k.lower(): k for k in out}
    for k, v in child.items():
        old_key = lower.get(k.lower())
        if old_key is not None and k == "filePath" and v == "":
            continue
        if old_key is not None and isinstance(out[old_key], dict) and isinstance(v, dict):
            out[old_key] = merge(out[old_key], v)
        else:
            if old_key is not None and old_key != k:
                del out[old_key]
            out[k] = v
            lower[k.lower()] = k
    return out


def get(d: dict | None, *keys: str):
    """``d[k1][k2]...`` with keys compared without case; None when a key is missing."""
    for key in keys:
        if not isinstance(d, dict):
            return None
        low = key.lower()
        d = next((v for k, v in d.items() if k.lower() == low), None)
    return d
