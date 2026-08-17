"""Xtream Codes API client for IPTV providers (Live / VOD / Series / EPG)."""

import requests

from .models import EpgEntry, UserInfo


class XtreamError(Exception):
    pass


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
        entries = data.get("epg_listings") or []
        return [
            EpgEntry(
                title=e.get("title", "") or "",
                description=e.get("description", "") or "",
                start=e.get("start", ""),
                end=e.get("end", ""),
                start_timestamp=e.get("start_timestamp", ""),
                stop_timestamp=e.get("stop_timestamp", ""),
            )
            for e in entries
        ]

    # ---- stream url builders ----
    def live_url(self, stream_id, ext: str = "ts") -> str:
        return f"{self.base}/live/{self.username}/{self.password}/{stream_id}.{ext}"

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
