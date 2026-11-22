"""Phone-side live HLS DVR relay for the Android app.

WHY THIS EXISTS (2026-11-22): the Windows player's DVR is a local growing
TS file; Android had none, so live TV could not be rewound and a Chromecast
could not be handed a seekable stream.  This module records the provider's
OWN HLS segments into the app cache, keeps a configurable window and
serves a local, seekable media playlist to BOTH the phone player and the
Cast receiver:

    provider m3u8 --(one HTTP connection, this module)--> local segments
       +--> http://127.0.0.1:<port>/s/<token>/live.m3u8   (phone)
       +--> http://<phone-lan-ip>:<port>/s/<token>/...    (Chromecast)

The segments are the provider's original MPEG-TS bytes (no decode, no
remux, no transcode), so picture and audio quality are bit-for-bit the
provider's.  The provider account allows ONE live connection; the relay
owns it and both players read locally.

Provider facts measured 2026-11-22: the live m3u8 302s to a per-request
host/token, the media playlist carries 6 x 10.009 s TS segments with
EXT-X-MEDIA-SEQUENCE, no encryption, and the segment URLs are absolute
paths under that final host.  A fresh playlist GET gets a fresh redirect,
which is how the ~15 min provider body cap is ridden transparently.

Failure policy: if the HLS is encrypted, fMP4/EXT-X-MAP, or never fills,
the relay raises ``RelayError`` and the bridge falls back to the direct
provider URL (playback works, DVR is unavailable for that channel).
"""

import os
import re
import shutil
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urljoin, urlsplit

import requests

USER_AGENT = "MichaelTVPlayer/1.0"
POLL_S = 4.0                 # playlist poll cadence (segments are 10 s)
START_SEGMENTS = 2           # 20 s of cushion before the player opens
MIN_SEGMENT_BYTES = 188 * 100
POLL_ERRORS_FATAL = 8        # consecutive playlist failures with no segments
FREE_FLOOR_BYTES = 512 * 1024 * 1024   # whole-session mode storage floor
_TS_SYNC = 0x47
_TARGET_FALLBACK = 11


class RelayError(Exception):
    """The relay cannot serve this stream (fall back to the direct URL)."""


def lan_ip():
    """The phone's LAN IPv4, or "" when it is not on a private network.

    A UDP connect() sends nothing; it only lets the kernel pick the
    source address of the default route.  Only private addresses are
    returned: a cellular (carrier NAT) address is unreachable from the
    TV and must not be advertised as a cast URL."""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = str(s.getsockname()[0] or "")
    except OSError:
        ip = ""
    finally:
        try:
            if s is not None:
                s.close()
        except OSError:
            pass
    if _is_private_ipv4(ip):
        return ip
    return ""


def _is_private_ipv4(ip: str) -> bool:
    try:
        parts = [int(p) for p in ip.split(".")]
    except (TypeError, ValueError):
        return False
    if len(parts) != 4 or any(p < 0 or p > 255 for p in parts):
        return False
    a, b = parts[0], parts[1]
    return (a == 10 or (a == 172 and 16 <= b <= 31)
            or (a == 192 and b == 168))


def _parse_playlist(text: str) -> dict:
    """Parse a media playlist (or detect a master and return variants).

    Only the tags the provider actually uses are interpreted; anything
    that would change the bytes on the way out (encryption, fMP4 init
    maps, byte ranges) is reported so the caller can fall back instead of
    serving a playlist that cannot play."""
    out = {"kind": "media", "variants": [], "target": 0, "media_seq": 0,
           "segments": [], "encrypted": False, "fmp4": False,
           "endlist": False}
    lines = [ln.strip() for ln in str(text or "").lstrip("\ufeff").splitlines()]
    pending_inf = None
    pending_disc = False
    expecting_variant = False
    pending_bw = 0
    for ln in lines:
        if not ln:
            continue
        if ln.startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"BANDWIDTH=(\d+)", ln)
            pending_bw = int(m.group(1)) if m else 0
            expecting_variant = True
            out["kind"] = "master"
            continue
        if ln.startswith("#EXT-X-KEY"):
            if "METHOD=NONE" not in ln.upper():
                out["encrypted"] = True
            continue
        if ln.startswith("#EXT-X-MAP"):
            out["fmp4"] = True
            continue
        if ln.startswith("#EXT-X-BYTERANGE"):
            out["fmp4"] = True      # sub-range segments: not supported
            continue
        if ln.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                out["media_seq"] = int(ln.split(":", 1)[1])
            except ValueError:
                out["media_seq"] = 0
            continue
        if ln.startswith("#EXT-X-TARGETDURATION:"):
            try:
                out["target"] = int(float(ln.split(":", 1)[1]))
            except ValueError:
                out["target"] = 0
            continue
        if ln.startswith("#EXT-X-DISCONTINUITY"):
            pending_disc = True
            continue
        if ln.startswith("#EXT-X-ENDLIST"):
            out["endlist"] = True
            continue
        if ln.startswith("#EXTINF:"):
            try:
                pending_inf = float(ln.split(":", 1)[1].split(",", 1)[0])
            except ValueError:
                pending_inf = 0.0
            continue
        if ln.startswith("#"):
            continue
        # a URI line
        if expecting_variant:
            out["variants"].append((pending_bw, ln))
            expecting_variant = False
            pending_bw = 0
            continue
        if pending_inf is not None:
            out["segments"].append((float(pending_inf), ln, pending_disc))
            pending_inf = None
            pending_disc = False
    if out["kind"] == "master":
        out["variants"].sort(key=lambda v: v[0])
    return out


class LiveRelay:
    def __init__(self, hls_url: str, cache_dir: str, window_s: int = 1800,
                 cast_ip: str = None, token: str = None):
        self.hls_url = str(hls_url)
        self.window_s = max(0, int(window_s or 0))
        self.token = token or uuid.uuid4().hex
        self.cast_ip = cast_ip if cast_ip is not None else lan_ip()
        self._root = os.path.join(str(cache_dir), "live_dvr")
        self._dir = os.path.join(self._root, self.token)
        self._session = requests.Session()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._segments = []        # {local, seq, dur, path, disc}
        self._next_local = 0
        self._last_provider_seq = None
        self._target = _TARGET_FALLBACK
        self._error = ""
        self._stale_since = None
        self._last_ok = 0.0
        self._thread = None
        self._httpd = None
        self._server_thread = None
        self._port = 0

    # ---------------------------------------------------------- lifecycle
    def start(self, timeout_s: float = 20.0):
        shutil.rmtree(self._dir, ignore_errors=True)
        os.makedirs(self._dir, exist_ok=True)
        self._httpd = _Server(("0.0.0.0", 0), _Handler)
        self._httpd.relay = self
        self._httpd.daemon_threads = True
        self._port = int(self._httpd.server_address[1])
        self._server_thread = threading.Thread(
            target=self._httpd.serve_forever, name="mtp-hls-http",
            daemon=True)
        self._server_thread.start()
        self._thread = threading.Thread(
            target=self._poll_loop, name="mtp-hls-poll", daemon=True)
        self._thread.start()
        self._ready.wait(max(1.0, float(timeout_s)))
        if self._error and not self._segments:
            err = self._error
            self.stop()
            raise RelayError(err)
        if len(self._segments) < START_SEGMENTS:
            err = self._error or "timed out waiting for the first segments"
            self.stop()
            raise RelayError(err)
        return self

    def stop(self):
        self._stop.set()
        self._ready.set()
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            try:
                threading.Thread(target=httpd.shutdown, daemon=True).start()
            except Exception:  # noqa: BLE001
                pass
            try:
                httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=3.0)
        try:
            self._session.close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self._dir, ignore_errors=True)

    # ------------------------------------------------------------- urls
    @property
    def local_url(self) -> str:
        return ("http://127.0.0.1:%d/s/%s/live.m3u8"
                % (self._port, self.token))

    @property
    def cast_url(self) -> str:
        if not self.cast_ip:
            return ""
        return ("http://%s:%d/s/%s/live.m3u8"
                % (self.cast_ip, self._port, self.token))

    def status(self) -> dict:
        with self._lock:
            buffered = sum(s["dur"] for s in self._segments)
            n = len(self._segments)
            stale = (0.0 if not self._segments
                     else max(0.0, time.time() - self._last_ok))
            return {
                "running": not self._stop.is_set(),
                "ready": self._ready.is_set() and n >= START_SEGMENTS,
                "segments": n,
                "buffered_s": round(buffered, 1),
                "window_s": self.window_s,
                "local_url": self.local_url,
                "cast_url": self.cast_url,
                "error": self._error,
                "stale_s": round(stale, 1),
            }

    # -------------------------------------------------------- provider IO
    def _fetch_playlist(self, url: str = None):
        resp = self._session.get(
            url or self.hls_url, headers={"User-Agent": USER_AGENT},
            timeout=(5, 15), allow_redirects=True)
        if resp.status_code != 200:
            raise RelayError("playlist HTTP %d" % resp.status_code)
        return resp.text, resp.url

    def _download_segment(self, url: str) -> str:
        tmp = os.path.join(self._dir, ".part-%s" % uuid.uuid4().hex)
        resp = self._session.get(
            url, headers={"User-Agent": USER_AGENT},
            timeout=(5, 30), stream=True, allow_redirects=True)
        if resp.status_code != 200:
            raise RelayError("segment HTTP %d" % resp.status_code)
        written = 0
        first = b""
        try:
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_content(256 * 1024):
                    if not chunk:
                        continue
                    if not first:
                        first = chunk[:1]
                    fh.write(chunk)
                    written += len(chunk)
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
        if first != bytes([_TS_SYNC]) or written < MIN_SEGMENT_BYTES:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise RelayError("segment is not MPEG-TS (%d bytes)" % written)
        return tmp

    def _poll_loop(self):
        playlist_url = self.hls_url
        errors = 0
        while not self._stop.is_set():
            try:
                text, final_url = self._fetch_playlist(playlist_url)
                parsed = _parse_playlist(text)
                if parsed["encrypted"]:
                    raise RelayError("provider HLS is encrypted")
                if parsed["fmp4"]:
                    raise RelayError("provider HLS uses fMP4/byte ranges")
                if parsed["kind"] == "master":
                    if not parsed["variants"]:
                        raise RelayError("master playlist has no variants")
                    # Highest bandwidth = the provider's full-quality feed.
                    variant = urljoin(final_url,
                                      parsed["variants"][-1][1])
                    if variant in (final_url, playlist_url):
                        raise RelayError("nested master playlist (%s -> %s)" % (final_url, variant))
                    playlist_url = variant
                    continue
                if not parsed["segments"]:
                    raise RelayError("media playlist has no segments")
                playlist_url = self.hls_url   # re-resolve tokens every poll
                self._target = parsed["target"] or _TARGET_FALLBACK
                self._ingest(parsed, final_url)
                errors = 0
                with self._lock:
                    self._error = ""
                    self._last_ok = time.time()
                    if len(self._segments) >= START_SEGMENTS:
                        self._ready.set()
            except RelayError as exc:
                with self._lock:
                    self._error = str(exc)
                if not self._segments:
                    self._fail(str(exc))
                    return
                errors += 1
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._error = "%s: %s" % (type(exc).__name__, exc)
                if not self._segments:
                    errors += 1
                    if errors >= POLL_ERRORS_FATAL:
                        self._fail(self._error)
                        return
        # loop
            self._stop.wait(POLL_S)

    def _ingest(self, parsed: dict, final_url: str):
        segs = parsed["segments"]
        seq0 = int(parsed["media_seq"])
        last_seq = seq0 + len(segs) - 1
        if self._last_provider_seq is None:
            # First fill: take the newest START_SEGMENTS only, so playback
            # starts in ~20 s instead of downloading the whole 60 s window.
            pick = segs[-START_SEGMENTS:]
            base = seq0 + len(segs) - len(pick)
            with self._lock:
                self._last_provider_seq = last_seq
            for off, (dur, uri, disc) in enumerate(pick):
                self._store(base + off, dur, uri, final_url, disc)
            return
        if last_seq < self._last_provider_seq:
            # The provider restarted its sequence (body cap / encoder
            # restart).  Keep the local timeline monotonic and mark a
            # discontinuity so the player resets its timestamps.  Only the
            # newest two segments are downloaded: the rest of the fresh
            # body predates the reset and would just be pruned.
            full = segs
            segs = [(d, u, True) for (d, u, _x) in full][-START_SEGMENTS:]
            seq0 = int(parsed["media_seq"]) + len(full) - len(segs)
            with self._lock:
                self._last_provider_seq = seq0 - 1
        for off, (dur, uri, disc) in enumerate(segs):
            seq = seq0 + off
            if seq <= self._last_provider_seq:
                continue
            # A provider window that skipped a sequence (a slow poll or a
            # CDN gap) means the local timeline jumps; tell the player.
            gap = seq > (self._last_provider_seq or seq) + 1
            try:
                self._store(seq, dur, uri, final_url, disc or gap)
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._error = "segment %d: %s" % (seq, exc)
                # stop at the gap; the next poll re-lists the same segment
                return

    def _store(self, seq: int, dur: float, uri: str, base: str, disc: bool):
        url = urljoin(base, uri)
        tmp = self._download_segment(url)
        with self._lock:
            local = self._next_local
            self._next_local += 1
            dst = os.path.join(self._dir, "seg_%d.ts" % local)
            os.replace(tmp, dst)
            self._segments.append({
                "local": local, "seq": int(seq), "dur": float(dur),
                "path": dst, "disc": bool(disc),
            })
            self._last_provider_seq = max(
                int(seq), self._last_provider_seq or int(seq))
        self._prune()

    def _prune(self):
        with self._lock:
            total = sum(s["dur"] for s in self._segments)
            drop = []
            low_disk = False
            try:
                low_disk = (shutil.disk_usage(self._dir).free
                            < FREE_FLOOR_BYTES)
            except OSError:
                low_disk = False
            while total > 0 and len(self._segments) > START_SEGMENTS:
                over_window = (self.window_s > 0
                               and total > float(self.window_s))
                if not (over_window or low_disk):
                    break
                old = self._segments.pop(0)
                total -= old["dur"]
                drop.append(old)
        for old in drop:
            try:
                os.unlink(old["path"])
            except OSError:
                pass

    def _fail(self, message: str):
        with self._lock:
            self._error = str(message)
        self._ready.set()

    # ---------------------------------------------------------- playlist
    def playlist_text(self) -> str:
        with self._lock:
            segs = list(self._segments)
            target = max(1, int(self._target or _TARGET_FALLBACK))
        lines = ["#EXTM3U", "#EXT-X-VERSION:3",
                 "#EXT-X-TARGETDURATION:%d" % target]
        if segs:
            lines.append("#EXT-X-MEDIA-SEQUENCE:%d" % segs[0]["local"])
        else:
            lines.append("#EXT-X-MEDIA-SEQUENCE:0")
        for s in segs:
            if s["disc"]:
                lines.append("#EXT-X-DISCONTINUITY")
            lines.append("#EXTINF:%.6f," % s["dur"])
            lines.append("seg/%d.ts" % s["local"])
        return "\n".join(lines) + "\n"

    def segment_path(self, local: int):
        with self._lock:
            for s in self._segments:
                if s["local"] == int(local) and os.path.isfile(s["path"]):
                    return s["path"]
        return None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003 — silence per-segment spam
        pass

    def _relay(self) -> LiveRelay:
        return self.server.relay  # type: ignore[attr-defined]

    def _split(self):
        path = unquote(urlsplit(self.path).path)
        parts = [p for p in path.split("/") if p]
        return parts

    def _send(self, code, ctype, body=b"", extra=None, length=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length",
                         str(len(body) if length is None else length))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_HEAD(self):
        try:
            self._route(head=True)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_GET(self):
        try:
            self._route(head=False)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _route(self, head: bool):
        relay = self._relay()
        parts = self._split()
        if len(parts) < 3 or parts[0] != "s" or parts[1] != relay.token:
            self._send(404, "text/plain", b"not found")
            return
        what = parts[2]
        if what == "live.m3u8":
            body = relay.playlist_text().encode("utf-8")
            self._send(200, "application/vnd.apple.mpegurl",
                       b"" if head else body)
            return
        if what == "status":
            import json as _json
            body = _json.dumps(relay.status()).encode("utf-8")
            self._send(200, "application/json", b"" if head else body)
            return
        if what == "seg" and len(parts) >= 4:
            name = parts[3]
            m = re.match(r"^(\d+)\.ts$", name)
            if not m:
                self._send(404, "text/plain", b"bad segment")
                return
            path = relay.segment_path(int(m.group(1)))
            if not path:
                self._send(404, "text/plain", b"segment expired")
                return
            try:
                size = os.path.getsize(path)
                start, end = 0, size - 1
                rng = self.headers.get("Range") or ""
                mrange = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
                if mrange:
                    if mrange.group(1):
                        start = int(mrange.group(1))
                    if mrange.group(2):
                        end = min(end, int(mrange.group(2)))
                    if start >= size or start > end:
                        self._send(416, "text/plain", b"range",
                                   {"Content-Range": "bytes */%d" % size})
                        return
                length = end - start + 1
                extra = {"Accept-Ranges": "bytes"}
                code = 200
                if rng:
                    code = 206
                    extra["Content-Range"] = "bytes %d-%d/%d" % (
                        start, end, size)
                if head:
                    self._send(code, "video/mp2t", b"", extra, length)
                    return
                with open(path, "rb") as fh:
                    fh.seek(start)
                    remaining = length
                    self.send_response(code)
                    self.send_header("Content-Type", "video/mp2t")
                    self.send_header("Content-Length", str(length))
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache, no-store")
                    if rng:
                        self.send_header("Content-Range",
                                         "bytes %d-%d/%d" % (start, end, size))
                    self.end_headers()
                    try:
                        while remaining > 0:
                            chunk = fh.read(min(256 * 1024, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                return
            except OSError:
                self._send(404, "text/plain", b"segment gone")
                return
        self._send(404, "text/plain", b"not found")


class _Server(ThreadingHTTPServer):
    """A player that seeks/restarts closes its socket mid-body; that is
    normal here and must not dump tracebacks into the Android log."""

    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, OSError)):
            return
        super().handle_error(request, client_address)


# ---------------------------------------------------------------- module
_current_lock = threading.Lock()
_current = None


def start(hls_url: str, cache_dir: str, window_s: int = 1800,
          cast_ip: str = None, timeout_s: float = 20.0) -> LiveRelay:
    """Start a relay for ``hls_url``, replacing any previous one.

    Raises ``RelayError`` when the stream cannot be relayed; the caller
    falls back to the direct URL."""
    global _current
    stop_current()
    relay = LiveRelay(hls_url, cache_dir, window_s=window_s,
                      cast_ip=cast_ip)
    try:
        relay.start(timeout_s=timeout_s)
    except Exception:
        relay.stop()
        raise
    with _current_lock:
        _current = relay
    return relay


def stop_current(token: str = None):
    """Stop the current relay, or only the one matching ``token``.

    The token matters on channel change: the OLD PlayerActivity's teardown
    runs on a background thread and can arrive AFTER the new channel's
    relay started.  Without the token it would kill the new relay (and
    the picture) moments after the switch."""
    global _current
    with _current_lock:
        relay = _current
        if relay is None:
            return
        if token and relay.token != token:
            return
        _current = None
    try:
        relay.stop()
    except Exception:  # noqa: BLE001
        pass


def status() -> dict:
    with _current_lock:
        relay = _current
    if relay is None:
        return {"running": False}
    try:
        return relay.status()
    except Exception as exc:  # noqa: BLE001
        return {"running": False, "error": repr(exc)}
