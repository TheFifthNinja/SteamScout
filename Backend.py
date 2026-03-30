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

import psutil
import requests
import websockets
from websockets.server import serve

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
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
            capture_output=True, text=True, timeout=6
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
        subprocess.run(["dxdiag", "/whql:off", "/t", out_file], capture_output=True, text=True, timeout=20)
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
    specs = {
        "cpu":     get_cpu_name(),
        "gpu":     get_gpu_name(),
        "ram_gb":  get_ram_gb(),
        "disk_free_gb": get_free_disk_gb(),
        "vram_gb": get_vram_gb(),
        "os":      get_windows_version(),
        "directx": get_directx_version(),
    }
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
        result = {
            "app_id":       app_id,
            "name":         d.get("name", "Unknown"),
            "header_image": d.get("header_image", ""),
            "minimum":      _parse_reqs(d.get("pc_requirements", {}).get("minimum", "")),
            "recommended":  _parse_reqs(d.get("pc_requirements", {}).get("recommended", "")),
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


def _gpu_score(name: str) -> Optional[float]:
    s = (name or "").lower()
    bonus = 0.0
    if "ti" in s:
        bonus += 4.0
    if "super" in s:
        bonus += 3.0
    if "xt" in s:
        bonus += 3.0

    n = re.search(r"(rtx|gtx|gt|mx)\s*(\d{3,4})", s)
    if n:
        fam = {"gt": 10, "mx": 15, "gtx": 30, "rtx": 45}.get(n.group(1), 0)
        model = int(n.group(2))
        return fam + model / 10.0 + bonus

    a = re.search(r"\brx\s*(\d{3,4})", s)
    if a:
        return 35 + int(a.group(1)) / 10.0 + bonus

    arc = re.search(r"\barc\s*a\s*(\d{3,4})", s)
    if arc:
        return 38 + int(arc.group(1)) / 10.0 + bonus

    return None


def _cpu_score(name: str) -> Optional[float]:
    s = (name or "").lower()

    i = re.search(r"\bi([3579])[-\s]?(\d{4,5})", s)
    if i:
        tier = int(i.group(1))
        model = i.group(2)
        gen = int(model[:2]) if len(model) == 5 else int(model[0])
        sku = int(model[-2:])
        return tier * 100 + gen * 10 + sku / 100.0

    ry = re.search(r"ryzen\s*([3579])\s*(\d{4,5})", s)
    if ry:
        tier = int(ry.group(1))
        model = ry.group(2)
        gen = int(model[0])
        sku = int(model[-2:])
        return tier * 100 + gen * 12 + sku / 100.0

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


def _ratio(measured: Optional[float], required: Optional[float], cap: float = 2.0) -> Optional[float]:
    if measured is None or required is None or required <= 0:
        return None
    return max(0.0, min(cap, measured / required))


def _score_to_band(score: float, bottleneck: float) -> tuple:
    # Use bottleneck to temper optimistic averages.
    effective = 0.65 * score + 0.35 * bottleneck

    if effective < 0.85:
        return (18, 32, "System is below one or more key requirements.")
    if effective < 1.0:
        return (28, 45, "Borderline setup; gameplay depends on map/scene complexity.")
    if effective < 1.2:
        return (40, 65, "Playable on low/medium with occasional drops.")
    if effective < 1.45:
        return (55, 85, "Good overall fit for this game's requirement profile.")
    return (70, 120, "Strong hardware headroom relative to listed requirements.")


def ai_predict_performance(pc: dict, reqs: dict, compat: dict) -> dict:
    """
    AI-style heuristic predictor (no external API required).
    Uses component ratios and pass/fail outcomes to estimate playability/FPS.
    """
    min_r = reqs.get("minimum", {})
    rec_r = reqs.get("recommended", {})

    pc_cpu = _cpu_score(pc.get("cpu", ""))
    pc_gpu = _gpu_score(pc.get("gpu", ""))
    pc_ram = pc.get("ram_gb")
    pc_vram = pc.get("vram_gb")
    pc_dx = _directx_major(pc.get("directx", ""))

    min_cpu = _required_score(min_r.get("cpu", ""), "cpu") if "cpu" in min_r else None
    min_gpu = _required_score(min_r.get("gpu", ""), "gpu") if "gpu" in min_r else None
    min_ram = _size_gb(min_r.get("ram", ""), default_unit="GB") if "ram" in min_r else None
    min_storage = _size_gb(min_r.get("storage", ""), default_unit="GB") if "storage" in min_r else None
    min_dx = _directx_major(min_r.get("directx", "")) if "directx" in min_r else None

    rec_cpu = _required_score(rec_r.get("cpu", ""), "cpu") if "cpu" in rec_r else None
    rec_gpu = _required_score(rec_r.get("gpu", ""), "gpu") if "gpu" in rec_r else None
    rec_ram = _size_gb(rec_r.get("ram", ""), default_unit="GB") if "ram" in rec_r else None

    # Weighted feature ratios focused on frame-time-critical components.
    feature_weights = {
        "gpu": 0.40,
        "cpu": 0.28,
        "ram": 0.16,
        "directx": 0.08,
        "vram": 0.08,
    }

    feature_ratios = {}
    feature_ratios["gpu"] = _ratio(pc_gpu, min_gpu)
    feature_ratios["cpu"] = _ratio(pc_cpu, min_cpu)
    feature_ratios["ram"] = _ratio(pc_ram, min_ram)

    if min_dx is not None and pc_dx is not None:
        feature_ratios["directx"] = 1.1 if pc_dx >= min_dx else 0.6
    else:
        feature_ratios["directx"] = None

    # Approximate VRAM floor from minimum GPU string and explicit requirement text if present.
    req_vram = None
    if min_r.get("gpu"):
        if re.search(r"\b(2|3|4|6|8|10|12|16)\s*gb\b", min_r["gpu"], re.IGNORECASE):
            req_vram = _size_gb(min_r["gpu"], default_unit="GB")
    if req_vram is None and min_gpu is not None:
        # Very rough mapping for older listed minimum GPUs.
        req_vram = 2.0 if min_gpu < 420 else 4.0 if min_gpu < 520 else 6.0
    feature_ratios["vram"] = _ratio(pc_vram, req_vram) if pc_vram is not None and req_vram is not None else None

    weighted_sum = 0.0
    used_weight = 0.0
    ratios_used = []
    for k, w in feature_weights.items():
        r = feature_ratios.get(k)
        if r is None:
            continue
        weighted_sum += r * w
        used_weight += w
        ratios_used.append(r)

    if used_weight <= 0.0:
        return estimate_performance(compat)

    score_min = weighted_sum / used_weight

    # Recommended-tier headroom signal.
    rec_signals = []
    for mine, need in ((pc_gpu, rec_gpu), (pc_cpu, rec_cpu), (pc_ram, rec_ram)):
        r = _ratio(mine, need)
        if r is not None:
            rec_signals.append(r)
    score_rec = sum(rec_signals) / len(rec_signals) if rec_signals else score_min

    # Blend minimum viability with recommended headroom.
    score = 0.72 * score_min + 0.28 * score_rec
    bottleneck = min(ratios_used) if ratios_used else score

    base_low, base_high, note = _score_to_band(score, bottleneck)
    min_ok = compat.get("overall_min")
    rec_ok = compat.get("overall_rec")
    if min_ok == "fail":
        base_low, base_high = min(base_low, 22), min(base_high, 36)
    elif min_ok == "pass" and rec_ok == "fail":
        base_low, base_high = min(base_low, 42), min(base_high, 58)

    low = f"{max(15, int(base_low + 6))}-{max(24, int(base_high + 10))} FPS"
    medium = f"{max(12, int(base_low - 2))}-{max(22, int(base_high + 2))} FPS"
    high = f"{max(10, int(base_low - 12))}-{max(18, int(base_high - 8))} FPS"

    # Additional metric users can surface later in overlay.
    one_percent_low = f"{max(8, int(base_low * 0.65))}-{max(15, int(base_low * 0.85))} FPS"

    parsed_features = len(ratios_used)
    confidence = "medium" if parsed_features >= 4 else "low"
    if parsed_features >= 5 and score_rec is not None:
        confidence = "high"

    bottleneck_label = "GPU" if feature_ratios.get("gpu") == bottleneck else "CPU" if feature_ratios.get("cpu") == bottleneck else "RAM/VRAM"

    return {
        "model": "AI Predictor v2 (weighted+bottleneck)",
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
                else:
                    status = "warn"
                t[key] = {
                    "status": status,
                    "yours": label,
                    "required": r[key],
                }

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

    for tier in ("minimum", "recommended"):
        statuses = [v["status"] for v in results.get(tier, {}).values()]
        measured = [s for s in statuses if s in {"pass", "fail"}]
        if not measured:
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


async def main():
    global pc_specs
    loop     = asyncio.get_event_loop()
    pc_specs = await loop.run_in_executor(None, collect_pc_specs)
    log.info(f"WebSocket server starting on ws://{WS_HOST}:{WS_PORT}")
    async with serve(ws_handler, WS_HOST, WS_PORT):
        await watcher_loop()


if __name__ == "__main__":
    asyncio.run(main())