"""What Dark STL's Finish and Cut tabs and its painted 3MF export left behind in saved settings.

They moved to Model Painter (github.com/DrFlGd/Model-Painter) in 0.55.0: Dark STL hands a model over with
Export › Export for Model Painter. A settings file, a saved variation or a bulk "Enhance with the current
settings" preset written before then can still hold their settings. Everything that reads one goes through
these, so those keys are ignored when read and left out when it's written again.
"""

from __future__ import annotations

# keys of Generate's settings, a saved variation's record and a bulk preset that only Finish, Cut or the
# painted 3MF read
SETTINGS = frozenset({"finish", "finished", "watertight", "cut", "cuts", "pieces", "paint", "lay_flat"})
# the page's preferences (Settings file › prefs) that only Finish, Cut or the painted 3MF kept
PREFS = frozenset({"finishOpts", "cutOpts", "cutPlans", "paintOpts", "paintSources", "paintPreview", "paintShow"})
# explanations folded on the model page's tabs that are gone
TIPS = frozenset({"finish", "cut", "paint"})
# what the Export tab can no longer write
FORMATS = frozenset({".3mf"})
# the key the page keeps its preferences under (web/ui/state.js)
PAGE = "ds-ui"


def settings(data) -> dict:
    """Generate's settings (or a bulk preset) without what Finish, Cut and the painted 3MF kept in them."""
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k not in SETTINGS}


def variation(data) -> dict:
    """A saved variation's record without what Finish, Cut and the painted 3MF kept, in it or its settings."""
    if not isinstance(data, dict):
        return {}
    out = settings(data)
    if isinstance(out.get("settings"), dict):
        out["settings"] = settings(out["settings"])
    return out


def prefs(data) -> dict:
    """The app's preferences (the settings file's ``prefs``: the page keeps its own under ``ds-ui``) without the
    retired tabs' own settings, a painted 3MF chosen as the export format, or the retired tabs' folded
    explanations."""
    if not isinstance(data, dict):
        return {}
    out = _page_prefs(data)
    if isinstance(out.get(PAGE), dict):
        out[PAGE] = _page_prefs(out[PAGE])
    return out


def _page_prefs(data: dict) -> dict:
    out = {k: v for k, v in data.items() if k not in PREFS}
    export = out.get("exportOpts")
    if isinstance(export, dict):
        export = {k: v for k, v in export.items() if k != "paint"}
        if str(export.get("format") or "").lower() in FORMATS:
            export.pop("format")  # back to the default (STL)
        out["exportOpts"] = export
    tips = out.get("tipsOpen")
    if isinstance(tips, dict):
        out["tipsOpen"] = {k: v for k, v in tips.items() if k not in TIPS}
    return out


__all__ = ["FORMATS", "PAGE", "PREFS", "SETTINGS", "TIPS", "prefs", "settings", "variation"]
