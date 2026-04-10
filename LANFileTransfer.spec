# -*- mode: python ; coding: utf-8 -*-


import os

DEBUG_BUILD = os.environ.get("LANFILETRANSFER_PYI_DEBUG", "0") == "1"


a = Analysis(
    ['tray_app.py'],
    pathex=[],
    binaries=[],
    datas=[('templates', 'templates'), ('logos.png', '.')],
    hiddenimports=['webview'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='LANFileTransfer',
    debug=DEBUG_BUILD,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='logos.ico',
)
