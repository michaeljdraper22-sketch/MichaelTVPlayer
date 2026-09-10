#!/usr/bin/env python3
"""Prepare the shared Python core + version for the Gradle/Chaquopy app.

Run from anywhere (normally via android-gradle/build_local.py):

    python android-gradle/prepare_core.py

What it does:

1. Copies the Windows app's pure-Python core modules verbatim from ../src
   into app/src/main/python/michaeltv_core/ (stremio.py, xtream.py,
   models.py — xtream.py imports ``from .models import EpgEntry,
   UserInfo`` at module level; stremio.py only needs the stdlib +
   requests).  The copy is byte-identical to src/, so the Android app
   runs the exact same Stremio/Xtream logic as the desktop player.
2. Writes michaeltv_core/__init__.py exposing the modules and APP_VERSION
   (regexed from src/config.py, same pattern as android/prepare_core.py).
3. Rewrites the versionCode / versionName lines in app/build.gradle —
   keyed on the MTP_VERSION_CODE / MTP_VERSION_NAME comment markers, so
   the APK always carries the desktop app's version ("2.1" -> 21).
4. Byte-compiles everything it produced, plus bridge.py, and exits
   non-zero on any error, so a broken sync can never reach Gradle.

Idempotent: safe to run any number of times.  Stdlib only — works on
Windows and Linux, Python 3.9+.
"""

import py_compile
import re
import shutil
import sys
from pathlib import Path

AG_DIR = Path(__file__).resolve().parent
REPO_ROOT = AG_DIR.parent
SRC_DIR = REPO_ROOT / "src"
CORE_DIR = AG_DIR / "app" / "src" / "main" / "python" / "michaeltv_core"
BRIDGE_PY = AG_DIR / "app" / "src" / "main" / "python" / "bridge.py"
APP_GRADLE = AG_DIR / "app" / "build.gradle"
CONFIG_PY = SRC_DIR / "config.py"

# xtream.py needs models.py (top-level ``from .models import ...``);
# stremio.py is fully self-contained besides requests.  Nothing else in
# src/ is imported by either (xtream's ``from . import feedback`` sits
# inside try/except and degrades silently when feedback is absent).
CORE_MODULES = ("stremio.py", "xtream.py", "models.py")

INIT_TEMPLATE = '''"""MichaelTV core — verbatim copy of the Windows app's src/ modules.

Synced from ../../../src by android-gradle/prepare_core.py; never edit
these files here, edit src/ and re-run the script.  stremio.py needs
only the stdlib + requests; xtream.py additionally imports models (same
package).  The ``feedback`` import inside xtream is optional and absent
here.  GENERATED — do not commit.
"""

APP_VERSION = "%s"

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
    """Digits of the version with dots stripped ("2.1" -> 21)."""
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
    return copied


def write_init(version: str) -> Path:
    init = CORE_DIR / "__init__.py"
    init.write_text(INIT_TEMPLATE % version, encoding="utf-8")
    return init


def rewrite_gradle_version(version: str, numeric: int) -> None:
    """Update the MTP_VERSION_*-marked lines in app/build.gradle."""
    try:
        text = APP_GRADLE.read_text(encoding="utf-8")
    except OSError as exc:
        fail("cannot read %s: %r" % (APP_GRADLE, exc))
    code_line = ('%sversionCode %d  // MTP_VERSION_CODE '
                 '(managed by prepare_core.py — do not edit)')
    name_line = ('%sversionName "%s"  // MTP_VERSION_NAME '
                 '(managed by prepare_core.py — do not edit)')
    text, n_code = re.subn(
        r"(?m)^(\s*)versionCode\s+\d+.*MTP_VERSION_CODE.*$",
        lambda m: code_line % (m.group(1), numeric), text)
    if n_code != 1:
        fail("app/build.gradle has no MTP_VERSION_CODE-marked versionCode "
             "line to rewrite (matches: %d)" % n_code)
    text, n_name = re.subn(
        r'(?m)^(\s*)versionName\s+"[^"]*".*MTP_VERSION_NAME.*$',
        lambda m: name_line % (m.group(1), version), text)
    if n_name != 1:
        fail("app/build.gradle has no MTP_VERSION_NAME-marked versionName "
             "line to rewrite (matches: %d)" % n_name)
    try:
        APP_GRADLE.write_text(text, encoding="utf-8")
    except OSError as exc:
        fail("cannot write %s: %r" % (APP_GRADLE, exc))


def compile_all() -> list:
    """py_compile every generated core module + bridge.py; fail on error."""
    compiled = []
    targets = sorted(CORE_DIR.glob("*.py")) + [BRIDGE_PY]
    for path in targets:
        if not path.is_file():
            fail("missing python file: %s" % path)
        py_compile.compile(str(path), doraise=True)
        compiled.append(path)
    return compiled


def main() -> None:
    if not SRC_DIR.is_dir():
        fail("src/ not found at %s — run from the MichaelTVPlayer repo" % SRC_DIR)

    version = read_app_version()
    copied = copy_core_modules()
    init = write_init(version)
    rewrite_gradle_version(version, numeric_version(version))
    compiled = compile_all()

    print("prepare_core: OK")
    print("  copied %d module(s) into %s:" % (len(copied), CORE_DIR))
    for path in copied:
        print("    %s (%d bytes)" % (path.name, path.stat().st_size))
    print("  wrote %s (APP_VERSION = %r)" % (init.name, version))
    print("  app/build.gradle: versionName %s, versionCode %d"
          % (version, numeric_version(version)))
    print("  byte-compiled %d file(s): %s"
          % (len(compiled), ", ".join(p.name for p in compiled)))


if __name__ == "__main__":
    main()
