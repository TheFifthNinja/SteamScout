<p align="center">
  <img src="SteamScoutIcon.png" alt="SteamScout" width="110">
</p>

<h1 align="center">SteamScout</h1>

<p align="center">
  <b>Floating overlay that tells you if your PC can run any Steam game — instantly.</b><br>
  Reads exactly which game page you're on and checks your specs in real time.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Platform-Windows%2010%2F11-0078D4?logo=windows&logoColor=white" />
  <img src="https://img.shields.io/badge/Python-3.9+-3776AB?logo=python&logoColor=white" />
  <img src="https://img.shields.io/badge/UI-Edge%20WebView2-0078D4?logo=microsoftedge&logoColor=white" />
  <img src="https://img.shields.io/badge/Steam-Compatible-1b2838?logo=steam&logoColor=white" />
  <img src="https://img.shields.io/badge/Tests-113%20passing-brightgreen?logo=pytest&logoColor=white" />
  <img src="https://img.shields.io/badge/License-MIT-green" />
</p>

---

## Download

> **Just want to use it?** No Python needed.

1. Go to the [**Releases page**](../../releases/latest)
2. Download `SteamScout.exe`
3. Double-click it — that's it

Game search connects to the cloud automatically. No configuration required.

> **Windows Defender warning?** This is a false positive common with PyInstaller apps.  
> Click **More info → Run anyway**, or allow it in Windows Security → Protection history.

---

## What it does

SteamScout sits in your system tray and watches Steam in the background. Browse to any game page and the overlay tells you instantly whether your PC can run it.

- **Compatibility check** — Compares your RAM, CPU, GPU, DirectX, OS, and storage against the game's minimum and recommended specs
- **FPS estimator** — Predicts frame rates at Low / Medium / High settings using benchmark-backed hardware scores
- **Upgrade suggestions** — If you fail a check, it shows you exactly what to upgrade with a search link
- **Game search** — Search the entire Steam catalog (~100k games) by name, genre, or tag without opening Steam
- **Real-time detection** — Reads Steam's internal debug port to know exactly which game you're viewing, every 1.5 seconds
- **System tray app** — Sits quietly in the tray, auto-shows when Steam opens, auto-hides when Steam closes
- **Dark & light themes** — Accent color, font picker, opacity slider, and font scaling in settings

---

## Running from source

### Prerequisites

| Requirement | Where to get it |
|---|---|
| Python 3.9 or newer | [python.org](https://python.org) — tick **"Add Python to PATH"** during install |
| Steam | Already installed if you're here |
| Internet connection | For game data and search |

### Steps

**1. Clone the repo**

```bash
git clone https://github.com/your-username/SteamScout.git
cd SteamScout
```

**2. Install dependencies**

```bash
pip install -r Requirements.txt
```

**3. Run**

```bash
python SteamScout.pyw
```

The app starts in the system tray. Look for the SteamScout icon near the clock.  
Open Steam and browse to any game page — the overlay will appear automatically.

---

## Building a standalone EXE

To create a single portable `.exe` you can share without requiring Python:

```bash
pip install pyinstaller pillow
python build.py
```

Output: `dist\SteamScout.exe`

---

## How to use it

### System tray

Right-click the SteamScout icon in your taskbar tray:

| Option | What it does |
|---|---|
| Show Overlay | Bring the overlay back (or just double-click the icon) |
| Start with Windows | Launch SteamScout automatically at boot |
| Quit SteamScout | Exit completely |

### Overlay controls

| Control | Action |
|---|---|
| Drag title bar | Move the overlay anywhere on screen |
| 🔍 | Open the game search panel |
| ⚙ | Open settings (theme, font, opacity) |
| ✕ | Hide to system tray |
| Drag any edge or corner | Resize the overlay |

### Compatibility results

| Icon | Meaning |
|---|---|
| ✓ | Meets the requirement |
| ✗ | Below the requirement |
| ⚠ | Could not verify |
| ℹ | Informational |

### Game search

Click **🔍** in the title bar, type any game name, then click **CHECK** on a result to run a full compatibility check — same engine as the real-time overlay.

---

## How it works

Steam runs its UI inside a Chromium browser (CEF). When launched with `-remote-debugging-port=8080`, that browser exposes a local JSON endpoint listing every open tab and its URL. SteamScout reads that URL every 1.5 seconds, extracts the App ID, then calls the Steam Store API for that game's system requirements.

| Steam page you're on | Overlay shows |
|---|---|
| `store.steampowered.com/app/730` | Compatibility check for CS2 |
| `.../app/730/library` | Same, from the library page |
| `.../app/730/news` | Same, from the news page |
| Any non-game Steam page | Overlay sleeps |

### Game search (Elasticsearch)

When SteamScout first runs, it indexes the Steam catalog in the background:

1. Fetches ~100k app names from Steam's `ISteamApps/GetAppList` API
2. Enriches each entry with genre, tags, ratings, and pricing from SteamSpy
3. Slowly caches full system requirements so future CHECK clicks are instant

The app is fully usable while this runs — it just means some games may show less detail in search until indexing completes.

---

## Troubleshooting

**"Connecting to backend…" never goes away**
- Something else is using port 8765
- Check `%APPDATA%\SteamScout\backend.log` for errors

**Overlay always shows "Watching Steam — navigate to a game page…"**
- SteamScout will restart Steam once with debug flags — this is normal on first run
- If it keeps happening: fully close Steam, then re-launch SteamScout
- To verify the debug port is working, open a browser and visit `http://localhost:8080/json`

**Game search shows "Elasticsearch Not Running"**
- Check your internet connection — game search requires cloud access
- If the issue persists, check `%APPDATA%\SteamScout\backend.log`

**Search results have no genre chips or tags**
- The catalog enrichment job is still running in the background
- Wait a few minutes and reopen the search panel

**Windows Defender / SmartScreen blocks the EXE**
- Common false positive with PyInstaller-built apps (no paid code-signing certificate)
- Click **More info → Run anyway**
- Or allow it in **Windows Security → Virus & threat protection → Protection history**

---

## Project structure

```
SteamScout/
├── SteamScout.pyw        Main entry point — system tray app
├── Backend.py            CEF reader, hardware spec collector, Steam API, WebSocket server
├── Overlay.py            Floating overlay window + search API bridge
├── overlay_ui.html       Overlay front-end (HTML / CSS / JS)
├── search/
│   ├── es_client.py      Elasticsearch wrapper
│   ├── service.py        search() and check_game() logic
│   └── catalog.py        Background catalog indexer
├── scripts/
│   └── update_catalog.py GitHub Actions catalog update job
├── tests/
│   └── test_backend.py   113 unit tests
├── build.py              PyInstaller build script
├── Requirements.txt      Python dependencies
└── SteamScoutIcon.png    App icon
```

**User data locations:**

| File | Path |
|---|---|
| Settings | `%APPDATA%\SteamScout\overlay_settings.json` |
| Log | `%APPDATA%\SteamScout\backend.log` |

---

## Running tests

```bash
pip install pytest
python -m pytest tests/ -v
```

113 tests cover the compatibility engine: hardware name normalization, benchmark lookups, GPU/CPU scoring, FPS estimation, DirectX and storage parsing, and min-only requirement edge cases.
