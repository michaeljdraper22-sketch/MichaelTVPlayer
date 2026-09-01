# -*- coding: utf-8 -*-
"""Profanity filter: read the text under the playback, mute the audio over
bad words — one word list for both live paths.

Two text sources feed the SAME engine (ProfanityEngine):

  LIVE TV — CCExtractor (bundled) reads closed captions out of the local
  DVR buffer the recorder already writes (see live_cc.py): one provider
  connection, the one playback already holds. Captions trail speech by
  1-3 s, so each cue's windows are shifted EARLIER by lead_s.

  MOVIES & SERIES — playback is routed through the local VOD relay
  (vod_splitter) when the filter (or the caption overlay) is on: it peels
  the file's embedded text track (MKV SRT/ASS, MP4 mov_text) from its
  cache and serves VLC byte-identical data over localhost — still one
  provider connection. Those cues are pre-timed: lead_s=0.

    cues (start, end, text)
        |
        v
  find_matches() + windows_from_cues()  -->  mute windows [(s, e)]
        |
        v
  ProfanityEngine (100 ms timer, PlayerView's playback clock)
        -->  VLCPlayer.set_filter_mute(True/False)
        -->  mask_text() also masks the words in the overlay captions

The filter reads the TRACK DATA — subtitles do NOT need to be visible.
libvlc 3 offers no way to hand the embedded text to the app, hence the
two app-side readers above.

Mute windows are computed at word granularity: within a cue, each word's
start/end is estimated from its share of the cue's characters — the
pad-before / pad-after / sync-offset settings exist to tune around that
estimate (and any track's timing drift).

Coverage: live CC and VOD text tracks (SRT/ASS-style). Image subtitles
(PGS, DVB) carry no text and are not filtered.
"""

import logging
import re
import shutil

from PyQt5 import QtCore

log = logging.getLogger("mtp")

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
    loosely, junk lines ignored (CCExtractor and the relay emit
    well-formed SRT anyway).

    ``keep_lines=True`` preserves the cue's internal line breaks (tags and
    brace overrides still stripped) — the caption overlay renders roll-up
    caption screens as a multi-line window. The default collapses to one
    line, which is all the profanity matcher needs.
    """

    def __init__(self, keep_lines: bool = False):
        self.cues = []
        self._buf = []
        self._keep_lines = bool(keep_lines)

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

    def _finish(self, lines) -> tuple:
        """Lines of one block -> (start_s, end_s, text) or None."""
        return _finish_cue(lines, self._keep_lines)


def _finish_cue(lines, keep_lines: bool):
    """Lines of one SRT block -> (start_s, end_s, text) or None."""
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
    if keep_lines:
        text = "\n".join(
            _ASS_RE.sub("", _TAG_RE.sub("", ln)).strip()
            for ln in text_lines if ln.strip())
        if not text:
            return None
        return (span[0], span[1], text)
    text = clean_text("\n".join(text_lines))
    if not text:
        return None
    return (span[0], span[1], text)


def windows_from_cues(cues, words, whole_cue: bool = False) -> list:
    """Cues + word list -> sorted, merged mute windows [(start_s, end_s)].

    Word timing inside a cue is proportional to character share — the
    standard approximation when only cue timestamps exist.
    whole_cue=True instead covers the ENTIRE cue (start..end) whenever any
    word matches: mute for as long as the word is in the subtitle.
    """
    wins = []
    for start, end, text in cues or ():
        spans = find_matches(text, words)
        if not spans:
            continue
        if whole_cue:
            if end > start:
                wins.append((float(start), float(end)))
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


def read_subtitle_text(path: str):
    """External subtitle file -> text, or None when unreadable.

    Stremio hands its subtitles over as a file (--sub-file=...); encodings
    vary by source (UTF-8 with/without BOM, occasionally UTF-16). A BOM
    settles it; otherwise UTF-8 with replacement characters — a mojibake
    word or two beats losing the whole filter pass.
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if data.startswith(b"\xff\xfe"):
        return data[2:].decode("utf-16-le", "replace")
    if data.startswith(b"\xfe\xff"):
        return data[2:].decode("utf-16-be", "replace")
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.decode("utf-8", "replace")


def find_ffmpeg() -> str:
    """Locate ffmpeg.exe (PATH, then the common winget location).

    Nothing in the app needs ffmpeg at runtime — the test suite uses it
    to build VOD-splitter fixtures (sample MKV/MP4 files with subs).
    """
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
        self.whole_cue = False        # True = mute the whole cue, not the word
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
        wins = windows_from_cues([(start, end, text)], self.words,
                                 whole_cue=self.whole_cue)
        if not wins:
            return
        if shift:
            wins = [(max(0.0, ws - shift), max(0.0, we - shift))
                    for ws, we in wins]
        self.windows = merge_windows(self.windows + wins, gap=0.05)

    def clear(self):
        self.windows = []
        self.set_muted(False)

    def shift_windows(self, delta: float):
        """Move every mute window by ``delta`` content seconds — the live
        arrival anchor rebases cue windows by whole seconds at once and the
        mute windows must follow, or the filter mutes the wrong speech."""
        if not self.windows or not delta:
            return
        self.windows = merge_windows(
            [(max(0.0, ws + delta), max(0.0, we + delta))
             for ws, we in self.windows], gap=0.05)

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
