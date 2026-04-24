# SteamScout

<p align="center">
  <img src="SteamScoutIcon.png" alt="SteamScout" width="128">
</p>

![Python](https://img.shields.io/badge/Python-3.9+-3776AB?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Windows%2010%2F11-0078D4?logo=windows&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)
![WebView2](https://img.shields.io/badge/UI-Edge%20WebView2-0078D4?logo=microsoftedge&logoColor=white)
![Steam](https://img.shields.io/badge/Steam-Compatible-1b2838?logo=steam&logoColor=white)
![Tests](https://img.shields.io/badge/Tests-113%20passing-brightgreen?logo=pytest&logoColor=white)

Real-time floating overlay that reads **exactly which Steam page you're on**
and tells you instantly whether your PC can run that game — complete with
**benchmark-backed GPU & CPU scoring**, **estimated FPS at Low / Medium / High**,
and **upgrade suggestions** with price links.

SteamScout also includes a **game search** powered by Elasticsearch: search
any game across the entire Steam catalog by name or genre and check compatibility
on demand — without having to browse to it in Steam.

SteamScout runs quietly in the system tray — just like Discord.  
When Steam opens, the overlay activates automatically.  
When Steam closes, it hides back to the tray.

---

## Features

- **Automatic detection** — Reads Steam's CEF debug port to identify which game page you're browsing, in real time
- **Full compatibility check** — Compares your RAM, OS, CPU, GPU, DirectX, and storage against every game's requirements
- **Benchmark-backed scoring** — 250+ GPU and 130+ CPU entries with relative performance scores for accurate comparisons
- **FPS estimator** — Predicts frame rates at Low / Medium / High presets using a min+recommended scoring blend
- **Game search** — Search the entire Steam catalog (~100k games) by name or genre; see compatibility at a glance
- **Min-only awareness** — Games that only list minimum requirements are handled gracefully with adjusted estimates
- **Upgrade suggestions** — Failed checks show one-click links to find compatible parts online
- **Lightweight overlay** — Runs in a WebView2 window; uses only CSS `transform`/`opacity` animations for near-zero GPU cost
- **System tray app** — Sits quietly in the tray; auto-shows when Steam opens, auto-hides when Steam closes
- **Dark & light themes** — Custom accent colors, font picker, opacity slider, and font scaling
- **Resizable & draggable** — Native Win32 resize handles and drag, works on any monitor

---

## For end users (download)

1. Download **SteamScout.exe** from the [releases page](../../releases/latest)
2. Double-click `SteamScout.exe`

Game search connects automatically to the cloud — no setup required.

---

## Running from source

### 1. Prerequisites

- **Python 3.9+** — https://python.org (tick "Add Python to PATH")
- **Steam** installed
- Internet connection

### 2. Install Python dependencies

```
pip install -r Requirements.txt
```

### 3. Run

```
python SteamScout.pyw
```

The app starts in the system tray. On first launch with Elasticsearch running,
it automatically indexes the Steam catalog in the background (this takes a few
minutes — the app is fully usable while it runs).

---

## Building a standalone EXE

```
pip install pyinstaller pillow
python build.py
```

Output: `dist\SteamScout.exe` — a single portable executable. No Python required
to run it on another machine. Game search connects automatically to the cloud.

---

## How it works

Steam runs its UI inside a Chromium browser (CEF). When launched with
`-remote-debugging-port=8080`, that browser exposes a local JSON endpoint
listing every open tab and its **full URL**. The checker reads that URL every
1.5 seconds, extracts the AppID and page section, then queries the Steam Store
API for that game's system requirements.

This means it correctly detects:

| Steam page you're on              | Overlay shows                       |
|-----------------------------------|-------------------------------------|
| `store.steampowered.com/app/730`  | Compatibility for CS2 (Store page)  |
| `.../app/730/library`             | Compatibility for CS2 (Library)     |
| `.../app/730/community`           | Compatibility for CS2 (Community)   |
| `.../app/730/news`                | Compatibility for CS2 (News)        |
| Any non-game Steam page           | Overlay sleeps                      |

### Game search (Elasticsearch)

On startup, two background jobs run:

1. **App list index** — calls Steam's `ISteamApps/GetAppList` (one request,
   ~100k games) and bulk-inserts names + IDs into Elasticsearch.
2. **SteamSpy enrichment** — fetches genre/tag data page by page (~1 req/s)
   and updates each document.
3. **Requirements enrichment** — slowly fetches full `pc_requirements` from
   the Steam Store API for each game (~1.5 s/game) and caches them so future
   `CHECK` clicks are instant.

Clicking a game in the search panel triggers an on-demand compatibility check
using the same engine as the real-time overlay.

---

## How to use

1. **System tray** — Right-click the SteamScout icon in your taskbar:
   - *Show Overlay* — bring the overlay back (or double-click the icon)
   - *Start with Windows* — launch SteamScout at boot
   - *Quit SteamScout* — exit completely

2. **Real-time overlay** — Appears automatically when Steam is running.
   Browse game pages in Steam and the overlay updates instantly.

3. **Game search** — Click **🔍** in the overlay title bar. Type any game name,
   optionally filter by genre chips, then click **CHECK** on a result to see
   full compatibility details in the main overlay view.

4. **Settings** — Click **⚙** for theme, font, opacity, and other options.

---

## Overlay controls

| Control | Action |
|---------|--------|
| Drag title bar | Reposition the overlay |
| **🔍** | Open game search |
| **⚙** | Open settings |
| **✕** | Hide to system tray |
| Resize handles | Drag any edge or corner to resize |

---

## Status icons

| Icon | Meaning |
|------|---------|
| ✓   | Meets requirement |
| ✗   | Below requirement |
| ℹ   | Informational |
| ⚠   | Warning / could not verify |

---

## Running tests

```
pip install pytest
python -m pytest tests/ -v
```

All 113 tests cover the compatibility engine: size/DX parsing, HTML requirement
extraction, hardware name normalization, GPU & CPU benchmark lookups, scoring
logic, performance estimation, and min-only edge cases.

---

## Troubleshooting

**"Connecting to backend…" never goes away**  
→ Something else is using port 8765  
→ Check `%APPDATA%\SteamScout\backend.log` for errors

**Overlay always shows "Watching Steam — navigate to a game page…"**  
→ SteamScout will restart Steam once with debug flags — this is normal  
→ If it persists, fully close Steam, then re-launch SteamScout  
→ Confirm the debug port: open a browser and visit `http://localhost:8080/json`

**Game search shows "Elasticsearch Not Running"**  
→ Check your internet connection — game search requires cloud access  
→ If the issue persists, check `%APPDATA%\SteamScout\backend.log`

**Search results have no genre chips**  
→ The catalog enrichment job is still running in the background — wait a few
  minutes and reopen the search panel

**Windows Defender / SmartScreen blocks the EXE**  
→ False positive common with PyInstaller apps (no paid code-signing cert)  
→ Click **"More info"** → **"Run anyway"**  
→ Or allow it in **Windows Security → Virus & threat protection → Protection history**

---

## File structure

```
SteamScout/
├── SteamScout.pyw       — Main entry point (system tray app)
├── Backend.py           — CEF reader, spec collector, Steam API, WebSocket server
├── Overlay.py           — Floating pywebview overlay + search API bridge
├── overlay_ui.html      — Overlay front-end (HTML / CSS / JS)
├── search/
│   ├── es_client.py     — Elasticsearch wrapper (graceful no-op when ES is down)
│   ├── catalog.py       — Background Steam + SteamSpy catalog indexer
│   └── service.py       — search() and check_game() business logic
├── SteamScoutIcon.png   — Application icon
├── build.py             — PyInstaller build script
├── Requirements.txt     — Python packages
├── tests/
│   └── test_backend.py  — 113 unit tests
└── README.md
```

User settings: `%APPDATA%\SteamScout\overlay_settings.json`  
Backend log: `%APPDATA%\SteamScout\backend.log`
