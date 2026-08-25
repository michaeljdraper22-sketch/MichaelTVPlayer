"""Xtream Codes API client for IPTV providers (Live / VOD / Series / EPG)."""

import base64
import binascii
import math
import time
import urllib.parse

import requests

from .models import EpgEntry, UserInfo


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

    # ---- low level ----
    def _api(self, action=None, timeout=None, **extra):
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
            raise XtreamError(f"Connection failed: {exc}") from exc
        if resp.status_code != 200:
            raise XtreamError(f"Server returned HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise XtreamError("Server did not return valid JSON") from exc
        if isinstance(data, dict) and data.get("user_info", {}).get("auth") == 0:
            raise XtreamError("Invalid username or password")
        return data

    # ---- account ----
    def authenticate(self) -> UserInfo:
        data = self._api()
        ui = data.get("user_info", {}) if isinstance(data, dict) else {}
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
        return self._api("get_vod_streams", category_id=category_id,
                         timeout=timeout) or []

    def vod_info(self, vod_id):
        return self._api("get_vod_info", vod_id=vod_id) or {}

    # ---- series ----
    def series_categories(self):
        return self._api("get_series_categories") or []

    def series(self, category_id=None, timeout=None):
        # Same as VOD: the full series list can be huge.
        return self._api("get_series", category_id=category_id,
                         timeout=timeout) or []

    def series_info(self, series_id):
        return self._api("get_series_info", series_id=series_id) or {}

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

    def timeshift_url(self, stream_id, utc_start: int, duration_min: int) -> str:
        """Catch-up (archive) stream: the provider serves the recorded
        broadcast from ``utc_start`` (epoch seconds) for ``duration_min``
        minutes.  Times are UTC, formatted YYYY-MM-DD:HH-MM.

        This panel's modern path form (/timeshift/u/p/id/start/dur) answers
        HTTP 513 — the legacy streaming endpoint is the one that works
        (probed 2026-08-24: 200, video/mp2t, duration honored, arbitrary
        mid-program starts honored)."""
        start = time.strftime("%Y-%m-%d:%H-%M", time.gmtime(int(utc_start)))
        return (
            f"{self.base}/streaming/timeshift.php"
            f"?username={urllib.parse.quote(self.username, safe='')}"
            f"&password={urllib.parse.quote(self.password, safe='')}"
            f"&stream={int(stream_id)}&start={start}"
            f"&duration={max(1, math.ceil(duration_min))}&extension=ts"
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
