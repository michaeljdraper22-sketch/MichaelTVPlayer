# -*- coding: utf-8 -*-
"""Forced-first MKV subtitle selection + CC-menu track re-targeting.

The 2026-11-01 Love Is Blind night: Netflix WEB-DL MKVs lay their
English tracks out as 'English (forced)' / 'English' / 'English (SDH)'
(forced FIRST), and the tap's language-keyed selection took the first
match — the near-empty forced track. The overlay rendered nothing, the
profanity filter armed ~zero windows (green dot, nothing to filter),
and CC-menu rows couldn't move the selection because every row is the
same language. These checks pin the fix:

[1] forced-aware default selection (name-marked AND FlagForced-marked,
    with the forced-only fallback preserved)
[2] the parser actually EMITS the full track's cues on a forced-first
    file (synthetic MKV: forced track first, empty; full track second)
[3] CC-menu picks re-target the relay (set_selected_track +
    _relay_track_for_pick name mapping, VLC 'Name - [lang]' and
    'Track N' spellings)

Run:  .venv\\Scripts\\python.exe test_forced_tracks.py
"""
import os
import struct
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5 import QtWidgets  # noqa: E402

from src.mkv_subs import MkvSubParser, track_is_forced  # noqa: E402
from src.vod_splitter import VodRelay  # noqa: E402

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


# ---- synthetic MKV builder (elementary: EBML + Segment > Tracks +
# Clusters with SimpleBlocks for one subtitle track each) ----

_EBML = b"\x1a\x45\xdf\xa3"
_DOCTYPE_VER = b"\x42\x86"
_DOCTYPE_READ = b"\x42\xf7"
_SEGMENT = b"\x18\x53\x80\x67"
_INFO = b"\x15\x49\xa9\x66"
_TC_SCALE = b"\x2a\xd7\xb1"
_TRACKS = b"\x16\x54\xae\x6b"
_TRACK_ENTRY = b"\xae"
_TNUM = b"\xd7"
_TTYPE = b"\x83"
_CODEC = b"\x86"
_LANG = b"\x22\xb5\x9c"
_NAME = b"\x53\x6e"
_FLAG_FORCED = b"\x55\xaa"
_CLUSTER = b"\x1f\x43\xb6\x75"
_TIMECODE = b"\xe7"
_SBLOCK = b"\xa3"


def _vint(n):
    """EBML size vint: minimal length, marker bit set."""
    if n == 0:
        return b"\x80"
    length = 1
    while n >= (1 << (7 * length)):
        length += 1
    out = n.to_bytes(length, "big")
    return bytes([out[0] | (0x80 >> (length - 1))]) + out[1:]


def _el(eid, body):
    return eid + _vint(len(body)) + body


def _track(number, name, lang, codec=b"S_TEXT/UTF8", forced=False):
    body = _el(_TNUM, bytes([number]))           # TrackNumber
    body += _el(_TTYPE, bytes([0x11]))           # TrackType: subtitle
    body += _el(_CODEC, codec)                   # CodecID
    body += _el(_LANG, lang)                     # Language
    if name:
        body += _el(_NAME, name.encode())        # Name
    if forced:
        body += _el(_FLAG_FORCED, b"\x01")       # FlagForced
    return _el(_TRACK_ENTRY, body)


def _cluster(tc_ms, blocks):
    body = _el(_TIMECODE, struct.pack(">Q", tc_ms))   # Timecode
    body += blocks
    return _el(_CLUSTER, body)


def _simple_block(track, rel_ms, text):
    frame = text.encode()
    body = _vint(track) + struct.pack(">h", rel_ms) + b"\x00" + frame
    return _el(_SBLOCK, body)


def build_mkv(tracks, clusters):
    ebml = _el(_EBML, _el(_DOCTYPE_VER, b"\x01") + _el(_DOCTYPE_READ, b"\x01"))
    seg_body = _el(_INFO, _el(_TC_SCALE, struct.pack(">Q", 1_000_000)))
    seg_body += _el(_TRACKS, b"".join(tracks))
    seg_body += b"".join(clusters)
    return ebml + _el(_SEGMENT, seg_body)


NETFLIX_LAYOUT = [
    _track(3, "English (forced)", b"en"),
    _track(4, "English", b"en"),
    _track(5, "English (SDH)", b"en"),
]

FLAG_ONLY_LAYOUT = [           # unnamed tracks, muxer set FlagForced
    _track(3, "", b"en", forced=True),
    _track(4, "", b"en"),
]

FORCED_ONLY_LAYOUT = [         # foreign film: the only English is forced
    _track(3, "English (forced)", b"en"),
    _track(4, "Spanish", b"spa"),
]

DIALOGUE_CLUSTERS = [
    _cluster(1000, _simple_block(3, 0, "[foreign speech]") +
             _simple_block(4, 0, "what the hell was that")),
    _cluster(4000, _simple_block(4, 0, "I have no damn idea")),
]


def main():
    print("[1] forced-aware default selection")
    p = MkvSubParser(prefer_language="eng")
    p._track_meta = {
        3: {"codec": "S_TEXT/UTF8", "lang": "en",
            "name": "English (forced)"},
        4: {"codec": "S_TEXT/UTF8", "lang": "en", "name": "English"},
        5: {"codec": "S_TEXT/UTF8", "lang": "en", "name": "English (SDH)"},
    }
    p._select_track()
    check("Netflix layout: FULL English track selected, not forced",
          p._selected == 4)
    check("forced detected by name", track_is_forced(p._track_meta[3]))
    check("plain track not forced", not track_is_forced(p._track_meta[4]))

    p2 = MkvSubParser(prefer_language="eng")
    p2._track_meta = {
        3: {"codec": "S_TEXT/UTF8", "lang": "en", "name": "",
            "forced": True},
        4: {"codec": "S_TEXT/UTF8", "lang": "en", "name": ""},
    }
    p2._select_track()
    check("FlagForced element honored (unnamed tracks)",
          p2._selected == 4)
    check("forced detected by flag", track_is_forced(p2._track_meta[3]))

    p3 = MkvSubParser(prefer_language="eng")
    p3._track_meta = {
        3: {"codec": "S_TEXT/UTF8", "lang": "en",
            "name": "English (forced)"},
        4: {"codec": "S_TEXT/UTF8", "lang": "spa", "name": "Spanish"},
    }
    p3._select_track()
    check("forced-only English still selected (foreign film)",
          p3._selected == 3)

    p4 = MkvSubParser(prefer_language="eng")
    p4._track_meta = {
        3: {"codec": "S_TEXT/UTF8", "lang": "en", "name": "Forced"},
        4: {"codec": "S_TEXT/UTF8", "lang": "en", "name": ""},
    }
    p4._select_track()
    check("bare 'Forced' name skipped for the unnamed full track",
          p4._selected == 4)

    print("[2] the parse pipeline on a forced-first file (name + flag "
          "variants)")
    for label, tracks in (("name-marked", NETFLIX_LAYOUT),
                          ("FlagForced-marked", FLAG_ONLY_LAYOUT)):
        data = build_mkv(tracks, DIALOGUE_CLUSTERS)
        parser = MkvSubParser(prefer_language="eng")
        parser.feed(data)
        texts = [c[2] for c in parser.cues]
        check("%s: full track's cues emitted" % label,
              "what the hell was that" in texts
              and "I have no damn idea" in texts)
        check("%s: forced track's cue NOT emitted" % label,
              "[foreign speech]" not in texts)

    data = build_mkv(FORCED_ONLY_LAYOUT, DIALOGUE_CLUSTERS)
    parser = MkvSubParser(prefer_language="eng")
    parser.feed(data)
    check("forced-only fallback still emits the forced cue",
          any("foreign" in c[2] for c in parser.cues))

    print("[3] CC-menu picks re-target the relay")
    app = QtWidgets.QApplication.instance() \
        or QtWidgets.QApplication(sys.argv)
    relay = VodRelay()
    relay.parser_tracks = {3: "S_TEXT/UTF8", 4: "S_TEXT/UTF8",
                           5: "S_TEXT/UTF8"}
    relay.parser_tracks_meta = {
        3: {"codec": "S_TEXT/UTF8", "lang": "en", "name": "Forced"},
        4: {"codec": "S_TEXT/UTF8", "lang": "en", "name": ""},
        5: {"codec": "S_TEXT/UTF8", "lang": "en", "name": "SDH"},
    }
    check("set_selected_track accepts a known track",
          relay.set_selected_track(5) is True)
    check("set_selected_track pins the pick",
          relay._prefer_track == 5 and relay._tap_restart)
    check("set_selected_track rejects unknown tracks",
          relay.set_selected_track(99) is False)
    check("set_selected_track is idempotent",
          relay.set_selected_track(5) is False)
    relay._tap_restart = False
    check("language switch retires the explicit pick",
          relay.set_prefer_language("spa") is True
          and relay._prefer_track is None)

    # name mapping (PlayerView._relay_track_for_pick) — needs a real
    # view for _spu_name; keep it light: offscreen Config load like the
    # other suites
    from src.config import Config
    from src.ui.player_view import PlayerView
    view = PlayerView(Config.load())
    view._spu_name = "Forced - [English]"
    check("VLC 'Name - [lang]' maps to the named MKV track",
          view._relay_track_for_pick(relay) == 3)
    view._spu_name = "Track 2 - [English]"
    check("VLC generic 'Track N' maps to the Nth subtitle track",
          view._relay_track_for_pick(relay) == 4)
    view._spu_name = "SDH - [English]"
    check("SDH pick maps to the SDH track",
          view._relay_track_for_pick(relay) == 5)
    view._spu_name = "English (SDH) - [English]"
    check("ODETTE-style 'English (SDH)' name maps to the SDH track",
          view._relay_track_for_pick(relay) == 5)
    view._spu_name = "Commentary - [English]"
    check("unmapped name returns None (language fallback path)",
          view._relay_track_for_pick(relay) is None)
    try:
        view.close()
        view.deleteLater()
    except Exception:
        pass

    print()
    print(f"forced-tracks: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for name in FAIL:
            print("  FAILED:", name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
