# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['C:\\Users\\GGPC\\Documents\\SteamScout\\SteamScout.pyw'],
    pathex=[],
    binaries=[],
    datas=[('C:\\Users\\GGPC\\Documents\\SteamScout\\SteamScoutIcon.png', '.')],
    hiddenimports=['pystray._win32'],
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
    name='SteamScout',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['C:\\Users\\GGPC\\Documents\\SteamScout\\steamscout.ico'],
)
