"""Xtream Codes API client for IPTV providers (Live / VOD / Series / EPG)."""

import base64
import binascii
import hashlib
import logging
import math
import threading
import time
import urllib.parse

import requests

from .models import EpgEntry, UserInfo

log = logging.getLogger("mtp.xtream")

# ---- provider snapshot (feeds the diagnostics uploads) ----
# Everything the automatic bug reports need about the PROVIDER side of a
# failure: which API calls happened, what they returned (item counts, not
# content), and what the account looked like. No credentials ever enter
# this dict — the server is stored as a salted-free sha256 prefix and the
# account fields are the provider's own status metadata.
PROVIDER_STATS = {
    "server_hash": "",   # sha256(base URL)[:12] — identifies the panel
    "account": {},       # status / exp_date / active_cons / max_connections / is_trial
    "actions": {},       # action -> {"n": calls, "items_last": len, "ts": epoch}
    "errors": [],        # last 20 {"action","error","ts"} — reasons, no creds
}
_stats_lock = threading.Lock()


def _record_action(action: str, data) -> None:
    try:
        with _stats_lock:
            entry = PROVIDER_STATS["actions"].setdefault(
                action, {"n": 0, "items_last": -1, "ts": 0})
            entry["n"] += 1
            entry["ts"] = time.time()
            if isinstance(data, list):
                entry["items_last"] = len(data)
                if not data:
                    log.warning("xtream %s: provider returned an EMPTY list",
                                action)
            elif isinstance(data, dict):
                entry["items_last"] = len(data)
    except Exception:
        pass


def _record_error(action: str, message: str) -> None:
    try:
        with _stats_lock:
            errs = PROVIDER_STATS["errors"]
            errs.append({"action": action or "auth",
                         "error": str(message)[:200],
                         "ts": time.time()})
            del errs[:-20]
    except Exception:
        pass


class XtreamError(Exception):
    pass


def decode_epg_text(s: str) -> str:
    """Xtream EPG titles/descriptions arrive base64-encoded on most panels
    (verified on this account: get_simple_data_table titles decode to plain
    text). Some panels send plain text instead — keep the original when the
    base64 decode is invalid or yields non-printable garbage."""
    if not s:
        return ""
    try:
        raw = base64.b64decode(s.strip(), validate=True)
        text = raw.decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return s
    if not text:
        return s
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\n\r\t")
    return text if printable / len(text) > 0.8 else s


class XtreamClient:
    def __init__(self, server: str, username: str, password: str, timeout: int = 20):
        if not server:
            raise XtreamError("No server URL configured")
        self.base = server.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "MichaelTVPlayer/1.0"})
        try:
            with _stats_lock:
                PROVIDER_STATS["server_hash"] = hashlib.sha256(
                    self.base.encode("utf-8", "replace")).hexdigest()[:12]
            from . import feedback
            feedback.set_server(self.base)
        except Exception:
            pass

    # ---- low level ----
    def _api(self, action=None, timeout=None, _retries=1, **extra):
        params = {"username": self.username, "password": self.password}
        if action:
            params["action"] = action
        for k, v in extra.items():
            if v is not None:
                params[k] = v
        url = f"{self.base}/player_api.php"
        try:
            resp = self.session.get(url, params=params,
                                    timeout=timeout or self.timeout)
        except requests.RequestException as exc:
            if _retries > 0:
                # transient network blip — one quiet retry before declaring
                # the panel unreachable (auto-reported either way on fail)
                time.sleep(1.0)
                return self._api(action, timeout, _retries - 1, **extra)
            _record_error(action, "connection: %r" % (exc,))
            log.error("xtream %s: connection failed: %r",
                      action or "auth", exc)
            raise XtreamError(f"Connection failed: {exc}") from exc
        if resp.status_code != 200:
            _record_error(action, "HTTP %d" % resp.status_code)
            log.error("xtream %s: server returned HTTP %d",
                      action or "auth", resp.status_code)
            raise XtreamError(f"Server returned HTTP {resp.status_code}")
        try:
            self._record_panel_hints(resp)
            data = resp.json()
        except ValueError as exc:
            body = (resp.text or "")[:120]
            _record_error(action, "non-JSON response: %s" % body)
            log.error("xtream %s: server did not return valid JSON "
                      "(body starts: %r)", action or "auth", body)
            raise XtreamError("Server did not return valid JSON") from exc
        if isinstance(data, dict) and data.get("user_info", {}).get("auth") == 0:
            _record_error(action, "auth rejected")
            log.error("xtream: invalid username or password")
            raise XtreamError("Invalid username or password")
        if isinstance(data, dict) and "user_info" not in data \
                and "error" in data:
            # some panels answer errors as 200 + {"error": "..."} instead of
            # a real payload — surface it instead of acting on garbage
            msg = str(data.get("error"))[:200]
            _record_error(action, "panel error payload: %s" % msg)
            log.error("xtream %s: panel returned an error payload: %s",
                      action or "auth", msg)
            raise XtreamError(f"Panel error: {msg}")
        _record_action(action or "auth", data)
        return data

    def _record_panel_hints(self, resp) -> None:
        """Fingerprint the panel once (server header etc.) for diagnostics —
        different Xtream/XUI forks behave differently, and knowing which
        family a user is on is half the diagnosis."""
        try:
            with _stats_lock:
                hints = PROVIDER_STATS.setdefault("panel_hints", {})
                if not hints:
                    for h in ("server", "x-powered-by"):
                        v = resp.headers.get(h)
                        if v:
                            hints[h] = v[:60]
        except Exception:
            pass

    # ---- account ----
    def authenticate(self) -> UserInfo:
        data = self._api()
        ui = data.get("user_info", {}) if isinstance(data, dict) else {}
        try:
            with _stats_lock:
                PROVIDER_STATS["account"] = {
                    k: ui.get(k, "")
                    for k in ("status", "exp_date", "is_trial",
                              "active_cons", "max_connections",
                              "created_at")
                }
        except Exception:
            pass
        return UserInfo(
            username=ui.get("username", ""),
            status=ui.get("status", ""),
            exp_date=ui.get("exp_date", ""),
            is_trial=ui.get("is_trial", ""),
            active_cons=ui.get("active_cons", ""),
            max_connections=ui.get("max_connections", ""),
            created_at=ui.get("created_at", ""),
        )

    # ---- live ----
    def live_categories(self):
        return self._api("get_live_categories") or []

    def live_streams(self, category_id=None):
        return self._api("get_live_streams", category_id=category_id) or []

    # ---- vod (movies) ----
    def vod_categories(self):
        return self._api("get_vod_categories") or []

    def vod_streams(self, category_id=None, timeout=None):
        # No category = the provider's ENTIRE movie library, which can be a
        # multi-MB JSON that takes far longer than the default timeout.
        try:
            data = self._api("get_vod_streams", category_id=category_id,
                             timeout=timeout) or []
        except XtreamError as exc:
            _record_error("get_vod_streams", "falling back to m3u (%s)"
                          % exc)
            data = []
        if not data and not category_id:
            # Some panels omit/break the JSON VOD endpoint but still serve
            # the classic playlist export — try it once per session before
            # showing the user an empty Movies tab (the brother-machine bug
            # class). Items carry the same keys the JSON form uses.
            return self._m3u_fallback("vod")
        return data

    def vod_info(self, vod_id):
        return self._api("get_vod_info", vod_id=vod_id) or {}

    # ---- series ----
    def series_categories(self):
        return self._api("get_series_categories") or []

    def series(self, category_id=None, timeout=None):
        # Same as VOD: the full series list can be huge.
        try:
            data = self._api("get_series", category_id=category_id,
                             timeout=timeout) or []
        except XtreamError as exc:
            _record_error("get_series", "falling back to m3u (%s)" % exc)
            data = []
        if not data and not category_id:
            return self._m3u_fallback("series")
        return data

    def series_info(self, series_id):
        return self._api("get_series_info", series_id=series_id) or {}

    # ---- playlist-export fallback (panels with broken JSON endpoints) ----
    _m3u_tried = set()   # sections already attempted this process

    def _m3u_fallback(self, section):
        """Classic get.php playlist export — the oldest, most universal
        Xtream endpoint there is. Used ONLY when the JSON VOD/series action
        came back empty/failed: panels that skip the JSON endpoints
        usually still serve this. One attempt per section per session."""
        try:
            if section in XtreamClient._m3u_tried:
                return []
            XtreamClient._m3u_tried.add(section)
            r = self.session.get(
                f"{self.base}/get.php",
                params={"username": self.username,
                        "password": self.password,
                        "type": section, "output": "ts"},
                timeout=90)
            if r.status_code != 200:
                _record_error("get.php %s" % section,
                              "HTTP %d" % r.status_code)
                return []
            items = self._parse_m3u(
                r.content.decode("utf-8", "replace"), section)
            if items:
                log.warning("xtream: JSON %s endpoint unusable — recovered "
                            "%d item(s) from get.php playlist export",
                            section, len(items))
                try:
                    from . import feedback
                    feedback.stat("m3u_fallback_used")
                    feedback.crumb("m3u fallback: %d %s items"
                                   % (len(items), section))
                except Exception:
                    pass
                _record_action("get.php_%s_fallback" % section, items)
            return items
        except Exception as exc:  # noqa: BLE001
            _record_error("get.php %s" % section, "%r" % (exc,))
            return []

    @staticmethod
    def _parse_m3u(text, section):
        """Playlist lines -> the same dict keys the JSON actions use."""
        id_key = "stream_id" if section == "vod" else "series_id"
        items, title = [], None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#EXTINF"):
                title = line.split(",", 1)[1].strip() if "," in line else ""
            elif line.startswith(("http://", "https://")) and title:
                # .../movie/u/p/<id>.<ext> — the id rides the FILENAME
                fname = line.rstrip("/").split("/")[-1]
                if "." in fname:
                    item_id, ext = fname.rsplit(".", 1)
                    ext = ext.lower()
                else:
                    item_id, ext = fname, "mp4"
                items.append({id_key: item_id, "name": title,
                              "container_extension": ext,
                              "category_id": ""})
                title = None
        return items

    # ---- epg ----
    def short_epg(self, stream_id, limit: int = 4):
        data = self._api("get_short_epg", stream_id=stream_id, limit=limit) or {}
        return self._epg_entries(data.get("epg_listings") or [])

    def epg_table(self, stream_id):
        """Full EPG data table for one channel — on archive-capable channels
        this covers the whole catch-up window (past programs included),
        unlike short_epg's now/next handful."""
        data = self._api("get_simple_data_table",
                         stream_id=stream_id) or {}
        return self._epg_entries(data.get("epg_listings") or [])

    @staticmethod
    def _epg_entries(listings):
        return [
            EpgEntry(
                title=e.get("title", "") or "",
                description=e.get("description", "") or "",
                start=e.get("start", ""),
                end=e.get("end", ""),
                start_timestamp=e.get("start_timestamp", ""),
                stop_timestamp=e.get("stop_timestamp", ""),
            )
            for e in listings
        ]

    # ---- stream url builders ----
    def live_url(self, stream_id, ext: str = "ts") -> str:
        return f"{self.base}/live/{self.username}/{self.password}/{stream_id}.{ext}"

    # Which catch-up URL form this panel serves. Panels differ: this one
    # answers the modern path with HTTP 513 and only the legacy
    # streaming/timeshift.php works (probed 2026-08-24) — other panels are
    # the reverse. The preference is per-session, remembered in PROVIDER_
    # STATS, and flipped by the catch-up rescue path when a stream will
    # not start at all ("legacy" first because it is the one verified here).
    TIMESHIFT_FORM_DEFAULT = "legacy"

    @property
    def timeshift_form(self) -> str:
        try:
            with _stats_lock:
                return PROVIDER_STATS.get(
                    "timeshift_form", self.TIMESHIFT_FORM_DEFAULT)
        except Exception:
            return self.TIMESHIFT_FORM_DEFAULT

    def flip_timeshift_form(self) -> str:
        """Switch to the other catch-up URL form and report it (called by
        the rescue path after a form's streams repeatedly fail to start)."""
        new = "modern" if self.timeshift_form == "legacy" else "legacy"
        try:
            with _stats_lock:
                PROVIDER_STATS["timeshift_form"] = new
        except Exception:
            pass
        log.warning("xtream: switching catch-up URL form to '%s' "
                    "(panel serves the other path)", new)
        return new

    def timeshift_url(self, stream_id, utc_start: int, duration_min: int,
                      form: str = None) -> str:
        """Catch-up (archive) stream: the provider serves the recorded
        broadcast from ``utc_start`` (epoch seconds) for ``duration_min``
        minutes.  Times are UTC, formatted YYYY-MM-DD:HH-MM.

        Two URL families exist (legacy query-string script vs modern path
        form) and panels support only one — see ``timeshift_form``."""
        start = time.strftime("%Y-%m-%d:%H-%M", time.gmtime(int(utc_start)))
        dur = max(1, math.ceil(duration_min))
        if (form or self.timeshift_form) == "modern":
            return (f"{self.base}/timeshift"
                    f"/{urllib.parse.quote(self.username, safe='')}"
                    f"/{urllib.parse.quote(self.password, safe='')}"
                    f"/{int(stream_id)}/{start}/{dur}/ts")
        return (
            f"{self.base}/streaming/timeshift.php"
            f"?username={urllib.parse.quote(self.username, safe='')}"
            f"&password={urllib.parse.quote(self.password, safe='')}"
            f"&stream={int(stream_id)}&start={start}"
            f"&duration={dur}&extension=ts"
        )

    def vod_url(self, stream_id, container_extension: str = "mp4") -> str:
        return (
            f"{self.base}/movie/{self.username}/{self.password}/"
            f"{stream_id}.{container_extension}"
        )

    def series_url(self, episode_id, container_extension: str = "mp4") -> str:
        return (
            f"{self.base}/series/{self.username}/{self.password}/"
            f"{episode_id}.{container_extension}"
        )
