"""
Launcher for the auto-launch monitor.
Runs the monitor as a hidden background process.
"""

import os
import subprocess
import sys

if __name__ == "__main__":
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        monitor_script = os.path.join(script_dir, "auto_launch_monitor.py")
        
        # Start monitor as hidden background process
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        
        subprocess.Popen(
            [sys.executable, monitor_script],
            startupinfo=startupinfo,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        )
    except Exception:
        pass
