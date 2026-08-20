# -*- coding: utf-8 -*-
"""Vendored CCExtractor: the static no-install fallback for releases.

Discovery order (live_cc.find_ccextractor): an INSTALLED CCExtractor
always wins (PATH / winget / Program Files); the vendored static build
only serves machines without one. These checks pin that contract.

Run:  .venv\\Scripts\\python.exe test_bundled_ccx.py
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.live_cc import (bundled_ccextractor, ccx_args,  # noqa: E402
                         find_ccextractor)

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


def main():
    root = os.path.dirname(os.path.abspath(__file__))
    vendored = os.path.join(root, "vendor", "ccextractorwin.exe")

    check("vendored exe exists in the repo", os.path.isfile(vendored))
    check("bundled_ccextractor() finds it (dev layout)",
          os.path.abspath(bundled_ccextractor()) == vendored)

    found = find_ccextractor()
    check("find_ccextractor() returns something", bool(found))
    if found:
        check("installed copy outranks the vendored fallback",
              os.path.abspath(found) != vendored
              or not (
                  os.path.isfile(r"C:\Program Files\CCExtractor"
                                 r"\ccextractorwinfull.exe")
                  or any(__import__("glob").glob(os.path.expandvars(
                      r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"
                      r"\CCExtractor*_*\ccextractorwinfull.exe")))))

    p = subprocess.run([vendored, "--version"], stdout=subprocess.PIPE,
                       stderr=subprocess.DEVNULL, timeout=30)
    out = p.stdout.decode("utf-8", "replace")
    check("vendored exe runs standalone (exit 0)", p.returncode == 0)
    check("reports itself as CCExtractor", "CCExtractor" in out)

    # The 0.88 build predates the long flags: it must get the legacy
    # single-dash form, while modern builds keep --stdin/--stdout.
    v_args = ccx_args(vendored)
    check("vendored 0.88 gets the legacy single-dash flags",
          v_args == ["-in=ts", "-srt", "-utf8", "-", "-stdout"])
    if found:
        m_args = ccx_args(found)
        check("installed build keeps the modern flags",
              m_args == ["-in=ts", "-srt", "-utf8", "--stdin", "--stdout"]
              or os.path.abspath(found) == vendored)
    probe = subprocess.run([vendored] + v_args, input=b"",
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=30)
    check("legacy flags accepted (no 'not understood' error)",
          b"not understood" not in probe.stdout
          and b"not understood" not in (probe.stderr or b""))

    print()
    if FAIL:
        print(f"FAILED {len(FAIL)}: {FAIL}")
        return 1
    print(f"all {len(PASS)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
