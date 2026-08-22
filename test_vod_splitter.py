# -*- coding: utf-8 -*-
"""Tests for the VOD splitter: relay correctness + subtitle extraction.

Builds small REAL containers with ffmpeg — MKVs (video + SRT subs,
video + ASS subs) served over file://, and MP4s (video + mov_text subs,
streaming layout AND faststart) served over a Range-capable local HTTP
provider — then checks: byte fidelity, Range requests, the content-type
the relay claims, VLC playback + seeking through the relay, and both
subtitle taps' cues (MKV streaming parser, MP4 moov-index tap).

Run:  .venv\\Scripts\\python.exe test_vod_splitter.py
"""
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtCore, QtWidgets  # noqa: E402

from src.player import VLCPlayer  # noqa: E402
from src.profanity import find_ffmpeg  # noqa: E402
from src import vod_splitter  # noqa: E402
from src.vod_splitter import VodRelay  # noqa: E402

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


FF = find_ffmpeg()
MKV = os.path.abspath("build/split_test.mkv").replace("\\", "/")
SRT_IN = os.path.abspath("build/split_test_in.srt").replace("\\", "/")
MP4 = os.path.abspath("build/split_test.mp4").replace("\\", "/")
MP4_FS = os.path.abspath("build/split_test_fs.mp4").replace("\\", "/")
SRT_M_IN = os.path.abspath("build/split_test_mp4_in.srt").replace("\\", "/")
TAIL_MKV = os.path.abspath("build/split_test_tail.mkv").replace("\\", "/")
TAIL_SRT_IN = os.path.abspath("build/split_test_tail_in.srt").replace("\\", "/")
SCALE_MKV = os.path.abspath("build/split_test_scale.mkv").replace("\\", "/")
ELST_MP4 = os.path.abspath("build/split_test_elst.mp4").replace("\\", "/")


def build_sample():
    os.makedirs("build", exist_ok=True)
    with open(SRT_IN, "w", encoding="utf-8") as f:
        f.write("1\n00:00:01,000 --> 00:00:03,000\n"
                "what the hell is this\n\n"
                "2\n00:00:05,000 --> 00:00:07,000\n"
                "clean as snow\n\n"
                "3\n00:00:09,000 --> 00:00:11,000\n"
                "damn dogs everywhere\n")
    subprocess.run(
        [FF, "-y", "-v", "error",
         "-f", "lavfi", "-i", "color=c=steelblue:s=256x144:d=12:r=10",
         "-i", SRT_IN,
         "-map", "0:v", "-map", "1:s", "-c:v", "libx264",
         "-preset", "ultrafast", "-c:s", "srt", MKV],
        check=True, timeout=120, creationflags=0x08000000)


def build_sample_mp4():
    """Video + mov_text subs: the plain streaming layout (moov at the
    end, like real providers serve) and a +faststart variant (moov at
    the head). Cue 2 is two lines to prove line breaks survive."""
    with open(SRT_M_IN, "w", encoding="utf-8") as f:
        f.write("1\n00:00:01,000 --> 00:00:03,000\n"
                "what the hell is this\n\n"
                "2\n00:00:05,000 --> 00:00:07,000\n"
                "clean as snow\nand twice as bright\n\n"
                "3\n00:00:09,000 --> 00:00:11,000\n"
                "damn dogs everywhere\n")
    for dst, extra in ((MP4, []), (MP4_FS, ["-movflags", "+faststart"])):
        subprocess.run(
            [FF, "-y", "-v", "error",
             "-f", "lavfi", "-i", "color=c=seagreen:s=256x144:d=12:r=10",
             "-i", SRT_M_IN,
             "-map", "0:v", "-map", "1:s", "-c:v", "libx264",
             "-preset", "ultrafast", "-c:s", "mov_text",
             "-metadata:s:s:0", "language=eng"] + extra + [dst],
            check=True, timeout=120, creationflags=0x08000000)


def build_tail_sample():
    """A ~4.7 MB MKV whose late cues live INSIDE the 2.5 MB tail prefetch:
    noise + CBR keeps the bitrate (and therefore byte offsets) predictable,
    so the t>=60s subtitle clusters land past total-_TAIL_PREFETCH while
    the t=1s cue stays inside the 512 KB head-ride region. This is the
    regression shape for "MKV captions stop in the last ~2.5 MB"."""
    with open(TAIL_SRT_IN, "w", encoding="utf-8") as f:
        f.write("1\n00:00:01,000 --> 00:00:03,000\n"
                "early head cue\n\n"
                "2\n00:01:00,000 --> 00:01:02,000\n"
                "late tail alpha\n\n"
                "3\n00:01:03,000 --> 00:01:05,000\n"
                "late tail beta\n\n"
                "4\n00:01:26,000 --> 00:01:28,000\n"
                "late tail gamma\n")
    subprocess.run(
        [FF, "-y", "-v", "error",
         "-f", "lavfi", "-i", "color=c=steelblue:s=256x144:d=90:r=10",
         "-i", TAIL_SRT_IN,
         "-vf", "noise=alls=18:allf=t",
         "-map", "0:v", "-map", "1:s", "-c:v", "libx264",
         "-preset", "ultrafast",
         "-b:v", "420k", "-minrate", "420k", "-maxrate", "420k",
         "-bufsize", "84k", "-x264-params", "nal-hrd=cbr",
         "-c:s", "srt", TAIL_MKV],
        check=True, timeout=300, creationflags=0x08000000)


def build_scale_sample():
    """Copy of the base MKV with Info/TimestampScale patched 1e6 -> 2e6
    ns (same 3-byte width, so no element shifts): every cluster/block
    timecode now means 2 ms, so a scale-honoring parser must report the
    1 s SRT cue at 2.0 s; one that hardcodes milliseconds says 1.0 s."""
    blob = bytearray(open(MKV, "rb").read())
    pat = b"\x2a\xd7\xb1\x83\x0f\x42\x40"      # TimecodeScale, 1e6
    i = bytes(blob).find(pat)
    assert i > 0, "no default TimestampScale element found to patch"
    blob[i + 4:i + 7] = (2_000_000).to_bytes(3, "big")
    with open(SCALE_MKV, "wb") as f:
        f.write(blob)


def build_elst_sample():
    """Plain mov_text MP4 with the TEXT trak's elst media_time patched
    0 -> 0.7 s worth of media ticks (same field width, no size changes):
    an edit-list-honoring parser must shift every cue 0.7 s earlier
    (1.0 s SRT cue -> 0.3 s); one that ignores elst says 1.0 s. ffmpeg
    itself always writes media_time=0, hence the patch."""
    with open(SRT_M_IN, "w", encoding="utf-8") as f:
        f.write("1\n00:00:01,000 --> 00:00:03,000\n"
                "what the hell is this\n\n"
                "2\n00:00:05,000 --> 00:00:07,000\n"
                "clean as snow\n\n"
                "3\n00:00:09,000 --> 00:00:11,000\n"
                "damn dogs everywhere\n")
    subprocess.run(
        [FF, "-y", "-v", "error",
         "-f", "lavfi", "-i", "color=c=seagreen:s=256x144:d=12:r=10",
         "-i", SRT_M_IN,
         "-map", "0:v", "-map", "1:s", "-c:v", "libx264",
         "-preset", "ultrafast", "-c:s", "mov_text",
         "-metadata:s:s:0", "language=eng", ELST_MP4],
        check=True, timeout=120, creationflags=0x08000000)
    from src.mp4_subs import _boxes
    blob = bytearray(open(ELST_MP4, "rb").read())
    patched = 0
    for typ, s, e in _boxes(blob, 0, len(blob)):
        if typ != b"moov":
            continue
        for t2, s2, e2 in _boxes(blob, s, e):
            if t2 != b"trak":
                continue
            hdlr = stsd = b""
            timescale = 0
            elst_at = None
            for t3, s3, e3 in _boxes(blob, s2, e2):
                if t3 == b"edts":
                    for t4, s4, e4 in _boxes(blob, s3, e3):
                        if t4 == b"elst":
                            elst_at = s4
                elif t3 != b"mdia":
                    continue
                for t4, s4, e4 in _boxes(blob, s3, e3):
                    if t4 == b"hdlr":
                        hdlr = bytes(blob[s4 + 8:s4 + 12])
                    elif t4 == b"mdhd":
                        off = 12 if blob[s4] == 0 else 20
                        timescale = int.from_bytes(
                            blob[s4 + off:s4 + off + 4], "big")
                    elif t4 == b"minf":
                        for t5, s5, e5 in _boxes(blob, s4, e4):
                            if t5 != b"stbl":
                                continue
                            for t6, s6, e6 in _boxes(blob, s5, e5):
                                if t6 == b"stsd":
                                    stsd = bytes(blob[s6 + 12:s6 + 16])
            if (hdlr not in (b"sbtl", b"text", b"subt")
                    and stsd != b"tx3g") or elst_at is None:
                continue
            assert timescale > 0, "no mdhd timescale for the text trak"
            ticks = int(0.7 * timescale)      # 0.7 s in media ticks
            if blob[elst_at] == 0:
                blob[elst_at + 12:elst_at + 16] = ticks.to_bytes(4, "big")
            else:
                blob[elst_at + 16:elst_at + 24] = ticks.to_bytes(8, "big")
            patched += 1
    assert patched == 1, f"expected one text-trak elst, patched {patched}"
    with open(ELST_MP4, "wb") as f:
        f.write(blob)


def get(url, rng=None):
    req = urllib.request.Request(url)
    if rng:
        req.add_header("Range", rng)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read()


def get_h(url, rng=None):
    """get() + response headers (content-type checks)."""
    req = urllib.request.Request(url)
    if rng:
        req.add_header("Range", rng)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read(), r.headers


class _ProviderHandler(BaseHTTPRequestHandler):
    """Test provider: a static blob with real Range support, the way a
    real VOD provider behaves (the relay's probe/tail/seek traffic all
    depend on ranged responses)."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_HEAD(self):
        blob = self.server.blob
        self.send_response(200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", self.server.ctype)
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()

    def do_GET(self):
        blob = self.server.blob
        self.server.hits += 1
        rng = self.headers.get("Range") or ""
        a = b = None
        if rng.startswith("bytes="):
            spec = rng[len("bytes="):]
            if spec.startswith("-"):
                n = int(spec[1:])
                a, b = max(0, len(blob) - n), len(blob) - 1
            else:
                a_s, _, b_s = spec.partition("-")
                a = int(a_s)
                b = int(b_s) if b_s else len(blob) - 1
        if a is None:
            chunk, code, cr = blob, 200, None
        else:
            chunk, code = blob[a:b + 1], 206
            cr = f"bytes {a}-{b}/{len(blob)}"
        self.send_response(code)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", self.server.ctype)
        if cr is not None:
            self.send_header("Content-Range", cr)
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        self.wfile.write(chunk)


class _Provider(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, blob, ctype):
        super().__init__(("127.0.0.1", 0), _ProviderHandler)
        self.blob = blob
        self.ctype = ctype
        self.hits = 0


def main():
    assert FF, "ffmpeg not found"
    build_sample()
    build_sample_mp4()
    build_tail_sample()
    build_scale_sample()
    build_elst_sample()
    sample = open(MKV, "rb").read()
    app = QtWidgets.QApplication(sys.argv)

    print("[1] relay starts on MKV and refuses non-MKV")
    relay = VodRelay()
    cues = []
    relay.cue.connect(lambda s, e, t: cues.append((s, e, t)))
    local = relay.start("file:///" + MKV.replace("\\", "/"),
                        "MichaelTVPlayer/1.0")
    check("mkv accepted, local URL returned",
          local.startswith("http://127.0.0.1:"))
    # Deterministic track discovery: the head fetch runs during the
    # async startup — before any request is served (handlers wait for
    # the startup event), so no request can rebase the cache window away
    # from the head before the metadata exists. Without it the tap races
    # VLC's Cues-index connection for the Tracks element; the loser
    # selects no track and emits nothing for the whole session ("no
    # subtitles on movies").
    deadline = time.time() + 10
    while time.time() < deadline and not relay.parser_tracks:
        app.processEvents()
        time.sleep(0.1)
    check("track metadata ready before first serve (head fetch)",
          bool(relay.parser_tracks) and relay.parser_selected is not None)
    check("tail prefetch covers VLC's ~2.3 MB Cues-index request",
          vod_splitter._TAIL_PREFETCH >= (2300 * 1000))
    relay2 = VodRelay()
    check("non-mkv refused (falls back to direct)",
          relay2.start("file:///" + os.path.abspath("README.md")
                       .replace("\\", "/"), "MichaelTVPlayer/1.0") == "")
    relay2.stop()

    print("[2] byte fidelity + range requests through the relay")
    opens_before = relay.provider_opens
    status, body, hdrs = get_h(local)
    check("full GET streams the whole file",
          status == 200 and body[:4] == b"\x1a\x45\xdf\xa3")
    check("mkv relay serves video/x-matroska",
          (hdrs.get("Content-Type") or "") == "video/x-matroska")
    # the provider stream is consumed lazily; the tap feeds from the
    # cache. Pull everything first so the cache is complete.
    deadline = time.time() + 30
    while time.time() < deadline and len(body) < len(sample):
        time.sleep(0.5)
        _, body = get(local)
    check("relayed bytes identical to the source file",
          body == sample)
    check("one provider connection serves the whole file "
          "(no reopen-per-chunk)",
          relay.provider_opens - opens_before <= 1)
    status, part = get(local, rng=f"bytes=100-199")
    check("range request served from cache",
          status == 206 and part == sample[100:200])
    status, part = get(local, rng=f"bytes={len(sample)-50}-")
    check("open-ended range",
          status == 206 and part == sample[-50:])

    print("[3] subtitle tap extracts the embedded cues")
    deadline = time.time() + 25
    while time.time() < deadline and len(cues) < 3:
        app.processEvents()
        time.sleep(0.25)
    texts = [c[2] for c in cues]
    check("all three cues extracted",
          len(cues) >= 3 and "what the hell is this" in texts
          and "damn dogs everywhere" in texts)
    hell = next(c for c in cues if "hell" in c[2])
    check("cue timing matches the source srt",
          abs(hell[0] - 1.0) < 0.3
          and abs(hell[1] - 3.0) < 0.3)   # BlockDuration = the real end

    print("[3b] unit: text-codec detection + ASS flattening")
    from src.mkv_subs import MkvSubParser, flatten_ass_text, is_text_codec

    check("text codecs detected",
          is_text_codec("S_TEXT/UTF8") and is_text_codec("S_TEXT/ASS")
          and is_text_codec("S_TEXT/SSA") and is_text_codec("S_ASS"))
    check("bitmap codecs rejected",
          not is_text_codec("S_HDMV/PGS") and not is_text_codec("S_VOBSUB")
          and not is_text_codec(""))
    check("override blocks stripped",
          flatten_ass_text(r"{\an8}Hello {\k20}world") == "Hello world")
    check("hard line breaks become newlines",
          flatten_ass_text("one\\Ntwo\\ntwo and a half\\h!")
          == "one\ntwo\ntwo and a half !")
    check("drawing payloads dropped",
          flatten_ass_text("{\\p1}m 0 0 l 10 10{\\p0}word") == "word")
    check("full dialogue line reduced to its text",
          flatten_ass_text("Dialogue: 0,0:00:01.00,0:00:02.00,"
                           "Default,,0,0,0,,Real, commas, kept")
          == "Real, commas, kept")
    check("ffmpeg field-style payload reduced to its text",
          flatten_ass_text("0,0,Default,,0,0,0,,what the hell is this")
          == "what the hell is this")
    check("bare full dialogue (10 fields) reduced to its text",
          flatten_ass_text("0,0:00:01.00,0:00:02.00,Default,,0,0,0,"
                           ",what the hell is this")
          == "what the hell is this")
    check("plain comma text left alone",
          flatten_ass_text("one, two, three") == "one, two, three")
    check("comment events skipped",
          flatten_ass_text("Comment: 0,0:00:01.00,0:00:02.00,"
                           "Default,,0,0,0,,no") == "")
    check("plain srt text passes through clean",
          flatten_ass_text(" <i>what the hell</i> ") == "what the hell")
    check("bidi wrappers stripped from Latin lines (rip-house styling)",
          flatten_ass_text("\u202b\xa0[dramatic\xa0music]")
          == "[dramatic music]")
    check("Arabic lines keep their bidi controls",
          flatten_ass_text("\u202bمرحبا\xa0بكم") == "\u202bمرحبا\xa0بكم")

    p = MkvSubParser()
    p._track_meta = {
        1: {"codec": "S_HDMV/PGS", "lang": "eng", "name": ""},
        2: {"codec": "S_TEXT/ASS", "lang": "eng", "name": "forced"},
    }
    p._select_track()
    check("ASS track selected over bitmap", p._selected == 2)

    print("[3b2] rebase keeps the tap alive (metadata carry)")
    # After a cache rebase the window no longer contains the Tracks
    # element (it lives only at the file head), so the fresh mid-stream
    # parser must be SEEDED with the snapshotted metadata — exactly what
    # _tap_cache does. Reproduce the contract here: parse the head, then
    # feed mid-file cluster bytes to a seeded mid-stream parser.
    head = sample[:1 << 16]
    phead = MkvSubParser(prefer_language="eng")
    phead.feed(head)
    check("head parse selects the text track",
          phead._selected is not None and phead._track_meta)
    cluster_off = sample.find(b"\x1f\x43\xb6\x75", 512)
    check("sample has a cluster past the head elements", cluster_off > 0)
    seeded = MkvSubParser(prefer_language="eng", mid_stream=True)
    seeded._track_meta = {n: dict(m) for n, m in phead._track_meta.items()}
    seeded._select_track()
    seeded._saw_tracks = True
    seeded._selected = phead._selected
    made = seeded.feed(sample[cluster_off:])
    check("seeded mid-stream parser extracts cues from mid-file bytes",
          any("hell" in c[2] or "dogs" in c[2] or "snow" in c[2]
              for c in made))
    plain = MkvSubParser(mid_stream=True)      # UNSEEDED: the old bug
    plain_made = plain.feed(sample[cluster_off:])
    check("unseeded mid-stream parser stays trackless (regression proof)",
          plain._selected is None and not plain_made)

    print("[3b-z] resync over a magic-less stretch returns (no spin)")
    # A zero/garbage region with no Cluster header used to wedge _parse
    # in a tight loop over a 4-byte buffer (core at 100%, tap dead).
    z = MkvSubParser(prefer_language="eng", mid_stream=True)
    z._track_meta = {n: dict(m) for n, m in phead._track_meta.items()}
    z._select_track()
    z._saw_tracks = True
    z._selected = phead._selected
    t0 = time.time()
    # unknown-size cluster header (valid EBML) followed by pure zeros
    z.feed(b"\x1f\x43\xb6\x75\x01\xff\xff\xff" + b"\x00" * (1 << 18))
    check("zero stretch after a cluster header doesn't spin",
          time.time() - t0 < 2.0)
    # ...and the parser still recues once real clusters return
    more = z.feed(b"\x00" * 64 + sample[cluster_off:])
    check("parser recovers after the zero stretch",
          any("hell" in c[2] or "dogs" in c[2] or "snow" in c[2]
              for c in more))

    print("[3c] e2e: ASS-only MKV flattens to text cues through the relay")
    ASS = os.path.abspath("build/split_test_ass.mkv").replace("\\", "/")
    subprocess.run(
        [FF, "-y", "-v", "error",
         "-f", "lavfi", "-i", "color=c=darkred:s=256x144:d=12:r=10",
         "-i", SRT_IN,
         "-map", "0:v", "-map", "1:s", "-c:v", "libx264",
         "-preset", "ultrafast", "-c:s", "ass", ASS],
        check=True, timeout=120, creationflags=0x08000000)
    relay_a = VodRelay()
    acues = []
    relay_a.cue.connect(lambda s, e, t: acues.append((s, e, t)))
    local_a = relay_a.start("file:///" + ASS, "MichaelTVPlayer/1.0")
    check("ass mkv accepted by the relay", bool(local_a))
    # the tap only sees bytes VLC pulls: drive the provider through the
    # relay so the cache (and the parser) actually fill
    sample_a = open(ASS, "rb").read()
    deadline = time.time() + 30
    body_a = b""
    while time.time() < deadline and (len(body_a) < len(sample_a)
                                      or len(acues) < 3):
        app.processEvents()
        _, body_a = get(local_a)
        time.sleep(0.25)
    atexts = [c[2] for c in acues]
    check("ass cues flattened to plain text",
          len(acues) >= 3 and "what the hell is this" in atexts
          and "damn dogs everywhere" in atexts
          and all("{" not in t and "Dialogue" not in t for t in atexts))
    hell_a = next(c for c in acues if "hell" in c[2])
    check("ass cue timing matches the source srt",
          abs(hell_a[0] - 1.0) < 0.3 and abs(hell_a[1] - 3.0) < 0.3)
    relay_a.stop()
    time.sleep(0.3)

    print("[4] VLC plays through the relay (and seeks)")
    opens_before = relay.provider_opens
    p = VLCPlayer(timeshift=False)
    p.play(local)
    ok_play = False
    length = 0
    t0 = time.time()
    while time.time() - t0 < 25:
        time.sleep(0.5)
        app.processEvents()
        if p.is_playing():
            ok_play = True
            length = p.get_length()
            if 8 <= length / 1000 <= 14:
                break
    check("playback runs through the relay", ok_play)
    check("duration sane (~12 s)", 8 <= length / 1000 <= 14)
    p.set_time(6000)
    time.sleep(3.0)
    app.processEvents()
    t = p.get_time()
    print(f"    (seek debug: t={t}, provider_opens="
          f"{relay.provider_opens - opens_before})")
    check("seek lands through the relay", 5500 <= t <= 9500)
    check("seek needs no provider reopen (cached window)",
          relay.provider_opens - opens_before <= 1)
    p.stop_and_release()

    relay.stop()
    time.sleep(0.5)
    check("cache file cleaned up", not os.path.exists(relay.cache_path or "x"))

    print("[4] unexpected start() failure must not leak the cache file")
    import glob
    import tempfile as _tf

    def _split_caches():
        return set(glob.glob(
            os.path.join(_tf.gettempdir(), "mtp_split_*")))

    before = _split_caches()
    relay3 = VodRelay()

    def _boom():
        raise RuntimeError("simulated probe crash")
    relay3._probe_head = _boom
    local3 = relay3.start("file:///definitely/not/here.mkv", "test/ua")
    check("crashed start returns '' (direct-playback fallback)",
          local3 == "")
    check("crashed start leaves no mtp_split_* cache behind",
          _split_caches() == before)
    relay3.stop()

    print("[4b] resume engage (start_offset): prefetch the tail only, "
          "VLC's walk + seek drive the provider")
    # Resume / mid-movie subtitle engage. The relay must come ready with
    # ONLY the tail prefetched: VLC's bytes=0- walk GET opens the
    # provider at 0 (its consumed bytes land in the cache and the tap
    # parses track metadata from them), and the seek GET at the resume
    # position lands the main stream exactly there. The old design
    # pre-fetched the head AND pre-opened a stream at the offset — on a
    # real provider VLC's walk replaced that stream before it served a
    # playback byte (~5 MB dead download + two extra provider opens).
    # Blob: real MKV head (tracks), a hole of zeros (outside both the
    # head walk and the tail prefetch), real MKV bytes at the resume
    # target (clusters -> cues after the rebase), then zero padding so
    # the tail prefetch window starts PAST the resume target.
    gap = 4 << 20
    blob_r = (sample[:len(sample) // 2] + b"\x00" * gap
              + sample[len(sample) // 2:] + b"\x00" * (3 << 20))
    prov_r = _Provider(blob_r, "video/x-matroska")
    threading.Thread(target=prov_r.serve_forever, daemon=True).start()
    relay_r = VodRelay()
    rcues = []
    relay_r.cue.connect(lambda s, e, t: rcues.append((s, e, t)))
    local_r = relay_r.start(
        f"http://127.0.0.1:{prov_r.server_address[1]}/movie.mkv",
        "MichaelTVPlayer/1.0", start_offset=1)
    check("resume engage accepted", bool(local_r))
    deadline = time.time() + 10
    while time.time() < deadline and not relay_r._ready.is_set():
        app.processEvents()
        time.sleep(0.1)
    check("resume startup reaches ready (tail prefetched)",
          relay_r._ready.is_set() and relay_r._tail_base >= 0)
    check("NO provider stream pre-acquired on resume",
          relay_r.provider_opens == 0)
    # VLC's opening walk: the head GET acquires the provider at 0 and
    # its consumed bytes land in the cache (the tap parses the Tracks
    # element from them)
    status, head_part = get(local_r, rng=f"bytes=0-65535")
    check("head walk GET served (opens the provider at 0)",
          status == 206 and head_part == blob_r[:65536]
          and relay_r.provider_opens == 1)
    deadline = time.time() + 10
    while time.time() < deadline and not relay_r.parser_tracks:
        app.processEvents()
        time.sleep(0.1)
    check("track metadata parsed from the walk-cached head",
          bool(relay_r.parser_tracks) and relay_r.parser_selected is not None)
    # the Cues-index GET is served from the tail prefetch — no provider
    status, tail_part = get(local_r, rng=f"bytes={len(blob_r) - 64}-")
    check("seek index served from the tail prefetch",
          status == 206 and tail_part == blob_r[-64:]
          and relay_r.provider_opens == 1)
    # the resume seek GET: the main provider stream opens exactly here
    # and the cache window rebases to it
    b_start = len(sample) // 2 + gap
    status, part_r = get(local_r, rng=f"bytes={b_start}-{b_start + 65535}")
    check("resume seek GET served from a fresh provider stream",
          status == 206 and part_r == blob_r[b_start:b_start + 65536]
          and relay_r.provider_opens == 2)
    check("cache window rebased to the seek target",
          relay_r.cache_base == b_start)
    deadline = time.time() + 20
    while time.time() < deadline and not rcues:
        app.processEvents()
        time.sleep(0.1)
    check("cues flow on the rebased window (mid-stream parser seeded)",
          any("hell" in c[2] or "dogs" in c[2] or "snow" in c[2]
              for c in rcues))
    relay_r.stop()
    time.sleep(0.3)

    print("[5] MP4: ftyp probe + mov_text cues through the relay")
    # a Range-capable local provider, like the real thing: the probe gets
    # 206 + Content-Range (so the relay knows the total and prefetches
    # the moov tail), and the whole flow runs over HTTP
    prov = _Provider(open(MP4, "rb").read(), "video/mp4")
    threading.Thread(target=prov.serve_forever, daemon=True).start()
    relay_m = VodRelay()
    mcues = []
    relay_m.cue.connect(lambda s, e, t: mcues.append((s, e, t)))
    local_m = relay_m.start(
        f"http://127.0.0.1:{prov.server_address[1]}/movie.mp4",
        "MichaelTVPlayer/1.0")
    check("mp4 accepted by the probe (ftyp), local URL returned",
          local_m.startswith("http://127.0.0.1:"))
    check("container detected as mp4", relay_m._container == "mp4")
    idx_deadline = time.time() + 5
    while time.time() < idx_deadline and not (
            relay_m._parser and relay_m._parser.have_index):
        app.processEvents()
        time.sleep(0.1)
    check("moov index parsed from the prefetched tail before playback",
          bool(relay_m._parser and relay_m._parser.have_index))
    check("tail was actually prefetched (Content-Range total)",
          relay_m._tail_base >= 0 and len(relay_m._tail) > 0)

    msample = open(MP4, "rb").read()
    status, body, hdrs = get_h(local_m)
    check("relay serves MP4 as video/mp4",
          status == 200 and body[4:8] == b"ftyp"
          and (hdrs.get("Content-Type") or "") == "video/mp4")
    opens_m = relay_m.provider_opens
    deadline = time.time() + 30
    while time.time() < deadline and (len(body) < len(msample)
                                      or len(mcues) < 3
                                      or not relay_m.parser_tracks):
        app.processEvents()
        _, body = get(local_m)
        time.sleep(0.25)
    check("mp4 byte fidelity through the relay", body == msample)
    status, part = get(local_m, rng=f"bytes=100-199")
    check("mp4 range request served",
          status == 206 and part == msample[100:200])
    status, part = get(local_m, rng=f"bytes={len(msample)-50}-")
    check("mp4 tail range served (prefetched moov region)",
          status == 206 and part == msample[-50:])
    check("parser saw the tx3g track (overlay check data)",
          any(c.upper() in ("TX3G", "MOV_TEXT")
              for c in relay_m.parser_tracks.values()))
    mtexts = [c[2] for c in mcues]
    check("all three mp4 cues extracted as plain text",
          len(mcues) == 3 and "what the hell is this" in mtexts
          and "damn dogs everywhere" in mtexts)
    two = next((c for c in mcues if "clean as snow" in c[2]), None)
    check("two-line cue keeps its line break",
          two is not None and "and twice as bright" in two[2]
          and "\n" in two[2])
    hell_m = next(c for c in mcues if "hell" in c[2])
    check("mp4 cue timing from the stts sample table",
          abs(hell_m[0] - 1.0) < 0.1 and abs(hell_m[1] - 3.0) < 0.1)
    check("gap-padding empty samples produce no cues",
          all(c[2].strip() for c in mcues))
    print(f"    (mp4 provider hits: {prov.hits}, relay provider_opens: "
          f"{relay_m.provider_opens})")
    check("mp4 relay keeps the one-connection discipline",
          relay_m.provider_opens - opens_m <= 2)

    print("[5b] unit: tx3g decode + re-parse dedupe")
    from src.mkv_subs import is_text_codec
    from src.mp4_subs import Mp4SubParser, decode_tx3g

    check("mp4 text fourccs detected",
          is_text_codec("tx3g") and is_text_codec("MOV_TEXT")
          and is_text_codec("text"))
    check("tx3g length prefix bounds the text (styling atoms stripped)",
          decode_tx3g(b"\x00\x03abc\x00\x00\x00\x0cstyl"
                      + b"\x00\x00\x00\x00") == "abc")
    check("tx3g CR line breaks normalized",
          decode_tx3g(b"\x00\x06a\rb\nc ") == "a\nb\nc")
    check("empty gap samples decode to nothing",
          decode_tx3g(b"\x00\x00") == "")
    check("corrupt tx3g length clamped",
          decode_tx3g(b"\xff\x05ab") == "ab")
    p2 = Mp4SubParser()
    p2.parse_tail(relay_m._tail)
    a = p2.extract(relay_m._tap_read, relay_m.cache_base,
                   relay_m.cache_size, relay_m._tail_base,
                   len(relay_m._tail))
    p2.rewind(0)
    b = p2.extract(relay_m._tap_read, relay_m.cache_base,
                   relay_m.cache_size, relay_m._tail_base,
                   len(relay_m._tail))
    check("mp4 re-parse emits identical cues", a == b and len(a) == 3)
    from src.ui.caption_overlay import CueStore
    store = CueStore()
    for s, e, t in list(a) + list(b):
        store.add(s, e, t)
    check("re-parsed mp4 cues dedupe on (start, text)",
          len(store.cues) == 3)

    print("[5c] faststart mp4 (moov at the head) over file://")
    relay_f = VodRelay()
    fcues = []
    relay_f.cue.connect(lambda s, e, t: fcues.append((s, e, t)))
    local_f = relay_f.start("file:///" + MP4_FS, "MichaelTVPlayer/1.0")
    fsample = open(MP4_FS, "rb").read()
    check("faststart mp4 accepted", bool(local_f))
    fbody = b""
    deadline = time.time() + 30
    while time.time() < deadline and (len(fbody) < len(fsample)
                                      or len(fcues) < 3):
        app.processEvents()
        _, fbody = get(local_f)
        time.sleep(0.25)
    check("faststart moov found by the cache-head walk",
          bool(relay_f._parser and relay_f._parser.have_index))
    check("faststart byte fidelity", fbody == fsample)
    check("faststart cues extracted with timing",
          len(fcues) == 3 and "damn dogs everywhere"
          in [c[2] for c in fcues]
          and any(abs(c[0] - 9.0) < 0.1 for c in fcues))
    relay_f.stop()
    time.sleep(0.3)

    print("[5d] VLC plays the mp4 through the relay (and seeks)")
    p = VLCPlayer(timeshift=False)
    p.play(local_m)
    ok_play = False
    length = 0
    t0 = time.time()
    while time.time() - t0 < 25:
        time.sleep(0.5)
        app.processEvents()
        if p.is_playing():
            ok_play = True
            length = p.get_length()
            if 8 <= length / 1000 <= 14:
                break
    check("mp4 playback runs through the relay", ok_play)
    check("mp4 duration sane (~12 s)", 8 <= length / 1000 <= 14)
    opens_v = relay_m.provider_opens
    p.set_time(6000)
    time.sleep(3.0)
    app.processEvents()
    t = p.get_time()
    print(f"    (mp4 seek debug: t={t}, provider_opens="
          f"{relay_m.provider_opens - opens_v})")
    check("mp4 seek lands through the relay", 5500 <= t <= 9500)
    check("mp4 seek needs no provider reopen (cached window)",
          relay_m.provider_opens - opens_v <= 1)
    p.stop_and_release()

    relay_m.stop()
    prov.shutdown()
    time.sleep(0.5)
    check("mp4 cache file cleaned up",
          not os.path.exists(relay_m.cache_path or "x"))

    print("[6] MKV tap sees the tail prefetch region "
          "(captions in the last ~2.5 MB)")
    # VLC's last reads of an MKV are served straight from _tail and NEVER
    # enter read_cache, and the MKV tap parsed only the cache — so cues
    # whose clusters live in the final _TAIL_PREFETCH bytes were lost
    # (full-file play), and a seek landing inside the region never even
    # rebased the window (no provider acquire happens for tail bytes), so
    # the tap stayed anchored elsewhere with its parser starved.
    tailblob = open(TAIL_MKV, "rb").read()
    tail_base = len(tailblob) - vod_splitter._TAIL_PREFETCH
    check("tail fixture: late cues inside the tail, early cue outside",
          len(tailblob) > 3_000_000
          and tailblob.find(b"late tail alpha") >= tail_base
          and tailblob.find(b"late tail gamma") >= tail_base
          and 0 < tailblob.find(b"early head cue")
          < vod_splitter._HEAD_PARSE_BYTES)
    prov_t = _Provider(tailblob, "video/x-matroska")
    threading.Thread(target=prov_t.serve_forever, daemon=True).start()

    # (a) full-file play: pull everything through the relay like a
    # play-through does; the late cues must still be tapped
    relay_t = VodRelay()
    tcues = []
    relay_t.cue.connect(lambda s, e, t: tcues.append((s, e, t)))
    local_t = relay_t.start(
        f"http://127.0.0.1:{prov_t.server_address[1]}/big.mkv",
        "MichaelTVPlayer/1.0")
    check("tail mkv accepted", bool(local_t))
    tbody = b""
    deadline = time.time() + 30
    while time.time() < deadline and (len(tbody) < len(tailblob)
                                      or len(tcues) < 4):
        app.processEvents()
        _, tbody = get(local_t)
        time.sleep(0.2)
    check("(a) tail fixture byte fidelity through the relay",
          tbody == tailblob)
    late = [c for c in tcues if "late tail" in c[2]]
    check("(a) full-file play taps the tail-region cues",
          len({c[2] for c in late}) >= 3)
    alpha = next((c for c in late if "alpha" in c[2]), None)
    check("(a) tail cue times on the file's own clock",
          alpha is not None and abs(alpha[0] - 60.0) < 0.5
          and abs(alpha[1] - 62.0) < 0.5)
    check("(a) early (non-tail) cues still tapped via the cache",
          any("early head cue" in c[2] for c in tcues))
    check("(a) whole-file pull stays on one provider connection",
          relay_t.provider_opens <= 1)
    relay_t.stop()
    time.sleep(0.3)

    # (b) a seek DIRECTLY into the tail region: the seek GET is served
    # from the prefetch (no provider stream, no rebase) — the tap must
    # still deliver cues for that region
    relay_t2 = VodRelay()
    tcues2 = []
    relay_t2.cue.connect(lambda s, e, t: tcues2.append((s, e, t)))
    local_t2 = relay_t2.start(
        f"http://127.0.0.1:{prov_t.server_address[1]}/big.mkv",
        "MichaelTVPlayer/1.0")
    deadline = time.time() + 10
    while time.time() < deadline and not (
            relay_t2._ready.is_set() and relay_t2.parser_tracks):
        app.processEvents()
        time.sleep(0.1)
    a = len(tailblob) - 1_000_000        # inside the tail prefetch
    status, part = get(local_t2, rng=f"bytes={a}-{a + 65535}")
    check("(b) tail-region seek GET served from the prefetch",
          status == 206 and part == tailblob[a:a + 65536]
          and relay_t2.provider_opens == 1)
    deadline = time.time() + 15
    while time.time() < deadline and not any(
            "late tail" in c[2] for c in tcues2):
        app.processEvents()
        time.sleep(0.25)
    check("(b) seek into the tail region keeps captions tapped",
          any("late tail" in c[2] for c in tcues2))
    relay_t2.stop()
    time.sleep(0.3)
    prov_t.shutdown()

    print("[6b] unit: _tap_read serves head/tail, refuses holes")
    from src.mp4_subs import _boxes as _mp4_boxes  # noqa: F401 (parity)
    rhole = VodRelay()
    rhole._cache = None
    rhole._cache_r = None       # __init__ leaves these to start()
    rhole._head = b"HEADBYTES"
    rhole._tail = (b"0123456789" * 300_000)[:vod_splitter._TAIL_PREFETCH]
    rhole._tail_base = 100
    check("head bytes served below the window",
          rhole._tap_read(0, 4) == b"HEAD")
    check("tail bytes served inside the tail region",
          rhole._tap_read(102, 4) == b"2345")
    check("hole between head and tail returns EMPTY, not wrong bytes",
          rhole._tap_read(50, 4) == b"")
    check("past the file end returns empty",
          rhole._tap_read(rhole._tail_base + len(rhole._tail), 4) == b"")
    rstrad = VodRelay()
    import tempfile as _tf2
    fd_s, path_s = _tf2.mkstemp(prefix="mtp_tapread_")
    os.write(fd_s, b"CACHEbytes")
    os.close(fd_s)
    rstrad._cache = open(path_s, "r+b")
    rstrad._cache_r = open(path_s, "rb")
    rstrad.cache_base = 90
    rstrad.cache_size = 10                      # [90, 100)
    rstrad._tail = b"0123456789"
    rstrad._tail_base = 100                     # contiguous
    check("cache->tail straddling read stitched whole",
          rstrad._tap_read(95, 10) == b"bytes01234")
    rstrad._cache.close()
    rstrad._cache_r.close()
    os.remove(path_s)

    print("[6c] unit: no provider stream is opened after stop()")
    relay_s = VodRelay()
    local_s = relay_s.start("file:///" + MKV.replace("\\", "/"),
                            "MichaelTVPlayer/1.0")
    check("stop-test relay started", bool(local_s))
    deadline = time.time() + 10
    while time.time() < deadline and not relay_s._ready.is_set():
        app.processEvents()
        time.sleep(0.1)
    opens_s = relay_s.provider_opens
    relay_s.stop()
    time.sleep(0.2)
    st_s = relay_s._acquire(0)
    check("_acquire refused after stop() (no dead connection)",
          st_s is None and relay_s.provider_opens == opens_s)

    print("[7] ASS bare fixed-fields fallback (ffmpeg-shaped payload)")
    from src.mkv_subs import flatten_ass_text
    check("bare fixed-field dialogue (empty Name) reduced to its text",
          flatten_ass_text("0,0:00:01.00,0:00:02.00,Default,,0,0,0,,"
                           "what the hell is this")
          == "what the hell is this")
    check("bare fixed-field text keeps its commas",
          flatten_ass_text("0,0:00:01.00,0:00:02.00,Default,,0,0,0,,"
                           "Real, commas, kept")
          == "Real, commas, kept")

    print("[7b] lang_matches: full-word hints match on word boundaries")
    from src.mkv_subs import lang_matches
    check("'english' does NOT match 'Non-English Comments'",
          not lang_matches("english", "und", "Non-English Comments"))
    check("'english' DOES match 'English Comments'",
          lang_matches("english", "und", "English Comments"))
    check("'english' matches parenthesized '(English)'",
          lang_matches("english", "und", "Signs (English)"))
    check("short codes keep alias behavior (en vs eng)",
          lang_matches("en", "eng", "")
          and not lang_matches("en", "", "French"))

    print("[8] MKV TimecodeScale honored in cue timing")
    scaleblob = open(SCALE_MKV, "rb").read()
    from src.mkv_subs import MkvSubParser
    ps = MkvSubParser(prefer_language="eng")
    made_s = ps.feed(scaleblob)
    hell_s = next((c for c in made_s if "hell" in c[2]), None)
    check("non-default TimecodeScale (2x) shifts cue times",
          hell_s is not None and abs(hell_s[0] - 2.0) < 0.05
          and abs(hell_s[1] - 6.0) < 0.05)
    check("default-scale files keep millisecond timing",
          abs(next(c for c in MkvSubParser().feed(
              open(MKV, "rb").read()) if "hell" in c[2])[0] - 1.0) < 0.05)
    prov_sc = _Provider(scaleblob, "video/x-matroska")
    threading.Thread(target=prov_sc.serve_forever, daemon=True).start()
    relay_sc = VodRelay()
    scues = []
    relay_sc.cue.connect(lambda s, e, t: scues.append((s, e, t)))
    local_sc = relay_sc.start(
        f"http://127.0.0.1:{prov_sc.server_address[1]}/scale.mkv",
        "MichaelTVPlayer/1.0")
    deadline = time.time() + 15
    while time.time() < deadline and not any(
            "hell" in c[2] for c in scues):
        app.processEvents()
        time.sleep(0.25)
    hell_sc = next((c for c in scues if "hell" in c[2]), None)
    check("scale honored through the live relay tap",
          hell_sc is not None and abs(hell_sc[0] - 2.0) < 0.05)
    relay_sc.stop()
    prov_sc.shutdown()
    time.sleep(0.3)

    print("[9] MP4 edit-list media_time honored in cue timing")
    from src.mp4_subs import Mp4SubParser
    elstblob = open(ELST_MP4, "rb").read()
    pe = Mp4SubParser(prefer_language="eng")
    pe.parse_tail(elstblob)
    made_e = pe.extract(lambda off, n: elstblob[off:off + n], 0,
                        len(elstblob))
    hell_e = next((c for c in made_e if "hell" in c[2]), None)
    check("elst media_time (0.7 s skip) shifts cue times earlier",
          hell_e is not None and abs(hell_e[0] - 0.3) < 0.05
          and abs(hell_e[1] - 2.3) < 0.05)
    pf = Mp4SubParser(prefer_language="eng")
    plainblob = open(MP4, "rb").read()
    pf.parse_tail(plainblob)
    made_f = pf.extract(lambda off, n: plainblob[off:off + n], 0,
                        len(plainblob))
    hell_f = next((c for c in made_f if "hell" in c[2]), None)
    check("media_time=0 edit lists leave timing alone",
          hell_f is not None and abs(hell_f[0] - 1.0) < 0.05)

    print()
    if FAIL:
        print(f"FAILED {len(FAIL)}: {FAIL}")
        return 1
    print(f"all {len(PASS)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
