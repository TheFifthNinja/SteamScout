# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('C:\\Users\\GGPC\\Documents\\SteamScout\\SteamScoutIcon.png', '.'), ('C:\\Users\\GGPC\\Documents\\SteamScout\\overlay_ui.html', '.')]
binaries = []
hiddenimports = ['Backend', 'Overlay', 'pystray._win32', 'webview', 'webview.platforms.winforms', 'webview.platforms.edgechromium', 'clr_loader', 'pythonnet']
tmp_ret = collect_all('webview')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['C:\\Users\\GGPC\\Documents\\SteamScout\\SteamScout.pyw'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    version='C:\\Users\\GGPC\\Documents\\SteamScout\\version_info.txt',
    icon=['C:\\Users\\GGPC\\Documents\\SteamScout\\steamscout.ico'],
    manifest='C:\\Users\\GGPC\\Documents\\SteamScout\\SteamScout.manifest',
)
