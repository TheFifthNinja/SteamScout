"""
Build SteamScout into a standalone Windows executable.

Usage:
    pip install pyinstaller pillow
    python build.py

The resulting SteamScout.exe is written to the  dist/  folder.
"""

import os
import sys

BUILD_DIR = os.path.dirname(os.path.abspath(__file__))
ICON_PNG = os.path.join(BUILD_DIR, "SteamScoutIcon.png")
ICON_ICO = os.path.join(BUILD_DIR, "steamscout.ico")
UI_HTML  = os.path.join(BUILD_DIR, "overlay_ui.html")
VERSION_INFO = os.path.join(BUILD_DIR, "version_info.txt")
MANIFEST = os.path.join(BUILD_DIR, "SteamScout.manifest")


def generate_ico():
    """Convert SteamScoutIcon.png → steamscout.ico (multi-size)."""
    from PIL import Image

    if not os.path.exists(ICON_PNG):
        print(f"  ERROR: {ICON_PNG} not found")
        sys.exit(1)

    src = Image.open(ICON_PNG).convert("RGBA")
    sizes = [16, 32, 48, 64, 128, 256]
    images = [src.resize((s, s), Image.LANCZOS) for s in sizes]

    images[0].save(
        ICON_ICO,
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=images[1:],
    )
    print(f"  Icon saved to {ICON_ICO}")


def build():
    print("=== SteamScout Build ===\n")

    print("[1/2] Generating .ico from SteamScoutIcon.png...")
    generate_ico()

    print("[2/2] Running PyInstaller...")
    import PyInstaller.__main__

    PyInstaller.__main__.run(
        [
            os.path.join(BUILD_DIR, "SteamScout.pyw"),
            "--name=SteamScout",
            "--onefile",
            "--windowed",
            "--noupx",
            f"--icon={ICON_ICO}",
            f"--add-data={ICON_PNG};.",
            f"--add-data={UI_HTML};.",
            f"--version-file={VERSION_INFO}",
            f"--manifest={MANIFEST}",
            "--clean",
            "--noconfirm",
            # Runtime-imported modules PyInstaller can't auto-detect
            "--hidden-import=Backend",
            "--hidden-import=Overlay",
            "--hidden-import=pystray._win32",
            "--hidden-import=webview",
            # Ensure pywebview's platform-specific backends are included
            "--hidden-import=webview.platforms.winforms",
            "--hidden-import=webview.platforms.edgechromium",
            "--hidden-import=clr_loader",
            "--hidden-import=pythonnet",
            "--collect-all=webview",
            # Search / Elasticsearch (cloud-connected, no local ES needed)
            "--hidden-import=search",
            "--hidden-import=search.es_client",
            "--hidden-import=search.catalog",
            "--hidden-import=search.service",
            "--hidden-import=elasticsearch",
            "--hidden-import=elastic_transport",
        ]
    )

    print("\n=== Build complete! ===")
    print(f"  EXE: {os.path.join(BUILD_DIR, 'dist', 'SteamScout.exe')}")


if __name__ == "__main__":
    build()
