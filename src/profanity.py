# -*- coding: utf-8 -*-
"""Profanity filter: read the subtitle track, mute the audio over bad words.

Architecture (movies & series — the content that carries text subtitle
tracks):

    ffmpeg (2nd connection to the same VOD file, read at several times
    realtime, video/audio packets discarded)  -->  SRT text on stdout
        |                                                         |
        v                                                         v
  SubtitleExtractor (QProcess)  -->  cues (start, end, text)
        |
        v
  find_matches() + windows_from_cues()  -->  mute windows [(s, e)]
        |
        v
  ProfanityEngine (100 ms timer, PlayerView's playback clock)
        -->  VLCPlayer.set_filter_mute(True/False)

The filter reads the TRACK DATA — subtitles do NOT need to be visible.
libvlc 3 offers no way to hand the embedded text to the app, hence the
parallel ffmpeg read (the app's Download button already opens a second
VOD connection while playing, so this matches existing usage).

Mute windows are computed at word granularity: within a cue, each word's
start/end is estimated from its share of the cue's characters — the
pad-before / pad-after / sync-offset settings exist to tune around that
estimate (and any track's timing drift).
"""

import logging
import re
import shutil
import threading

from PyQt5 import QtCore

log = logging.getLogger("mtp")

# Feature switch: live-TV filtering runs on closed captions extracted from
# the LOCAL DVR buffer (see live_cc.py) — one provider connection, the one
# the recorder already holds. Movies & series support comes later (a
# single-connection text source for VOD does not exist yet).
PROFANITY_AVAILABLE = True

# ---- word levels ----
LEVELS = ("exact", "partial", "whole")

# (word, level) starter list — every entry is editable/removable in the
# dialog. "whole" masks compounds too (fuck -> fucked/fucking/motherfucker);
# "exact" touches only the standalone word (safer for ambiguous stems).
DEFAULT_WORDS = (
    ("fuck", "whole"),
    ("shit", "whole"),
    ("bitch", "whole"),
    ("asshole", "whole"),
    ("bastard", "exact"),
    ("cunt", "whole"),
    ("dick", "exact"),
    ("piss", "exact"),
    ("crap", "exact"),
    ("damn", "exact"),
    ("goddamn", "whole"),
    ("whore", "whole"),
    ("slut", "whole"),
)

_TAG_RE = re.compile(r"<[^>]+>")            # <i>, <b>, <font ...>
_ASS_RE = re.compile(r"\{[^}]*\}")          # {\an8} style overrides
_WORD_RE = re.compile(r"\w+", re.UNICODE)   # for whole-word spans
_SRT_TIME = re.compile(
    r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def clean_text(raw: str) -> str:
    """SRT text -> plain single-line text (tags, braces, markup gone)."""
    t = raw.replace("\r", "")
    t = _TAG_RE.sub("", t)
    t = _ASS_RE.sub("", t)
    return " ".join(t.split())


def find_matches(text: str, words) -> list:
    """[(char_start, char_end)] of every bad-word occurrence in ``text``.

    exact    — standalone word only (\\b match); 'dog' hits 'the dog barked'
               but not 'doghouse'
    partial  — the substring anywhere; masks just the substring ('***house')
    whole    — the substring anywhere; masks the whole containing word
               ('********')
    Matching is case-insensitive; spans are in ORIGINAL-text coordinates so
    word timing can be derived from character positions.
    """
    spans = []
    low = text.lower()
    for entry in words or ():
        if isinstance(entry, (tuple, list)) and len(entry) == 2:
            word, level = entry
        else:
            word, level = entry, "exact"
        word = str(word).strip().lower()
        if not word or level not in LEVELS:
            continue
        if level == "exact":
            for m in re.finditer(r"\b" + re.escape(word) + r"\b", low):
                spans.append((m.start(), m.end()))
        else:
            start = 0
            while True:
                i = low.find(word, start)
                if i < 0:
                    break
                if level == "partial":
                    spans.append((i, i + len(word)))
                else:   # whole — extend to the containing word chars
                    s = i
                    while s > 0 and (low[s - 1].isalnum()):
                        s -= 1
                    e = i + len(word)
                    while e < len(low) and low[e].isalnum():
                        e += 1
                    spans.append((s, e))
                start = i + 1
    return spans


def parse_srt_time(line: str):
    """'00:00:01,600 --> 00:00:04,200' -> (1.6, 4.2) or None."""
    m = _SRT_TIME.search(line or "")
    if not m:
        return None
    g = [int(x) for x in m.groups()]
    return (g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000.0,
            g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000.0)


class SrtParser:
    """Incremental SRT parser: feed stdout lines, get finished cues.

    Tolerant: index lines are optional, blank-line separation enforced
    loosely, junk lines ignored (ffmpeg output is well-formed anyway).
    """

    def __init__(self):
        self.cues = []
        self._buf = []

    def feed(self, chunk: str) -> list:
        """Feed a text chunk; returns cues completed by this chunk."""
        done = []
        self._buf.append(chunk)
        data = "".join(self._buf)
        # process only complete lines; keep the trailing partial line
        lines = data.split("\n")
        self._buf = [lines.pop()] if lines else []
        cur = getattr(self, "_pending", [])
        self._pending = []
        for line in lines:
            l = line.strip()
            if not l:
                if cur:
                    done.append(self._finish(cur))
                    cur = []
            else:
                cur.append(line)
        self._pending = cur
        return [c for c in done if c]

    def flush(self) -> list:
        cur = getattr(self, "_pending", [])
        self._pending = []
        if cur:
            c = self._finish(cur)
            return [c] if c else []
        return []

    @staticmethod
    def _finish(lines) -> tuple:
        """Lines of one block -> (start_s, end_s, text) or None."""
        span = None
        text_lines = []
        for ln in lines:
            t = parse_srt_time(ln)
            if t is not None:
                span = t
            elif span is not None:
                text_lines.append(ln)
        if span is None:
            return None
        text = clean_text("\n".join(text_lines))
        if not text:
            return None
        return (span[0], span[1], text)


def windows_from_cues(cues, words) -> list:
    """Cues + word list -> sorted, merged mute windows [(start_s, end_s)].

    Word timing inside a cue is proportional to character share — the
    standard approximation when only cue timestamps exist.
    """
    wins = []
    for start, end, text in cues or ():
        spans = find_matches(text, words)
        if not spans:
            continue
        n = len(text)
        dur = max(0.0, end - start)
        for s, e in spans:
            ws = start + (s / n) * dur
            we = start + (e / n) * dur
            if we > ws:
                wins.append((ws, we))
    return merge_windows(wins)


def merge_windows(wins, gap: float = 0.0) -> list:
    if not wins:
        return []
    out = []
    ws, we = sorted(wins)[0]
    for s, e in sorted(wins)[1:]:
        if s <= we + gap:
            we = max(we, e)
        else:
            out.append((ws, we))
            ws, we = s, e
    out.append((ws, we))
    return out


def mask_text(text: str, words) -> str:
    """Render ``text`` with matches masked — '*** in the ********'."""
    out = list(text)
    for s, e in find_matches(text, words):
        for i in range(s, min(e, len(out))):
            out[i] = "*"
    return "".join(out)


def find_ffmpeg() -> str:
    """Locate ffmpeg.exe (PATH, then the common winget location)."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import glob
        import os
        for pat in (
            os.path.expandvars(
                r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"
                r"\Gyan.FFmpeg*_Microsoft.Winget.Source_*"
                r"\ffmpeg-*\bin\ffmpeg.exe"),
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        ):
            hits = glob.glob(pat)
            if hits:
                return hits[0]
    except Exception:  # noqa: BLE001
        pass
    return ""


class SubtitleExtractor(QtCore.QObject):
    """Streams one subtitle track out of a remote media file with ffmpeg.

    ffmpeg reads the input faster than realtime (``-readrate``) and copies
    ONLY the subtitle stream to SRT on stdout — audio/video packets are
    demuxed but discarded. Cues are emitted as they arrive.
    """

    cue = QtCore.pyqtSignal(float, float, str)     # start_s, end_s, text
    failed = QtCore.pyqtSignal(str)
    started_ok = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc = None
        self.parser = SrtParser()
        self._want_index = None
        self._chosen_index = None
        self.frontier_s = -1.0      # end time of the last cue received

    # ---- lifecycle ----
    def start(self, url: str, user_agent: str, prefer_language: str = "",
              start_at: float = 0.0, readrate: int = 6):
        """Begin extracting. Call probe_track() first (or index defaults
        to sub-stream 0). Returns False when ffmpeg is missing."""
        self.stop()
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            self.failed.emit("ffmpeg not found")
            return False
        self._prefer_language = (prefer_language or "").lower()
        self._start_at = max(0.0, float(start_at))
        idx = 0 if self._want_index is None else int(self._want_index)
        args = [
            "-hide_banner", "-nostdin", "-loglevel", "error",
            "-user_agent", user_agent,
        ]
        if self._start_at > 1.0:
            args += ["-ss", f"{self._start_at:.1f}"]
        args += [
            "-readrate", str(max(1, int(readrate))),
            "-i", url,
            "-map", f"0:s:{idx}",
            "-c:s", "srt", "-f", "srt", "pipe:1",
        ]
        self.proc = QtCore.QProcess(self)
        self.proc.setProgram(ffmpeg)
        self.proc.setArguments(args)
        self.proc.readyReadStandardOutput.connect(self._on_stdout)
        self.proc.finished.connect(self._on_finished)
        self.proc.errorOccurred.connect(self._on_error)
        self.proc.start()
        return True

    def stop(self):
        if self.proc is not None:
            try:
                self.proc.readyReadStandardOutput.disconnect()
                self.proc.finished.disconnect()
                self.proc.errorOccurred.disconnect()
            except Exception:  # noqa: BLE001
                pass
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass
            self.proc = None
        self.frontier_s = -1.0

    # ---- track choice (from an ffprobe pass) ----
    def probe_track(self, url: str, user_agent: str) -> bool:
        """Pick the sub stream index: preferred language, else English,
        else 0. Blocking (runs ffprobe) — call from a worker thread."""
        import os as _os
        ff = find_ffmpeg()
        if not ff:
            return False
        exe = _os.path.join(_os.path.dirname(ff), "ffprobe.exe")
        import json as _json
        import subprocess
        cmd = [exe, "-v", "error", "-user_agent", user_agent,
               "-show_entries",
               "stream=index,codec_type:stream_tags=language,title",
               "-of", "json", url]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=45,
                                 creationflags=0x08000000)
            data = _json.loads(out.stdout or "{}")
        except Exception as exc:  # noqa: BLE001
            try:
                log.warning("profanity probe failed: %r", exc)
            except Exception:
                pass
            return False
        subs = []
        for s in data.get("streams", []):
            if s.get("codec_type") == "subtitle":
                tags = s.get("tags", {}) or {}
                subs.append((s.get("index", 0),
                             str(tags.get("language", "")),
                             str(tags.get("title", ""))))
        if not subs:
            return False
        want = getattr(self, "_prefer_language", "")
        for i, (idx, lang, title) in enumerate(subs):
            blob = f"{lang} {title}".lower()
            if want and want in blob:
                self._want_index = i
                return True
        for i, (idx, lang, title) in enumerate(subs):
            if "english" in f"{lang} {title}".lower():
                self._want_index = i
                return True
        self._want_index = 0
        return True

    # ---- process output ----
    def _on_stdout(self):
        if self.proc is None:
            return
        raw = bytes(self.proc.readAllStandardOutput())
        try:
            text = raw.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return
        for start, end, ctxt in self.parser.feed(text):
            self._emit_cue(start, end, ctxt)

    def _emit_cue(self, start, end, text):
        # cues are relative to the -ss point when one was used
        start += self._start_at
        end += self._start_at
        self.frontier_s = max(self.frontier_s, end)
        self.cue.emit(start, end, text)

    def _on_finished(self):
        for start, end, ctxt in self.parser.flush():
            self._emit_cue(start, end, ctxt)
        try:
            log.info("profanity extractor finished (frontier=%.1fs)",
                     self.frontier_s)
        except Exception:
            pass

    def _on_error(self, err):
        self.failed.emit(f"ffmpeg error {err}")


class ProfanityEngine(QtCore.QObject):
    """Decides filter-mute on/off from the playback clock + mute windows."""

    def __init__(self, player, parent=None):
        super().__init__(parent)
        self.player = player          # VLCPlayer (set_filter_mute)
        self.words = list(DEFAULT_WORDS)
        self.pad_before_s = 0.12
        self.pad_after_s = 0.25
        self.sync_s = 0.0             # + = mute later, − = earlier
        self.lead_s = 1.5             # captions lag speech: mute EARLIER by this
        self.windows = []             # sorted [(start, end)]
        self.enabled = False
        self.muted = False
        self._last_state = None

    # ---- data ----
    def add_cue(self, start: float, end: float, text: str,
                lead_s: float = None):
        """One caption/subtitle cue arrived: fold its bad-word windows in.
        Live captions lag the spoken word by 1-3 s, so their windows are
        shifted EARLIER by lead_s (tunable per channel). VOD subtitle
        tracks are pre-timed — they pass lead_s=0."""
        shift = self.lead_s if lead_s is None else float(lead_s)
        wins = windows_from_cues([(start, end, text)], self.words)
        if not wins:
            return
        if shift:
            wins = [(max(0.0, ws - shift), max(0.0, we - shift))
                    for ws, we in wins]
        self.windows = merge_windows(self.windows + wins, gap=0.05)

    def clear(self):
        self.windows = []
        self.set_muted(False)

    # ---- evaluation ----
    def evaluate(self, play_s: float):
        """Apply the filter-mute state for the current playback position."""
        if not self.enabled:
            return
        t = float(play_s) + self.sync_s
        on = any(ws - self.pad_before_s <= t <= we + self.pad_after_s
                 for ws, we in self.windows)
        self.set_muted(on)

    def set_muted(self, on: bool):
        on = bool(on)
        if on == self.muted:
            return
        self.muted = on
        try:
            self.player.set_filter_mute(on)
        except Exception as exc:  # noqa: BLE001
            try:
                log.debug("filter mute failed: %r", exc)
            except Exception:
                pass
