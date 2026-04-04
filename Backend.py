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
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            out[key] = m.group(1).strip().rstrip("*").strip()
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
    m = re.search(r"directx\s*(\d+)|dx\s*(\d+)|version\s*(\d+)", s, re.IGNORECASE)
    if not m:
        return None
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

    n = re.search(r"(rtx|gtx|gt|mx)\s*(\d{3,4})", s)
    if n:
        fam = {"gt": 500, "mx": 1500, "gtx": 4000, "rtx": 12000}.get(n.group(1), 0)
        model = int(n.group(2))
        result = fam + model * 3.5 + bonus
        _gpu_score_cache[name] = result
        return result

    a = re.search(r"\brx\s*(\d{3,4})", s)
    if a:
        result = 3000 + int(a.group(1)) * 4.0 + bonus
        _gpu_score_cache[name] = result
        return result

    arc = re.search(r"\barc\s*a?\s*(\d{3,4})", s)
    if arc:
        result = 3500 + int(arc.group(1)) * 4.0 + bonus
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

    i = re.search(r"\bi([3579])[-\s]?(\d{4,5})", s)
    if i:
        tier = int(i.group(1))
        model = i.group(2)
        gen = int(model[:2]) if len(model) == 5 else int(model[0])
        sku = int(model[-2:])
        result = tier * 1000 + gen * 350 + sku * 2.0
        _cpu_score_cache[name] = result
        return result

    ry = re.search(r"ryzen\s*([3579])\s*(\d{4,5})", s)
    if ry:
        tier = int(ry.group(1))
        model = ry.group(2)
        gen = int(model[0])
        sku = int(model[-2:])
        result = tier * 1000 + gen * 400 + sku * 2.0
        _cpu_score_cache[name] = result
        return result

    _cpu_score_cache[name] = None
    return None


def _required_score(req: str, kind: str) -> Optional[float]:
    # Steam often lists alternatives separated by '/' or 'or'.
    parts = re.split(r"\s*/\s*|\s+or\s+|\|", req, flags=re.IGNORECASE)
    vals = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        val = _gpu_score(p) if kind == "gpu" else _cpu_score(p)
        if val is not None:
            vals.append(val)
    if not vals:
        return None
    # Requirement alternatives are equivalent-ish; minimum threshold is enough.
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


def _score_to_band(score: float, bottleneck: float) -> tuple:
    """
    Convert a performance score ratio + bottleneck into an estimated FPS band.
    Uses a non-linear curve inspired by real-world diminishing-returns behaviour:
    doubling hardware above requirements does NOT double FPS.
    """
    # The bottleneck limits the real effective performance more than averages suggest.
    effective = 0.55 * score + 0.45 * bottleneck

    if effective < 0.55:
        return (10, 20, "Well below requirements; likely unplayable or slideshow.")
    if effective < 0.75:
        return (15, 28, "Significantly below requirements; heavy stuttering expected.")
    if effective < 0.90:
        return (22, 38, "System is below one or more key requirements.")
    if effective < 1.0:
        return (30, 48, "Borderline setup; gameplay depends on scene complexity.")
    if effective < 1.15:
        return (40, 60, "Meets requirements; playable on low-medium settings.")
    if effective < 1.35:
        return (50, 75, "Comfortable headroom for medium settings.")
    if effective < 1.60:
        return (60, 95, "Good fit; high settings should be achievable.")
    if effective < 2.0:
        return (75, 120, "Strong hardware headroom; high/ultra viable.")
    return (90, 144, "Overkill for this title; expect very high framerates.")


def ai_predict_performance(pc: dict, reqs: dict, compat: dict) -> dict:
    """
    Performance predictor v3: benchmark-backed hardware scoring with weighted
    component ratios, non-linear FPS curves, and GPU/CPU bottleneck analysis.

    Algorithm:
    1.  Score user's CPU & GPU via PassMark-calibrated lookup tables.
    2.  Score the game's required CPU & GPU the same way.
    3.  Compute per-component ratios (user / required), capped at 3.0.
    4.  GPU ratio is weighted highest (0.45) since games are overwhelmingly
        GPU-bound; CPU (0.30) is next; RAM/VRAM/DX share the rest.
    5.  The *bottleneck* (lowest single ratio) drags the FPS down harder
        than a pure weighted average would suggest, reflecting real-world
        behaviour where one slow component starves the pipeline.
    6.  Blend min-tier viability (65%) with rec-tier headroom (35%).
    7.  Map to FPS bands using non-linear diminishing-returns curve.
    """
    min_r = reqs.get("minimum", {})
    rec_r = reqs.get("recommended", {})

    pc_cpu  = _cpu_score(pc.get("cpu", ""))
    pc_gpu  = _gpu_score(pc.get("gpu", ""))
    pc_ram  = pc.get("ram_gb")
    pc_vram = pc.get("vram_gb")
    pc_dx   = _directx_major(pc.get("directx", ""))

    min_cpu = _required_score(min_r.get("cpu", ""), "cpu") if "cpu" in min_r else None
    min_gpu = _required_score(min_r.get("gpu", ""), "gpu") if "gpu" in min_r else None
    min_ram = _size_gb(min_r.get("ram", ""), default_unit="GB") if "ram" in min_r else None
    min_dx  = _directx_major(min_r.get("directx", "")) if "directx" in min_r else None

    rec_cpu = _required_score(rec_r.get("cpu", ""), "cpu") if "cpu" in rec_r else None
    rec_gpu = _required_score(rec_r.get("gpu", ""), "gpu") if "gpu" in rec_r else None
    rec_ram = _size_gb(rec_r.get("ram", ""), default_unit="GB") if "ram" in rec_r else None

    # ── Weighted feature ratios (GPU‑dominant like real games) ──
    feature_weights = {
        "gpu":     0.45,
        "cpu":     0.30,
        "ram":     0.12,
        "vram":    0.08,
        "directx": 0.05,
    }

    feature_ratios: dict[str, Optional[float]] = {}
    feature_ratios["gpu"] = _ratio(pc_gpu, min_gpu)
    feature_ratios["cpu"] = _ratio(pc_cpu, min_cpu)
    feature_ratios["ram"] = _ratio(pc_ram, min_ram)

    # DirectX: binary pass/fail with a small boost for exceeding
    if min_dx is not None and pc_dx is not None:
        feature_ratios["directx"] = 1.15 if pc_dx >= min_dx else 0.5
    else:
        feature_ratios["directx"] = None

    # VRAM: infer from GPU requirement text or GPU tier
    req_vram = None
    for src in (min_r.get("gpu", ""), rec_r.get("gpu", "")):
        m = re.search(r"\b(\d+)\s*gb\b", src, re.IGNORECASE)
        if m:
            req_vram = float(m.group(1))
            break
    if req_vram is None and min_gpu is not None:
        # Heuristic: map GPU class to typical VRAM floor
        if min_gpu < 3000:
            req_vram = 1.0
        elif min_gpu < 6000:
            req_vram = 2.0
        elif min_gpu < 12000:
            req_vram = 4.0
        else:
            req_vram = 6.0
    feature_ratios["vram"] = _ratio(pc_vram, req_vram) if pc_vram and req_vram else None

    # ── Compute weighted score ──
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

    # ── Recommended-tier headroom ──
    rec_ratios: list[float] = []
    for val, need in ((pc_gpu, rec_gpu), (pc_cpu, rec_cpu), (pc_ram, rec_ram)):
        r = _ratio(val, need)
        if r is not None:
            rec_ratios.append(r)
    score_rec = sum(rec_ratios) / len(rec_ratios) if rec_ratios else score_min

    # Blend minimum viability with recommended headroom
    # If no recommended data exists, rely 100% on minimum-tier scoring
    rec_ok = compat.get("overall_rec")
    if rec_ratios:
        score = 0.65 * score_min + 0.35 * score_rec
    else:
        score = score_min

    # Bottleneck is the weakest link
    bottleneck = min(ratios_used) if ratios_used else score

    # ── Map to FPS band ──
    base_low, base_high, note = _score_to_band(score, bottleneck)

    # Clamp if pass/fail results contradict the numeric prediction
    min_ok = compat.get("overall_min")
    if min_ok == "fail":
        base_low  = min(base_low, 20)
        base_high = min(base_high, 35)
        note = "Below minimum requirements; expect poor performance."
    elif min_ok == "pass" and rec_ok == "fail":
        base_high = min(base_high, 65)

    # When only minimum reqs are available, note the reduced accuracy
    if rec_ok in ("unavailable", "unknown") and not rec_ratios:
        note += " (Only minimum requirements available; estimate less precise.)"
        note = note.strip()

    # ── Preset differentiation: low ≈ base, medium ≈ -15%, high ≈ -30% ──
    low_lo  = max(12, int(base_low * 1.05))
    low_hi  = max(20, int(base_high * 1.10))
    med_lo  = max(10, int(base_low * 0.82))
    med_hi  = max(18, int(base_high * 0.88))
    high_lo = max(8,  int(base_low * 0.60))
    high_hi = max(15, int(base_high * 0.68))

    low    = f"{low_lo}-{low_hi} FPS"
    medium = f"{med_lo}-{med_hi} FPS"
    high   = f"{high_lo}-{high_hi} FPS"

    # 1% lows and frame-time
    one_pct_lo = max(6,  int(base_low * 0.55))
    one_pct_hi = max(12, int(base_low * 0.80))
    one_percent_low = f"{one_pct_lo}-{one_pct_hi} FPS"

    # Confidence based on how many features we could actually score
    parsed_features = len(ratios_used)
    has_rec = bool(rec_ratios)
    if parsed_features >= 4 and has_rec:
        confidence = "high"
    elif parsed_features >= 3 and has_rec:
        confidence = "medium"
    elif parsed_features >= 3:
        confidence = "medium"  # min-only but enough components
    else:
        confidence = "low"

    # Identify the bottleneck component
    bottleneck_label = "Balanced"
    if feature_ratios.get("gpu") is not None and feature_ratios["gpu"] == bottleneck:
        bottleneck_label = "GPU"
    elif feature_ratios.get("cpu") is not None and feature_ratios["cpu"] == bottleneck:
        bottleneck_label = "CPU"
    elif feature_ratios.get("vram") is not None and feature_ratios["vram"] == bottleneck:
        bottleneck_label = "VRAM"
    elif feature_ratios.get("ram") is not None and feature_ratios["ram"] == bottleneck:
        bottleneck_label = "RAM"

    return {
        "model": "AI Predictor v3 (benchmark-backed)",
        "confidence": confidence,
        "score": round(score, 2),
        "score_min": round(score_min, 2),
        "score_rec": round(score_rec, 2),
        "bottleneck": bottleneck_label,
        "note": note,
        "metrics": {
            "one_percent_low": one_percent_low,
            "render_latency_ms": f"~{round(1000 / max(25.0, base_high), 1)}-{round(1000 / max(12.0, base_low), 1)} ms",
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