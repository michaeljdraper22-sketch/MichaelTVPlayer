# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: builds dist\\MichaelTV.exe — a single
double-clickable file, no console window, no Python install required.

Build:  build.bat  (or: .venv\\Scripts\\pyinstaller --noconfirm MichaelTVPlayer.spec)
"""

# Vendored CCExtractor 0.96.6 win portable, minimal runtime subset
# (WP4b): the CLI exe + the DLLs it statically imports, see
# vendor/CCEXTRACTOR-VENDORED.txt. Zero-install caption support in
# every release (an installed copy still wins discovery — see
# live_cc.find_ccextractor).
_vendor_ccx = [
    "ccextractorwinfull.exe",
    "libgpac.dll", "avcodec-60.dll", "avformat-60.dll", "avfilter-9.dll",
    "avdevice-60.dll", "avutil-58.dll", "swscale-7.dll", "swresample-4.dll",
    "postproc-57.dll", "libcryptoMD.dll", "libsslMD.dll",
    "OpenSVCDecoder.dll", "vcruntime140.dll",
]
datas = [
    ("assets/icon.ico", "assets"),
    ("vendor/COPYING-ccextractor.txt", "vendor"),
    ("vendor/CCEXTRACTOR-VENDORED.txt", "vendor"),
] + [(f"vendor/{name}", "vendor") for name in _vendor_ccx]

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
