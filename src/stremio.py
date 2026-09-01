"""Stremio handoff integration: turn a Stremio "open in external player"
handoff into a playable, then autoplay the next episode without Stremio.

How the handoff works on Windows (verified against stremio-web/stremio-core
source, 2026-09): Stremio's only desktop external-player option is "M3U
Playlist" — clicking a stream downloads a playlist.m3u containing one line,
the stream URL. For torrent sources that URL points at the LOCAL Stremio
streaming server (stremio-service / the desktop runtime, port 11470):

    http://127.0.0.1:11470/{infoHash}/{fileIdx}

For addon HTTP sources it is the direct stream URL. That download is
picked up by src/watchfolder.py (MichaelTV watches the Downloads
folder — Windows 11's .m3u UserChoice is tamper-locked to the Store
Media Player, so waiting for an association launch is a dead end), or
handed in directly by the patched Stremio server's VLC-style launch
line (src/streampatch.py).

Next-episode autoplay never talks to the Stremio app: it uses the same
public plumbing Stremio itself uses —
  * Cinemeta (v3-cinemeta.strem.io)  — series metadata / episode lists,
  * the user's stream addon(s)        — /stream/series/{imdb}:{s}:{e}.json,
  * the local streaming server        — POST /{infoHash}/create starts the
    torrent, GET /{infoHash}/{fileIdx} plays it.
"""

import logging
import os
import re
import time

import requests

log = logging.getLogger("mtp.stremio")

CINEMETA = "https://v3-cinemeta.strem.io"
DEFAULT_SERVER = "http://127.0.0.1:11470"
USER_AGENT = "MichaelTVPlayer/1.0 (Stremio handoff)"

_session = requests.Session()
_session.headers["User-Agent"] = USER_AGENT

_meta_cache = {}          # imdb_id -> series meta (episode lists are big)


# ---------------------------------------------------------------------------
# handoff parsing

def parse_handoff_arg(arg: str) -> str:
    """Turn a command-line argument into a stream URL, or "".

    Accepts a direct http(s) URL, or a path to a playlist file Stremio
    downloaded (.m3u / .strm — also .txt, which browsers rename duplicates
    to). Playlist files carry the URL on the first non-comment line."""
    if not arg:
        return ""
    arg = arg.strip().strip('"')
    if arg.startswith(("http://", "https://")):
        return arg
    if not os.path.isfile(arg):
        return ""
    low = arg.lower()
    if not low.endswith((".m3u", ".m3u8", ".strm", ".txt")):
        return ""
    try:
        with open(arg, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(1 << 16)
    except OSError as exc:
        log.warning("stremio handoff: cannot read %r: %r", arg, exc)
        return ""
    return parse_m3u(text)


def parse_m3u(text: str) -> str:
    """First playable URL in an .m3u/.strm body (Stremio writes exactly
    one; being liberal here costs nothing)."""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("http://", "https://", "rtmp://", "rtsp://")):
            return line
    return ""


def parse_launch_args(args):
    """Full command-line parse of an external-player launch (the patched
    Stremio streaming server invokes MichaelTV exactly like it invoked
    VLC: ``--start-time=<sec> --no-video-title-show [--sub-file=<srt>]
    "<url>"``; Windows .m3u association launches pass just the playlist
    path). Returns ``{url, start_at, sub_file}`` or None if nothing
    playable. Unknown dash-args are ignored (and their values are never
    mistaken for the URL)."""
    start_at = 0.0
    sub_file = ""
    url = ""
    args = [str(a).strip().strip('"') for a in (args or []) if str(a).strip()]
    i = 0
    while i < len(args):
        arg = args[i]
        low = arg.lower()
        if low.startswith("--start-time="):
            try:
                start_at = max(0.0, float(arg.split("=", 1)[1]))
            except ValueError:
                pass
        elif low.startswith("--sub-file="):
            # cmd.exe does not quote it; a path with spaces arrives glued
            # to the next flag — re-join up to the next " --" if needed
            sub_file = arg.split("=", 1)[1]
            if sub_file and not os.path.isfile(sub_file) \
                    and " --" in sub_file:
                sub_file = sub_file.rsplit(" --", 1)[0]
        elif arg.startswith("-"):
            pass                      # --no-video-title-show & friends
        elif not url:
            url = parse_handoff_arg(arg)
        i += 1
    if not url:
        return None
    return {"url": url, "start_at": start_at, "sub_file": sub_file}


_SERVER_URL_RE = re.compile(
    r"^https?://(?P<host>[^/]+)/(?P<hash>[0-9a-fA-F]{40})(?:/(?P<idx>\d+))?"
    r"(?:[/?#]|$)")

# Torrentio-style debrid resolve links (what "Play in external player"
# hands over when the stream was debrid-resolved):
#   /resolve/{provider}/{userKey}/{infoHash}/{fileName}/{fileIdx}/{fileName}
# Debridio's play links carry the hash too, without an index:
#   /play/series/{provider}/{userKey}/{addonId}/{infoHash}/{fileName}
# Either way the infoHash (+ fileIdx when present) lets playback fall
# back to the local Stremio server's torrent when the debrid link
# itself stalls (seen live: transient 502s from the resolve endpoint).
_RESOLVE_URL_RE = re.compile(
    r"/resolve/[a-zA-Z0-9]+/[^/]+/(?P<hash>[0-9a-fA-F]{40})/[^/]+"
    r"/(?P<idx>\d+)/", re.IGNORECASE)
_HASH_SEG_RE = re.compile(r"/(?P<hash>[0-9a-fA-F]{40})(?:/|$)")


def parse_resolve_url(url: str):
    """(info_hash, file_idx) if ``url`` is a debrid resolve/play link
    carrying its torrent's info hash, else None. Torrentio links carry
    the file index; others default to 0 (episode releases are
    single-file, and the debrid link itself was already file-specific)."""
    m = _RESOLVE_URL_RE.search(url or "")
    if m:
        try:
            return (m.group("hash").lower(), int(m.group("idx") or 0))
        except ValueError:
            return None
    if "resolve/" in (url or "") or "/play/" in (url or ""):
        m = _HASH_SEG_RE.search(url)
        if m:
            return (m.group("hash").lower(), 0)
    return None


def parse_server_url(url: str):
    """(info_hash, file_idx) if ``url`` is a Stremio streaming-server play
    URL, else None. file_idx defaults to 0 when the server omits it."""
    m = _SERVER_URL_RE.match(url or "")
    if not m:
        return None
    return m.group("hash").lower(), int(m.group("idx") or 0)


# ---------------------------------------------------------------------------
# naming / season-episode parsing

_SE_RES = [
    re.compile(r"\bS(?P<s>\d{1,2})\s*E(?P<e>\d{1,3})\b", re.IGNORECASE),
    re.compile(r"\b(?P<s>\d{1,2})x(?P<e>\d{2,3})\b"),
]


def parse_se(text: str):
    """(season, episode) parsed from a torrent/file name, or None."""
    if not text:
        return None
    for rx in _SE_RES:
        m = rx.search(text)
        if m:
            s, e = int(m.group("s")), int(m.group("e"))
            if 0 <= s <= 100 and 1 <= e <= 500:
                return s, e
    return None


_JUNK_RES = [
    re.compile(r"\bS\d{1,2}\s*E\d{1,3}\b.*$", re.IGNORECASE),
    re.compile(r"\b\d{1,2}x\d{2,3}\b.*$", re.IGNORECASE),
    re.compile(r"(2160p|1080p?|720p|480p|WEB[-.]?DL|WEBRip|BluRay|Blu-Ray|"
               r"HDTV|HDTVRip|HDR10?|DV|DTS|AAC2?\.?0?|AC3|5\.1|7\.1|"
               r"x264|h\.?265|x265|HEVC|AVC|10bit|8bit|REPACK|PROPER|"
               r"MULTI|VFI|Complete|Season\s*\d+)", re.IGNORECASE),
    re.compile(r"\b(www|com|net|org)\b", re.IGNORECASE),
    re.compile(r"[\.\-_]+(?=[\.\-_]|$)"),
]


def clean_show_name(text: str) -> str:
    """Reduce a torrent/file name to something a catalog search likes:
    strip the episode marker + quality/codec noise, separators to spaces."""
    name = text or ""
    for rx in _JUNK_RES:
        name = rx.sub(" ", name)
    name = re.sub(r"[\.\-_]+", " ", name)
    return re.sub(r"\s+", " ", name).strip(" -–—()[]{}.")


def _scan_for_names(obj):
    """Collect string values under name-ish keys anywhere in a JSON blob
    (the streaming server's stats shape is not contractual)."""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and re.fullmatch(
                    r"[a-z0-9_. ]*(name|title|filename|file)", k,
                    re.IGNORECASE) and len(v) > 3:
                found.append(v)
            else:
                found.extend(_scan_for_names(v))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(_scan_for_names(v))
    return found


def _bdecode(data: bytes):
    """Minimal bencode decoder (torrent metainfo only — dicts, lists,
    ints, byte strings; nothing else is needed for a .torrent name)."""
    def dec(pos):
        head = data[pos:pos + 1]
        if head == b"d":
            out, pos = {}, pos + 1
            while data[pos:pos + 1] != b"e":
                key, pos = dec(pos)
                val, pos = dec(pos)
                out[key] = val
            return out, pos + 1
        if head == b"l":
            out, pos = [], pos + 1
            while data[pos:pos + 1] != b"e":
                val, pos = dec(pos)
                out.append(val)
            return out, pos + 1
        if head == b"i":
            end = data.index(b"e", pos)
            return int(data[pos + 1:end]), end + 1
        end = data.index(b":", pos)
        n = int(data[pos:end])
        start = end + 1
        return data[start:start + n], start + n
    if len(data) > (8 << 20):
        raise ValueError("torrent metainfo too large")
    val, _ = dec(0)
    return val


def _torrent_metainfo(info_hash: str):
    """Fetch a .torrent from a public magnet cache and return
    (torrent_name, largest_file_name) — the identity fallback when the
    streaming server cannot tell us the torrent's name."""
    try:
        resp = _session.get(
            "https://itorrents.org/torrent/%s.torrent" % info_hash,
            timeout=15)
        if resp.status_code != 200 or len(resp.content) < 16:
            return None
        meta = _bdecode(resp.content)
        info = meta.get(b"info") or {}
        tname = (info.get(b"name") or b"").decode("utf-8", "replace")
        best_len, best_name = -1, ""
        for f in info.get(b"files") or []:
            try:
                length = int(f.get(b"length") or 0)
                path = f.get(b"path") or []
                fname = path[-1].decode("utf-8", "replace") if path else ""
            except Exception:  # noqa: BLE001
                continue
            if length > best_len and fname:
                best_len, best_name = length, fname
        return tname, best_name
    except Exception as exc:  # noqa: BLE001
        log.info("stremio: metainfo lookup for %s failed: %r",
                 info_hash[:12], exc)
        return None


_CD_FILENAME_RE = re.compile(
    r"filename\*?=(?:UTF-8'')?([^\s;]+)", re.IGNORECASE)


def _content_disposition(url: str) -> str:
    """File name from a Content-Disposition header (one ranged byte,
    header only — never pulls the body). Debrid hosts and stream proxies
    name their files there even when the URL path is opaque."""
    try:
        resp = _session.get(url, headers={"Range": "bytes=0-0"},
                            timeout=8, stream=True)
        try:
            disp = resp.headers.get("Content-Disposition") or ""
        finally:
            resp.close()
        m = _CD_FILENAME_RE.search(disp)
        if not m:
            return ""
        name = m.group(1).strip('" ')
        from urllib.parse import unquote
        return unquote(name)
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# local streaming server

class StreamingServer:
    """The Stremio streaming server (stremio-service / desktop runtime).
    All calls are tolerant: any failure returns a falsy result and the
    caller falls back (direct URL playback, no autoplay, …)."""

    def __init__(self, base: str = ""):
        self.base = (base or DEFAULT_SERVER).rstrip("/")

    def health(self) -> bool:
        try:
            return _session.get(self.base + "/network-info",
                                timeout=4).status_code == 200
        except requests.RequestException:
            return False

    def stats(self, info_hash: str, file_idx: int):
        """Per-torrent stats (dict) or None. Used for the torrent NAME —
        the documented fields are counters, so any string field that looks
        like a name is accepted (shape is not contractual)."""
        try:
            resp = _session.get(
                "%s/%s/%d/stats.json" % (self.base, info_hash, file_idx),
                timeout=6)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    return data
        except Exception:  # noqa: BLE001
            pass
        return None

    def torrent_names(self, info_hash: str, file_idx: int):
        """Name candidates for a torrent the server is (or was) serving."""
        names = []
        for data in (self.stats(info_hash, file_idx), self._global_stats()):
            if data:
                names.extend(_scan_for_names(data))
        seen, out = set(), []
        for n in names:
            if n not in seen:
                seen.add(n)
                out.append(n)
        return out

    def _global_stats(self):
        try:
            resp = _session.get(self.base + "/stats.json", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    return data
        except Exception:  # noqa: BLE001
            pass
        return None

    def create(self, info_hash: str, trackers=()) -> bool:
        """Ask the server to start serving a torrent by info hash (the
        same POST stremio-core issues for magnet sources; peer discovery
        falls back to DHT when no trackers are passed)."""
        body = {}
        trackers = [t for t in trackers if t]
        if trackers:
            body = {"peerSearch": {
                "creation": 40,
                ">x": 200,
                "infoHash": info_hash,
                "sources": ["tracker:" + t for t in trackers[:12]],
            }}
        try:
            resp = _session.post(
                "%s/%s/create" % (self.base, info_hash), json=body,
                timeout=20)
            return 200 <= resp.status_code < 300
        except requests.RequestException as exc:
            log.warning("stremio: server create for %s failed: %r",
                        info_hash[:12], exc)
            return False

    def play_url(self, info_hash: str, file_idx: int) -> str:
        return "%s/%s/%d" % (self.base, info_hash, file_idx)


# ---------------------------------------------------------------------------
# Cinemeta (series metadata)

def series_meta(imdb_id: str):
    """Full series meta (with the ordered videos/episode list), cached."""
    if imdb_id in _meta_cache:
        return _meta_cache[imdb_id]
    try:
        resp = _session.get(
            "%s/meta/series/%s.json" % (CINEMETA, imdb_id), timeout=15)
        data = resp.json()
        meta = (data or {}).get("meta") or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("stremio: cinemeta meta %s failed: %r", imdb_id, exc)
        return {}
    if meta.get("name"):
        _meta_cache[imdb_id] = meta
    return meta


def search_series(name: str):
    """Catalog search -> list of {id, name} (best first). One quiet
    retry on a blip — Cinemeta occasionally reads slow."""
    if not name:
        return []
    for attempt in (0, 1):
        try:
            resp = _session.get(
                "%s/catalog/series/top/search=%s.json" % (CINEMETA,
                                                          name[:60]),
                timeout=20)
            metas = ((resp.json() or {}).get("metas")) or []
            break
        except requests.RequestException as exc:
            if not attempt:
                time.sleep(1.0)
                continue
            log.warning("stremio: cinemeta search %r failed: %r", name, exc)
            return []
        except Exception as exc:  # noqa: BLE001
            log.warning("stremio: cinemeta search %r failed: %r", name, exc)
            return []
    out = []
    for m in metas:
        if m.get("type") in (None, "series") and m.get("id") and \
                m.get("name"):
            out.append({"id": str(m["id"]), "name": str(m["name"])})
    return out


def find_series(name: str):
    """Best (imdb_id, canonical_name) for a show name, or None. Results
    must share a word with the query so a bad parse can't match a
    completely unrelated show; release-group/site prefix spam (which the
    cleaner can't know about) is handled by re-searching with leading
    words progressively dropped."""
    if not name:
        return None
    words = [w for w in re.split(r"\W+", name) if len(w) > 2]
    for trim in range(min(4, max(0, len(words) - 1))):
        query = " ".join(words[trim:])
        if not query:
            break
        wanted = {w for w in re.split(r"\W+", query.lower()) if len(w) > 2}
        for cand in search_series(query):
            cand_words = {w for w in re.split(r"\W+", cand["name"].lower())
                          if len(w) > 2}
            if cand_words & wanted:
                return cand["id"], cand["name"]
    return None


def _ordered_episodes(meta: dict, season: int):
    """The meta's episodes as a sorted (season, episode) list (specials /
    season 0 are skipped unless that is where the user already is)."""
    eps = set()
    for v in meta.get("videos") or []:
        try:
            s = int(str(v.get("season", 0)).strip() or 0)
            e = int(str(v.get("episode", 0)).strip() or 0)
        except (TypeError, ValueError):
            continue
        if s >= 0 and e >= 1 and (s > 0 or season == 0):
            eps.add((s, e))
    return sorted(eps)


def next_episode(meta: dict, season: int, episode: int):
    """The (season, episode) AFTER the given one in the meta's episode
    list (season finales roll into the next season; specials/season 0 are
    skipped unless that is where the user already is)."""
    ordered = _ordered_episodes(meta, season)
    try:
        idx = ordered.index((season, episode))
    except ValueError:
        return None
    return ordered[idx + 1] if idx + 1 < len(ordered) else None


def prev_episode(meta: dict, season: int, episode: int):
    """The (season, episode) BEFORE the given one — a season premiere
    rolls back to the previous season's finale; S01E01 has nothing
    before it."""
    ordered = _ordered_episodes(meta, season)
    try:
        idx = ordered.index((season, episode))
    except ValueError:
        return None
    return ordered[idx - 1] if idx > 0 else None


# ---------------------------------------------------------------------------
# stream addons (Torrentio-style)

def _addon_bases(config) -> list:
    raw = []
    try:
        raw = (config.data.get("stremio_addons")
               or ["https://torrentio.strem.fun"])
    except Exception:  # noqa: BLE001
        raw = ["https://torrentio.strem.fun"]
    out = []
    for base in raw:
        base = str(base or "").strip().rstrip("/")
        # users paste the manifest URL from Stremio's addon page — the
        # resource endpoints hang off everything BEFORE /manifest.json
        if base.endswith("/manifest.json"):
            base = base[: -len("/manifest.json")]
        base = base.rstrip("/")
        if base.startswith(("http://", "https://")) and base not in out:
            out.append(base)
    return out or ["https://torrentio.strem.fun"]


def addon_streams(config, imdb_id: str, season: int, episode: int):
    """Merged /stream/series/{imdb}:{s}:{e}.json results from every
    configured addon (add-ons answer 204/empty when they have nothing).
    One quiet retry on connection blips — a single ReadTimeout was seen
    live leaving a "no usable stream" dead end at end-of-episode."""
    streams = []
    for base in _addon_bases(config):
        url = "%s/stream/series/%s:%d:%d.json" % (base, imdb_id,
                                                  season, episode)
        for attempt in (1, 2):
            try:
                resp = _session.get(url, timeout=25)
                if resp.status_code != 200:
                    break
                data = resp.json()
                for s in (data or {}).get("streams") or []:
                    if isinstance(s, dict):
                        s = dict(s)
                        s["_addon"] = base
                        streams.append(s)
                break
            except requests.exceptions.HTTPError:
                break          # the addon answered — don't retry those
            except Exception as exc:  # noqa: BLE001
                if attempt == 1:
                    continue
                log.info("stremio: addon %s query failed: %r", base, exc)
    return streams


_RES_RE = re.compile(r"\b(2160|1440|1080|720|480|360)(?:p|i)?\b",
                     re.IGNORECASE)
_RES_ALIAS_RE = re.compile(r"\b(4k|8k|uhd)\b", re.IGNORECASE)
_SEEDS_RE = re.compile(r"\U0001F464\s*(\d+)")          # 👤 23
_SIZE_RE = re.compile(r"\U0001F4BE\s*([\d.]+)\s*(GB|MB)", re.IGNORECASE)


def _stream_parts(stream: dict):
    text = " ".join(str(stream.get(k) or "") for k in ("name", "title"))
    text += " " + str(((stream.get("behaviorHints") or {})
                       .get("filename")) or "")
    res = 0
    m = _RES_RE.search(text)
    if m:
        res = int(m.group(1))
    elif _RES_ALIAS_RE.search(text):
        res = 2160
    seeds = 0
    m = _SEEDS_RE.search(text)
    if m:
        seeds = int(m.group(1))
    size_gb = 0.0
    m = _SIZE_RE.search(text)
    if m:
        size_gb = float(m.group(1)) / \
            (1024.0 if m.group(2).upper() == "MB" else 1.0)
    return res, seeds, size_gb


def best_stream(config, streams: list):
    """Pick the stream to autoplay: playable sources only (a direct url,
    or infoHash+fileIdx for the local server), preferred resolution
    first, then seeders, with oversized rups demoted."""
    if not streams:
        return None
    try:
        prefer = int(config.data.get("stremio_prefer_resolution", 1080))
    except (TypeError, ValueError):
        prefer = 1080

    def usable(s):
        if s.get("url"):
            return True
        return bool(s.get("infoHash")) and s.get("fileIdx") is not None

    def score(s):
        res, seeds, size_gb = _stream_parts(s)
        # bucket 0 = exact preferred resolution, 1 = below it, 2 = above,
        # 3 = unknown; then sane size, then seeders; resolution only as a
        # tie-break (tuple sorts ascending, best first)
        res_fit = 0 if res == prefer else (1 if 0 < res < prefer
                                           else 2 if res else 3)
        size_pen = 1 if size_gb > 25 else 0
        return (res_fit, size_pen, -min(seeds, 500), -res)

    ranked = sorted([s for s in streams if usable(s)], key=score)
    return ranked[0] if ranked else None


# ---------------------------------------------------------------------------
# identity + playables

def resolve_identity(url: str, server: StreamingServer):
    """Figure out what a handed-off URL is playing:
    {series_imdb, series_name, season, episode, torrent_name, file_name}
    or None. Torrent name <- streaming server stats <- public .torrent
    cache; season/episode <- the episode file name, else the torrent name;
    the show <- a Cinemeta search on the cleaned name."""
    parsed = parse_server_url(url)
    file_name, torrent_name = "", ""
    if parsed:
        info_hash, file_idx = parsed
        for name in server.torrent_names(info_hash, file_idx):
            if not file_name and parse_se(name):
                file_name = name
            elif not torrent_name:
                torrent_name = name
            if file_name:
                break
        if not torrent_name and not file_name:
            mi = _torrent_metainfo(info_hash)
            if mi:
                torrent_name, file_name = mi
    else:
        # direct addon/debrid URL (Torbox / Premiumize / addon host): the
        # file name often rides in the path...
        tail = url.rstrip("/").split("/")[-1]
        tail = re.sub(r"\.(mkv|mp4|avi|ts|webm|strm)(\?.*)?$", "", tail,
                      flags=re.IGNORECASE)
        try:
            from urllib.parse import unquote
            tail = unquote(tail)
        except Exception:  # noqa: BLE001
            pass
        file_name = tail
        if not parse_se(file_name):
            # ...and when it doesn't, debrid/proxy servers still send it
            # in Content-Disposition (one ranged byte, header only)
            disp = _content_disposition(url)
            if disp:
                file_name = disp

    se = parse_se(file_name) or parse_se(torrent_name)
    if not se:
        log.info("stremio: no season/episode marker in %r / %r",
                 file_name[:60], torrent_name[:60])
        return None
    hit = find_series(clean_show_name(file_name or torrent_name))
    if not hit:
        log.info("stremio: catalog search found no show for %r",
                 clean_show_name(file_name or torrent_name))
        return None
    episode_name = ""
    try:
        # fetch (and cache) the series meta here on the worker: it
        # carries the episode's display name AND pre-warms the cache
        # next_playable needs minutes later at end-of-episode
        episode_name = _episode_title(series_meta(hit[0]),
                                      se[0], se[1])
    except Exception:  # noqa: BLE001
        pass
    return {
        "stremio_imdb": hit[0], "series_name": hit[1],
        "season": se[0], "episode": se[1],
        "episode_name": episode_name,
        "torrent_name": torrent_name, "file_name": file_name,
    }


def playable_from_url(url: str) -> dict:
    """The initial playable for a handed-off URL (identity fields are
    filled in later, in place, by resolve_identity)."""
    parsed = parse_server_url(url)
    if parsed:
        info_hash, file_idx = parsed
        return {
            "kind": "stremio",
            "title": "Stremio stream",
            "url": url,
            "fav_key": "stremio:%s:%d" % (info_hash[:16], file_idx),
            "icon": "",
            "info_hash": info_hash,
            "file_idx": file_idx,
        }
    resolved = parse_resolve_url(url)
    if resolved:
        # debrid resolve link: carries the torrent identity for the
        # local-server fallback when the debrid link stalls
        info_hash, file_idx = resolved
        return {
            "kind": "stremio",
            "title": "Stremio stream",
            "url": url,
            "fav_key": "stremio:%s:%d" % (info_hash[:16], file_idx),
            "icon": "",
            "info_hash": info_hash,
            "file_idx": file_idx,
        }
    import hashlib
    digest = hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()[:16]
    return {
        "kind": "stremio",
        "title": "Stremio stream",
        "url": url,
        "fav_key": "stremio:url:%s" % digest,
        "icon": "",
    }


def next_playable(config, cur: dict):
    """Worker-thread heart of autoplay: resolve the current playable's
    identity if missing, then find + prepare the NEXT episode's playable.
    Returns the playable, or None (nothing found)."""
    return _adjacent_playable(config, cur, +1)


def prev_playable(config, cur: dict):
    """Worker-thread backend of the ⏮ button: the same resolution /
    stream / server chain as next_playable, one episode EARLIER."""
    return _adjacent_playable(config, cur, -1)


def _adjacent_playable(config, cur: dict, step: int):
    server = StreamingServer(
        (config.data.get("stremio_server") if config else None) or "")

    imdb = cur.get("stremio_imdb")
    if not imdb:
        ident = resolve_identity(cur.get("url", ""), server)
        if not ident:
            return None
        cur.update(ident)
        imdb = cur["stremio_imdb"]
        log.info("stremio: identified %r -> %s S%02dE%02d",
                 ident.get("file_name", "")[:50] or imdb, imdb,
                 ident["season"], ident["episode"])

    season = int(cur.get("season") or 0)
    episode = int(cur.get("episode") or 0)
    meta = series_meta(imdb)
    if step > 0:
        nxt = next_episode(meta, season, episode)
    else:
        nxt = prev_episode(meta, season, episode)
    if not nxt:
        return None
    s, e = nxt

    streams = addon_streams(config, imdb, s, e)
    stream = best_stream(config, streams)
    if not stream:
        log.info("stremio: no usable stream for %s S%02dE%02d "
                 "(%d candidates)", imdb, s, e, len(streams))
        return None

    series_name = cur.get("series_name") or meta.get("name") or "Series"
    title = "%s \u2014 S%02dE%02d" % (series_name, s, e)
    ep_title = _episode_title(meta, s, e)
    if ep_title:
        title += " %s" % ep_title

    if stream.get("url"):
        url = stream["url"]
        info_hash, file_idx = None, None
    else:
        info_hash = str(stream["infoHash"]).lower()
        file_idx = int(stream.get("fileIdx") or 0)
        if not server.create(info_hash):
            log.warning("stremio: streaming server refused create for "
                        "%s — cannot autoplay", info_hash[:12])
            return None
        url = server.play_url(info_hash, file_idx)

    nxt_playable = {
        "kind": "stremio",
        "title": title,
        "url": url,
        "fav_key": "stremio:%s:%d:%d" % (imdb, s, e),
        "icon": meta.get("poster") or "",
        "stremio_imdb": imdb,
        "season": s,
        "episode": e,
        "series_name": series_name,
    }
    if info_hash:
        nxt_playable["info_hash"] = info_hash
        nxt_playable["file_idx"] = file_idx
    return nxt_playable


def _episode_title(meta: dict, season: int, episode: int) -> str:
    for v in meta.get("videos") or []:
        try:
            if int(v.get("season", -1)) == season and \
                    int(v.get("episode", -1)) == episode:
                # Cinemeta's series videos carry the episode name in
                # "name" ("title" is null there — that gap was why the
                # player showed bare "Show — S01E02" lines)
                return str(v.get("name") or v.get("title") or "")
        except (TypeError, ValueError):
            continue
    return ""
