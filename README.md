# SteamScout

Real-time floating overlay that reads **exactly which Steam page you're on**
and tells you instantly whether your PC can run that game.

SteamScout runs quietly in the system tray — just like Discord.  
When Steam opens, the overlay activates automatically.  
When Steam closes, it hides back to the tray.

---

## Installation

### Option 1 — Installer (recommended)

1. Download **SteamScoutSetup.exe** from the releases page
2. Run the installer
3. SteamScout starts in the system tray — done!

### Option 2 — Portable EXE

1. Download **SteamScout.exe**
2. Place it anywhere and double-click to run

### Option 3 — From source (developers)

1. Install **Python 3.9+** from https://python.org (tick "Add Python to PATH")
2. `pip install -r Requirements.txt`
3. `python SteamScout.pyw`

You can also run `python Overlay.py` and `python Backend.py` separately
for development.

---

## How it works

Steam runs its UI inside a Chromium browser (CEF). When launched with
`-remote-debugging-port=8080`, that browser exposes a local JSON endpoint
listing every open tab and its **full URL**. The checker reads that URL every
1.5 seconds, extracts the AppID and page section, then queries the Steam Store
API for that game's system requirements.

This means it correctly detects:

| Steam page you're on           | Overlay shows                     |
|-------------------------------|-----------------------------------|
| `store.steampowered.com/app/730`  | Compatibility for CS2 (Store page)  |
| `.../app/730/library`         | Compatibility for CS2 (Library)     |
| `.../app/730/community`       | Compatibility for CS2 (Community)   |
| `.../app/730/news`            | Compatibility for CS2 (News)        |
| Any non-game Steam page       | Overlay sleeps                      |

---

## Requirements

- **Windows 10 or 11**
- **Steam** installed
- Internet connection

> Python is only needed when running from source.

---

## How to use

1. **System tray** — Right-click the SteamScout icon in your taskbar for options:
   - *Show Overlay* — bring the overlay window back (or double-click the icon)
   - *Start with Windows* — launch SteamScout at boot
   - *Quit SteamScout* — exit completely

   If Steam isn't running when you start SteamScout, you'll get a notification
   letting you know it's waiting in the background.

2. **Overlay** — Appears automatically when Steam is running.
   Browse game pages in Steam and the overlay updates in real-time.
   Click **✕** to hide it back to the tray (the app keeps running).

3. **Settings** — Click the **⚙** button in the overlay for theme, font,
   opacity, and other options.

---

## Overlay controls

- **Drag** anywhere to reposition
- **✕** to hide to system tray
- **⚙** to open settings

---

## Building from source

To create a standalone `SteamScout.exe`:

```
pip install pyinstaller pillow
python build.py
```

The EXE is written to `dist/SteamScout.exe`.

To create a Windows installer, install
[Inno Setup](https://jrsoftware.org/isinfo.php) and compile `installer.iss`.

---

## Status icons

| Icon | Meaning |
|------|---------|
| ✓   | Meets requirement |
| ✗   | Below requirement |
| ℹ   | Informational (shown side-by-side for manual check) |
| ⚠   | Warning |

RAM and OS version are checked precisely. GPU and CPU are shown
side-by-side because comparing GPU model strings reliably requires a
benchmark database — you can judge at a glance whether e.g. your RTX 3070
beats the required GTX 970.

---

## Troubleshooting

**"Connecting to backend…" never goes away**  
→ Make sure nothing else is using port 8765  
→ Check `%APPDATA%\SteamScout\backend.log` for errors

**Overlay always shows "Watching Steam — navigate to a game page…"**  
→ SteamScout will restart Steam once with debug flags enabled — this is normal  
→ If it persists, fully close Steam, then re-launch SteamScout  
→ Confirm the debug port works: open a browser and visit `http://localhost:8080/json`  

**Steam took a long time to restart**  
→ Completely normal — Steam downloads updates on restart sometimes  

---

## File structure

```
SteamScout/
├── SteamScout.pyw     — Main entry point (system tray app)
├── Backend.py         — CEF URL reader, spec collector, Steam API, WebSocket server
├── Overlay.py         — Floating tkinter overlay
├── SteamScoutIcon.png — Application icon
├── build.py           — PyInstaller build script
├── installer.iss      — Inno Setup installer script
├── Requirements.txt   — Python packages
└── README.md
```

User settings are stored in `%APPDATA%\SteamScout\overlay_settings.json`
(not in the install directory).