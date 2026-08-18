# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: builds dist\\MichaelTV.exe — a single
double-clickable file, no console window, no Python install required.

Build:  build.bat  (or: .venv\\Scripts\\pyinstaller --noconfirm MichaelTVPlayer.spec)
"""

datas = [("assets/icon.ico", "assets")]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath={},
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MichaelTV",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,               # no terminal window
    icon="assets/icon.ico",
)
