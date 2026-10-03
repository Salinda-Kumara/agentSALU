"""SALU entry point.

  python -m salu              desktop interface (local server + app window)
  python -m salu --no-window  server only; open the printed URL yourself
  python -m salu chat         terminal chat
  python -m salu check        run startup checks and exit
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path


def _find_app_browser() -> str | None:
    """Edge/Chrome can open a URL as a chrome-less app window (desktop feel, no extra deps)."""
    candidates = []
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            candidates += [Path(base) / "Microsoft/Edge/Application/msedge.exe",
                           Path(base) / "Google/Chrome/Application/chrome.exe"]
    for c in candidates:
        if c.is_file():
            return str(c)
    return shutil.which("msedge") or shutil.which("chrome")


def open_window(url: str, profile_dir: Path) -> None:
    browser = _find_app_browser()
    if browser:
        profile_dir.mkdir(parents=True, exist_ok=True)
        subprocess.Popen([browser, f"--app={url}", "--window-size=1320,880", f"--user-data-dir={profile_dir}",
                          "--no-first-run", "--no-default-browser-check"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        webbrowser.open(url)


def run_desktop(open_ui: bool) -> int:
    import uvicorn

    from .app import SaluApp
    from .web.server import create_web_app

    salu = SaluApp()
    s = salu.settings
    token = secrets.token_urlsafe(24)
    app = create_web_app(salu, token)
    url = f"http://127.0.0.1:{s.server.port}/?t={token}"

    if open_ui and s.server.open_window:
        def later():
            time.sleep(1.2)
            open_window(url, s.path("data/ui-profile"))
        threading.Thread(target=later, daemon=True).start()

    # The URL contains this launch's access token: printed to the console only, never logged.
    print(f"\n  SALU interface: {url}\n  (Ctrl+C to stop)\n", flush=True)
    uvicorn.run(app, host=s.server.host, port=s.server.port, log_level="warning", access_log=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        os.system("")  # enable ANSI colours in the Windows console
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
    p = argparse.ArgumentParser(prog="salu", description="SALU personal AI assistant")
    p.add_argument("command", nargs="?", default="ui", choices=["ui", "chat", "check"])
    p.add_argument("--no-window", action="store_true", help="don't open the desktop window")
    args = p.parse_args(argv)

    from .config import ConfigError
    try:
        if args.command == "ui":
            return run_desktop(open_ui=not args.no_window)
        from .app import SaluApp
        from .cli import run_chat, run_check
        salu = SaluApp(console_log=False)
        return asyncio.run(run_chat(salu) if args.command == "chat" else run_check(salu))
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
