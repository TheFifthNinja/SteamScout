"""
SteamScout — System tray application.
Runs quietly in the background like Discord.  When Steam opens the overlay
activates automatically; when Steam closes it hides back to the tray.
"""

import sys
import os

# .pyw / frozen exe may have no console — prevent write errors
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import asyncio
import ctypes
import subprocess
import threading
import time
import winreg

import psutil
import pystray
import requests
from PIL import Image

# ── Paths ──────────────────────────────────────────────────────────────────────

def _app_dir():
    """Directory where the application binaries live."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _data_dir():
    """Return %APPDATA%/SteamScout for user data (settings, etc.)."""
    d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "SteamScout")
    os.makedirs(d, exist_ok=True)
    return d


APP_DIR = _app_dir()
STEAM_DEBUG_URL = "http://localhost:8080/json"
ICON_PNG = os.path.join(APP_DIR, "SteamScoutIcon.png")

# ── Single-instance guard ──────────────────────────────────────────────────────

def _acquire_single_instance():
    """Return True if this is the only running instance (Windows mutex)."""
    ctypes.windll.kernel32.CreateMutexW(None, False, "SteamScout_SingleInstance")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


# ── Icon ───────────────────────────────────────────────────────────────────────

def _load_tray_icon():
    """Load the SteamScout icon PNG, falling back to a generated icon."""
    try:
        if os.path.exists(ICON_PNG):
            img = Image.open(ICON_PNG).convert("RGBA")
            img = img.resize((64, 64), Image.LANCZOS)
            return img
    except Exception:
        pass
    # Fallback: solid blue circle with white S
    from PIL import ImageDraw, ImageFont
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([2, 2, size - 3, size - 3], fill="#1b9cff")
    try:
        font = ImageFont.truetype("segoeui.ttf", 38)
    except Exception:
        font = ImageFont.load_default()
    draw.text((size // 2, size // 2), "S", fill="white", font=font, anchor="mm")
    return img


# ── Steam helpers ──────────────────────────────────────────────────────────────

def _find_steam_path():
    """Locate steam.exe via the registry or common install paths."""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"
        )
        val, _ = winreg.QueryValueEx(key, "SteamExe")
        winreg.CloseKey(key)
        path = os.path.normpath(val)
        if os.path.exists(path):
            return path
    except Exception:
        pass
    for p in [
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Steam", "steam.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Steam", "steam.exe"),
    ]:
        if os.path.exists(p):
            return p
    return None


def _is_steam_running():
    for proc in psutil.process_iter(["name"]):
        try:
            if (proc.info.get("name") or "").lower() == "steam.exe":
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return False


def _is_debug_endpoint_up():
    try:
        return requests.get(STEAM_DEBUG_URL, timeout=3).status_code == 200
    except Exception:
        return False


def _ensure_steam_debug():
    """Restart Steam with debug flags if the CEF endpoint is unreachable.
    Returns True once the endpoint responds."""
    if _is_debug_endpoint_up():
        return True

    steam = _find_steam_path()
    if not steam:
        return False

    # Kill running Steam instances
    for proc in psutil.process_iter(["name"]):
        try:
            if (proc.info.get("name") or "").lower() == "steam.exe":
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    time.sleep(3)

    # Restart with debug flags
    subprocess.Popen(
        [steam, "-cef-enable-debugging", "-cef-remote-debugging-port=8080"],
        creationflags=0x00000008,  # DETACHED_PROCESS
    )

    for _ in range(10):
        time.sleep(2)
        if _is_debug_endpoint_up():
            return True
    return False


# ── Autostart (registry) ──────────────────────────────────────────────────────

_REG_RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
_REG_NAME = "SteamScout"


def _is_autostart_enabled():
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _REG_RUN, 0, winreg.KEY_READ
        )
        try:
            winreg.QueryValueEx(key, _REG_NAME)
            return True
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False


def _toggle_autostart(enable):
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _REG_RUN, 0, winreg.KEY_SET_VALUE
        )
        if enable:
            if getattr(sys, "frozen", False):
                cmd = f'"{sys.executable}"'
            else:
                pythonw = os.path.join(
                    os.path.dirname(sys.executable), "pythonw.exe"
                )
                if not os.path.exists(pythonw):
                    pythonw = sys.executable
                cmd = f'"{pythonw}" "{os.path.join(APP_DIR, "SteamScout.pyw")}"'
            winreg.SetValueEx(key, _REG_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, _REG_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception:
        pass


# ── Main application ──────────────────────────────────────────────────────────

class SteamScoutApp:
    def __init__(self):
        self._running = True
        self._steam_active = False
        self._debug_ready = False
        self._overlay_visible = False
        self._root = None
        self._overlay = None
        self._tray = None
        self._notified_waiting = False

    # ── Public ──────────────────────────────────────────────────────────────

    def run(self):
        import tkinter as tk
        from Overlay import Overlay

        self._root = tk.Tk()
        self._root.withdraw()                       # start hidden
        self._overlay = Overlay(self._root, close_callback=self._hide)
        self._root.withdraw()                       # ensure hidden after Overlay init
        self._root.protocol("WM_DELETE_WINDOW", self._hide)

        # Backend WebSocket server in a daemon thread
        self._start_backend()

        # System tray icon in a daemon thread
        self._start_tray()

        # Steam process monitor via tkinter after-loop
        self._poll_steam()

        self._root.mainloop()

    # ── Backend ─────────────────────────────────────────────────────────────

    def _start_backend(self):
        def _run():
            import Backend
            asyncio.run(Backend.main())

        threading.Thread(target=_run, daemon=True, name="Backend").start()

    # ── System tray ─────────────────────────────────────────────────────────

    def _start_tray(self):
        icon_img = _load_tray_icon()
        menu = pystray.Menu(
            pystray.MenuItem("Show Overlay", self._on_tray_show, default=True),
            pystray.MenuItem(
                "Start with Windows",
                self._on_tray_autostart,
                checked=lambda _: _is_autostart_enabled(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit SteamScout", self._on_tray_quit),
        )
        self._tray = pystray.Icon("SteamScout", icon_img, "SteamScout — Waiting for Steam", menu)
        threading.Thread(target=self._tray.run, daemon=True, name="Tray").start()

    def _update_tray_tooltip(self, text):
        if self._tray:
            self._tray.title = text

    def _on_tray_show(self, icon=None, item=None):
        if self._root:
            self._root.after(0, self._show)

    def _on_tray_quit(self, icon=None, item=None):
        self._running = False
        if self._tray:
            self._tray.stop()
        if self._root:
            self._root.after(0, self._root.destroy)

    def _on_tray_autostart(self, icon=None, item=None):
        _toggle_autostart(not _is_autostart_enabled())

    # ── Overlay visibility ──────────────────────────────────────────────────

    def _show(self):
        if self._root:
            self._root.deiconify()
            self._root.lift()
            if self._overlay:
                self._root.attributes(
                    "-topmost",
                    bool(self._overlay.settings.get("always_on_top", True)),
                )
            self._overlay_visible = True

    def _hide(self):
        if self._root:
            if self._overlay and hasattr(self._overlay, "_close_settings"):
                self._overlay._close_settings()
            self._root.withdraw()
            self._overlay_visible = False

    # ── Steam monitor (tkinter after-loop) ──────────────────────────────────

    def _poll_steam(self):
        if not self._running:
            return

        steam_on = _is_steam_running()

        if steam_on and not self._steam_active:
            self._steam_active = True
            self._notified_waiting = False
            self._update_tray_tooltip("SteamScout — Connecting to Steam…")
            if not self._debug_ready:
                threading.Thread(
                    target=self._setup_debug_and_show, daemon=True
                ).start()
            else:
                self._update_tray_tooltip("SteamScout — Active")
                self._show()
        elif not steam_on and self._steam_active:
            self._steam_active = False
            self._debug_ready = False
            self._update_tray_tooltip("SteamScout — Waiting for Steam")
            self._hide()
        elif not steam_on and not self._notified_waiting:
            # First poll after launch with no Steam running
            self._notified_waiting = True
            self._update_tray_tooltip("SteamScout — Waiting for Steam")
            if self._tray:
                try:
                    self._tray.notify(
                        "SteamScout is running in the background.\n"
                        "The overlay will appear when you open Steam.",
                        "SteamScout",
                    )
                except Exception:
                    pass

        self._root.after(3000, self._poll_steam)

    def _setup_debug_and_show(self):
        """Ensure the debug endpoint is reachable, then show the overlay."""
        ok = _ensure_steam_debug()
        self._debug_ready = ok

        if ok and self._root:
            self._update_tray_tooltip("SteamScout — Active")
            self._root.after(0, self._show)
        elif self._tray:
            self._update_tray_tooltip("SteamScout — Could not connect")
            try:
                self._tray.notify(
                    "Could not enable Steam debug endpoint.\n"
                    "Try restarting Steam, then SteamScout.",
                    "SteamScout",
                )
            except Exception:
                pass


# ── Entry ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not _acquire_single_instance():
        sys.exit(0)
    SteamScoutApp().run()
