"""Persistent settings for the Android app (JSON in App.user_data_dir).

Stored keys (settings.json):

    addons               list of Stremio addon base URLs, priority order
    resolution_pref      "match" | "auto" | "2160" | "1440" | "1080" |
                         "720" | "480"      (same value space as the
                         desktop app's stremio_resolution_pref)
    size_demote_gb       streams larger than this are demoted in ranking
                         (0 = never demote; 25 = desktop default)
    xtream_server        Xtream panel base URL ("" = Live TV disabled)
    xtream_username
    xtream_password

This class ALSO doubles as the config object handed to the shared core
(michaeltv_core.stremio) — that code touches exactly three things:

    config.data["stremio_addons"]         (stremio._addon_bases, :952)
    config.stremio_resolution_pref        (stremio.rank_streams, :1140)
    config.stremio_size_demote_gb         (stremio.rank_streams, :1140)

so ``data`` is a synthesized facade view, and the two properties below
mirror the desktop src/config.py validation exactly.
"""

import json
import os

RES_PREFS = ("match", "auto", "2160", "1440", "1080", "720", "480")

DEFAULTS = {
    "addons": ["https://torrentio.strem.fun"],
    "resolution_pref": "1080",       # src/config.py DEFAULTS
    "size_demote_gb": 25,            # src/config.py DEFAULTS
    "xtream_server": "",
    "xtream_username": "",
    "xtream_password": "",
}

FILENAME = "settings.json"


class Settings:
    def __init__(self, data, path):
        self._data = data            # the persisted dict
        self.path = path

    # ---- persistence ----

    @classmethod
    def load(cls, user_data_dir):
        path = os.path.join(user_data_dir, FILENAME)
        data = dict(DEFAULTS)
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    stored = json.load(fh)
                if isinstance(stored, dict):
                    for key in DEFAULTS:
                        if key in stored:
                            data[key] = stored[key]
            except (OSError, ValueError):
                pass                 # corrupt file -> defaults
        return cls(data, path)

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2)
        except OSError:
            pass

    def replace(self, data):
        """Swap in a whole new settings dict (Settings screen Save)."""
        merged = dict(DEFAULTS)
        for key in DEFAULTS:
            if key in (data or {}):
                merged[key] = data[key]
        self._data = merged
        self.save()

    # ---- plain accessors (app side) ----

    @property
    def addons(self):
        """Cleaned addon base list (the core's own cleanup, mirrored)."""
        out = []
        for base in (self._data.get("addons") or []):
            base = str(base or "").strip().rstrip("/")
            if base.endswith("/manifest.json"):
                base = base[: -len("/manifest.json")].rstrip("/")
            if base.startswith(("http://", "https://")) and base not in out:
                out.append(base)
        return out or list(DEFAULTS["addons"])

    @property
    def resolution_pref(self):
        val = str(self._data.get("resolution_pref", "") or "").strip()
        return val if val in RES_PREFS else "1080"

    @property
    def size_demote_gb(self):
        try:
            val = int(self._data.get("size_demote_gb", 25))
        except (TypeError, ValueError):
            val = 25
        return max(0, min(500, val))

    def xtream_configured(self):
        return bool(self.xtream_server.strip() and
                    self.xtream_username.strip() and
                    self.xtream_password.strip())

    @property
    def xtream_server(self):
        return str(self._data.get("xtream_server", "") or "")

    @property
    def xtream_username(self):
        return str(self._data.get("xtream_username", "") or "")

    @property
    def xtream_password(self):
        return str(self._data.get("xtream_password", "") or "")

    # ---- stremio.py config facade (see module docstring) ----

    @property
    def data(self):
        # consumed by michaeltv_core.stremio._addon_bases via
        # ``config.data.get("stremio_addons")``
        return {"stremio_addons": self.addons}

    @property
    def stremio_resolution_pref(self):
        return self.resolution_pref

    @property
    def stremio_size_demote_gb(self):
        return self.size_demote_gb
