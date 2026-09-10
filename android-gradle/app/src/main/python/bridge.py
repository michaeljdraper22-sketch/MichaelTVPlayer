"""Python side of the MichaelTV Android WebView bridge.

Java (MainActivity) constructs ``Bridge(files_dir)`` once via Chaquopy
and forwards every ``py.rpc(cmd, argsJson)`` JavascriptInterface call to
:meth:`Bridge.rpc`.  Commands take a JSON object and return a JSON
object; every failure is returned as ``{"error": "..."}`` so nothing
ever raises across the bridge (a Java-side traceback would kill the
WebView's JS thread mid-call).

The Stremio/Xtream logic itself is the DESKTOP app's code, copied
verbatim into michaeltv_core/ by prepare_core.py — the single source of
truth stays src/.
"""

import datetime
import json
import os
import re
import threading

from michaeltv_core import APP_VERSION, stremio, xtream

DEFAULT_SETTINGS = {
    "addons": [],               # Stremio stream addon base URLs
    "resolution_pref": "match",  # match | auto | 2160|1440|1080|720|480
    "size_demote_gb": 0,         # demote (never exclude) streams above this
    "xtream_server": "",
    "xtream_username": "",
    "xtream_password": "",
}

_RESOLUTIONS = ("match", "auto", "2160", "1440", "1080", "720", "480")

_YEAR_RE = re.compile(r"(?:19|20)\d{2}")


class BridgeError(Exception):
    """A command failed in an expected, user-presentable way."""


class _ConfigFacade:
    """The settings slice stremio needs from a "config" object:
    attributes ``stremio_resolution_pref`` / ``stremio_size_demote_gb``
    and ``.data["stremio_addons"]`` (see src/stremio.py:952 and :1140)."""

    def __init__(self, settings):
        self.data = {"stremio_addons": list(settings.get("addons") or [])}
        pref = str(settings.get("resolution_pref") or "match")
        self.stremio_resolution_pref = pref if pref in _RESOLUTIONS else "match"
        try:
            self.stremio_size_demote_gb = float(settings.get("size_demote_gb") or 0)
        except (TypeError, ValueError):
            self.stremio_size_demote_gb = 0.0


class Bridge:

    def __init__(self, files_dir):
        self._files_dir = str(files_dir)
        self._settings_path = os.path.join(self._files_dir, "settings.json")
        self._settings = dict(DEFAULT_SETTINGS)
        self._load_settings()
        self._xt = None            # (creds key, XtreamClient) cache
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # the one entry point Java calls

    def rpc(self, cmd, args_json):
        try:
            with self._lock:
                args = json.loads(args_json) if args_json else {}
                if not isinstance(args, dict):
                    args = {}
                handler = getattr(self, "_cmd_" + str(cmd), None)
                if handler is None:
                    return json.dumps({"error": "unknown command: %s" % cmd})
                result = handler(args)
                if not isinstance(result, dict):
                    result = {"ok": bool(result)}
                return json.dumps(result)
        except Exception as exc:  # noqa: BLE001 — never raise across the bridge
            return json.dumps({"error": "%s: %s" % (type(exc).__name__, exc)})

    # ------------------------------------------------------------------
    # settings persistence (atomic write; re-read on every launch)

    def _load_settings(self):
        try:
            with open(self._settings_path, "r", encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                for key, default in DEFAULT_SETTINGS.items():
                    self._settings[key] = stored.get(key, default)
        except (OSError, ValueError):
            pass

    def _save_settings(self):
        tmp = self._settings_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._settings, f, indent=1)
        os.replace(tmp, self._settings_path)

    def _cmd_ping(self, args):
        return {"ok": True, "version": APP_VERSION}

    def _cmd_get_settings(self, args):
        return {"ok": True, "settings": dict(self._settings)}

    def _cmd_set_settings(self, args):
        clean = dict(self._settings)
        if isinstance(args.get("addons"), list):
            clean["addons"] = [str(a).strip() for a in args["addons"]
                               if str(a).strip()]
        pref = args.get("resolution_pref")
        if isinstance(pref, str):
            clean["resolution_pref"] = pref if pref in _RESOLUTIONS else "match"
        if "size_demote_gb" in args:
            try:
                clean["size_demote_gb"] = float(args["size_demote_gb"] or 0)
            except (TypeError, ValueError):
                clean["size_demote_gb"] = 0
        for key in ("xtream_server", "xtream_username"):
            if key in args:
                clean[key] = str(args[key] or "").strip()
        if "xtream_password" in args:
            clean["xtream_password"] = str(args["xtream_password"] or "")
        self._settings = clean
        self._save_settings()
        self._xt = None            # creds may have changed
        return {"ok": True, "settings": dict(self._settings)}

    # ------------------------------------------------------------------
    # Stremio search / detail / streams

    def _cmd_search(self, args):
        q = str(args.get("q") or "").strip()
        if not q:
            return {"ok": True, "results": []}
        results = []
        for m in stremio.search_movies(q):
            results.append({
                "kind": "movie", "imdb": m["id"], "name": m["name"],
                "year": m.get("year") or "", "poster": m.get("poster") or "",
            })
        # search_series() drops year/poster, so use the raw catalog metas.
        for m in stremio._catalog_search("series", q):
            if m.get("type") not in (None, "series"):
                continue
            if not (m.get("id") and m.get("name")):
                continue
            ym = _YEAR_RE.match(
                str(m.get("releaseInfo") or m.get("year") or ""))
            results.append({
                "kind": "series", "imdb": str(m["id"]),
                "name": str(m["name"]),
                "year": ym.group(0) if ym else "",
                "poster": str(m.get("poster") or ""),
            })
        return {"ok": True, "results": results}

    def _cmd_detail(self, args):
        imdb = str(args.get("imdb") or "")
        if not imdb:
            raise BridgeError("imdb required")
        meta = stremio.series_meta(imdb)
        if not meta:
            raise BridgeError("series metadata unavailable (Cinemeta)")
        videos = meta.get("videos") or []
        seasons = sorted({v.get("season") for v in videos
                          if isinstance(v.get("season"), int)})
        episodes = [{
            "season": v.get("season"), "episode": v.get("episode"),
            "name": str(v.get("name") or ""),
        } for v in videos
            if isinstance(v.get("season"), int)
            and isinstance(v.get("episode"), int)]
        return {"ok": True, "name": str(meta.get("name") or ""),
                "seasons": seasons, "episodes": episodes}

    @staticmethod
    def _stream_row(s):
        """One ranked stream as the UI row: raw name/title, playable flag,
        url ONLY when playable, plus a small quality/size summary."""
        res, _seeds, size_gb = stremio._stream_parts(s)
        bits = []
        if res:
            bits.append("%dp" % res)
        if size_gb:
            bits.append("%.1f GB" % size_gb)
        row = {
            "name": str(s.get("name") or ""),
            "title": str(s.get("title") or ""),
            "playable": bool(s.get("url")),
            "size_text": " \u00b7 ".join(bits),
            "infoHash": str(s.get("infoHash") or ""),
            "fileIdx": s.get("fileIdx"),
            "addon": str(s.get("_addon") or ""),
        }
        if row["playable"]:
            row["url"] = str(s.get("url"))
        return row

    def _cmd_streams_movie(self, args):
        imdb = str(args.get("imdb") or "")
        if not imdb:
            raise BridgeError("imdb required")
        cfg = _ConfigFacade(self._settings)
        streams = stremio.addon_movie_streams(cfg, imdb)
        ranked = stremio.rank_streams(cfg, streams)
        return {"ok": True,
                "streams": [self._stream_row(s) for s in ranked]}

    def _cmd_streams_series(self, args):
        imdb = str(args.get("imdb") or "")
        try:
            season = int(args.get("season"))
            episode = int(args.get("episode"))
        except (TypeError, ValueError):
            raise BridgeError("imdb, season and episode are required")
        if not imdb:
            raise BridgeError("imdb required")
        cfg = _ConfigFacade(self._settings)
        streams = stremio.addon_streams(cfg, imdb, season, episode)
        ranked = stremio.rank_streams(cfg, streams)
        return {"ok": True,
                "streams": [self._stream_row(s) for s in ranked]}

    def _cmd_probe(self, args):
        url = str(args.get("url") or "")
        if not url:
            raise BridgeError("url required")
        return {"ok": True, "alive": bool(stremio.probe_debrid(url))}

    # ------------------------------------------------------------------
    # Xtream live TV

    def _xt_client(self):
        s = self._settings
        server = str(s.get("xtream_server") or "").strip()
        user = str(s.get("xtream_username") or "").strip()
        pwd = str(s.get("xtream_password") or "")
        if not (server and user):
            raise BridgeError("No Xtream account configured — add server, "
                              "username and password in Settings")
        key = (server, user, pwd)
        if self._xt is None or self._xt[0] != key:
            client = xtream.XtreamClient(
                xtream.normalize_server_url(server), user, pwd)
            # Share stremio's module-level Session (spec) while keeping a
            # panel-friendly user agent.
            client.session = stremio._session
            client.session.headers["User-Agent"] = "MichaelTVPlayer/1.0"
            self._xt = (key, client)
        return self._xt[1]

    def _cmd_xt_auth(self, args):
        client = self._xt_client()
        info = client.authenticate()
        bits = []
        if info.status:
            bits.append(str(info.status).capitalize())
        if str(info.exp_date).isdigit():
            try:
                bits.append("expires " + datetime.datetime.fromtimestamp(
                    int(info.exp_date)).strftime("%Y-%m-%d"))
            except (ValueError, OSError, OverflowError):
                pass
        if str(info.max_connections):
            bits.append("%s/%s connections" % (
                info.active_cons or "?", info.max_connections))
        if str(info.is_trial) == "1":
            bits.append("trial")
        return {"ok": True, "username": info.username,
                "info_summary": " \u00b7 ".join(bits) or "Connected"}

    def _cmd_xt_categories(self, args):
        client = self._xt_client()
        cats = client.live_categories()
        return {"ok": True, "categories": [
            {"id": c.get("category_id"),
             "name": str(c.get("category_name") or c.get("name") or "")}
            for c in cats if c.get("category_id") is not None]}

    def _cmd_xt_channels(self, args):
        client = self._xt_client()
        cat = args.get("category_id")
        cat = str(cat) if cat is not None else None
        streams = client.live_streams(cat)
        return {"ok": True, "channels": [
            {"stream_id": s.get("stream_id"),
             "name": str(s.get("name") or ""),
             "logo": str(s.get("stream_icon") or "")}
            for s in streams if s.get("stream_id") is not None]}

    def _cmd_xt_url(self, args):
        client = self._xt_client()
        try:
            stream_id = int(args.get("stream_id"))
        except (TypeError, ValueError):
            raise BridgeError("stream_id required")
        return {"ok": True, "url": client.live_url(stream_id, "ts")}
