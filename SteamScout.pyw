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
ICON_ICO = os.path.join(APP_DIR, "steamscout.ico")

# ── Single-instance guard ──────────────────────────────────────────────────────

def _acquire_single_instance():
    """Return True if this is the only running instance (Windows mutex)."""
    ctypes.windll.kernel32.CreateMutexW(None, False, "SteamScout_SingleInstance")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


# ── Icon ───────────────────────────────────────────────────────────────────────

def _load_tray_icon():
    """Load the SteamScout icon, preferring .ico for crisp tray rendering."""
    # Try .ico first — gives Windows the native multi-size icon it expects
    for path in (ICON_ICO, ICON_PNG):
        try:
            if os.path.exists(path):
                img = Image.open(path).convert("RGBA")
                img = img.resize((64, 64), Image.LANCZOS)
                return img
        except Exception:
            continue
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
    try:
        for proc in psutil.process_iter(["name"]):
            try:
                if (proc.info.get("name") or "").lower() == "steam.exe":
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
    except Exception:
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
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
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
        self._overlay = None
        self._tray = None
        self._notified_waiting = False
        self._steam_check_result = None       # latest result from bg thread
        self._steam_check_lock = threading.Lock()

    # ── Public ──────────────────────────────────────────────────────────────

    def run(self):
        try:
            from Overlay import Overlay
        except Exception as e:
            print(f"Failed to import Overlay: {e}", file=sys.stderr)
            return

        self._overlay = Overlay(close_callback=self._hide)

        # Backend WebSocket server in a daemon thread
        self._start_backend()

        # System tray icon in a daemon thread
        self._start_tray()

        # Steam process checker + poller in a background thread
        threading.Thread(target=self._steam_monitor_loop, daemon=True, name="SteamMonitor").start()

        # Overlay.start() blocks (runs the pywebview event loop on main thread)
        try:
            self._overlay.start()
        except Exception as e:
            print(f"Overlay crashed: {e}", file=sys.stderr)

    # ── Backend ─────────────────────────────────────────────────────────────

    def _start_backend(self):
        def _run():
            try:
                import Backend
                asyncio.run(Backend.main())
            except Exception as e:
                print(f"Backend crashed: {e}", file=sys.stderr)

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
        self._show()

    def _on_tray_quit(self, icon=None, item=None):
        self._running = False
        try:
            if self._tray:
                self._tray.stop()
        except Exception:
            pass
        try:
            if self._overlay and self._overlay._window:
                self._overlay._window.destroy()
        except Exception:
            pass

    def _on_tray_autostart(self, icon=None, item=None):
        _toggle_autostart(not _is_autostart_enabled())

    # ── Overlay visibility ──────────────────────────────────────────────────

    def _show(self):
        if self._overlay:
            self._overlay.show()
            self._overlay_visible = True

    def _hide(self):
        if self._overlay:
            self._overlay.hide()
            self._overlay_visible = False

    # ── Steam monitor (background thread loop) ─────────────────────────────

    def _steam_monitor_loop(self):
        """Combined Steam check + poll loop, runs entirely in a daemon thread."""
        while self._running:
          try:
            steam_on = _is_steam_running()

            if steam_on and not self._steam_active:
                self._steam_active = True
                self._notified_waiting = False
                self._update_tray_tooltip("SteamScout — Connecting to Steam…")
                if not self._debug_ready:
                    self._setup_debug_and_show()
                else:
                    self._update_tray_tooltip("SteamScout — Active")
                    self._show()
            elif not steam_on and self._steam_active:
                self._steam_active = False
                self._debug_ready = False
                self._update_tray_tooltip("SteamScout — Waiting for Steam")
                self._hide()
            elif not steam_on and not self._notified_waiting:
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

            time.sleep(3)
          except Exception:
            time.sleep(5)

    def _setup_debug_and_show(self):
        """Ensure the debug endpoint is reachable, then show the overlay."""
        ok = _ensure_steam_debug()
        self._debug_ready = ok

        if ok:
            self._update_tray_tooltip("SteamScout — Active")
            self._show()
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
