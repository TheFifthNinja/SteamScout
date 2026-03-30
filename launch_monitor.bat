@echo off
REM Auto-launch monitor launcher - runs SteamScout when Steam opens
REM Called by Windows Registry at startup

cd /d "%~dp0"
python auto_launch_monitor.py
