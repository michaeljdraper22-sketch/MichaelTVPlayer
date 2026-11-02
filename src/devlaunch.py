"""Windowless-Python launching for source-tree (dev) runs.

Every Windows entry point that can spawn or register a MichaelTV
launcher from a source run — the Stremio server.js handoff relay
(src/streampatch.py) and the .m3u "Open with" ProgId command
(src/fileassoc.py) — must use pythonw.exe. Registering or spawning
console python.exe instead attaches a console window for the whole
player session (and Stremio's child.exec of the handoff even waits on
it), which is why the 2026-11-02 dev-launch-mode work routed them all
through pythonw_exe() here.
"""

import os
import sys


def repo_root() -> str:
    """Repo root for a source run (the parent of src/)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main_py_path() -> str:
    return os.path.join(repo_root(), "main.py")


def pythonw_exe() -> str:
    """A WINDOWLESS Python that can run main.py: the repo venv's
    pythonw when present, else pythonw.exe beside sys.executable (same
    venv / same install), else sys.executable unchanged (last resort —
    a checkout without .venv; may flash a console)."""
    cand = os.path.join(repo_root(), ".venv", "Scripts", "pythonw.exe")
    if os.path.isfile(cand):
        return cand
    exe = sys.executable or "pythonw.exe"
    if os.path.basename(exe).lower() == "pythonw.exe":
        return exe
    sibling = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.isfile(sibling):
        return sibling
    return exe
