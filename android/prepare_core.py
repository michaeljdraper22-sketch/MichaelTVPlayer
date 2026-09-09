#!/usr/bin/env python3
"""Prepare the shared core for the Android build.

Run this from anywhere (usually the repo root) BEFORE buildozer:

    python android/prepare_core.py

What it does:

1. Copies the Windows app's pure-Python core modules verbatim from ../src
   into ./michaeltv_core/ (stremio.py, xtream.py and models.py — xtream.py
   imports ``from .models import EpgEntry, UserInfo`` at module level, and
   stremio.py only needs the stdlib + requests). The copy is byte-identical
   to src/, so the Android app runs the exact same Stremio/Xtream logic as
   the desktop player.
2. Writes michaeltv_core/__init__.py exposing the modules.
3. Reads APP_VERSION from src/config.py and rewrites the ``version =`` and
   ``android.numeric_version =`` lines in ./buildozer.spec so the APK always
   carries the desktop app's version ("2.1" -> numeric 21).
4. Byte-compiles every .py file in michaeltv_core/ and FAILS (exit 1) on
   any error, so a broken sync can never reach buildozer.

Stdlib only — works on Windows and Linux, Python 3.9+.
"""

import py_compile
import re
import shutil
import sys
from pathlib import Path

ANDROID_DIR = Path(__file__).resolve().parent
REPO_ROOT = ANDROID_DIR.parent
SRC_DIR = REPO_ROOT / "src"
CORE_DIR = ANDROID_DIR / "michaeltv_core"
SPEC_PATH = ANDROID_DIR / "buildozer.spec"
CONFIG_PY = SRC_DIR / "config.py"

# xtream.py needs models.py (top-level ``from .models import ...``);
# stremio.py is fully self-contained besides requests.  Nothing else in
# src/ is imported by either (xtream's ``from . import feedback`` sits
# inside try/except and degrades silently when feedback is absent).
CORE_MODULES = ("stremio.py", "xtream.py", "models.py")

INIT_PY = '''"""MichaelTV core — verbatim copy of the Windows app's src/ modules.

Synced from ../src by android/prepare_core.py; never edit these files
here, edit src/ and re-run the script.  stremio.py needs only the stdlib
+ requests; xtream.py additionally imports models (same package).  The
``feedback`` import inside xtream is optional and absent here.
"""

from . import models, stremio, xtream  # noqa: F401
'''


def fail(message: str) -> None:
    print("prepare_core: ERROR: %s" % message, file=sys.stderr)
    sys.exit(1)


def read_app_version() -> str:
    try:
        text = CONFIG_PY.read_text(encoding="utf-8")
    except OSError as exc:
        fail("cannot read %s: %r" % (CONFIG_PY, exc))
    m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not m:
        fail('no APP_VERSION = "..." line found in src/config.py')
    return m.group(1)


def numeric_version(version: str) -> int:
    """Digits of the version with dots stripped ("2.1" -> 21).

    Android requires a positive integer; a version with no digits at all
    (shouldn't happen) falls back to 1.
    """
    digits = re.sub(r"\D", "", version)
    return int(digits) if digits else 1


def copy_core_modules() -> list:
    copied = []
    CORE_DIR.mkdir(parents=True, exist_ok=True)
    for name in CORE_MODULES:
        src = SRC_DIR / name
        if not src.is_file():
            fail("missing core module: %s" % src)
        dst = CORE_DIR / name
        shutil.copyfile(src, dst)          # byte-identical copy
        copied.append(dst)
    init = CORE_DIR / "__init__.py"
    init.write_text(INIT_PY, encoding="utf-8")
    copied.append(init)
    return copied


def rewrite_spec(version: str, numeric: int) -> None:
    if not SPEC_PATH.is_file():
        fail("missing %s (run this from a checkout that has android/)" % SPEC_PATH)
    try:
        text = SPEC_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        fail("cannot read %s: %r" % (SPEC_PATH, exc))
    text, n_ver = re.subn(r"(?m)^version\s*=.*$", "version = %s" % version,
                          text, count=1)
    if n_ver != 1:
        fail("buildozer.spec has no 'version =' line to rewrite")
    text, n_num = re.subn(
        r"(?m)^android\.numeric_version\s*=.*$",
        "android.numeric_version = %d" % numeric, text, count=1)
    if n_num != 1:
        fail("buildozer.spec has no 'android.numeric_version =' line to rewrite")
    try:
        SPEC_PATH.write_text(text, encoding="utf-8")
    except OSError as exc:
        fail("cannot write %s: %r" % (SPEC_PATH, exc))


def compile_core() -> list:
    """py_compile every file in michaeltv_core/; raise on any error."""
    compiled = []
    for path in sorted(CORE_DIR.glob("*.py")):
        py_compile.compile(str(path), doraise=True)
        compiled.append(path)
    return compiled


def main() -> None:
    if not SRC_DIR.is_dir():
        fail("src/ not found at %s — run from the MichaelTVPlayer repo" % SRC_DIR)

    version = read_app_version()
    copied = copy_core_modules()
    rewrite_spec(version, numeric_version(version))
    compiled = compile_core()

    print("prepare_core: OK")
    print("  copied %d module(s) into %s:" % (len(copied), CORE_DIR))
    for path in copied:
        print("    %s (%d bytes)" % (path.name, path.stat().st_size))
    print("  APP_VERSION = %r -> buildozer.spec version = %s, "
          "android.numeric_version = %d" % (version, version,
                                            numeric_version(version)))
    print("  byte-compiled %d file(s): %s"
          % (len(compiled), ", ".join(p.name for p in compiled)))


if __name__ == "__main__":
    main()
