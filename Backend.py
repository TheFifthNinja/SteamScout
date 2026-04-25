"""
Steam Compatibility Checker - Backend (v2)
Uses Steam's CEF remote debugging port to read the exact URL Steam is showing,
extracts the AppID and page section, fetches requirements, and broadcasts
compatibility results via WebSocket to the overlay.
"""

import asyncio
import json
import re
import logging
import subprocess
import sys
import tempfile
import os
import threading
from typing import Optional, Tuple

# Suppress console windows from subprocess calls (reduces AV false positives)
_STARTUPINFO = None
_CREATION_FLAGS = 0
if sys.platform == "win32":
    _STARTUPINFO = subprocess.STARTUPINFO()
    _STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _STARTUPINFO.wShowWindow = 0  # SW_HIDE
    _CREATION_FLAGS = subprocess.CREATE_NO_WINDOW

import psutil
import requests
import websockets
from websockets.server import serve

def _log_handler():
    """Use a file handler in AppData when running without a console (frozen/.pyw)."""
    if sys.stdout is not None and hasattr(sys.stdout, "write"):
        try:
            sys.stdout.write("")
            return logging.StreamHandler(sys.stdout)
        except Exception:
            pass
    log_dir = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "SteamScout")
    os.makedirs(log_dir, exist_ok=True)
    return logging.FileHandler(os.path.join(log_dir, "backend.log"), encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    handlers=[_log_handler()]
)
log = logging.getLogger(__name__)

CEF_DEBUG_URL = "http://localhost:8080/json"
WS_HOST       = "localhost"
WS_PORT       = 8765


# ──────────────────────────────────────────────────────────────────────────────
# 1.  PC SPEC DETECTION
# ──────────────────────────────────────────────────────────────────────────────

def _wmic(query: str) -> list:
    try:
        r = subprocess.run(
            ["wmic"] + query.split(),
            capture_output=True, text=True, timeout=6,
            startupinfo=_STARTUPINFO,
            creationflags=_CREATION_FLAGS,
        )
        lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
        return lines[1:] if len(lines) > 1 else []
    except Exception:
        return []


def get_cpu_name() -> str:
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
        )
        name, _ = winreg.QueryValueEx(key, "ProcessorNameString")
        return name.strip()
    except Exception:
        lines = _wmic("cpu get Name")
        return lines[0] if lines else "Unknown CPU"


def get_gpu_name() -> str:
    lines = _wmic("path win32_VideoController get Name")
    return lines[0] if lines else "Unknown GPU"


def get_vram_gb() -> float:
    lines = _wmic("path win32_VideoController get AdapterRAM")
    try:
        return round(int(lines[0]) / (1024 ** 3), 1) if lines else 0.0
    except (ValueError, IndexError):
        return 0.0


def get_ram_gb() -> float:
    return round(psutil.virtual_memory().total / (1024 ** 3), 1)


def get_free_disk_gb() -> float:
    try:
        return round(psutil.disk_usage("C:\\").free / (1024 ** 3), 1)
    except Exception:
        return 0.0


def get_windows_version() -> str:
    lines = _wmic("os get Caption")
    return lines[0] if lines else "Windows"


def get_directx_version() -> str:
    # Prefer dxdiag output since registry keys can be ambiguous on modern Windows.
    out_file = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
            out_file = f.name
        subprocess.run(["dxdiag", "/whql:off", "/t", out_file],
                       capture_output=True, text=True, timeout=20,
                       startupinfo=_STARTUPINFO,
                       creationflags=_CREATION_FLAGS)
        for enc in ("utf-16", "utf-8", "cp1252", "latin-1"):
            try:
                with open(out_file, "r", encoding=enc, errors="ignore") as f:
                    txt = f.read()
                m = re.search(r"DirectX Version:\s*([^\r\n]+)", txt, re.IGNORECASE)
                if m:
                    return m.group(1).strip()
            except Exception:
                continue
    except Exception:
        pass
    finally:
        if out_file and os.path.exists(out_file):
            try:
                os.remove(out_file)
            except Exception:
                pass

    # Fallback: infer from OS generation when dxdiag parsing fails.
    os_name = get_windows_version().lower()
    if "11" in os_name or "10" in os_name:
        return "DirectX 12"
    if "8" in os_name or "7" in os_name:
        return "DirectX 11"
    return "Unknown"


def collect_pc_specs() -> dict:
    log.info("Collecting PC specifications...")
    _collectors = {
        "cpu":          (get_cpu_name, "Unknown CPU"),
        "gpu":          (get_gpu_name, "Unknown GPU"),
        "ram_gb":       (get_ram_gb, 0.0),
        "disk_free_gb": (get_free_disk_gb, 0.0),
        "vram_gb":      (get_vram_gb, 0.0),
        "os":           (get_windows_version, "Windows"),
        "directx":      (get_directx_version, "Unknown"),
    }
    specs = {}
    for key, (fn, fallback) in _collectors.items():
        try:
            specs[key] = fn()
        except Exception as e:
            log.warning("Failed to collect %s: %s", key, e)
            specs[key] = fallback
    log.info(f"PC specs: {specs}")
    return specs


# ──────────────────────────────────────────────────────────────────────────────
# 2.  CEF PAGE DETECTION
# ──────────────────────────────────────────────────────────────────────────────

# Match broad Steam game URL shapes seen in CEF tabs.
_URL_PATTERNS = [
    (re.compile(r"(?:https?://)?store\.steampowered\.com/app/(\d+)(?:/([^/?#]*))?", re.IGNORECASE), "store"),
    (re.compile(r"steaminternalbrowser://store/app/(\d+)(?:/([^/?#]*))?", re.IGNORECASE), "store"),
    (re.compile(r"steam://(?:openurl/)?(?:https?://)?store\.steampowered\.com/app/(\d+)(?:/([^/?#]*))?", re.IGNORECASE), "store"),
    (re.compile(r"steam://(?:nav/)?games/details/(\d+)", re.IGNORECASE), "library"),
    (re.compile(r"steaminternalbrowser://library/app/(\d+)", re.IGNORECASE), "library"),
    (re.compile(r"(?:https?://)?store\.steampowered\.com/library/app/(\d+)", re.IGNORECASE), "library"),
]

STORE_SECTIONS = {
    "":             "Store page",
    "community":    "Community hub",
    "news":         "News",
    "reviews":      "Reviews",
    "workshop":     "Workshop",
    "achievements": "Achievements",
    "dlc":          "DLC",
}


def classify_url(url: str) -> Optional[dict]:
    for pattern, source in _URL_PATTERNS:
        m = pattern.search(url)
        if not m:
            continue

        app_id = int(m.group(1))
        if app_id < 10:
            return None

        raw_section = (m.group(2) if m.lastindex and m.lastindex >= 2 else "") or ""
        section = raw_section.lower().strip("/")
        if source == "library":
            label = "Library"
        else:
            label = STORE_SECTIONS.get(section, "Store page")

        return {"app_id": app_id, "section": label}

    return None


def get_steam_page() -> Tuple[Optional[dict], bool]:
    """
    Poll Steam's CEF debug endpoint for the active game page.
    Returns ({app_id, section, url, title} or None, debug_endpoint_ok).
    Section is a display label such as "Store page" or "Library".
    """
    try:
        resp = requests.get(CEF_DEBUG_URL, timeout=2)
        tabs = resp.json()
    except Exception:
        return None, False

    for tab in tabs:
        url  = tab.get("url", "")
        info = classify_url(url)
        if info:
            info["url"]   = url
            info["title"] = tab.get("title", "")
            return info, True

    return None, True


# ──────────────────────────────────────────────────────────────────────────────
# 3.  STEAM STORE API
# ──────────────────────────────────────────────────────────────────────────────

STEAM_DETAILS_URL = "https://store.steampowered.com/api/appdetails"
_req_cache: dict = {}


def fetch_requirements(app_id: int) -> Optional[dict]:
    if app_id in _req_cache:
        return _req_cache[app_id]
    try:
        r = requests.get(
            STEAM_DETAILS_URL,
            params={"appids": app_id, "l": "english"},
            timeout=10
        )
        data    = r.json()
        payload = data.get(str(app_id), {})
        if not payload.get("success"):
            return None
        d = payload["data"]
        pc_req = d.get("pc_requirements", {})
        # Some Steam apps return pc_requirements as a list or string instead of dict
        if not isinstance(pc_req, dict):
            pc_req = {}
        result = {
            "app_id":       app_id,
            "name":         d.get("name", "Unknown"),
            "header_image": d.get("header_image", ""),
            "app_type":     d.get("type", "game"),
            "minimum":      _parse_reqs(pc_req.get("minimum", "")),
            "recommended":  _parse_reqs(pc_req.get("recommended", "")),
        }
        _req_cache[app_id] = result
        return result
    except Exception as e:
        log.warning(f"appdetails fetch failed for {app_id}: {e}")
        return None


def _parse_reqs(html: str) -> dict:
    if not html:
        return {}
    # Flatten inline line-breaks so "GTX 970<br>or AMD RX 480" becomes a single capturable line.
    h = re.sub(r"<br\s*/?>", " ", html, flags=re.IGNORECASE)
    # Strip inline formatting tags (spans, links, bold within text) that would cut off capture.
    h = re.sub(r"</?(?:em|i|b|u|span|a|small|code|nobr)[^>]*>", "", h, flags=re.IGNORECASE)
    patterns = {
        "os":      r"OS[^<]*</strong>\s*([^<]+)",
        "cpu":     r"(?:Processor|CPU)[^<]*</strong>\s*([^<]+)",
        "ram":     r"Memory[^<]*</strong>\s*([\d]+(?:\.[\d]+)?\s*(?:TB|GB|MB)?)",
        "gpu":     r"(?:Graphics|GPU)[^<]*</strong>\s*([^<]+)",
        "directx": r"DirectX[^<]*</strong>\s*([^<]+)",
        "storage": r"Storage[^<]*</strong>\s*([\d]+(?:\.[\d]+)?\s*(?:TB|GB|MB)?)",
        "network": r"Network[^<]*</strong>\s*([^<]+)",
    }
    out = {}
    for key, pat in patterns.items():
        m = re.search(pat, h, re.IGNORECASE)
        if m:
            text = re.sub(r"\s+", " ", m.group(1)).strip().rstrip("*").strip()
            if text:
                out[key] = text
    return out


# ──────────────────────────────────────────────────────────────────────────────
# 4.  COMPATIBILITY ENGINE
# ──────────────────────────────────────────────────────────────────────────────

def _size_gb(s: str, default_unit: str = "GB") -> Optional[float]:
    m = re.search(r"([\d.]+)\s*(TB|GB|MB)?", s, re.IGNORECASE)
    if not m:
        return None
    value = float(m.group(1))
    unit = (m.group(2) or default_unit).upper()
    if unit == "TB":
        return value * 1024
    if unit == "MB":
        return value / 1024
    return value


def _directx_major(s: str) -> Optional[int]:
    m = re.search(r"directx\s*(\d+)|dx\s*(\d+)|version\s*(\d+)|direct3d\s*(\d+)", s, re.IGNORECASE)
    if not m:
        # Bare number like "12" in a field that should be DX version
        m = re.match(r"^\s*(\d{1,2})\s*$", s.strip())
        if not m:
            return None
        return int(m.group(1))
    for g in m.groups():
        if g:
            return int(g)
    return None


# ── GPU benchmark lookup (based on PassMark G3D Mark scores) ──────────────────
# Values are approximate relative-performance scores normalised so that
# a GTX 1060 ≈ 10 000, giving a usable spread from ancient cards (~500)
# up to the latest flagships (~42 000).

_gpu_score_cache: dict[str, Optional[float]] = {}
_cpu_score_cache: dict[str, Optional[float]] = {}

_GPU_BENCHMARKS: dict[str, int] = {
    # ── NVIDIA RTX 50 series ──
    "rtx 5090":       41577,  "rtx 5080":      35683,  "rtx 5070 ti":   32431,
    "rtx 5070":       28753,

    # ── NVIDIA RTX 40 series ──
    "rtx 4090":       38068,  "rtx 4080 super": 34436, "rtx 4080":      28710,
    "rtx 4070 ti super": 31575, "rtx 4070 ti":  27500, "rtx 4070 super": 29971,
    "rtx 4070":       26919,  "rtx 4060 ti":   22614,  "rtx 4060":      19517,

    # ── NVIDIA RTX 30 series ──
    "rtx 3090 ti":    29288,  "rtx 3090":      26540,  "rtx 3080 ti":   26900,
    "rtx 3080":       25022,  "rtx 3070 ti":   23480,  "rtx 3070":      22125,
    "rtx 3060 ti":    20270,  "rtx 3060":      17005,  "rtx 3050":      12800,

    # ── NVIDIA RTX 20 series ──
    "rtx 2080 ti":    21472,  "rtx 2080 super": 19400, "rtx 2080":      18200,
    "rtx 2070 super": 18140,  "rtx 2070":      16500,  "rtx 2060 super": 16100,
    "rtx 2060":       14095,

    # ── NVIDIA GTX 16 series ──
    "gtx 1660 ti":    12700,  "gtx 1660 super": 12700, "gtx 1660":      11800,
    "gtx 1650 super": 10600,  "gtx 1650":       7850,

    # ── NVIDIA GTX 10 series ──
    "gtx 1080 ti":    18591,  "gtx 1080":      15500,  "gtx 1070 ti":   14700,
    "gtx 1070":       13508,  "gtx 1060":      10049,  "gtx 1050 ti":    6361,
    "gtx 1050":        5115,

    # ── NVIDIA GTX 9 series ──
    "gtx 980 ti":     13200,  "gtx 980":       11000,  "gtx 970":        9639,
    "gtx 960":         7150,  "gtx 950":        5900,

    # ── NVIDIA GTX 7 series ──
    "gtx 780 ti":      9250,  "gtx 780":        8200,  "gtx 770":        7050,
    "gtx 760":         5650,  "gtx 750 ti":     3900,  "gtx 750":        3400,

    # ── NVIDIA GTX 6 series ──
    "gtx 690":         9500,  "gtx 680":        7500,  "gtx 670":        6500,
    "gtx 660 ti":      5800,  "gtx 660":        5000,  "gtx 650 ti":    2533,
    "gtx 650":         2100,

    # ── NVIDIA older ──
    "gtx 580":         4250,  "gtx 570":        3700,  "gtx 560 ti":    3100,
    "gtx 560":         2700,  "gtx 550 ti":     1559,  "gtx 480":       3800,
    "gtx 470":         3200,  "gtx 460":        2271,
    "gt 1030":         2402,  "gt 730":         1100,   "gt 710":         700,
    "gt 640":          1200,  "gt 630":          900,   "gt 610":         500,

    # ── NVIDIA MX series (laptop) ──
    "mx570":           5500,  "mx550":          4500,   "mx450":          4200,
    "mx350":           3200,  "mx330":          2800,   "mx250":          2600,
    "mx230":           2100,  "mx150":          2400,   "mx130":          1500,
    "mx110":           1200,

    # ── AMD RX 7000 series ──
    "rx 7900 xtx":    31407,  "rx 7900 xt":    26500,  "rx 7900 gre":   23000,
    "rx 7800 xt":     23200,  "rx 7700 xt":    20500,  "rx 7600 xt":    16500,
    "rx 7600":        15200,

    # ── AMD RX 6000 series ──
    "rx 6950 xt":     27500,  "rx 6900 xt":    25500,  "rx 6800 xt":    23000,
    "rx 6800":        20000,  "rx 6750 xt":    18000,  "rx 6700 xt":    16200,
    "rx 6700":        14000,  "rx 6650 xt":    14500,  "rx 6600 xt":    14000,
    "rx 6600":        12500,  "rx 6500 xt":     6600,  "rx 6400":        5000,

    # ── AMD RX 5000 series ──
    "rx 5700 xt":     14600,  "rx 5700":       13200,  "rx 5600 xt":    12200,
    "rx 5500 xt":      8500,

    # ── AMD RX Vega / 500 / 400 series ──
    "rx vega 64":     12500,  "rx vega 56":    11200,
    "rx 590":          9300,  "rx 580":         8791,  "rx 570":         7500,
    "rx 560":          3430,  "rx 550":         2600,  "rx 480":         9100,
    "rx 470":          8000,  "rx 460":         4200,

    # ── AMD older ──
    "r9 fury x":      10500,  "r9 fury":       9800, "r9 390x":        9000,
    "r9 390":          8200,  "r9 380x":        7000, "r9 380":         6300,
    "r9 290x":         7500,  "r9 290":         6800, "r9 280x":        6000,
    "r9 280":          5200,  "r9 270x":        4700, "r9 270":         4200,
    "r7 370":          3600,  "r7 360":         3000, "r7 265":         3500,
    "r7 260x":         3300,  "r7 260":         2900, "r7 250":         1800,
    "hd 7970":         5800,  "hd 7950":        5100, "hd 7870":        4600,
    "hd 7850":         3900,  "hd 7790":        3500, "hd 7770":        2800,
    "hd 7750":         2200,  "hd 6970":        3800, "hd 6950":        3400,
    "hd 6870":         3100,  "hd 6850":        2800, "hd 6790":        2600,
    "hd 6770":         2300,  "hd 5870":        3200, "hd 5850":        2900,
    "hd 5770":         1349,  "hd 5670":        1000,

    # ── Intel Arc ──
    "arc a770":       17200,  "arc a750":      15200,  "arc a580":      11500,
    "arc a380":        5800,  "arc a310":        3200,
}

# ── CPU benchmark lookup (relative gaming scores) ────────────────────────────
# Normalised so that an i5-10400 ≈ 10 000.  These reflect real-world gaming
# benchmarks (single-thread-biased multi-core), NOT pure Cinebench all-core.

_CPU_BENCHMARKS: dict[str, int] = {
    # ── Intel 15th gen (Arrow Lake) ──
    "core ultra 9 285k":  21000, "core ultra 7 265k":  19500, "core ultra 5 245k": 17500,

    # ── Intel 14th / 13th gen (Raptor Lake) ──
    "i9-14900k":  20500, "i9-14900kf": 20500, "i9-14900":  19000,
    "i7-14700k":  19500, "i7-14700kf": 19500, "i7-14700":  18000,
    "i5-14600k":  18000, "i5-14600kf": 18000, "i5-14600":  16500, "i5-14400": 14500, "i5-14400f": 14500,
    "i3-14100":   12500, "i3-14100f":  12500,
    "i9-13900k":  20000, "i9-13900kf": 20000, "i9-13900":  18500,
    "i7-13700k":  19000, "i7-13700kf": 19000, "i7-13700":  17000,
    "i5-13600k":  17500, "i5-13600kf": 17500, "i5-13600":  16000, "i5-13400": 14000, "i5-13400f": 14000,
    "i3-13100":   12000, "i3-13100f":  12000,

    # ── Intel 12th gen (Alder Lake) ──
    "i9-12900k":  18500, "i9-12900kf": 18500, "i9-12900":  17000,
    "i7-12700k":  17500, "i7-12700kf": 17500, "i7-12700":  16000,
    "i5-12600k":  16000, "i5-12600kf": 16000, "i5-12600":  14500, "i5-12400": 13000, "i5-12400f": 13000,
    "i3-12100":   11500, "i3-12100f":  11500,

    # ── Intel 11th gen (Rocket Lake) ──
    "i9-11900k":  15500, "i9-11900kf": 15500, "i9-11900":  14500,
    "i7-11700k":  14500, "i7-11700kf": 14500, "i7-11700":  13500,
    "i5-11600k":  13500, "i5-11600kf": 13500, "i5-11400":  12000, "i5-11400f": 12000,

    # ── Intel 10th gen (Comet Lake) ──
    "i9-10900k":  14000, "i9-10900kf": 14000, "i9-10900":  13200,
    "i7-10700k":  13000, "i7-10700kf": 13000, "i7-10700":  12500,
    "i5-10600k":  11500, "i5-10600kf": 11500, "i5-10400":  10000, "i5-10400f": 10000,
    "i3-10100":    8500, "i3-10100f":   8500, "i3-10300":   9000,

    # ── Intel 9th gen (Coffee Lake Refresh) ──
    "i9-9900k":   13000, "i9-9900kf":  13000, "i9-9900":   12500,
    "i7-9700k":   12000, "i7-9700kf":  12000, "i7-9700":   11000,
    "i5-9600k":   10500, "i5-9600kf":  10500, "i5-9400":    9000, "i5-9400f": 9000,
    "i3-9100":     7500, "i3-9100f":    7500,

    # ── Intel 8th gen (Coffee Lake) ──
    "i7-8700k":   11500, "i7-8700":   10500,
    "i5-8600k":    9800, "i5-8400":    8500,
    "i3-8100":     6800,

    # ── Intel 7th gen (Kaby Lake) ──
    "i7-7700k":    9800, "i7-7700":    9000,
    "i5-7600k":    8500, "i5-7500":    7700, "i5-7400": 7200,
    "i3-7100":     5800,

    # ── Intel 6th gen (Skylake) ──
    "i7-6700k":    9000, "i7-6700":    8200,
    "i5-6600k":    7800, "i5-6500":    7200, "i5-6400": 6500,
    "i3-6100":     5300,

    # ── Intel 4th gen (Haswell) ──
    "i7-4790k":    8000, "i7-4790":    7400, "i7-4770k": 7500, "i7-4770": 7000,
    "i5-4690k":    6800, "i5-4690":    6400, "i5-4590":  6200, "i5-4460": 5800,
    "i3-4170":     4500, "i3-4160":    4400, "i3-4130":  4200,

    # ── Intel 3rd gen (Ivy Bridge) ──
    "i7-3770k":    6500, "i7-3770":    6200,
    "i5-3570k":    5800, "i5-3570":    5500, "i5-3470": 5200,
    "i3-3220":     3500,

    # ── Intel 2nd gen (Sandy Bridge) ──
    "i7-2700k":    5800, "i7-2600k":   5600, "i7-2600": 5400,
    "i5-2500k":    5000, "i5-2500":    4800, "i5-2400": 4500,
    "i3-2120":     3200, "i3-2100":    3000,

    # ── Intel older ──
    "i7-960":      3000, "i7-920":     2700,
    "i5-760":      2600, "i5-750":     2400,
    "pentium g4560": 4500, "pentium g5400": 5000,

    # ── AMD Ryzen 9000 series ──
    "ryzen 9 9950x":  22000, "ryzen 9 9900x":  20500,
    "ryzen 7 9700x":  19000, "ryzen 5 9600x":  17500,

    # ── AMD Ryzen 7000 series ──
    "ryzen 9 7950x":  21000, "ryzen 9 7950x3d": 21500, "ryzen 9 7900x": 20000, "ryzen 9 7900": 19000,
    "ryzen 7 7800x3d": 20000, "ryzen 7 7700x": 18500, "ryzen 7 7700": 17500,
    "ryzen 5 7600x":  17000, "ryzen 5 7600":   16000,

    # ── AMD Ryzen 5000 series ──
    "ryzen 9 5950x":  18000, "ryzen 9 5900x":  17500,
    "ryzen 7 5800x3d": 17000, "ryzen 7 5800x": 16000, "ryzen 7 5800": 15500, "ryzen 7 5700x": 15000,
    "ryzen 5 5600x":  14500, "ryzen 5 5600":   13500, "ryzen 5 5500":  12000,

    # ── AMD Ryzen 3000 series ──
    "ryzen 9 3950x":  14500, "ryzen 9 3900x":  14000, "ryzen 9 3900": 13500,
    "ryzen 7 3800x":  13000, "ryzen 7 3700x":  12500,
    "ryzen 5 3600x":  11500, "ryzen 5 3600":   11000, "ryzen 5 3500":  9500,
    "ryzen 3 3300x":   9500, "ryzen 3 3100":    8500,

    # ── AMD Ryzen 2000 series ──
    "ryzen 7 2700x":  10000, "ryzen 7 2700":    9500,
    "ryzen 5 2600x":   9000, "ryzen 5 2600":    8500, "ryzen 5 2400g": 7500,
    "ryzen 3 2200g":   6000,

    # ── AMD Ryzen 1000 series ──
    "ryzen 7 1800x":   8500, "ryzen 7 1700x":   8000, "ryzen 7 1700": 7500,
    "ryzen 5 1600x":   7200, "ryzen 5 1600":    7000, "ryzen 5 1500x": 6500,
    "ryzen 3 1300x":   5500, "ryzen 3 1200":    5000,

    # ── AMD FX series ──
    "fx-9590":  5500, "fx-9370":  5200, "fx-8370":  5000, "fx-8350":  4800,
    "fx-8320":  4500, "fx-8300":  4300, "fx-6350":  4000, "fx-6300":  3800,
    "fx-4350":  3200, "fx-4300":  3000,

    # ── AMD older ──
    "phenom ii x6 1100t": 3200, "phenom ii x6 1090t": 3100, "phenom ii x4 965": 2600,
    "phenom ii x4 955":   2500, "athlon x4 860k":     2800, "athlon x4 750k":  2200,
    "athlon ii x4 640":   2000,
}


def _normalise_hw_name(name: str) -> str:
    """Lowercase, collapse whitespace, strip common prefixes/suffixes."""
    s = (name or "").lower()
    # Strip vendor prefixes
    for prefix in ("nvidia ", "geforce ", "amd ", "radeon ", "intel ", "core ", "® ", "(r) ", "(tm) "):
        s = s.replace(prefix, "")
    # Collapse whitespace & trim
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _fuzzy_gpu_lookup(name: str) -> Optional[int]:
    """Look up a GPU name in the benchmark table with fuzzy matching."""
    s = _normalise_hw_name(name)
    if not s:
        return None

    # Exact match
    if s in _GPU_BENCHMARKS:
        return _GPU_BENCHMARKS[s]

    # Try with common suffix removal (e.g. "8 GB", "12 GB", "OC", "Gaming")
    cleaned = re.sub(r"\s*\d+\s*gb\b", "", s)
    cleaned = re.sub(r"\s*(oc|gaming|eagle|founders edition|ventus|aero|strix|tuf)\b", "", cleaned).strip()
    if cleaned in _GPU_BENCHMARKS:
        return _GPU_BENCHMARKS[cleaned]

    # Best-match: find the longest table key that appears as a substring
    best_key, best_score = None, 0
    for key, score in _GPU_BENCHMARKS.items():
        if key in s and len(key) > best_score:
            best_key, best_score = key, len(key)
    if best_key:
        return _GPU_BENCHMARKS[best_key]

    return None


def _fuzzy_cpu_lookup(name: str) -> Optional[int]:
    """Look up a CPU name in the benchmark table with fuzzy matching."""
    s = _normalise_hw_name(name)
    if not s:
        return None

    # Exact match
    if s in _CPU_BENCHMARKS:
        return _CPU_BENCHMARKS[s]

    # Normalise Intel model format: "i7 12700k" -> "i7-12700k"
    s_dash = re.sub(r"\bi([3579])\s+", r"i\1-", s)
    if s_dash in _CPU_BENCHMARKS:
        return _CPU_BENCHMARKS[s_dash]

    # Best-match: find the longest table key that appears as a substring
    best_key, best_score = None, 0
    for key, score in _CPU_BENCHMARKS.items():
        if key in s and len(key) > best_score:
            best_key, best_score = key, len(key)
    if best_key:
        return _CPU_BENCHMARKS[best_key]
    # Also try s_dash
    for key, score in _CPU_BENCHMARKS.items():
        if key in s_dash and len(key) > best_score:
            best_key, best_score = key, len(key)
    if best_key:
        return _CPU_BENCHMARKS[best_key]

    return None


def _gpu_score(name: str) -> Optional[float]:
    """Return a GPU performance score via benchmark lookup, falling back to regex heuristic."""
    if name in _gpu_score_cache:
        return _gpu_score_cache[name]
    val = _fuzzy_gpu_lookup(name)
    if val is not None:
        _gpu_score_cache[name] = float(val)
        return float(val)

    # Regex fallback for GPUs not in the table
    s = (name or "").lower()
    bonus = 0.0
    if " ti" in s or s.endswith("ti"):
        bonus += 800.0
    if "super" in s:
        bonus += 600.0
    if " xt" in s or s.endswith("xt"):
        bonus += 600.0
    if " xtx" in s or s.endswith("xtx"):
        bonus += 400.0  # additional on top of xt

    # NVIDIA GeForce RTX / GTX / GT / MX
    n = re.search(r"(rtx|gtx|gt|mx)\s*(\d{3,4})", s)
    if n:
        fam = {"gt": 500, "mx": 1500, "gtx": 4000, "rtx": 12000}.get(n.group(1), 0)
        model = int(n.group(2))
        result = fam + model * 3.5 + bonus
        _gpu_score_cache[name] = result
        return result

    # AMD Radeon RX
    a = re.search(r"\brx\s*(\d{3,4})", s)
    if a:
        result = 3000 + int(a.group(1)) * 4.0 + bonus
        _gpu_score_cache[name] = result
        return result

    # AMD Radeon R5/R7/R9 (older GCN)
    rad = re.search(r"\br([579])\s*(\d{3})", s)
    if rad:
        tier = int(rad.group(1))
        model = int(rad.group(2))
        result = tier * 400 + model * 3.0 + bonus
        _gpu_score_cache[name] = result
        return result

    # AMD Radeon HD (very old)
    hd = re.search(r"\bhd\s*(\d{4})", s)
    if hd:
        result = 800 + int(hd.group(1)) * 0.5 + bonus
        _gpu_score_cache[name] = result
        return result

    # Intel Arc
    arc = re.search(r"\barc\s*a?\s*(\d{3,4})", s)
    if arc:
        result = 3500 + int(arc.group(1)) * 4.0 + bonus
        _gpu_score_cache[name] = result
        return result

    # Intel UHD / HD / Iris (integrated)
    igpu = re.search(r"\b(?:uhd|hd|iris(?:\s*(?:pro|plus|xe))?)\s*(\d{3,4})?\b", s)
    if igpu:
        model = int(igpu.group(1)) if igpu.group(1) else 600
        base = 800 if "iris" in s else 400
        result = base + model * 0.8
        _gpu_score_cache[name] = result
        return result

    # NVIDIA Quadro (workstation)
    quadro = re.search(r"\bquadro\s*(?:rtx\s*)?(\w*\d{3,4})", s)
    if quadro:
        model_str = quadro.group(1)
        digits = re.search(r"(\d+)", model_str)
        model_num = int(digits.group(1)) if digits else 2000
        base = 10000 if "rtx" in s else 4000
        result = base + model_num * 2.0 + bonus
        _gpu_score_cache[name] = result
        return result

    _gpu_score_cache[name] = None
    return None


def _cpu_score(name: str) -> Optional[float]:
    """Return a CPU performance score via benchmark lookup, falling back to regex heuristic."""
    if name in _cpu_score_cache:
        return _cpu_score_cache[name]
    val = _fuzzy_cpu_lookup(name)
    if val is not None:
        _cpu_score_cache[name] = float(val)
        return float(val)

    # Regex fallback
    s = (name or "").lower()

    # Intel Core iN-XXXXX (covers 2nd through 14th+ gen)
    i = re.search(r"\bi([3579])[-\s]?(\d{4,5})", s)
    if i:
        tier = int(i.group(1))
        model = i.group(2)
        gen = int(model[:2]) if len(model) == 5 else int(model[0])
        sku = int(model[-2:])
        k_bonus = 500 if re.search(r"\d[kf]", s) else 0
        result = tier * 1000 + gen * 350 + sku * 2.0 + k_bonus
        _cpu_score_cache[name] = result
        return result

    # Intel Core Ultra (e.g. "Core Ultra 7 155H")
    ultra = re.search(r"(?:core\s*)?ultra\s*([579])\s*(\d{3})", s)
    if ultra:
        tier = int(ultra.group(1))
        model = int(ultra.group(2))
        result = tier * 1200 + model * 40
        _cpu_score_cache[name] = result
        return result

    # AMD Ryzen N XXXX (covers 1000 through 9000 series)
    ry = re.search(r"ryzen\s*([3579])\s*(\d{4,5})", s)
    if ry:
        tier = int(ry.group(1))
        model = ry.group(2)
        gen = int(model[0])
        sku = int(model[-2:])
        x_bonus = 500 if "x" in s.split(model)[-1][:3] else 0
        x3d_bonus = 1500 if "x3d" in s else 0
        result = tier * 1000 + gen * 400 + sku * 2.0 + x_bonus + x3d_bonus
        _cpu_score_cache[name] = result
        return result

    # AMD FX series
    fx = re.search(r"\bfx[-\s]?(\d{4})", s)
    if fx:
        result = 2000 + int(fx.group(1)) * 0.5
        _cpu_score_cache[name] = result
        return result

    # AMD Athlon / Phenom
    ath = re.search(r"\b(?:athlon|phenom)\b.*?(\d{3,4})", s)
    if ath:
        result = 1500 + int(ath.group(1)) * 1.5
        _cpu_score_cache[name] = result
        return result

    # Intel Pentium / Celeron
    pen = re.search(r"\b(?:pentium|celeron)\s*(?:g|j|n)?(\d{3,5})", s)
    if pen:
        result = 2000 + int(pen.group(1)) * 0.6
        _cpu_score_cache[name] = result
        return result

    # Intel Xeon (workstation/server)
    xeon = re.search(r"\bxeon\b.*?(?:e[357]|w)[-\s]?(\d{4,5})", s)
    if xeon:
        model = int(xeon.group(1))
        result = 5000 + model * 1.2
        _cpu_score_cache[name] = result
        return result

    # Very generic fallback: "X GHz" with core count
    ghz = re.search(r"(\d+(?:\.\d+)?)\s*ghz", s)
    cores = re.search(r"(\d+)[-\s]*cores?", s)
    if ghz:
        freq = float(ghz.group(1))
        n_cores = int(cores.group(1)) if cores else 4
        result = freq * 1800 + n_cores * 600
        _cpu_score_cache[name] = result
        return result

    _cpu_score_cache[name] = None
    return None


def _required_score(req: str, kind: str) -> Optional[float]:
    # Steam lists GPU/CPU alternatives separated by '/', 'or', 'and', 'and/or', or '&'.
    parts = re.split(r"\s*/\s*|\s+(?:and/or|or|and)\s+|\s*&\s*|\|", req, flags=re.IGNORECASE)
    vals = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # Strip common suffixes that confuse scoring
        p = re.sub(r"\s*\(.*?\)\s*$", "", p)  # "(or equivalent)"
        p = re.sub(r"\s*(?:or\s+)?(?:equivalent|better|higher|above|comparable|newer)\s*$", "", p, flags=re.IGNORECASE)
        val = _gpu_score(p) if kind == "gpu" else _cpu_score(p)
        if val is not None:
            vals.append(val)
    if not vals:
        # Last resort: try scoring the entire string as-is
        val = _gpu_score(req) if kind == "gpu" else _cpu_score(req)
        if val is not None:
            return val
        return None
    # Any of the listed alternatives is sufficient — use the minimum threshold.
    return min(vals)


def _fallback_component_eval(kind: str, pc: dict, required_text: str) -> Optional[dict]:
    req = (required_text or "").lower()
    cpu = (pc.get("cpu") or "").lower()
    gpu = (pc.get("gpu") or "").lower()

    if kind == "cpu":
        modern_cpu_markers = [
            "core i3", "core i5", "core i7", "core i9", "ryzen", "threadripper", "xeon", "pentium gold", "pentium g",
        ]

        if "sse3" in req:
            if any(m in cpu for m in modern_cpu_markers):
                return {"status": "pass", "reason": "Modern CPU family likely supports SSE3.", "source": "heuristic"}
            if "pentium 4" in cpu:
                return {"status": "pass", "reason": "Pentium 4 baseline matched; SSE3 treated as supported.", "source": "heuristic"}
            if "athlon xp" in cpu or "pentium iii" in cpu:
                return {"status": "fail", "reason": "Detected very old CPU family likely below SSE3 baseline.", "source": "heuristic"}
            return {"status": "warn", "reason": "Cannot verify SSE3 capability from CPU name alone.", "source": "heuristic"}

        if "pentium 4" in req and ("or later" in req or "or better" in req):
            if any(m in cpu for m in modern_cpu_markers) or "pentium 4" in cpu:
                return {"status": "pass", "reason": "CPU appears newer than Pentium 4 baseline.", "source": "heuristic"}
            return {"status": "warn", "reason": "Could not map CPU to Pentium 4-or-later baseline.", "source": "heuristic"}

    if kind == "gpu":
        if "hardware accelerated" in req and "dedicated memory" in req:
            if "microsoft basic display" in gpu or "unknown" in gpu:
                return {"status": "warn", "reason": "GPU model is unclear for hardware acceleration check.", "source": "heuristic"}
            vram = pc.get("vram_gb", 0.0) or 0.0
            status = "pass" if vram >= 1.0 else "warn"
            return {
                "status": status,
                "reason": "Dedicated GPU memory heuristic used for non-specific graphics requirement.",
                "source": "heuristic",
            }

    return None


def estimate_performance(compat: dict) -> dict:
    min_ok = compat.get("overall_min")
    rec_ok = compat.get("overall_rec")

    if min_ok == "fail":
        return {
            "confidence": "medium",
            "note": "Below minimum requirements; expect instability.",
            "presets": {
                "low": "<30 FPS",
                "medium": "Not recommended",
                "high": "Not playable",
            },
        }

    if min_ok == "pass" and rec_ok == "fail":
        return {
            "confidence": "low",
            "note": "Meets minimum but not recommended specs.",
            "presets": {
                "low": "35-60 FPS",
                "medium": "25-45 FPS",
                "high": "<30 FPS",
            },
        }

    if min_ok == "pass" and rec_ok == "pass":
        return {
            "confidence": "low",
            "note": "Based on requirement tiers; real FPS varies by scene and drivers.",
            "presets": {
                "low": "60+ FPS",
                "medium": "45-75 FPS",
                "high": "35-60 FPS",
            },
        }

    return {
        "confidence": "low",
        "note": "Insufficient requirement detail for accurate estimate.",
        "presets": {
            "low": "Unknown",
            "medium": "Unknown",
            "high": "Unknown",
        },
    }


def _ratio(measured: Optional[float], required: Optional[float], cap: float = 3.0) -> Optional[float]:
    if measured is None or required is None or required <= 0:
        return None
    return max(0.0, min(cap, measured / required))


def _harmonic_mean(values: list) -> float:
    """Harmonic mean — naturally sensitive to low outliers (bottleneck-aware)."""
    if not values:
        return 0.0
    return len(values) / sum(1.0 / max(v, 0.001) for v in values)


def _score_to_fps(effective: float) -> tuple:
    """
    Continuous piecewise-linear mapping from effective hardware ratio to
    (median_fps, half_spread) anchored at 1080p medium settings.

    Anchors calibrated to aggregated GPU benchmark data vs stated game requirements.
    Spread reflects prediction uncertainty and narrows as the ratio rises.
    """
    ANCHORS = [
        # (ratio, median_fps, half-spread)
        (0.00,   5.0, 0.55),
        (0.50,  12.0, 0.45),
        (0.75,  22.0, 0.38),
        (0.90,  30.0, 0.32),
        (1.00,  38.0, 0.28),
        (1.20,  52.0, 0.24),
        (1.40,  66.0, 0.21),
        (1.65,  84.0, 0.19),
        (2.00, 105.0, 0.17),
        (3.00, 140.0, 0.14),
    ]
    if effective <= ANCHORS[0][0]:
        return ANCHORS[0][1], ANCHORS[0][2]
    if effective >= ANCHORS[-1][0]:
        return ANCHORS[-1][1], ANCHORS[-1][2]
    for i in range(len(ANCHORS) - 1):
        r0, f0, s0 = ANCHORS[i]
        r1, f1, s1 = ANCHORS[i + 1]
        if r0 <= effective <= r1:
            t = (effective - r0) / (r1 - r0)
            return f0 + t * (f1 - f0), s0 + t * (s1 - s0)
    return 38.0, 0.28


def ai_predict_performance(pc: dict, reqs: dict, compat: dict) -> dict:
    """
    Performance predictor v4: continuous FPS curve with harmonic-mean bottleneck
    and VRAM stutter penalty.

    Key improvements over v3:
    - Continuous piecewise-linear FPS mapping instead of discrete step bands.
    - Bottleneck is the harmonic mean of GPU+CPU ratios: more sensitive to the
      weaker component than arithmetic mean, less punishing than pure min().
    - VRAM below requirement applies an exponential stutter penalty.
    - Quality presets use empirically observed GPU-load deltas (+30% low, -32% high).
    - Recommended-tier score is GPU-weighted (0.55 / 0.30 / 0.15).
    """
    min_r = reqs.get("minimum", {})
    rec_r = reqs.get("recommended", {})

    pc_cpu  = _cpu_score(pc.get("cpu", ""))
    pc_gpu  = _gpu_score(pc.get("gpu", ""))
    pc_ram  = pc.get("ram_gb")
    pc_vram = pc.get("vram_gb")
    pc_dx   = _directx_major(pc.get("directx", ""))
    pc_storage = pc.get("free_disk_gb")

    min_cpu = _required_score(min_r.get("cpu", ""), "cpu") if "cpu" in min_r else None
    min_gpu = _required_score(min_r.get("gpu", ""), "gpu") if "gpu" in min_r else None
    min_ram = _size_gb(min_r.get("ram", ""), default_unit="GB") if "ram" in min_r else None
    min_dx  = _directx_major(min_r.get("directx", "")) if "directx" in min_r else None
    min_storage = _size_gb(min_r.get("storage", ""), default_unit="GB") if "storage" in min_r else None

    rec_cpu = _required_score(rec_r.get("cpu", ""), "cpu") if "cpu" in rec_r else None
    rec_gpu = _required_score(rec_r.get("gpu", ""), "gpu") if "gpu" in rec_r else None
    rec_ram = _size_gb(rec_r.get("ram", ""), default_unit="GB") if "ram" in rec_r else None

    # GPU-dominant weights reflect gaming's typical bottleneck profile.
    feature_weights = {
        "gpu":     0.45,
        "cpu":     0.28,
        "ram":     0.12,
        "vram":    0.08,
        "directx": 0.04,
        "storage": 0.03,
    }

    feature_ratios: dict[str, Optional[float]] = {}
    feature_ratios["gpu"] = _ratio(pc_gpu, min_gpu)
    feature_ratios["cpu"] = _ratio(pc_cpu, min_cpu)
    feature_ratios["ram"] = _ratio(pc_ram, min_ram)

    if pc_storage is not None and min_storage is not None and min_storage > 0:
        feature_ratios["storage"] = min(_ratio(pc_storage, min_storage) or 0.0, 1.5)
    else:
        feature_ratios["storage"] = None

    if min_dx is not None and pc_dx is not None:
        feature_ratios["directx"] = 1.15 if pc_dx >= min_dx else 0.5
    else:
        feature_ratios["directx"] = None

    # VRAM: extract from requirement text or infer from GPU benchmark class
    req_vram = None
    for src in (min_r.get("gpu", ""), rec_r.get("gpu", "")):
        m = re.search(r"\b(\d+)\s*gb\b", src, re.IGNORECASE)
        if m:
            req_vram = float(m.group(1))
            break
    if req_vram is None and min_gpu is not None:
        if min_gpu < 3000:
            req_vram = 1.0
        elif min_gpu < 6000:
            req_vram = 2.0
        elif min_gpu < 12000:
            req_vram = 4.0
        else:
            req_vram = 6.0
    feature_ratios["vram"] = _ratio(pc_vram, req_vram) if pc_vram and req_vram else None

    # Weighted score (minimum-tier)
    weighted_sum = 0.0
    used_weight  = 0.0
    ratios_used: list[float] = []
    for k, w in feature_weights.items():
        r = feature_ratios.get(k)
        if r is None:
            continue
        weighted_sum += r * w
        used_weight  += w
        ratios_used.append(r)

    if used_weight <= 0.0:
        return estimate_performance(compat)

    score_min = weighted_sum / used_weight

    # Recommended-tier score: GPU-primary weighted average
    rec_pairs = [(pc_gpu, rec_gpu, 0.55), (pc_cpu, rec_cpu, 0.30), (pc_ram, rec_ram, 0.15)]
    rec_wsum, rec_wused = 0.0, 0.0
    rec_ratios: list[float] = []
    for val, need, w in rec_pairs:
        r = _ratio(val, need)
        if r is not None:
            rec_wsum  += r * w
            rec_wused += w
            rec_ratios.append(r)
    score_rec = (rec_wsum / rec_wused) if rec_wused > 0 else score_min

    blended = (0.60 * score_min + 0.40 * score_rec) if rec_ratios else score_min

    # Bottleneck: harmonic mean of GPU and CPU ratios.
    # Harmonic mean is sensitive to the weaker component (like real GPU-CPU
    # bottlenecks) without the harshness of pure min(), and excludes non-FPS
    # signals like storage and DirectX from the bottleneck calculation.
    fps_core = [r for k, r in feature_ratios.items() if k in ("gpu", "cpu") and r is not None]
    bottleneck = _harmonic_mean(fps_core) if fps_core else blended

    # VRAM stutter penalty: below-budget VRAM causes texture streaming and
    # severe 1% lows. Model as exponential drag on the effective ratio.
    # vram_ratio 0.5 → ~34% penalty; 0.75 → ~16%; 0.9 → ~6%
    vram_ratio = feature_ratios.get("vram")
    vram_penalty = 1.0
    if vram_ratio is not None and vram_ratio < 1.0:
        vram_penalty = vram_ratio ** 0.6

    # Pull blended score toward the bottleneck, then apply VRAM drag
    effective = (0.60 * blended + 0.40 * bottleneck) * vram_penalty

    # Clamp so numeric score never contradicts explicit pass/fail verdict
    min_ok = compat.get("overall_min")
    rec_ok = compat.get("overall_rec")
    if min_ok == "fail":
        effective = min(effective, 0.85)
    elif min_ok == "pass" and rec_ok == "fail":
        effective = min(effective, 1.45)

    median_fps, spread = _score_to_fps(effective)

    # Widen spread under VRAM pressure (stutter variance is unpredictable)
    if vram_ratio is not None and vram_ratio < 1.0:
        spread = min(0.55, spread + (1.0 - vram_ratio) * 0.15)

    parsed_features = len(ratios_used)
    has_rec = bool(rec_ratios)
    if parsed_features < 3:
        spread = min(0.55, spread + 0.08)

    # Note
    if min_ok == "fail":
        note = "Below minimum requirements; expect poor performance."
    elif effective < 0.55:
        note = "Well below requirements; likely unplayable or slideshow."
    elif effective < 0.75:
        note = "Significantly below requirements; heavy stuttering expected."
    elif effective < 0.90:
        note = "System is below one or more key requirements."
    elif effective < 1.0:
        note = "Borderline setup; gameplay depends on scene complexity."
    elif effective < 1.15:
        note = "Meets minimum requirements; playable on low-medium settings."
    elif effective < 1.40:
        note = "Comfortable headroom; medium settings at 1080p."
    elif effective < 1.70:
        note = "Good fit; high settings at 1080p or medium at 1440p viable."
    elif effective < 2.10:
        note = "Strong headroom; high/ultra at 1080p or high at 1440p viable."
    else:
        note = "Overkill for this title; consider 1440p or 4K."

    if vram_ratio is not None and vram_ratio < 0.85 and min_ok != "fail":
        note += " VRAM shortage may cause stuttering."
    if rec_ok in ("unavailable", "unknown") and not rec_ratios:
        note += " (Only minimum requirements available; estimate less precise.)"
    note = note.strip()

    # Quality presets anchored at 1080p medium.
    # Low settings reduce GPU load (shadow quality, draw distance) → +30% FPS.
    # High/Ultra add RT, SSAO, dense geometry, high-res shadows → -32% FPS.
    LOW_BOOST = 1.30
    HIGH_DRAG = 0.68

    def _fmt(center: float, sp: float, floor_lo: int) -> str:
        lo = max(floor_lo, int(center * (1.0 - sp)))
        hi = max(floor_lo + 5, int(center * (1.0 + sp)))
        return f"{lo}-{hi} FPS"

    low    = _fmt(median_fps * LOW_BOOST, spread, 10)
    medium = _fmt(median_fps,              spread,  8)
    high   = _fmt(median_fps * HIGH_DRAG,  spread,  5)

    # 1% lows: ~62% of average FPS; drops to ~50% when VRAM is under pressure.
    one_pct_factor = 0.50 if (vram_ratio is not None and vram_ratio < 1.0) else 0.62
    one_pct_lo = max(4, int(median_fps * one_pct_factor * (1.0 - spread * 0.5)))
    one_pct_hi = max(8, int(median_fps * one_pct_factor * (1.0 + spread * 0.3)))
    one_percent_low = f"{one_pct_lo}-{one_pct_hi} FPS"

    if parsed_features >= 4 and has_rec:
        confidence = "high"
    elif parsed_features >= 3 or (parsed_features >= 2 and has_rec):
        confidence = "medium"
    else:
        confidence = "low"

    # Label the weakest FPS-critical component, but only when it's actually
    # constraining — either below requirement (<1.0) or noticeably weaker than
    # the strongest component and still inside the headroom-limited zone.
    fps_labeled = [(k, feature_ratios[k]) for k in ("gpu", "cpu", "vram", "ram")
                   if feature_ratios.get(k) is not None]
    bottleneck_label = "Balanced"
    if fps_labeled:
        weakest_k, weakest_v = min(fps_labeled, key=lambda x: x[1])
        strongest_v = max(v for _, v in fps_labeled)
        is_constraining = weakest_v < 1.0 or weakest_v < min(1.3, strongest_v * 0.75)
        if is_constraining:
            bottleneck_label = weakest_k.upper()

    fps_hi = max(25.0, median_fps * (1.0 + spread))
    fps_lo = max(8.0,  median_fps * (1.0 - spread))

    return {
        "model": "AI Predictor v4 (continuous-curve)",
        "confidence": confidence,
        "score": round(effective, 2),
        "score_min": round(score_min, 2),
        "score_rec": round(score_rec, 2),
        "bottleneck": bottleneck_label,
        "note": note,
        "metrics": {
            "one_percent_low": one_percent_low,
            "render_latency_ms": f"~{round(1000 / fps_hi, 1)}-{round(1000 / fps_lo, 1)} ms",
        },
        "presets": {
            "low": low,
            "medium": medium,
            "high": high,
        },
    }


def check_compatibility(pc: dict, reqs: dict) -> dict:
    results = {}

    for tier in ("minimum", "recommended"):
        r = reqs.get(tier, {})
        t = {}

        if "ram" in r:
            req_gb = _size_gb(r["ram"], default_unit="GB")
            if req_gb is not None:
                # Windows-reported GiB can be slightly below marketed GB labels (e.g., 15.8 vs 16).
                eps = 0.3
                if pc["ram_gb"] + eps >= req_gb:
                    status = "pass"
                elif pc["ram_gb"] + 1.0 >= req_gb:
                    status = "warn"
                else:
                    status = "fail"
                t["ram"] = {
                    "status":   status,
                    "yours":    f"{pc['ram_gb']} GB",
                    "required": f"{req_gb:.0f} GB",
                }

        if "os" in r:
            req_os = r["os"].lower()
            pc_os  = pc["os"].lower()
            win_m  = re.search(r"windows\s*(10|11|7|8\.?\d?|vista|xp)?", req_os)
            status = "pass"
            if win_m and win_m.group(1):
                ver_map = {"xp": 0, "vista": 1, "7": 2, "8": 3, "81": 3, "10": 4, "11": 5}
                req_v   = ver_map.get(win_m.group(1).replace(".", ""), 4)
                pc_v    = 5 if "11" in pc_os else 4 if "10" in pc_os else 3
                status  = "pass" if pc_v >= req_v else "fail"
            t["os"] = {
                "status":   status,
                "yours":    pc["os"],
                "required": r["os"],
            }

        for key, label in [("cpu", pc["cpu"]), ("gpu", pc["gpu"])]:
            if key in r:
                yours = _gpu_score(label) if key == "gpu" else _cpu_score(label)
                req_score = _required_score(r[key], key)
                if yours is not None and req_score is not None:
                    status = "pass" if yours >= req_score else "fail"
                    note = ""
                else:
                    resolved = _fallback_component_eval(key, pc, r[key])
                    if resolved:
                        status = resolved.get("status", "warn")
                        note = resolved.get("reason", "")
                    else:
                        status = "warn"
                        note = "Could not confidently parse this requirement text."
                t[key] = {
                    "status": status,
                    "yours": label,
                    "required": r[key],
                }
                if note:
                    t[key]["note"] = note

        if "directx" in r:
            req_dx = _directx_major(r["directx"])
            your_dx = _directx_major(pc.get("directx", ""))
            if req_dx is not None and your_dx is not None:
                status = "pass" if your_dx >= req_dx else "fail"
            else:
                status = "warn"
            t["directx"] = {
                "status": status,
                "yours": pc.get("directx", "Unknown"),
                "required": r["directx"],
            }

        if "storage" in r:
            req_storage_gb = _size_gb(r["storage"], default_unit="GB")
            if req_storage_gb is not None:
                ok = pc.get("disk_free_gb", 0.0) >= req_storage_gb
                t["storage"] = {
                    "status": "pass" if ok else "fail",
                    "yours": f"{pc.get('disk_free_gb', 0.0)} GB free",
                    "required": f"{req_storage_gb:.0f} GB free",
                }
            else:
                t["storage"] = {"status": "info", "yours": "Check free disk space", "required": r["storage"]}

        results[tier] = t

    # Core rule: if recommended passes, minimum must also pass.
    rec = results.get("recommended", {})
    minn = results.get("minimum", {})
    for key, rec_entry in rec.items():
        if rec_entry.get("status") == "pass" and key in minn and minn[key].get("status") in ("fail", "warn"):
            minn[key]["status"] = "pass"

    for tier in ("minimum", "recommended"):
        tier_data = results.get(tier, {})
        statuses = [v["status"] for v in tier_data.values()]
        measured = [s for s in statuses if s in {"pass", "fail"}]
        if not measured:
            # Distinguish "game has no reqs for this tier" from "data but unparseable"
            tier_reqs = reqs.get(tier)
            if tier_reqs is None or (isinstance(tier_reqs, dict) and len(tier_reqs) == 0
                                     and not tier_data):
                results[f"overall_{tier[:3]}"] = "unavailable"
            else:
                results[f"overall_{tier[:3]}"] = "unknown"
        elif "fail" in measured:
            results[f"overall_{tier[:3]}"] = "fail"
        else:
            results[f"overall_{tier[:3]}"] = "pass"

    # Always include estimated metrics so overlay can always render this section.
    results["performance"] = ai_predict_performance(pc, reqs, results)

    return results


# ──────────────────────────────────────────────────────────────────────────────
# 5.  WEBSOCKET SERVER
# ──────────────────────────────────────────────────────────────────────────────

CLIENTS: set = set()
pc_specs: dict = {}
# Set once collect_pc_specs() finishes so search threads can safely wait.
_specs_ready = threading.Event()


async def ws_handler(websocket):
    CLIENTS.add(websocket)
    log.info(f"Overlay connected ({len(CLIENTS)} total)")
    await websocket.send(json.dumps({"type": "specs", "data": pc_specs}))
    try:
        async for _ in websocket:
            pass
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        CLIENTS.discard(websocket)


async def broadcast(msg: dict):
    if not CLIENTS:
        return
    raw = json.dumps(msg)
    await asyncio.gather(*(c.send(raw) for c in list(CLIENTS)), return_exceptions=True)


# ──────────────────────────────────────────────────────────────────────────────
# 6.  WATCHER LOOP
# ──────────────────────────────────────────────────────────────────────────────

async def watcher_loop():
    loop     = asyncio.get_event_loop()
    last_key = None
    debug_ok_last = True
    log.info("CEF page watcher active. Polling every 1.5 s...")

    while True:
      try:
        page, debug_ok = await loop.run_in_executor(None, get_steam_page)

        if not debug_ok:
            if debug_ok_last:
                log.warning("Steam debug endpoint unavailable at %s", CEF_DEBUG_URL)
                await broadcast({
                    "type": "warning",
                    "msg": "Steam debug port is unavailable. Relaunch Steam with debug flags.",
                })
            debug_ok_last = False
            if last_key is not None:
                last_key = None
            await asyncio.sleep(1.5)
            continue

        if not debug_ok_last:
            log.info("Steam debug endpoint is available again")
            await broadcast({"type": "idle"})
        debug_ok_last = True

        if page is None:
            if last_key is not None:
                last_key = None
                await broadcast({"type": "idle"})
        else:
            # User preference: detect the game itself, not the exact subsection.
            key = page["app_id"]
            if key != last_key:
                last_key      = key
                section_label = page["section"]
                log.info(f"Steam → AppID {page['app_id']} / {section_label}")

                await broadcast({
                    "type":    "loading",
                    "app_id":  page["app_id"],
                    "section": section_label,
                })

                reqs = await loop.run_in_executor(None, fetch_requirements, page["app_id"])
                if reqs:
                    compat = check_compatibility(pc_specs, reqs)
                    log.info(
                        "Compat verdict for AppID %s: minimum=%s recommended=%s",
                        page["app_id"],
                        compat.get("overall_min", "unknown"),
                        compat.get("overall_rec", "unknown"),
                    )
                    await broadcast({
                        "type":    "result",
                        "app_id":  page["app_id"],
                        "section": section_label,
                        "reqs":    reqs,
                        "compat":  compat,
                    })
                else:
                    await broadcast({
                        "type":    "error",
                        "app_id":  page["app_id"],
                        "section": section_label,
                        "msg":     "No system requirements found for this game.",
                    })

        await asyncio.sleep(1.5)
      except Exception as e:
        log.error("Watcher loop error (recovering): %s", e, exc_info=True)
        await asyncio.sleep(3)


async def main():
    global pc_specs
    loop     = asyncio.get_event_loop()
    pc_specs = await loop.run_in_executor(None, collect_pc_specs)
    _specs_ready.set()
    log.info(f"WebSocket server starting on ws://{WS_HOST}:{WS_PORT}")
    for attempt in range(5):
        try:
            async with serve(ws_handler, WS_HOST, WS_PORT):
                await watcher_loop()
        except OSError as e:
            if attempt < 4:
                log.warning("WS port %s busy, retrying in %ds: %s", WS_PORT, 2 * (attempt + 1), e)
                await asyncio.sleep(2 * (attempt + 1))
            else:
                log.error("Could not bind WS port %s after 5 attempts", WS_PORT)
                raise


if __name__ == "__main__":
    asyncio.run(main())