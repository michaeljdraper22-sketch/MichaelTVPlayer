"""Persistent settings storage (saved as JSON under %APPDATA%/MichaelTVPlayer)."""

import json
import os
import uuid
from pathlib import Path

APP_NAME = "MichaelTVPlayer"

DEFAULTS = {
    "server_url": "",
    "username": "",
    "password": "",
    "volume": 100,
    "timeshift": True,
    "network_caching": 1500,      # ms, 0..50000
    "theme": "dark",
    "enabled_countries": [],      # selected country/group tokens (Live TV)
    "countries_configured": False,
    "vod_enabled_countries": [],  # Movies country/group filter
    "vod_countries_configured": False,
    "series_enabled_countries": [],   # Series country/group filter
    "series_countries_configured": False,
    "dvr_enabled": False,         # user's last DVR preference (never auto-start)
    "dvr_max_minutes": 30,        # rolling DVR buffer window
    "record_folder": "",          # where permanent recordings are saved
    "chase_delay": 15,            # seconds behind live in DVR/chase mode
    "favorites": [],              # list of "playable" dicts
    "recents": [],                # list of "playable" dicts (most-recent first)
    "custom_channels": [],        # user-defined stream URLs
    "last_channel": None,
    "auto_play_last": False,
    "window_geometry": None,      # [x, y, w, h]
    "window_state": None,         # "normal" | "maximized" | "fullscreen"
    "splitter_sizes": [460, 880],
    "last_tab": 0,
    # Which playback-control buttons are shown on the video overlay
    # (Settings ▸ Playback controls…).
    "control_buttons": {
        "back60": True, "back10": True, "play": True, "fwd10": True,
        "begin": True, "live": True, "dvr": True, "rec": True,
        "cc": True, "scale": True, "speed": True, "mute": True,
        "volume": True, "timebar": True,
    },
    "scale_mode": "fit",          # "fit" | "stretch" | "crop"
}

BUTTON_KEYS = (
    "back60", "back10", "play", "fwd10", "begin", "live", "dvr", "rec",
    "cc", "scale", "speed", "mute", "volume", "timebar",
)


def _data_dir() -> Path:
    base = os.environ.get("APPDATA") if os.name == "nt" else None
    base = base or os.path.expanduser("~")
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


class Config:
    def __init__(self, data: dict, path: Path):
        self.data = data
        self.path = path

    @classmethod
    def load(cls) -> "Config":
        path = _data_dir() / "settings.json"
        data = dict(DEFAULTS)
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data.update(loaded)
            except Exception:
                pass
        return cls(data, path)

    def save(self) -> None:
        try:
            self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ---- account ----
    @property
    def server_url(self) -> str:
        return self.data.get("server_url", "")

    @property
    def username(self) -> str:
        return self.data.get("username", "")

    @property
    def password(self) -> str:
        return self.data.get("password", "")

    def has_account(self) -> bool:
        return bool(self.server_url and self.username and self.password)

    def normalized_server(self) -> str:
        s = self.server_url.strip()
        if not s:
            return ""
        if not s.startswith(("http://", "https://")):
            s = "http://" + s
        return s.rstrip("/")

    # ---- player prefs ----
    @property
    def volume(self) -> int:
        return int(self.data.get("volume", 100))

    @volume.setter
    def volume(self, value: int) -> None:
        self.data["volume"] = int(value)

    @property
    def timeshift(self) -> bool:
        return bool(self.data.get("timeshift", True))

    @timeshift.setter
    def timeshift(self, value: bool) -> None:
        self.data["timeshift"] = bool(value)

    # ---- collections ----
    @property
    def favorites(self) -> list:
        return self.data.get("favorites", [])

    @property
    def recents(self) -> list:
        return self.data.get("recents", [])

    @property
    def custom_channels(self) -> list:
        return self.data.get("custom_channels", [])

    def is_favorite(self, fav_key: str) -> bool:
        return any(f.get("fav_key") == fav_key for f in self.favorites)

    def toggle_favorite(self, playable: dict) -> bool:
        """Add or remove a playable; returns the new is-favorite state."""
        favs = self.data.setdefault("favorites", [])
        key = playable.get("fav_key")
        for i, existing in enumerate(favs):
            if existing.get("fav_key") == key:
                del favs[i]
                self.save()
                return False
        favs.append(playable)
        self.save()
        return True

    def add_recent(self, playable: dict) -> None:
        recs = self.data.setdefault("recents", [])
        key = playable.get("fav_key")
        recs[:] = [r for r in recs if r.get("fav_key") != key]
        recs.insert(0, playable)
        del recs[20:]
        self.save()

    def add_custom_channel(self, name: str, url: str, icon: str = "") -> dict:
        chans = self.data.setdefault("custom_channels", [])
        item = {
            "fav_key": "custom:" + uuid.uuid4().hex[:8],
            "kind": "custom",
            "title": name or url,
            "url": url,
            "icon": icon,
        }
        chans.append(item)
        self.save()
        return item

    def remove_custom_channel(self, fav_key: str) -> None:
        chans = self.data.setdefault("custom_channels", [])
        chans[:] = [c for c in chans if c.get("fav_key") != fav_key]
        self.save()

    # ---- network / theme / ui prefs ----
    @property
    def network_caching(self) -> int:
        return int(self.data.get("network_caching", 1500))

    @network_caching.setter
    def network_caching(self, value: int) -> None:
        self.data["network_caching"] = max(0, min(50000, int(value)))

    @property
    def theme(self) -> str:
        return self.data.get("theme", "dark")

    @theme.setter
    def theme(self, value: str) -> None:
        self.data["theme"] = value

    @property
    def enabled_countries(self) -> list:
        return list(self.data.get("enabled_countries", []))

    @enabled_countries.setter
    def enabled_countries(self, value) -> None:
        self.data["enabled_countries"] = sorted(set(value or []))

    @property
    def countries_configured(self) -> bool:
        return bool(self.data.get("countries_configured", False))

    @countries_configured.setter
    def countries_configured(self, value: bool) -> None:
        self.data["countries_configured"] = bool(value)

    # Movies / Series country filters (same shape as the Live TV one)
    @property
    def vod_enabled_countries(self) -> list:
        return list(self.data.get("vod_enabled_countries", []))

    @vod_enabled_countries.setter
    def vod_enabled_countries(self, value) -> None:
        self.data["vod_enabled_countries"] = sorted(set(value or []))

    @property
    def vod_countries_configured(self) -> bool:
        return bool(self.data.get("vod_countries_configured", False))

    @vod_countries_configured.setter
    def vod_countries_configured(self, value: bool) -> None:
        self.data["vod_countries_configured"] = bool(value)

    @property
    def series_enabled_countries(self) -> list:
        return list(self.data.get("series_enabled_countries", []))

    @series_enabled_countries.setter
    def series_enabled_countries(self, value) -> None:
        self.data["series_enabled_countries"] = sorted(set(value or []))

    @property
    def series_countries_configured(self) -> bool:
        return bool(self.data.get("series_countries_configured", False))

    @series_countries_configured.setter
    def series_countries_configured(self, value: bool) -> None:
        self.data["series_countries_configured"] = bool(value)


    @property
    def window_geometry(self):
        return self.data.get("window_geometry")

    @window_geometry.setter
    def window_geometry(self, value) -> None:
        self.data["window_geometry"] = value

    @property
    def window_state(self) -> str:
        return self.data.get("window_state", "normal")

    @window_state.setter
    def window_state(self, value: str) -> None:
        self.data["window_state"] = value

    @property
    def splitter_sizes(self):
        return self.data.get("splitter_sizes", [460, 880])

    @splitter_sizes.setter
    def splitter_sizes(self, value) -> None:
        self.data["splitter_sizes"] = list(value)

    @property
    def last_tab(self) -> int:
        return int(self.data.get("last_tab", 0))

    @last_tab.setter
    def last_tab(self, value: int) -> None:
        self.data["last_tab"] = int(value)

    @property
    def control_buttons(self) -> dict:
        stored = self.data.get("control_buttons") or {}
        merged = dict(DEFAULTS["control_buttons"])
        for key in BUTTON_KEYS:
            if key in stored:
                merged[key] = bool(stored[key])
        return merged

    @control_buttons.setter
    def control_buttons(self, value) -> None:
        clean = dict(DEFAULTS["control_buttons"])
        for key in BUTTON_KEYS:
            if key in (value or {}):
                clean[key] = bool(value[key])
        self.data["control_buttons"] = clean

    @property
    def scale_mode(self) -> str:
        mode = str(self.data.get("scale_mode", "fit"))
        return mode if mode in ("fit", "stretch", "crop") else "fit"

    @scale_mode.setter
    def scale_mode(self, value: str) -> None:
        self.data["scale_mode"] = value if value in ("fit", "stretch", "crop") \
            else "fit"


    @property
    def dvr_enabled(self) -> bool:
        return bool(self.data.get("dvr_enabled", False))

    @dvr_enabled.setter
    def dvr_enabled(self, value: bool) -> None:
        self.data["dvr_enabled"] = bool(value)

    @property
    def dvr_max_minutes(self) -> int:
        return int(self.data.get("dvr_max_minutes", 30))

    @dvr_max_minutes.setter
    def dvr_max_minutes(self, value: int) -> None:
        self.data["dvr_max_minutes"] = max(1, int(value))

    @property
    def record_folder(self) -> str:
        return self.data.get("record_folder", "")

    @record_folder.setter
    def record_folder(self, value: str) -> None:
        self.data["record_folder"] = value or ""

    @property
    def chase_delay(self) -> int:
        return int(self.data.get("chase_delay", 15))

    @chase_delay.setter
    def chase_delay(self, value: int) -> None:
        self.data["chase_delay"] = max(5, min(120, int(value)))
