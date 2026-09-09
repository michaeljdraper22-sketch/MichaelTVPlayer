#!/usr/bin/env python3
"""Tap the center of the first UI node whose text contains argv[1].

Used by the cloud emulator test workflow: dumps the current UI hierarchy
via uiautomator, finds the node, taps its center. Exits 2 (without
tapping) when the text is not on screen.

    ADB=<path-to-adb> python3 scripts/tap_text.py "Settings"
"""

import os
import re
import subprocess
import sys


def main():
    if len(sys.argv) != 2:
        print("usage: tap_text.py <text>", file=sys.stderr)
        return 2
    want = sys.argv[1].lower()
    adb = os.environ.get("ADB", "adb")

    subprocess.run([adb, "shell", "uiautomator", "dump", "/sdcard/ui.xml"],
                   capture_output=True, text=True)
    subprocess.run([adb, "pull", "/sdcard/ui.xml", "/tmp/ui.xml"],
                   capture_output=True)
    try:
        data = open("/tmp/ui.xml", encoding="utf-8", errors="replace").read()
    except OSError:
        print("tap_text: could not read UI dump", file=sys.stderr)
        return 2

    for m in re.finditer(
            r'text="([^"]*)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            data):
        if want in m.group(1).lower():
            x = (int(m.group(2)) + int(m.group(4))) // 2
            y = (int(m.group(3)) + int(m.group(5))) // 2
            subprocess.run([adb, "shell", "input", "tap", str(x), str(y)])
            print("tapped '%s' at %d,%d" % (sys.argv[1], x, y))
            return 0
    print("NOT FOUND in dump: '%s'" % sys.argv[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
