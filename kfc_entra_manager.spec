# PyInstaller spec for building the KFC Entra User Manager desktop app.
# Build with:  pyinstaller kfc_entra_manager.spec
# Output:      dist/KFC Entra User Manager(.exe)

# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

hiddenimports = (
    collect_submodules("msal")
    + collect_submodules("webview")
    + collect_submodules("openpyxl")
)

datas = [
    ("kfc_entra/templates", "kfc_entra/templates"),
    ("kfc_entra/static",    "kfc_entra/static"),
] + collect_data_files("openpyxl")

a = Analysis(
    ["desktop.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="KFC Entra User Manager",
    icon="kfc_entra/static/img/app.ico",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
