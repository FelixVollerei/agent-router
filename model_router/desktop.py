"""Double-click launcher: reuse the exact authenticated localhost instance, or start one."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import webbrowser


def app_dir():
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share")) / "EngineeringModelRouter"


def open_existing(state: Path):
    try:
        session = json.loads((state / "session.json").read_text(encoding="utf-8"))
        port = int(session["port"])
        if not 1024 <= port <= 65535:
            return False
        url = f"http://127.0.0.1:{port}"
        with urllib.request.urlopen(url + "/health", timeout=1) as response:
            health = json.load(response)
        if health.get("instance") != session["instance"] or health.get("app") != "engineering-model-router" or health.get("ui_version") != 3:
            return False
        webbrowser.open(url + "/#" + session["token"])
        return True
    except (OSError, ValueError, KeyError):
        return False


def main():
    state = app_dir()
    state.mkdir(parents=True, exist_ok=True)
    mutex = None
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        mutex = kernel.CreateMutexW(None, True, "Local\\EngineeringModelRouterDesktop-v3")
        if not mutex:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:
            for _ in range(20):
                if open_existing(state):
                    return
                time.sleep(.25)
            return  # Existing owner is still starting; never create a second runner.
    if open_existing(state):
        return
    from .webapp import serve
    root = Path(__file__).resolve().parent.parent
    try:
        serve(root / "router.example.toml", state, open_browser=True)
    except Exception as exc:
        (state / "startup-error.txt").write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, f"启动失败：{exc}\n详情：{state / 'startup-error.txt'}", "Model Router", 0x10)
        raise


if __name__ == "__main__":
    main()
