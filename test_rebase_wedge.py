# -*- coding: utf-8 -*-
"""Mid-stream rebase wedge: garbage 0x00 size byte at a seek boundary.

A seek (or a provider body-death reopen) rebases the cache window to an
arbitrary mid-block byte and the tap restarts its parser there. If the
garbage decodes as [nonzero id byte][0x00], the 0x00 can NEVER begin a
size vint (>8-byte length = invalid) — but _parse treated "size
undecodable" as "wait for more bytes" and broke on every pass, forever.
The buffer ballooned (90+ MB measured on the real E06 stream) and zero
cues came out: subtitles and profanity muting stayed dead until a
stream reload (live-seen 2026-11-02 00:08, Love Is Blind S11E06 scrub).

Run:  .venv\\Scripts\\python.exe test_rebase_wedge.py
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# reuse the synthetic MKV builder from the forced-tracks suite
_spec = importlib.util.spec_from_file_location(
    "tft", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "test_forced_tracks.py"))
_tft = importlib.util.module_from_spec(_spec)
sys.modules["tft"] = _tft
src = open(_spec.origin, encoding="utf-8").read().split("def main()")[0]
exec(compile(src, _spec.origin, "exec"), _tft.__dict__)

from src.mkv_subs import MkvSubParser  # noqa: E402

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


META = {
    3: {"codec": "S_TEXT/UTF8", "lang": "en", "name": "Forced"},
    4: {"codec": "S_TEXT/UTF8", "lang": "en", "name": ""},
    5: {"codec": "S_TEXT/UTF8", "lang": "en", "name": "SDH"},
}


def fresh_mid_stream():
    p = MkvSubParser(prefer_language="eng", mid_stream=True)
    p._track_meta = {n: dict(m) for n, m in META.items()}
    p._select_track()
    p._saw_tracks = True
    return p


def main():
    data = _tft.build_mkv(
        _tft.NETFLIX_LAYOUT,
        [_tft._cluster(1000, _tft._simple_block(4, 0, "line one")),
         _tft._cluster(4000, _tft._simple_block(4, 0, "line two"))])

    print("[1] the wedge: garbage id + 0x00 size at a rebase boundary")
    p = fresh_mid_stream()
    p.feed(b"\xce\x00\x93\x26 garbage head" + data[len(data) // 2:])
    check("0x00 size garbage no longer wedges the parse",
          any("line" in c[2] for c in p.cues))
    check("buffer drained after recovery (no balloon)",
          len(p.buf) < 4096)

    print("[2] plain garbage head (no 0x00) still resyncs and parses")
    p2 = fresh_mid_stream()
    p2.feed(b"\x12\x34\x56\x78\x9a" + data[len(data) // 2:])
    check("mid-block entry still emits cues", len(p2.cues) >= 1)

    print("[3] a size vint genuinely straddling the chunk edge waits, "
          "then parses")
    p3 = fresh_mid_stream()
    tail = data[data.index(b"\x1f\x43\xb6\x75"):]   # from first cluster
    cut = 8                                          # inside the cluster
    #                                          # body (Timecode child)
    p3.feed(tail[:cut])
    check("straddled element buffered, nothing wedged or lost",
          len(p3.cues) == 0 and 0 <= len(p3.buf) < 16)
    p3.feed(tail[cut:])
    check("completion of the header parses the cluster cues",
          len(p3.cues) >= 1)

    print("[4] absolute timecodes survive the resync (file clock)")
    p4 = fresh_mid_stream()
    p4.feed(b"\xce\x00" + tail)
    if p4.cues:
        s = p4.cues[0][0]
        check("cue sits on the file's own timeline (~1.0 s or ~4.0 s)",
              any(abs(s - want) < 0.01 for want in (1.0, 4.0)))
    else:
        check("cue sits on the file's own timeline", False)

    print()
    print(f"rebase-wedge: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for name in FAIL:
            print("  FAILED:", name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
