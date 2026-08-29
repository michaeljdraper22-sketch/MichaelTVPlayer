# -*- coding: utf-8 -*-
"""In-session feedback store — everything the automatic diagnostics uploads
need beyond what the log tail already carries.

Holds (all in-memory, thread-safe via a lock, never raising):
- SESSION_STATS: counters (plays, VLC errors, buffering events, stalls,
  seeks, chase rescues, recording failures, UI freezes)
- CRUMBS: a ring of the last user-visible actions ("what was he doing")
- USAGE: feature usage counts (tab switches, records, downloads, …)
- dirty-session marker: proves a previous run died without a clean exit
- UI freeze watchdog: a Qt timer stamps ui_now(); a daemon thread alarms
  when the stamp goes stale (the upload path is non-Qt, so a FROZEN app
  can still report its own freeze)

No credentials are ever stored here; probe helpers return statuses only.
"""

import logging
import os
import threading
import time
import urllib.parse

log = logging.getLogger("mtp.feedback")

_lock = threading.Lock()

SESSION_STATS = {}
CRUMBS = []        # [(epoch, text)] — last 40
USAGE = {}         # feature -> count
CRUMB_MAX = 40

_ui_last_beat = time.time()
_ui_freeze_reported = False
_dirty_prev_session = False

# host of the current provider panel (in-memory only, used for latency
# probing; never rendered into any report)
_server_host = ""
_server_scheme = "http"


# ---- generic recorders ----------------------------------------------------

def stat(key: str, add: int = 1) -> None:
    try:
        with _lock:
            SESSION_STATS[key] = SESSION_STATS.get(key, 0) + add
    except Exception:
        pass


def crumb(text: str) -> None:
    try:
        with _lock:
            CRUMBS.append((time.time(), str(text)[:160]))
            del CRUMBS[:-CRUMB_MAX]
    except Exception:
        pass


def usage(feature: str) -> None:
    try:
        with _lock:
            USAGE[feature] = USAGE.get(feature, 0) + 1
    except Exception:
        pass


def set_server(base_url: str) -> None:
    """Remember the panel host (never printed) for network probing."""
    global _server_host, _server_scheme
    try:
        p = urllib.parse.urlparse(str(base_url))
        if p.hostname:
            _server_host, _server_scheme = p.hostname, p.scheme or "http"
    except Exception:
        pass


# ---- dirty session marker --------------------------------------------------

_MARKER_NAME = "session.open"


def _log_dir() -> str:
    from .logging_setup import LOG_DIR
    return LOG_DIR


def session_start() -> bool:
    """Call once at startup. Writes the open-session marker and returns
    True when the PREVIOUS session ended dirty (no clean shutdown —
    crash, kill, power loss, wedge)."""
    global _dirty_prev_session
    try:
        p = os.path.join(_log_dir(), _MARKER_NAME)
        _dirty_prev_session = os.path.exists(p)
        if _dirty_prev_session:
            log.warning("previous session did not shut down cleanly "
                        "(crash / kill / power loss?)")
        with open(p, "w") as f:
            f.write(str(os.getpid()))
        return _dirty_prev_session
    except Exception:
        return False


def session_end() -> None:
    """Call on the clean-exit path; removes the marker."""
    try:
        try:
            os.remove(os.path.join(_log_dir(), _MARKER_NAME))
        except OSError:
            pass
    except Exception:
        pass


def prev_session_dirty() -> bool:
    return _dirty_prev_session


# ---- UI freeze watchdog ------------------------------------------------------

def ui_beat() -> None:
    """Stamp 'the Qt event loop is alive'. Call from a QTimer (~5 s)."""
    global _ui_last_beat
    _ui_last_beat = time.time()


def start_ui_watchdog(check_s: float = 30.0, stale_s: float = 60.0,
                      report_s: float = 180.0) -> None:
    """Daemon thread watching the Qt stamp. A stale stamp means the UI is
    frozen while the rest of the process lives — logged at ERROR so the
    (non-Qt) diagnostics trigger reports it even mid-freeze."""
    global _ui_freeze_reported

    def _watch():
        global _ui_freeze_reported
        while True:
            time.sleep(check_s)
            try:
                lag = time.time() - _ui_last_beat
                if lag > stale_s:
                    stat("ui_freeze_events")
                    stat("ui_freeze_max_lag_s", 0)
                    with _lock:
                        if SESSION_STATS.get("ui_freeze_max_lag_s", 0) < lag:
                            SESSION_STATS["ui_freeze_max_lag_s"] = round(lag)
                    if lag > report_s and not _ui_freeze_reported:
                        _ui_freeze_reported = True
                        log.error("UI freeze detected: event loop unresponsive "
                                  "for %.0f s", lag)
                else:
                    _ui_freeze_reported = False
            except Exception:
                pass

    try:
        t = threading.Thread(target=_watch, name="mtp-ui-watchdog",
                             daemon=True)
        t.start()
    except Exception:
        pass


# ---- stream probes -----------------------------------------------------------

def probe_url(url: str, timeout: float = 6.0) -> str:
    """Best-effort HTTP probe of a stream URL — status + content type only,
    returned as a short string for logs/reports. Never raises; the first
    kilobyte is read (some panels answer HEAD with a lie)."""
    try:
        import requests
        r = requests.get(url, stream=True, timeout=timeout,
                         headers={"User-Agent": "MichaelTVPlayer-probe"})
        ctype = (r.headers.get("Content-Type") or "?").split(";")[0]
        chunk = b""
        try:
            for chunk in r.iter_content(1024):
                break
        except Exception:
            pass
        r.close()
        return "HTTP %s %s %s" % (
            r.status_code, ctype, "data" if chunk else "empty")
    except Exception as exc:  # noqa: BLE001
        return "probe failed: %s" % type(exc).__name__


def probe_panel_network() -> dict:
    """Latency/basic reachability of the provider panel (host is NOT
    included in the result — it stays in memory only). Runs on the upload
    thread; every step capped."""
    out = {"panel_tcp_ms": -1, "panel_scheme": _server_scheme,
           "proxy": ""}
    if not _server_host:
        out["panel_tcp_ms"] = -2   # no server configured this session
        return out
    try:
        import socket
        port = 443 if _server_scheme == "https" else 80
        t0 = time.time()
        s = socket.create_connection((_server_host, port), timeout=5)
        out["panel_tcp_ms"] = round((time.time() - t0) * 1000)
        try:
            out["ip_version"] = "IPv6" if ":" in s.getpeername()[0] \
                else "IPv4"
        except Exception:
            pass
        s.close()
    except Exception as exc:  # noqa: BLE001
        out["panel_tcp_ms"] = -3   # unreachable
        out["panel_tcp_error"] = type(exc).__name__
    try:
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            v = os.environ.get(var)
            if v:
                out["proxy"] = var.lower()
                break
    except Exception:
        pass
    return out


# ---- report snapshot ----------------------------------------------------------

def snapshot() -> dict:
    """Thread-safe copy of everything for the diagnostics report."""
    try:
        with _lock:
            stats = dict(SESSION_STATS)
            crumbs = list(CRUMBS)
            usage = dict(USAGE)
        return {"stats": stats, "crumbs": crumbs, "usage": usage,
                "prev_session_dirty": _dirty_prev_session}
    except Exception:
        return {"stats": {}, "crumbs": [], "usage": [],
                "prev_session_dirty": False}
