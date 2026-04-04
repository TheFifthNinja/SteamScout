"""
Steam Compatibility Checker - Overlay (v3 — pywebview / Edge WebView2)
GPU-accelerated floating overlay window.  Connects to the backend WebSocket
and pushes real-time compatibility info to an HTML/CSS/JS front-end.
"""

import asyncio
import json
import os
import re
import sys
import threading
import time
import webbrowser
from html import unescape
from urllib.parse import quote_plus

import requests
import websockets
import webview                           # pywebview >= 5.0

if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes
    _user32 = ctypes.windll.user32
    _user32.FindWindowW.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.LPCWSTR]
    _user32.FindWindowW.restype = ctypes.wintypes.HWND
    _user32.SetWindowPos.argtypes = [
        ctypes.wintypes.HWND, ctypes.wintypes.HWND,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
    ]
    _user32.SetWindowPos.restype = ctypes.wintypes.BOOL
    _VK_LBUTTON = 0x01
    _SWP_NOZORDER = 0x0004
    _SWP_NOACTIVATE = 0x0010
    _SWP_NOSIZE = 0x0001
    _MIN_W, _MIN_H = 460, 180

WS_URL = "ws://localhost:8765"

# ── Paths ──────────────────────────────────────────────────────────────────────

def _data_dir():
    d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "SteamScout")
    os.makedirs(d, exist_ok=True)
    return d

SETTINGS_PATH = os.path.join(_data_dir(), "overlay_settings.json")

def _ui_path():
    """Locate overlay_ui.html next to this script (or in the frozen bundle)."""
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS if hasattr(sys, "_MEIPASS") else os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "overlay_ui.html")

# ── Settings ───────────────────────────────────────────────────────────────────

THEMES = {
    "dark": {
        "BG": "#0f0f13", "SURFACE": "#1a1a22", "SURFACE2": "#22222e",
        "BORDER": "#2a2a38", "TEXT": "#e0e0f0", "DIM": "#6060a0",
        "BRIGHT": "#ffffff", "STEAM": "#1b9cff",
    },
    "light": {
        "BG": "#f4f6f8", "SURFACE": "#ffffff", "SURFACE2": "#ebeff3",
        "BORDER": "#c7d0d9", "TEXT": "#1f2933", "DIM": "#5c6b7a",
        "BRIGHT": "#0f1720", "STEAM": "#0b78d1",
    },
}

GREEN    = "#4ade80"; GREEN_BG  = "#0d2416"
YELLOW   = "#fbbf24"; YELLOW_BG = "#221800"
RED      = "#f87171"; RED_BG    = "#220d0d"
BLUE     = "#60a5fa"; BLUE_BG   = "#0d1628"

STATUS_COLORS = {
    "dark": {
        "pass":    {"color": GREEN,    "bg": GREEN_BG,  "text": "#d0fce0", "icon": "✓"},
        "fail":    {"color": RED,      "bg": RED_BG,    "text": "#fdd", "icon": "✗"},
        "info":    {"color": BLUE,     "bg": BLUE_BG,   "text": "#d0e4ff", "icon": "ℹ"},
        "warn":    {"color": YELLOW,   "bg": YELLOW_BG, "text": "#fde68a", "icon": "⚠"},
        "unknown": {"color": "#6060a0","bg": "#1a1a22", "text": "#d8d8f0", "icon": "?"},
    },
    "light": {
        "pass":    {"color": "#15803d","bg": "#dcfce7", "text": "#14532d", "icon": "✓"},
        "fail":    {"color": "#dc2626","bg": "#fee2e2", "text": "#7f1d1d", "icon": "✗"},
        "info":    {"color": "#2563eb","bg": "#dbeafe", "text": "#1e3a5f", "icon": "ℹ"},
        "warn":    {"color": "#d97706","bg": "#fef3c7", "text": "#78350f", "icon": "⚠"},
        "unknown": {"color": "#475569","bg": "#f1f5f9", "text": "#1e293b", "icon": "?"},
    },
}

def _default_settings():
    return {
        "theme_mode": "dark",
        "custom_colors": {},
        "font_family": "Bahnschrift",
        "custom_font_files": [],
        "upgrade_query_mode": "specific",
        "always_on_top": True,
        "opacity": 0.96,
        "font_scale": 1.0,
        "window_width": 520,
        "window_height": 260,
        "auto_launch": False,
    }

def _load_settings():
    base = _default_settings()
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            base.update({k: v for k, v in raw.items() if k in base})
            if not isinstance(base.get("custom_colors"), dict):
                base["custom_colors"] = {}
            if not isinstance(base.get("custom_font_files"), list):
                base["custom_font_files"] = []
    except Exception:
        pass
    return base

def _save_settings(data):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

# ── eBay helpers ───────────────────────────────────────────────────────────────

def _ebay_search_url(query):
    return f"https://www.ebay.com/sch/i.html?_nkw={quote_plus(query)}&_sop=15&LH_BIN=1"

def _strip_html(text):
    return re.sub(r"<[^>]+>", "", text or "").strip()

def _money_to_float(price_text):
    m = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", price_text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except Exception:
        return None

def _extract_ebay_cheapest(html):
    cards = re.findall(
        r'<li[^>]*class="[^"]*s-item[^"]*"[^>]*>(.*?)</li>',
        html, re.IGNORECASE | re.DOTALL,
    )
    best = None
    for block in cards[:30]:
        link_m = re.search(r"class=\"s-item__link\"[^>]*href=\"([^\"]+)\"", block, re.IGNORECASE)
        title_m = re.search(r"class=\"s-item__title\"[^>]*>(.*?)</", block, re.IGNORECASE | re.DOTALL)
        price_m = re.search(r"class=\"s-item__price\"[^>]*>(.*?)</", block, re.IGNORECASE | re.DOTALL)
        if not link_m or not title_m or not price_m:
            continue
        title = _strip_html(unescape(title_m.group(1)))
        price = _strip_html(unescape(price_m.group(1)))
        url = unescape(link_m.group(1))
        value = _money_to_float(price)
        if value is None:
            continue
        if "shop on ebay" in title.lower() or "results matching fewer words" in title.lower():
            continue
        cand = {"title": title, "price": price, "url": url, "store": "eBay", "price_value": value}
        if best is None or cand["price_value"] < best["price_value"]:
            best = cand
    if not best:
        return {}
    best.pop("price_value", None)
    return best

# ── JS <-> Python bridge ──────────────────────────────────────────────────────

class Api:
    """Methods exposed to JavaScript via window.pywebview.api.*"""

    def __init__(self, overlay):
        self._ov = overlay

    def open_url(self, url):
        webbrowser.open(url)

    def get_settings(self):
        return self._ov.settings

    def save_settings(self, new_settings):
        self._ov.settings.update(new_settings)
        _save_settings(self._ov.settings)
        self._ov._apply_window_settings()
        return True

    def get_themes(self):
        return THEMES

    def pick_font_file(self):
        result = self._ov._window.create_file_dialog(
            webview.OPEN_DIALOG,
            file_types=("Font Files (*.ttf;*.otf)",),
        )
        if result and len(result) > 0:
            path = result[0]
            files = self._ov.settings.setdefault("custom_font_files", [])
            if path not in files:
                files.append(path)
            _save_settings(self._ov.settings)
            return path
        return None

    def toggle_auto_launch(self, enable):
        return self._ov._toggle_auto_launch(enable)

    def close_overlay(self):
        if self._ov._close_callback:
            self._ov._close_callback()
        else:
            self._ov._window.destroy()

    def get_status_colors(self):
        mode = self._ov.settings.get("theme_mode", "dark")
        return STATUS_COLORS.get(mode, STATUS_COLORS["dark"])

    def get_system_fonts(self):
        """Return a sorted list of system font family names."""
        fonts = set()
        if sys.platform == "win32":
            import winreg
            _style_suffixes = re.compile(
                r"\s+(Bold|Italic|Oblique|Light|Thin|Medium|SemiBold|Semi Bold|"
                r"ExtraBold|Extra Bold|ExtraLight|Extra Light|Black|Heavy|"
                r"Condensed|Narrow|Regular|Book|Demi|DemiBold|Demi Bold|"
                r"SemiLight|Semi Light|UltraLight|Ultra Light|UltraBold|Ultra Bold)"
                r"(\s+(Bold|Italic|Oblique|Light|Thin|Medium|Regular|Condensed|Narrow))*$",
                re.IGNORECASE,
            )
            try:
                key = winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts",
                )
                i = 0
                while True:
                    try:
                        name, _, _ = winreg.EnumValue(key, i)
                        # Strip " (TrueType)" etc.
                        name = name.split(" (TrueType)")[0]
                        name = name.split(" (OpenType)")[0]
                        name = name.split(" & ")[0]
                        # Strip style suffixes to get the family name
                        family = _style_suffixes.sub("", name).strip()
                        if family:
                            fonts.add(family)
                        i += 1
                    except OSError:
                        break
                winreg.CloseKey(key)
            except Exception:
                pass
        # Add custom font files as well
        for fp in self._ov.settings.get("custom_font_files", []):
            fonts.add(os.path.splitext(os.path.basename(fp))[0])
        # Always include defaults
        fonts.update(["Bahnschrift", "Segoe UI", "Segoe UI Variable", "Consolas", "Arial", "Cascadia Code"])
        return sorted(fonts, key=str.lower)

    def start_native_resize(self, direction):
        """Spawn a background thread that polls cursor and resizes via SetWindowPos."""
        if sys.platform != "win32":
            return
        hwnd = self._ov._get_hwnd()
        if not hwnd:
            return
        threading.Thread(
            target=self._ov._resize_loop, args=(hwnd, direction), daemon=True
        ).start()

    def start_native_drag(self):
        """Spawn a background thread that polls cursor and drags via SetWindowPos."""
        if sys.platform != "win32":
            return
        hwnd = self._ov._get_hwnd()
        if not hwnd:
            return
        threading.Thread(
            target=self._ov._drag_loop, args=(hwnd,), daemon=True
        ).start()

# ── Overlay class ──────────────────────────────────────────────────────────────

class Overlay:
    def __init__(self, root=None, close_callback=None):
        """
        root is accepted for API compat with SteamScout.pyw but unused.
        close_callback is invoked when the user clicks X.
        """
        self._tk_root = root
        self._close_callback = close_callback
        self.settings = _load_settings()
        self._window = None
        self._last_result = None
        self._deal_cache = {}
        self._requirements_tab = "minimum"
        self._ready = threading.Event()
        self._hwnd = None

    # ── Public API (called from SteamScout.pyw) ────────────────────────────

    def start(self):
        """Create the pywebview window and start the WS listener. Blocks."""
        api = Api(self)
        w = int(self.settings.get("window_width", 520))
        h = int(self.settings.get("window_height", 260))

        self._window = webview.create_window(
            "SteamScout",
            url=_ui_path(),
            js_api=api,
            width=w,
            height=h,
            x=40, y=40,
            resizable=True,
            frameless=True,
            easy_drag=False,
            on_top=bool(self.settings.get("always_on_top", True)),
            transparent=False,
            min_size=(460, 180),
        )
        self._window.events.loaded += self._on_dom_ready
        self._window.events.closing += self._on_closing
        self._window.events.resized += self._on_resized

        threading.Thread(target=self._ws_thread, daemon=True).start()
        webview.start(debug=False)

    def show(self):
        if self._window:
            self._window.show()

    def hide(self):
        if self._window:
            self._window.hide()

    # ── Window events ──────────────────────────────────────────────────────

    def _get_hwnd(self):
        """Get native Win32 HWND, cached after first lookup."""
        if self._hwnd:
            return self._hwnd
        hwnd = _user32.FindWindowW(None, "SteamScout")
        if not hwnd:
            hwnd = _user32.GetForegroundWindow()
        if hwnd:
            self._hwnd = hwnd
        return hwnd

    def _resize_loop(self, hwnd, direction):
        """Poll cursor position and resize window via SetWindowPos until mouse released."""
        try:
            rect = ctypes.wintypes.RECT()
            pt = ctypes.wintypes.POINT()
            _user32.GetWindowRect(hwnd, ctypes.byref(rect))
            _user32.GetCursorPos(ctypes.byref(pt))
            sx, sy = pt.x, pt.y
            ol, ot, oright, ob = rect.left, rect.top, rect.right, rect.bottom

            time.sleep(0.01)
            while _user32.GetAsyncKeyState(_VK_LBUTTON) & 0x8000:
                _user32.GetCursorPos(ctypes.byref(pt))
                dx, dy = pt.x - sx, pt.y - sy
                nl, nt, nr, nb = ol, ot, oright, ob
                if "e" in direction:
                    nr = max(ol + _MIN_W, oright + dx)
                if "s" in direction:
                    nb = max(ot + _MIN_H, ob + dy)
                if "w" in direction:
                    nl = min(oright - _MIN_W, ol + dx)
                if "n" in direction:
                    nt = min(ob - _MIN_H, ot + dy)
                _user32.SetWindowPos(
                    hwnd, None, nl, nt, nr - nl, nb - nt,
                    _SWP_NOZORDER | _SWP_NOACTIVATE,
                )
                time.sleep(0.016)

            _user32.GetWindowRect(hwnd, ctypes.byref(rect))
            self.settings["window_width"] = rect.right - rect.left
            self.settings["window_height"] = rect.bottom - rect.top
            _save_settings(self.settings)
        except Exception:
            pass

    def _drag_loop(self, hwnd):
        """Poll cursor position and move window via SetWindowPos until mouse released."""
        try:
            rect = ctypes.wintypes.RECT()
            pt = ctypes.wintypes.POINT()
            _user32.GetWindowRect(hwnd, ctypes.byref(rect))
            _user32.GetCursorPos(ctypes.byref(pt))
            sx, sy = pt.x, pt.y
            ox, oy = rect.left, rect.top

            time.sleep(0.01)
            while _user32.GetAsyncKeyState(_VK_LBUTTON) & 0x8000:
                _user32.GetCursorPos(ctypes.byref(pt))
                _user32.SetWindowPos(
                    hwnd, None,
                    ox + (pt.x - sx), oy + (pt.y - sy), 0, 0,
                    _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_NOSIZE,
                )
                time.sleep(0.016)
        except Exception:
            pass

    def _on_dom_ready(self):
        self._ready.set()
        self._push_settings()
        if self._last_result:
            self._push_to_js("showResult", self._last_result)
        else:
            self._push_to_js("showIdle")

    def _on_closing(self):
        if self._window:
            try:
                self.settings["window_width"] = self._window.width
                self.settings["window_height"] = self._window.height
            except Exception:
                pass
            _save_settings(self.settings)

    def _on_resized(self, width, height):
        self.settings["window_width"] = width
        self.settings["window_height"] = height
        _save_settings(self.settings)

    def _apply_window_settings(self):
        if not self._window:
            return
        try:
            self._window.on_top = bool(self.settings.get("always_on_top", True))
        except Exception:
            pass
        self._push_settings()

    def _push_settings(self):
        mode = self.settings.get("theme_mode", "dark")
        colors = dict(THEMES.get(mode, THEMES["dark"]))
        colors.update(self.settings.get("custom_colors", {}))
        sc = STATUS_COLORS.get(mode, STATUS_COLORS["dark"])
        self._push_to_js("applySettings", {
            **self.settings,
            "colors": colors,
            "status_colors": sc,
        })

    def _push_to_js(self, fn_name, *args):
        if not self._window or not self._ready.is_set():
            return
        try:
            args_json = ", ".join(json.dumps(a) for a in args)
            self._window.evaluate_js(f"window.SS.{fn_name}({args_json})")
        except Exception:
            pass

    # ── WebSocket ──────────────────────────────────────────────────────────

    def _ws_thread(self):
        asyncio.run(self._ws_loop())

    async def _ws_loop(self):
        backoff = 1
        while True:
            try:
                async with websockets.connect(WS_URL) as ws:
                    backoff = 1  # reset on successful connect
                    self._ready.wait()
                    self._push_to_js("showIdle")
                    async for raw in ws:
                        msg = json.loads(raw)
                        self._dispatch(msg)
            except Exception:
                self._ready.wait()
                self._push_to_js("showConnecting")
                await asyncio.sleep(min(backoff, 10))
                backoff = min(backoff * 2, 10)

    def _dispatch(self, msg):
        try:
            t = msg.get("type")
            if t == "idle":
                self._push_to_js("showIdle")
            elif t == "loading":
                self._push_to_js("showLoading", msg.get("app_id", 0), msg.get("section", ""))
            elif t == "result":
                self._process_result(msg)
            elif t == "error":
                self._push_to_js("showError", msg.get("app_id", 0), msg.get("section", ""), msg.get("msg", ""))
            elif t == "warning":
                self._push_to_js("showWarning", msg.get("msg", ""))
        except Exception:
            pass

    # ── Result processing ──────────────────────────────────────────────────

    def _process_result(self, data):
        try:
            self._last_result = data
            compat = data.get("compat", {})
            if not compat.get("performance"):
                compat["performance"] = self._fallback_performance(compat)
            self._prefetch_upgrade_deals(data)
            self._push_to_js("showResult", data)
        except Exception:
            pass

    def _fallback_performance(self, compat):
        min_ok = compat.get("overall_min")
        rec_ok = compat.get("overall_rec")
        if min_ok == "fail":
            return {
                "confidence": "medium",
                "note": "Below minimum requirements; expect low FPS.",
                "presets": {"low": "<30 FPS", "medium": "Not recommended", "high": "Not playable"},
            }
        if min_ok == "pass" and rec_ok == "unavailable":
            return {
                "confidence": "low",
                "note": "Only minimum requirements available; estimate less precise.",
                "presets": {"low": "50-80 FPS", "medium": "35-60 FPS", "high": "25-45 FPS"},
            }
        if min_ok == "pass" and rec_ok == "fail":
            return {
                "confidence": "low",
                "note": "Meets minimum requirements.",
                "presets": {"low": "35-60 FPS", "medium": "25-45 FPS", "high": "<30 FPS"},
            }
        if min_ok == "pass" and rec_ok == "pass":
            return {
                "confidence": "low",
                "note": "Estimated from requirement tiers.",
                "presets": {"low": "60+ FPS", "medium": "45-75 FPS", "high": "35-60 FPS"},
            }
        return {
            "confidence": "low",
            "note": "Insufficient data for precise estimate.",
            "presets": {"low": "Unknown", "medium": "Unknown", "high": "Unknown"},
        }

    def _prefetch_upgrade_deals(self, data):
        compat = data.get("compat", {})
        for tier in ("minimum", "recommended"):
            tier_data = compat.get(tier, {})
            for key in ("ram", "cpu", "gpu", "storage"):
                row = tier_data.get(key)
                if not row or row.get("status") != "fail":
                    continue
                required = row.get("required", "")
                query = self._upgrade_query(key, required)
                if query in self._deal_cache:
                    continue
                self._deal_cache[query] = {"loading": True, "url": _ebay_search_url(query)}
                threading.Thread(target=self._fetch_deal, args=(query,), daemon=True).start()

    def _fetch_deal(self, query):
        url = _ebay_search_url(query)
        headers = {"User-Agent": "Mozilla/5.0"}
        deal = {"loading": False, "url": url, "title": "", "price": "", "store": "eBay"}
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.ok:
                parsed = _extract_ebay_cheapest(r.text)
                if parsed.get("url"):
                    deal.update(parsed)
        except Exception:
            pass
        self._deal_cache[query] = deal
        if self._last_result:
            self._push_deals()

    def _push_deals(self):
        self._push_to_js("updateDeals", self._deal_cache)

    # ── Upgrade query logic ────────────────────────────────────────────────

    def _is_vague_requirement(self, key, required):
        s = (required or "").strip().lower()
        if not s:
            return True
        vague = [
            "hardware accelerated", "dedicated memory", "compatible",
            "or better", "equivalent", "support", "shader", "feature level",
            "dx", "directx", "intel hd", "integrated graphics",
            "see notes", "tbd", "unknown",
        ]
        if any(m in s for m in vague):
            return True
        if key in {"ram", "storage"} and not re.search(r"\b\d+(?:\.\d+)?\s*(tb|gb|mb)\b", s, re.IGNORECASE):
            return True
        return False

    def _extract_size_gb(self, text):
        m = re.search(r"(\d+(?:\.\d+)?)\s*(tb|gb|mb)\b", text or "", re.IGNORECASE)
        if not m:
            return None
        value = float(m.group(1))
        unit = m.group(2).lower()
        if unit == "tb": return int(round(value * 1024))
        if unit == "mb": return max(1, int(round(value / 1024)))
        return int(round(value))

    def _upgrade_query(self, key, required):
        required = re.sub(r"\s+", " ", (required or "").strip())
        mode = self.settings.get("upgrade_query_mode", "specific")
        vague = self._is_vague_requirement(key, required)
        if key == "ram":
            if mode == "general": return "gaming PC RAM upgrade"
            size_gb = self._extract_size_gb(required)
            if size_gb: return f"buy {size_gb}GB desktop RAM DDR4 or DDR5"
            if vague: return "desktop gaming RAM upgrade kit"
            return f"buy {required} desktop RAM"
        if key == "storage":
            if mode == "general": return "gaming PC SSD storage upgrade"
            size_gb = self._extract_size_gb(required)
            kind = "SSD" if "ssd" in required.lower() or "nvme" in required.lower() else "drive"
            if size_gb: return f"buy {size_gb}GB {kind} for PC"
            if vague: return "gaming PC SSD storage upgrade"
            return f"buy {required} PC storage"
        if key == "cpu":
            if mode == "general": return "gaming PC CPU upgrade"
            if vague: return "gaming PC CPU upgrade"
            return f"buy PC CPU similar or better than {required}"
        if key == "gpu":
            if mode == "general": return "gaming PC graphics card upgrade"
            if vague: return "gaming PC dedicated graphics card upgrade"
            return f"buy graphics card similar or better than {required}"
        return f"buy PC part upgrade {required}"

    # ── Auto-launch (registry) ─────────────────────────────────────────────

    def _toggle_auto_launch(self, enable):
        try:
            import winreg
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            reg_name = "SteamScout"
            reg = winreg.ConnectRegistry(None, winreg.HKEY_CURRENT_USER)
            key = winreg.OpenKey(reg, key_path, 0, winreg.KEY_SET_VALUE)
            if enable:
                if getattr(sys, 'frozen', False):
                    cmd = f'"{sys.executable}"'
                else:
                    script_dir = os.path.dirname(os.path.abspath(__file__))
                    main_script = os.path.join(script_dir, "SteamScout.pyw")
                    if not os.path.exists(main_script):
                        winreg.CloseKey(key)
                        return False
                    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
                    if not os.path.exists(pythonw):
                        pythonw = sys.executable
                    cmd = f'"{pythonw}" "{main_script}"'
                winreg.SetValueEx(key, reg_name, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, reg_name)
                except FileNotFoundError:
                    pass
            winreg.CloseKey(key)
            return True
        except Exception:
            return False


# ── Standalone entry point ─────────────────────────────────────────────────────

def main():
    ov = Overlay(close_callback=lambda: sys.exit(0))
    ov.start()

if __name__ == "__main__":
    main()
