# -*- coding: utf-8 -*-
"""Regression test: the v2.1 hardening that came out of the 2026-09-02
field night (GitHub issues #3/#4/#5).

  1. path-style credential redaction — feedback.redact_url at LOG time
     (the player's probe / never-started lines leaked a real account
     into PUBLIC issue #3) and diagnostics.scrub on upload as backstop
  2. updater.verify_sha256 against a LOCAL checksum file (the original
     draft urlopen'd a path and would have crashed the first time a
     release actually shipped a .sha256 asset)
  3. xtream._http_error_message — plain-language 403/429/5xx wording
     that still carries the provider's own error body

Run:  python test_v21_fixes.py
"""
import hashlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.feedback import redact_url  # noqa: E402
from src import diagnostics, updater  # noqa: E402
from src.xtream import _http_error_message  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "FAIL ") + name + ("" if cond else extra))


class _FakeResp:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text


def main():
    print("[1] redact_url — path-style credentials masked at log time")
    leak = ("playback never started within 20s (state=idle "
            "url=http://26D7N4FV.cdngold8k.com/live/26D7N4FV/85093768/"
            "96054.ts) — probing stream")
    r = redact_url(leak)
    check("issue #3 leak line: user+pass gone, id kept",
          "26D7N4FV/85093768" not in r and "85093768" not in r
          and "96054.ts" in r and "live/REDACTED/REDACTED/" in r)
    probe = "stream probe: HTTP 403 application/json data — url=http://h.com/movie/myuser/mypass123/456.mkv"
    r = redact_url(probe)
    check("movie URL redacted", "mypass123" not in r
          and "movie/REDACTED/REDACTED/456.mkv" in r)
    check("timeshift URL redacted",
          "timeshift/REDACTED/REDACTED/1.ts"
          in redact_url("http://h/timeshift/ab/cd/1.ts"))
    check("series URL redacted",
          "series/REDACTED/REDACTED/2.mp4"
          in redact_url("http://h/series/ab/cd/2.mp4"))
    check("query-style creds untouched here (scrubber's job)",
          "password=zz" in redact_url("http://h/a?password=zz"))
    check("non-URL text unchanged", redact_url("hello world") == "hello world")
    check("empty/None safe", redact_url("") == "" and redact_url(None) == "")

    print("[2] diagnostics.scrub backstop")
    s = diagnostics.scrub(
        "probe: HTTP 403 — url=http://x.cdngold8k.com/live/UU/PP/7.ts\n"
        "movie http://h/movie/u2/p2/9.mkv")
    check("scrub redacts path creds on upload",
          "live/REDACTED/REDACTED/7.ts" in s and "PP" not in s
          and "movie/REDACTED/REDACTED/9.mkv" in s and "p2" not in s)

    print("[3] verify_sha256 against a LOCAL checksum file")
    root = tempfile.mkdtemp(prefix="mtp_sha_")
    zp = os.path.join(root, "u.zip")
    with open(zp, "wb") as f:
        f.write(b"payload-bytes")
    good = os.path.join(root, "good.sha256")
    with open(good, "w") as f:
        f.write("%s  u.zip\n" % hashlib.sha256(b"payload-bytes").hexdigest())
    bad = os.path.join(root, "bad.sha256")
    with open(bad, "w") as f:
        f.write("%s  u.zip\n" % ("0" * 64))
    junk = os.path.join(root, "junk.sha256")
    with open(junk, "w") as f:
        f.write("")
    try:
        updater.verify_sha256(zp, good)
        check("matching checksum passes", True)
    except RuntimeError as e:
        check("matching checksum passes", False, f"({e})")
    try:
        updater.verify_sha256(zp, bad)
        check("mismatching checksum raises", False)
    except RuntimeError as e:
        check("mismatching checksum raises", "integrity check" in str(e))
    try:
        updater.verify_sha256(zp, junk)
        check("empty checksum file raises", False)
    except RuntimeError:
        check("empty checksum file raises", True)

    print("[4] _http_error_message — plain language, body kept")
    m = _http_error_message(_FakeResp(
        403, '{"error":"Authentication failed"}'))
    check("403 keeps the code", "HTTP 403" in m)
    check("403 carries the provider's error text",
          "Authentication failed" in m)
    check("403 explains what it usually is",
          "refusing this connection" in m)
    m = _http_error_message(_FakeResp(429, ""))
    check("429 hint present", "rate-limiting" in m and "HTTP 429" in m)
    m = _http_error_message(_FakeResp(503, "busy"))
    check("5xx hint present", "server error" in m and "busy" in m)
    m = _http_error_message(_FakeResp(404, ""))
    check("other codes keep the classic wording",
          m.startswith("Server returned HTTP 404") and "[" not in m)

    print()
    if FAIL:
        print("FAILED %d:" % len(FAIL))
        for f in FAIL:
            print("  - " + f)
        return 1
    print("ALL %d CHECKS PASSED" % len(PASS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
