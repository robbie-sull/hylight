# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller build spec for HyLight on Windows. Produces dist/HyLight/
(a --onedir folder with HyLight.exe inside). Build on Windows with
build_windows.bat, or by hand:

    pyinstaller --noconfirm HyLight_win.spec

The macOS build is HyLight.spec; this one differs in:
  - keyring's Windows Credential Manager backend (keyring.backends.Windows,
    plus win32ctypes, which it uses) instead of the macOS Keychain one --
    keyring discovers backends at runtime, so without the hidden import it
    silently falls back to no credential storage.
  - pystray's Windows backend (pystray._win32) and tray_loop for the tray
    icon. pystray picks its backend at import time, so static analysis
    can't see it.
  - the tray icon image (assets/HyLight_icon_1024.png) is bundled.
  - no PyObjC / native_loop (macOS only).

--onedir rather than --onefile: a onefile exe unpacks itself to a temp
folder on every launch, which is slower and is exactly the pattern
antivirus heuristics flag most often.

console=False: no console window. sys.stderr is None in that case, so
logging only goes to ~/.dexcom_led_button/hylight.log (see
dexcom_led_button.py).
"""

from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

VERSION = (0, 5, 0, 0)
VERSION_STR = "0.5.0"

datas = [
    # web_ui.py reads this at import time to inline the header logo, and
    # tray_loop.py loads the tray icon -- both from ASSETS_DIR, which is
    # sys._MEIPASS/assets in a frozen build.
    ("assets/logo_header_b64.txt", "assets"),
    ("assets/HyLight_icon_1024.png", "assets"),
]
binaries = []
hiddenimports = [
    "keyring.backends.Windows",
    "win32ctypes.core",
    "web_ui",
    "config",
    # Imported lazily in main() on Windows only.
    "tray_loop",
    "pystray._win32",
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
    # macOS-only modules; keep them out even if something mentions them.
    excludes=["native_loop", "objc", "AppKit", "Foundation", "PyObjCTools"],
    noarchive=False,
)
pyz = PYZ(a.pure)

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=VERSION, prodvers=VERSION),
    kids=[
        StringFileInfo([
            StringTable("040904B0", [
                StringStruct("CompanyName", "Danko Panko's"),
                StringStruct("FileDescription", "HyLight"),
                StringStruct("FileVersion", VERSION_STR),
                StringStruct("InternalName", "HyLight"),
                StringStruct("LegalCopyright", "Danko Panko's"),
                StringStruct("OriginalFilename", "HyLight.exe"),
                StringStruct("ProductName", "HyLight"),
                StringStruct("ProductVersion", VERSION_STR),
            ])
        ]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HyLight",
    icon="assets/HyLight.ico",
    version=version_info,
    debug=False,
    strip=False,
    upx=False,  # UPX-packed exes trip antivirus far more often
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
