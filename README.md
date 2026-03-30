# Steam Compatibility Checker

Real-time floating overlay that reads **exactly which Steam page you're on**
and tells you instantly whether your PC can run that game.

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
- **Python 3.9+** — https://python.org  
  Tick **"Add Python to PATH"** during install
- **Steam** installed
- Internet connection

---

## Quick start

1. Put all files in the same folder
2. Double-click **`run.bat`**

`run.bat` will:
1. Install Python dependencies
2. Find your Steam install path automatically
3. Restart Steam with the debug flag enabled (Steam restarts once — this is normal)
4. Launch the backend and the overlay

After that, just browse Steam normally. The overlay updates automatically.

---

## Overlay controls

- **Drag** anywhere to reposition
- **✕** to close (also stops the backend)

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
→ Check the minimised backend terminal for errors  
→ Make sure nothing else is using port 8765  

**Overlay always shows "Watching Steam — navigate to a game page…"**  
→ Steam must have been launched via `run.bat` (with the debug flag)  
→ If you launched Steam manually before running, close it and run `run.bat` again  
→ Confirm the debug port works: open a browser and visit `http://localhost:8080/json`  

**Steam took a long time to restart**  
→ Completely normal — Steam downloads updates on restart sometimes  

---

## File structure

```
steam-compat-checker/
├── backend.py       — CEF URL reader, spec collector, Steam API, WebSocket server
├── overlay.py       — Floating tkinter overlay
├── requirements.txt — Python packages
├── run.bat          — One-click launcher
└── README.md
```