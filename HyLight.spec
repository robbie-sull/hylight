# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller build spec for HyLight (the LED button's monitoring app +
settings web UI). Produces dist/HyLight.app.

Build:
    pyinstaller HyLight.spec

Two dependencies need explicit help here, both confirmed empirically
via a standalone smoke test before this spec was written:
  - `keyring`'s macOS Keychain backend is discovered dynamically at
    runtime (not a plain import PyInstaller's static analysis can see),
    so it needs a --hidden-import or it silently falls back to no
    credential storage in the frozen build.
  - `hid` (hidapi) wraps a compiled native library; collect_all() pulls
    in its binary alongside the Python bindings.

console=False: a normal double-click app with no Terminal window. Logs go to
~/.dexcom_led_button/hylight.log instead (see dexcom_led_button.py).
"""

from PyInstaller.utils.hooks import collect_all

datas = [
    # web_ui.py reads this at import time to inline the header logo
    # image -- see web_ui.py's ASSETS_DIR (sys._MEIPASS in a frozen
    # build, not next to web_ui.py's own __file__).
    ("assets/logo_header_b64.txt", "assets"),
]
binaries = []
hiddenimports = [
    "keyring.backends.macOS",
    # Local modules -- already traced automatically via the literal
    # `from web_ui import create_app` / `from config import ...`
    # statements in dexcom_led_button.py, listed here too as cheap
    # insurance against that changing later.
    "web_ui",
    "config",
    # Cocoa event loop (native_loop.py) -- imported lazily at runtime and
    # only in the packaged build, so list it and PyObjC's modules
    # explicitly rather than trusting static analysis to find them.
    "native_loop",
    "objc",
    "AppKit",
    "Foundation",
    "PyObjCTools.AppHelper",
]

for pkg in ("hid",):
    pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hiddenimports

a = Analysis(
    ["dexcom_led_button.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HyLight",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    target_arch=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="HyLight",
)

app = BUNDLE(
    coll,
    name="HyLight.app",
    icon="assets/HyLight.icns",
    bundle_identifier="com.dankopanko.hylight",
    version="0.5.0",
    info_plist={
        "CFBundleName": "HyLight",
        "CFBundleDisplayName": "HyLight",
        "CFBundleShortVersionString": "0.5.0",
        "NSHumanReadableCopyright": "Danko Panko's",
        "LSMinimumSystemVersion": "11.0",
    },
)
