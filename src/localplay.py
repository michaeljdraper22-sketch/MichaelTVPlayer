"""Local video files ("open anything like VLC") — playables, sidecar
subtitle detection, and a subtitle tap for the caption overlay /
profanity filter.

Playback itself needs nothing from here: a ``kind="file"`` playable is
handed to PlayerView.play_media, VLC opens the path directly, and every
seekable-file behavior (scrub bar, speed, LIVE-as-skip-to-end) follows
from the file reporting a length.

What DOES need help is the app's signature text pipeline (restyled
overlay captions + profanity muting): VLC 3 never exposes cue text to
libvlc, so — exactly like network VOD — the bytes must be parsed by the
app's own MKV/MP4 subtitle parsers. On the network that means the local
VodRelay; for a file on disk the relay's whole job (single provider
connection, cache window, tail prefetch) is pointless. LocalSubTap just
reads the file and feeds the same parsers:

  * MKV: linear scan from byte 0 (the Tracks element sits at the head,
    subtitle blocks scatter through the clusters — a full pass at disk
    speed outpaces 1x playback within seconds).
  * MP4: the moov index from the file tail (or head, faststart), then
    all text samples at their stco offsets — random access makes the
    relay's incremental frontier walk unnecessary.

The tap is INDEPENDENT of the playback URL, which buys one simplification
the relay cannot have: engaging captions mid-file starts the tap with no
playback restart (the relay must BE the playback URL, hence
_restart_through_relay).

Sidecar subtitle files (movie.srt next to movie.mkv) ride the existing
external-sub_file machinery instead: VLC shows the file itself, and the
overlay takeover (extended to kind="file") renders it styled.
"""

import hashlib
import logging
import os
import threading

from PyQt5 import QtCore

from .mkv_subs import MkvSubParser
from .mp4_subs import Mp4SubParser

log = logging.getLogger("mtp.localplay")

# What the File > Open dialog offers / drag-drop / handoff accepts. VLC
# plays far more than this; the list is the "video file" definition for
# the app's plumbing (sidecar detection stops at the first extension).
VIDEO_EXTENSIONS = (
    ".mp4", ".m4v", ".mkv", ".webm", ".mov", ".avi", ".wmv", ".flv",
    ".mpg", ".mpeg", ".m2ts", ".ts", ".mts", ".vob", ".ogv", ".3gp",
    ".divx", ".asf", ".m2v",
)

# Sidecar subtitle extensions, in pick order (a plain "movie.srt" beats
# "movie.en.srt"; ASS wins over SSA only by list order below).
SUBTITLE_EXTENSIONS = (".srt", ".ass", ".ssa", ".vtt")
# language tokens that may sit between basename and extension
_SIDECAR_LANGS = ("en", "eng", "english", "und")

_EBML_MAGIC = b"\x1a\x45\xdf\xa3"      # MKV/WebM
_FTYP = b"ftyp"                          # MP4 family (offset 4)
_TAIL_BYTES = 4 << 20                    # moov hunt window (relay's size)
_CHUNK = 1 << 20                         # MKV scan reads


def is_video_path(path: str) -> bool:
    try:
        return os.path.splitext(str(path or ""))[1].lower() \
            in VIDEO_EXTENSIONS
    except Exception:  # noqa: BLE001 - paths can be nasty
        return False


def make_file_playable(path: str) -> dict:
    """A local video file as a playable dict. ``fav_key`` hashes the
    LOWERCASED absolute path so recents/favorites dedupe a file the user
    re-opened via different casing."""
    path = os.path.abspath(path)
    base = os.path.basename(path)
    title = os.path.splitext(base)[0] or base
    digest = hashlib.sha1(path.lower().encode("utf-8",
                                              "replace")).hexdigest()[:16]
    playable = {
        "kind": "file",
        "title": title,
        "url": path,
        "fav_key": "file:" + digest,
    }
    sub = find_sidecar_sub(path)
    if sub:
        playable["sub_file"] = sub
        playable["sub_name"] = os.path.basename(sub)
    return playable


def find_sidecar_sub(path: str) -> str:
    """A subtitle file sitting next to the video, VLC-style. Exact
    basename first ("Movie.srt"), then a language-suffixed variant
    ("Movie.en.srt"). First hit wins; "" when there is none."""
    root = os.path.splitext(path)[0]
    for ext in SUBTITLE_EXTENSIONS:
        cand = root + ext
        if os.path.isfile(cand):
            return cand
    for lang in _SIDECAR_LANGS:
        for ext in SUBTITLE_EXTENSIONS:
            cand = "%s.%s%s" % (root, lang, ext)
            if os.path.isfile(cand):
                return cand
    return ""


class LocalSubTap(QtCore.QObject):
    """Feed a local MKV/MP4's embedded text subtitles to the app as cues.

    Same signal contract as VodRelay (``cue(start, end, text)``,
    ``failed(str)``) and the same track-metadata attributes
    (``parser_tracks`` / ``parser_tracks_meta`` / ``parser_selected``)
    so PlayerView's caption plumbing treats it like a relay. Cues are
    pre-timed on the file's own timeline = VLC's get_time axis.
    """

    cue = QtCore.pyqtSignal(float, float, str)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parser_tracks = {}
        self.parser_tracks_meta = {}
        self.parser_selected = None
        self._alive = True
        self._restart = False
        self._prefer = "eng"
        self._path = ""
        self._thread = None
        self._container = ""

    # ---- lifecycle ----
    def start(self, path: str, prefer_language: str = "eng") -> bool:
        """Open the file, detect the container, launch the scan thread.
        False (with ``failed`` emitted) when the file isn't MKV/MP4 —
        VLC keeps rendering whatever it can."""
        try:
            with open(path, "rb") as f:
                head = f.read(12)
        except OSError as exc:
            self.failed.emit("cannot open: %r" % exc)
            return False
        if head[:4] == _EBML_MAGIC:
            self._container = "mkv"
        elif len(head) >= 8 and head[4:8] == _FTYP:
            self._container = "mp4"
        else:
            # AVI/TS/WMV/...: no text-extraction parser (and TS bitmap
            # subs couldn't be restyled anyway) — VLC renders its own.
            self.failed.emit("not an MKV or MP4 file")
            return False
        self._path = path
        self._prefer = (prefer_language or "eng").lower()
        self._thread = threading.Thread(
            target=self._scan, args=(path,), daemon=True, name="mtp-filetap")
        self._thread.start()
        return True

    def stop(self):
        self._alive = False

    def set_prefer_language(self, prefer: str) -> bool:
        """CC-menu language change: restart the scan with the new
        preference. True when it actually changed (the caller drops the
        old language's cues, as with the relay). A finished scan is
        respawned — MKV threads exit once the file is fully read."""
        prefer = (prefer or "").lower()
        if not prefer or prefer == self._prefer:
            return False
        self._prefer = prefer
        th = self._thread
        if th is not None and th.is_alive():
            self._restart = True
        else:
            self._restart = False
            self._thread = threading.Thread(
                target=self._scan, args=(self._path,), daemon=True,
                name="mtp-filetap")
            self._thread.start()
        return True

    # ---- workers ----
    def _scan(self, path: str):
        try:
            if self._container == "mkv":
                self._scan_mkv(path)
            else:
                self._scan_mp4(path)
        except Exception as exc:  # noqa: BLE001 - a dead tap loses
            # captions, never playback; VLC takes rendering back
            if self._alive:
                self.failed.emit("tap crashed: %r" % exc)

    def _scan_mkv(self, path: str):
        with open(path, "rb") as f:
            parser = MkvSubParser(prefer_language=self._prefer)
            self._snap(parser)
            while self._alive:
                if self._restart:
                    self._restart = False
                    f.seek(0)
                    parser = MkvSubParser(prefer_language=self._prefer)
                chunk = f.read(_CHUNK)
                if not chunk:
                    break
                for cue in parser.feed(chunk):
                    self.cue.emit(*cue)
                self._snap(parser)

    def _scan_mp4(self, path: str):
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            def read(off: int, n: int) -> bytes:
                f.seek(off)
                return f.read(n)

            parser = Mp4SubParser(prefer_language=self._prefer)
            tail = read(max(0, size - _TAIL_BYTES),
                        min(_TAIL_BYTES, size))
            parser.parse_tail(tail)
            if not parser.have_index:
                parser.parse_head_bytes(read(0, _TAIL_BYTES))
            if not parser.have_index:
                parser.scan_head(read, 0, size)
            self._snap(parser)
            while self._alive:
                # cache_base=0, cache_size=size: every sample is readable
                # NOW on a local disk — one extract pulls the whole track
                for cue in parser.extract(read, 0, size, -1, 0):
                    self.cue.emit(*cue)
                if not self._restart:
                    break
                self._restart = False
                parser.reselect(self._prefer)
                self._snap(parser)

    def _snap(self, parser):
        """Publish track metadata for the UI's text-track check and the
        CC menu's re-selections (same shape as VodRelay's snapshots)."""
        if not parser._track_meta:
            return
        self.parser_tracks_meta = dict(parser._track_meta)
        self.parser_tracks = {num: m["codec"]
                              for num, m in parser._track_meta.items()}
        self.parser_selected = parser._selected
