"""
Auto-launch monitor for SteamScout.
Watches for Steam process and launches SteamScout when Steam opens (if enabled).
Runs in background as a daemon.
"""

import os
import json
import time
import subprocess
import psutil

SETTINGS_PATH = os.path.join(os.path.dirname(__file__), "overlay_settings.json")
LOG_PATH = os.path.join(os.path.dirname(__file__), "monitor_debug.log")


def log_msg(msg: str):
    """Write debug message to log file."""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def load_auto_launch_setting() -> bool:
    """Check if auto-launch on Steam start is enabled."""
    try:
        if os.path.exists(SETTINGS_PATH):
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                settings = json.load(f)
                enabled = bool(settings.get("auto_launch", False))
                log_msg(f"Settings loaded: auto_launch={enabled}")
                return enabled
    except Exception as e:
        log_msg(f"Error loading settings: {e}")
    return False


def is_steam_running() -> bool:
    """Check if Steam process is currently running."""
    try:
        for proc in psutil.process_iter(['name']):
            if proc.info['name'].lower() == 'steam.exe':
                return True
    except Exception as e:
        log_msg(f"Error checking Steam: {e}")
    return False


def is_steamscout_running() -> bool:
    """Check if SteamScout overlay is already running."""
    try:
        for proc in psutil.process_iter(['name', 'cmdline']):
            try:
                name = proc.info.get('name', '').lower()
                cmdline = proc.info.get('cmdline', [])
                # Check for overlay.py or SteamScoutOverlay.exe
                if 'overlay.py' in ' '.join(cmdline).lower() or 'steamscoutoverlay.exe' in name:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception as e:
        log_msg(f"Error checking SteamScout: {e}")
    return False


def launch_steamscout():
    """Launch SteamScout."""
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        
        # Try dist version first
        dist_launcher = os.path.join(script_dir, "dist", "SteamScout.bat")
        if os.path.exists(dist_launcher):
            log_msg(f"Launching: {dist_launcher}")
            subprocess.Popen(
                dist_launcher,
                shell=True,
                cwd=script_dir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            log_msg("SteamScout launched successfully")
            return True
        
        # Fallback to run.bat
        run_launcher = os.path.join(script_dir, "run.bat")
        if os.path.exists(run_launcher):
            log_msg(f"Launching: {run_launcher}")
            subprocess.Popen(
                run_launcher,
                shell=True,
                cwd=script_dir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            log_msg("SteamScout launched successfully")
            return True
        
        log_msg(f"ERROR: Neither dist launcher nor run.bat found in {script_dir}")
    except Exception as e:
        log_msg(f"ERROR launching SteamScout: {e}")
    return False


def main():
    """Monitor for Steam and launch SteamScout when Steam opens."""
    log_msg("Monitor started")
    steam_was_running = False
    
    while True:
        try:
            steam_running = is_steam_running()
            
            # Steam just started
            if steam_running and not steam_was_running:
                log_msg("Steam process detected!")
                steam_was_running = True
                time.sleep(2)  # Give Steam a moment to fully initialize
                
                if load_auto_launch_setting():
                    if not is_steamscout_running():
                        log_msg("Launching SteamScout...")
                        launch_steamscout()
                    else:
                        log_msg("SteamScout already running, skipping launch")
                else:
                    log_msg("Auto-launch disabled in settings, skipping launch")
            
            # Steam just stopped
            elif not steam_running and steam_was_running:
                log_msg("Steam process ended")
                steam_was_running = False
            
            time.sleep(2)  # Check every 2 seconds
            
        except Exception as e:
            log_msg(f"Monitor error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
