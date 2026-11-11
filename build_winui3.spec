# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for LivePhotoMaker, WinUI3 (Microsoft.UI.Xaml) edition.

    .venv\\Scripts\\python.exe -m PyInstaller --noconfirm build_winui3.spec
"""
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH)
SHARED = ROOT.parent
NAME = "LivePhotoMaker"

datas, binaries, hiddenimports = [], [], []
for pkg in ("imageio_ffmpeg",):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

hiddenimports += [
    "winui3_ui",
    "winui3_ui.winui3_shell",
    "winui3_ui.tkcompat",
    "winui3_ui.native_dialogs",
    "winrt.microsoft.ui.xaml.xamltypeinfo",
    "winrt.windows.foundation",
    "winrt.windows.graphics",
]

a = Analysis(
    [str(ROOT / "livephoto.py")],
    pathex=[str(SHARED)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["sv_ttk", "pywinstyles", "darkdetect",
              "imageio", "numpy", "matplotlib"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                     # UPX breaks WinRT DLLs
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[str(ROOT / "icon.ico")],
)
