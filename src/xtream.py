"""Xtream Codes API client for IPTV providers (Live / VOD / Series / EPG)."""

import base64
import binascii
import hashlib
import json
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


def _http_error_message(resp) -> str:
    """Final (post-retry) HTTP failure as a plain-language message.

    The body is surfaced because this provider's 403 outage replies
    {"error":"Authentication failed"} — indistinguishable from bad
    credentials without it (issue #5) — and the hints say what a 403/429
    from an IPTV panel almost always is (field diagnosis of the "dead"
    line on GitHub issue #3: a provider-side block/outage, not an app
    bug — the old bare "Server returned HTTP 403" sent the user chasing
    a phantom app regression)."""
    status = getattr(resp, "status_code", 0)
    hint = ""
    if status == 403:
        hint = (" — the provider is refusing this connection (rate "
                "limit, outage or block); retry in a few minutes")
    elif status == 429:
        hint = (" — the provider is rate-limiting; wait a minute, then "
                "reload the lists")
    elif 500 <= status < 600:
        hint = " — provider server error; retry shortly"
    body = ""
    try:
        body = (resp.text or "").strip()[:120]
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                body = str(parsed.get("error") or body)
        except ValueError:
            pass
    except Exception:  # noqa: BLE001
        pass
    detail = f" [{body}]" if body else ""
    return f"Server returned HTTP {status}{hint}{detail}"


def normalize_server_url(s: str) -> str:
    """Clean a pasted panel URL.  Pastes from chat/email carry trailing
    newlines and signature dashes — one such paste reached DNS as
    ``host%0a%0a%0a-----`` (field log, GitHub issue #5).  Keep the first
    line, strip paste artifacts, ensure a scheme, drop trailing slashes."""
    s = (s or "").strip()
    if not s:
        return ""
    s = s.splitlines()[0].strip()
    s = s.rstrip("/- \t")
    if s and not s.startswith(("http://", "https://")):
        s = "http://" + s
    return s.rstrip("/")


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
    # Provider flakiness schedule (GitHub issue #5, field-verified on
    # cdngold8k): the panel rate-limits on per-second CONCURRENCY (7 truly
    # parallel calls -> 1-2 x 429; sequential always 200) and has hard
    # outage windows where EVERY request 403s "Authentication failed" for
    # minutes even with valid credentials.  429/5xx/network errors back
    # off up to 4 retries; 403 gets 2 quick retries (it is usually a
    # transient backend flap, not bad credentials — but a definitive
    # 200-body auth rejection still fails immediately, no retries).
    _BULK_BACKOFF = (0.8, 1.5, 2.5, 4.0, 6.0)
    _FAST_BACKOFF = (0.8, 1.5)     # interactive calls: quick retries only
    _RETRY_403 = 2
    # Short TTL for list payloads: the Countries dialog and tab switches
    # used to re-download the same megabytes within seconds.
    _CACHE_TTL = 120.0

    def __init__(self, server: str, username: str, password: str,
                 timeout: int = 20, max_concurrent: int = 2):
        if not server:
            raise XtreamError("No server URL configured")
        self.base = server.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        # Bulk list loads (categories / whole-library streams) share this
        # gate so the startup burst cannot trip per-second rate limiters.
        # Interactive one-shot calls (EPG on click, vod/series info, auth)
        # stay OUTSIDE it — a 45 MB movie-list download must never stall
        # them.  Configurable (Settings > Provider connection speed,
        # default 2, 1..16) because tolerance varies per provider.
        self._gate = threading.BoundedSemaphore(
            max(1, min(16, int(max_concurrent or 2))))
        self._cache = {}            # key -> (expires_epoch, data)
        self._cache_lock = threading.Lock()
        self._dedupe = {}           # key -> in-flight {"done","data","error"}
        self._dedupe_lock = threading.Lock()
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
    def _api(self, action=None, timeout=None, bulk=False, **extra):
        params = {"username": self.username, "password": self.password}
        if action:
            params["action"] = action
        for k, v in extra.items():
            if v is not None:
                params[k] = v
        url = f"{self.base}/player_api.php"
        backoff = self._BULK_BACKOFF if bulk else self._FAST_BACKOFF
        resp = None
        for attempt in range(len(backoff) + 1):
            exc = None
            try:
                if bulk:
                    # a slot is held ONLY for the HTTP exchange — never
                    # while backing off (a 429'd call must not block the
                    # others) and never while parsing a multi-MB body
                    with self._gate:
                        resp = self.session.get(
                            url, params=params,
                            timeout=timeout or self.timeout)
                else:
                    resp = self.session.get(
                        url, params=params, timeout=timeout or self.timeout)
            except requests.RequestException as e:
                exc = e
            if exc is None:
                if resp.status_code == 200:
                    break
                retryable = (resp.status_code == 429
                             or 500 <= resp.status_code < 600
                             or (resp.status_code == 403
                                 and attempt < self._RETRY_403))
                if retryable and attempt < len(backoff):
                    _record_error(action, "HTTP %d (retrying)"
                                  % resp.status_code)
                    delay = backoff[attempt]
                    try:    # honour the panel's own ask when it has one
                        delay = max(delay, float(
                            resp.headers.get("Retry-After")))
                    except (TypeError, ValueError):
                        pass
                    time.sleep(delay)
                    continue
                _record_error(action, "HTTP %d" % resp.status_code)
                log.error("xtream %s: server returned HTTP %d",
                          action or "auth", resp.status_code)
                raise XtreamError(_http_error_message(resp))
            # connection-level failure — ONE quiet retry. The long ladder
            # is for rate-limited/5xx ANSWERS, not dead hosts: a typo'd
            # server URL (or the login "Test Connection" against one)
            # must fail fast, not hang through ~15 s of backoff.
            if attempt == 0:
                time.sleep(1.0)
                continue
            _record_error(action, "connection: %r" % (exc,))
            log.error("xtream %s: connection failed: %r",
                      action or "auth", exc)
            raise XtreamError(f"Connection failed: {exc}") from exc
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

    # ---- bulk loads: TTL cache + in-flight dedupe (issue #5) ----
    # Startup wants the same lists from several widgets at once — the Live
    # tab, the Catch-Up tab and the Countries dialog each fetch
    # get_live_categories, and Live + Catch-Up each downloaded the whole
    # 7 MB get_live_streams (~11 parallel calls, ~150 MB in 7 s).  One
    # flight per key plus a short cache turns the burst into one download.
    # Every caller gets its own shallow copy so nobody's in-place sorting
    # or filtering leaks into anyone else's list.
    def _bulk(self, key, fetch, refresh=False):
        if not refresh:
            with self._cache_lock:
                hit = self._cache.get(key)
                if hit and hit[0] > time.time():
                    data = hit[1]
                    return list(data) if isinstance(data, list) else data
            with self._dedupe_lock:
                flight = self._dedupe.get(key)
                is_leader = flight is None
                if is_leader:
                    flight = {"done": threading.Event(), "data": None,
                              "error": None}
                    self._dedupe[key] = flight
            if not is_leader:
                flight["done"].wait()
                if flight["error"] is not None:
                    raise flight["error"]
                data = flight["data"]
                return list(data) if isinstance(data, list) else data
        else:
            flight = {"done": threading.Event(), "data": None, "error": None}
            is_leader = True
        try:
            data = fetch()
            flight["data"] = data      # followers read this after wait()
        except BaseException as exc:
            flight["error"] = exc
            raise
        finally:
            if is_leader:
                flight["done"].set()
                with self._dedupe_lock:
                    if self._dedupe.get(key) is flight:
                        del self._dedupe[key]
        with self._cache_lock:
            self._cache[key] = (time.time() + self._CACHE_TTL, data)
        return list(data) if isinstance(data, list) else data

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
    def live_categories(self, refresh=False):
        return self._bulk(
            ("live_categories",),
            lambda: self._api("get_live_categories", bulk=True),
            refresh) or []

    def live_streams(self, category_id=None, refresh=False):
        return self._bulk(
            ("live_streams", category_id),
            lambda: self._api("get_live_streams", category_id=category_id,
                              bulk=True),
            refresh) or []

    # ---- vod (movies) ----
    def vod_categories(self, refresh=False):
        return self._bulk(
            ("vod_categories",),
            lambda: self._api("get_vod_categories", bulk=True),
            refresh) or []

    def vod_streams(self, category_id=None, timeout=None, refresh=False):
        return self._bulk(
            ("vod_streams", category_id),
            lambda: self._vod_fetch(category_id, timeout),
            refresh) or []

    def _vod_fetch(self, category_id, timeout):
        # No category = the provider's ENTIRE movie library, which can be a
        # multi-MB JSON that takes far longer than the default timeout.
        try:
            data = self._api("get_vod_streams", category_id=category_id,
                             timeout=timeout, bulk=True) or []
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
    def series_categories(self, refresh=False):
        return self._bulk(
            ("series_categories",),
            lambda: self._api("get_series_categories", bulk=True),
            refresh) or []

    def series(self, category_id=None, timeout=None, refresh=False):
        return self._bulk(
            ("series", category_id),
            lambda: self._series_fetch(category_id, timeout),
            refresh) or []

    def _series_fetch(self, category_id, timeout):
        # Same as VOD: the full series list can be huge.
        try:
            data = self._api("get_series", category_id=category_id,
                             timeout=timeout, bulk=True) or []
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
