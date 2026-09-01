"""Persistent settings storage (saved as JSON under %APPDATA%/MichaelTVPlayer)."""

import json
import os
import uuid
from pathlib import Path

APP_NAME = "MichaelTVPlayer"
# App version — bumped per release; the Settings ▸ Check for updates action
# compares it against the latest GitHub release tag (see src/updater.py).
APP_VERSION = "1.5.7"

# Subtitle appearance. Values map 1:1 onto a libvlc option (see
# player.subtitle_instance_args) so an untouched config emits NO extra VLC
# arguments at all — except prefer_bar, which steers the APP-rendered
# overlay's placement (VLC-rendered tracks can't be moved at runtime).
SUBTITLE_KEYS = (
    "delay_ms", "font", "size", "pos_pct", "text_color",
    "bg_enabled", "bg_color", "bg_opacity",
    "outline_enabled", "outline_color", "outline_thickness",
    "prefer_bar",
)
SUBTITLE_DEFAULTS = {
    "delay_ms": 0,            # + shows subtitles LATER, − earlier (applied live)
    "font": "",               # "" = VLC's default font
    "size": 40,               # px at 1080p; 0 = auto (scales with video)
    "pos_pct": 0,             # −100..100, 0 = default bottom placement
    "text_color": "#FFFFFF",
    "bg_enabled": False,      # backing box behind the text
    "bg_color": "#000000",
    "bg_opacity": 50,         # 0..100 %, only meaningful when bg_enabled
    "outline_enabled": True,  # VLC's default look has a black outline
    "outline_color": "#000000",
    "outline_thickness": 4,   # VLC units, default 4 ("normal")
    "prefer_bar": True,       # windowed letterbox: park subs in the black bar
}

# Profanity filter. Off by default (opt-in). Words are stored as a flat
# [word, level] list so removals of defaults persist; level is one of
# exact / partial / whole (see src.profanity).
PROFANITY_DEFAULTS = {
    "enabled": False,
    "words": [],                # [] = the curated DEFAULT_WORDS list
    "pad_before_ms": 120,       # mute starts N ms before the word
    "pad_after_ms": 250,        # mute ends N ms after the word
    "sync_ms": 0,               # + mute later, − mute earlier (track drift)
    "lead_ms": 1500,            # captions lag speech: mute EARLIER by this
    "whole_cue": False,         # True = mute the whole line, not just the word
}

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
    "dvr_max_minutes": 30,        # rolling DVR buffer window
    "record_folder": "",          # where permanent recordings are saved
    "download_folder": "",        # where catch-up window / VOD downloads go
    # Seconds behind live the always-on DVR chase keeps live TV. 5 is the
    # floor: the caption cushion (profanity filter / app-rendered captions)
    # cannot mute/render in time with less.
    "chase_delay": 5,
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
        "begin": True, "live": True, "rec": True,
        "cc": True, "audio": True, "scale": True, "speed": True,
        "mute": True, "volume": True, "timebar": True,
        "autoplay": True, "playnext": True,
    },
    "autoplay_next": True,          # the autoplay toggle's state (series/catch-up)
    "scale_mode": "fit",          # "fit" | "stretch" | "crop"
    "subtitle_appearance": dict(SUBTITLE_DEFAULTS),
    "profanity": dict(PROFANITY_DEFAULTS),
    # Opt-in diagnostics ("Help improve MichaelTV", Settings menu). Off
    # until the user turns it on; see src/diagnostics.py for what is sent.
    "telemetry_enabled": False,
    "telemetry_token": "",         # fine-grained GitHub PAT (issues:write)
    "telemetry_id": "",            # random install id, set on first send
    "telemetry_last_sent": 0.0,    # epoch of the last uploaded report
    "telemetry_repo": "",          # "" = diagnostics.REPO
    # Stremio handoff (Settings > Stremio handoff…): addon URLs queried
    # for next-episode streams (Torrentio-shaped /stream/series/… API),
    # the local Stremio streaming server that turns torrents into HTTP,
    # and the resolution preferred when picking a stream.
    "stremio_addons": ["https://torrentio.strem.fun"],
    "stremio_server": "http://127.0.0.1:11470",
    "stremio_prefer_resolution": 1080,
    "stremio_watch_downloads": True,   # auto-play Stremio's playlist.m3u
                                       # the moment it lands in Downloads
}

BUTTON_KEYS = (
    "back60", "back10", "play", "fwd10", "begin", "live", "rec",
    "cc", "audio", "scale", "speed", "mute", "volume", "timebar",
    "autoplay", "playnext",
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
        # one-time migration: the old default size 0 (VLC "auto") became a
        # concrete number, so the setting starts somewhere tunable — 0/Auto
        # can still be chosen explicitly afterwards (marker stops this from
        # overriding a deliberate Auto on later launches). Copy first:
        # DEFAULTS' inner dicts are shared objects.
        sub = data.get("subtitle_appearance")
        if isinstance(sub, dict):
            sub = dict(sub)
            if not data.get("_sub_size_migrated") and sub.get("size", 0) == 0:
                sub["size"] = SUBTITLE_DEFAULTS["size"]
            data["subtitle_appearance"] = sub
        data["_sub_size_migrated"] = True
        # one-time migration: live TV is ALWAYS in DVR chase mode now, and
        # the user approved trading ~5 s of latency for unified captions —
        # configs still on the old 15 s default come down to 5 (an explicit
        # other value is kept; the floor is 5 either way).
        if not data.get("_chase_delay_migrated"):
            if int(data.get("chase_delay", 5) or 5) == 15:
                data["chase_delay"] = 5
            data["_chase_delay_migrated"] = True
        # one-time migration: the Catch-Up tab was inserted after Series,
        # which shifts the stored Favorites/Custom tab indices by one.
        if not data.get("_catchup_tab_migrated"):
            lt = data.get("last_tab")
            if isinstance(lt, int) and lt >= 3:
                data["last_tab"] = lt + 1
            data["_catchup_tab_migrated"] = True
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
    def autoplay_next(self) -> bool:
        return bool(self.data.get("autoplay_next", True))

    @autoplay_next.setter
    def autoplay_next(self, value: bool) -> None:
        self.data["autoplay_next"] = bool(value)
        self.save()

    @property
    def scale_mode(self) -> str:
        mode = str(self.data.get("scale_mode", "fit"))
        return mode if mode in ("fit", "stretch", "crop") else "fit"

    @scale_mode.setter
    def scale_mode(self, value: str) -> None:
        self.data["scale_mode"] = value if value in ("fit", "stretch", "crop") \
            else "fit"

    @property
    def subtitle_appearance(self) -> dict:
        stored = self.data.get("subtitle_appearance") or {}
        merged = dict(SUBTITLE_DEFAULTS)
        for key in SUBTITLE_KEYS:
            if key in stored:
                merged[key] = stored[key]
        return merged

    @subtitle_appearance.setter
    def subtitle_appearance(self, value) -> None:
        clean = dict(SUBTITLE_DEFAULTS)
        for key in SUBTITLE_KEYS:
            if key in (value or {}):
                clean[key] = value[key]
        self.data["subtitle_appearance"] = clean

    @property
    def profanity(self) -> dict:
        stored = self.data.get("profanity") or {}
        merged = dict(PROFANITY_DEFAULTS)
        for key in ("enabled", "pad_before_ms", "pad_after_ms", "sync_ms",
                    "lead_ms", "whole_cue"):
            if key in stored:
                merged[key] = stored[key]
        words = stored.get("words")
        if isinstance(words, list) and words:
            clean = []
            for w in words:
                if isinstance(w, (list, tuple)) and len(w) == 2 \
                        and str(w[0]).strip() \
                        and w[1] in ("exact", "partial", "whole"):
                    clean.append([str(w[0]).strip().lower(), w[1]])
            merged["words"] = clean
        return merged

    @profanity.setter
    def profanity(self, value) -> None:
        clean = dict(PROFANITY_DEFAULTS)
        v = value or {}
        clean["enabled"] = bool(v.get("enabled", False))
        clean["pad_before_ms"] = max(0, min(5000,
                                            int(v.get("pad_before_ms", 120))))
        clean["pad_after_ms"] = max(0, min(5000,
                                           int(v.get("pad_after_ms", 250))))
        clean["sync_ms"] = max(-10000, min(10000,
                                           int(v.get("sync_ms", 0))))
        clean["lead_ms"] = max(0, min(10000,
                                      int(v.get("lead_ms", 1500))))
        clean["whole_cue"] = bool(v.get("whole_cue", False))
        words = v.get("words")
        clean["words"] = []
        if isinstance(words, list):
            for w in words:
                if isinstance(w, (list, tuple)) and len(w) == 2 \
                        and str(w[0]).strip() \
                        and w[1] in ("exact", "partial", "whole"):
                    clean["words"].append([str(w[0]).strip().lower(), w[1]])
        self.data["profanity"] = clean


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
    def download_folder(self) -> str:
        return self.data.get("download_folder", "")

    @download_folder.setter
    def download_folder(self, value: str) -> None:
        self.data["download_folder"] = value or ""

    @property
    def chase_delay(self) -> int:
        return int(self.data.get("chase_delay", 5))

    @chase_delay.setter
    def chase_delay(self, value: int) -> None:
        self.data["chase_delay"] = max(5, min(120, int(value)))

    # ---- opt-in diagnostics (Settings ▸ "Help improve MichaelTV…") ----

    @property
    def telemetry_enabled(self) -> bool:
        return bool(self.data.get("telemetry_enabled", False))

    @telemetry_enabled.setter
    def telemetry_enabled(self, value: bool) -> None:
        self.data["telemetry_enabled"] = bool(value)

    @property
    def telemetry_token(self) -> str:
        return self.data.get("telemetry_token", "") or ""

    @telemetry_token.setter
    def telemetry_token(self, value: str) -> None:
        self.data["telemetry_token"] = (value or "").strip()

    @property
    def telemetry_id(self) -> str:
        """Random install id — generated once, then sticky (correlates
        reports from the same machine without sending anything personal)."""
        tid = self.data.get("telemetry_id", "") or ""
        if not tid:
            tid = uuid.uuid4().hex[:12]
            self.data["telemetry_id"] = tid
        return tid

    @property
    def telemetry_last_sent(self) -> float:
        try:
            return float(self.data.get("telemetry_last_sent", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @telemetry_last_sent.setter
    def telemetry_last_sent(self, value: float) -> None:
        try:
            self.data["telemetry_last_sent"] = float(value)
        except (TypeError, ValueError):
            pass

    # ---- Stremio handoff (Settings > Stremio handoff…) ----

    @property
    def stremio_addons(self) -> list:
        raw = self.data.get("stremio_addons") \
            or DEFAULTS["stremio_addons"]
        out = []
        if isinstance(raw, list):
            for base in raw:
                base = str(base or "").strip().rstrip("/")
                if base.startswith(("http://", "https://")):
                    out.append(base)
        return out or list(DEFAULTS["stremio_addons"])

    @stremio_addons.setter
    def stremio_addons(self, value) -> None:
        clean = []
        for base in (value or []):
            base = str(base or "").strip().rstrip("/")
            if base.endswith("/manifest.json"):
                base = base[: -len("/manifest.json")].rstrip("/")
            if base.startswith(("http://", "https://")) and base not in clean:
                clean.append(base)
        self.data["stremio_addons"] = clean \
            or list(DEFAULTS["stremio_addons"])

    @property
    def stremio_server(self) -> str:
        base = str(self.data.get("stremio_server", "") or "").strip().rstrip("/")
        return base if base.startswith(("http://", "https://")) \
            else DEFAULTS["stremio_server"]

    @stremio_server.setter
    def stremio_server(self, value: str) -> None:
        base = str(value or "").strip().rstrip("/")
        self.data["stremio_server"] = base \
            if base.startswith(("http://", "https://")) \
            else DEFAULTS["stremio_server"]

    @property
    def stremio_prefer_resolution(self) -> int:
        try:
            val = int(self.data.get("stremio_prefer_resolution", 1080))
        except (TypeError, ValueError):
            val = 1080
        return val if val in (0, 480, 720, 1080, 1440, 2160) else 1080

    @stremio_prefer_resolution.setter
    def stremio_prefer_resolution(self, value: int) -> None:
        try:
            val = int(value)
        except (TypeError, ValueError):
            val = 1080
        self.data["stremio_prefer_resolution"] = \
            val if val in (0, 480, 720, 1080, 1440, 2160) else 1080

    @property
    def stremio_watch_downloads(self) -> bool:
        return bool(self.data.get("stremio_watch_downloads", True))

    @stremio_watch_downloads.setter
    def stremio_watch_downloads(self, value: bool) -> None:
        self.data["stremio_watch_downloads"] = bool(value)
