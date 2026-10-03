"""Find installed applications: aliases, App Paths registry, PATH, Start Menu shortcuts."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import IS_WINDOWS

# Common names -> executable / shell target.
ALIASES: dict[str, str] = {
    "chrome": "chrome.exe", "google chrome": "chrome.exe", "edge": "msedge.exe", "microsoft edge": "msedge.exe",
    "firefox": "firefox.exe", "notepad": "notepad.exe", "calculator": "calc.exe", "calc": "calc.exe",
    "paint": "mspaint.exe", "explorer": "explorer.exe", "file explorer": "explorer.exe", "files": "explorer.exe",
    "terminal": "wt.exe", "windows terminal": "wt.exe", "cmd": "cmd.exe", "command prompt": "cmd.exe",
    "powershell": "powershell.exe", "task manager": "taskmgr.exe", "control panel": "control.exe",
    "settings": "ms-settings:", "vscode": "code", "vs code": "code", "visual studio code": "code", "code": "code",
    "word": "winword.exe", "excel": "excel.exe", "powerpoint": "powerpnt.exe", "outlook": "outlook.exe",
    "teams": "ms-teams:", "snipping tool": "snippingtool.exe", "docker": "Docker Desktop", "docker desktop": "Docker Desktop",
    "spotify": "spotify:", "whatsapp": "whatsapp:", "store": "ms-windows-store:",
}


@dataclass
class AppTarget:
    name: str
    target: str          # path to exe/lnk or a URI
    source: str          # alias | app-paths | path | start-menu | uri

    def to_dict(self) -> dict:
        return {"name": self.name, "target": self.target, "source": self.source}


def _app_paths(exe: str) -> str | None:
    if not IS_WINDOWS:
        return None
    import winreg
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as k:
                val, _ = winreg.QueryValueEx(k, "")
                val = os.path.expandvars(val.strip('"'))
                if os.path.isfile(val):
                    return val
        except OSError:
            continue
    return None


def start_menu_dirs() -> list[Path]:
    dirs = []
    for base in (os.environ.get("ProgramData"), os.environ.get("APPDATA")):
        if base:
            dirs.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return [d for d in dirs if d.is_dir()]


def list_start_menu_apps() -> list[AppTarget]:
    apps, seen = [], set()
    for d in start_menu_dirs():
        for lnk in d.rglob("*.lnk"):
            name = lnk.stem
            low = name.lower()
            if low in seen or any(w in low for w in ("uninstall", "readme", "help", "documentation", "release notes")):
                continue
            seen.add(low)
            apps.append(AppTarget(name, str(lnk), "start-menu"))
    return sorted(apps, key=lambda a: a.name.lower())


def resolve_app(name: str) -> AppTarget | None:
    q = name.strip().lower().removesuffix(".exe")
    if not q:
        return None
    target = ALIASES.get(q, q)
    if target.endswith(":"):
        return AppTarget(name, target, "uri")
    exe = target if target.endswith(".exe") else f"{target}.exe"
    if hit := _app_paths(exe):
        return AppTarget(name, hit, "app-paths")
    for candidate in (exe, target):
        if hit := shutil.which(candidate):
            if hit.lower().endswith((".exe", ".cmd", ".bat")):
                return AppTarget(name, hit, "path")
    # Start Menu: exact name, then prefix, then contains.
    apps = list_start_menu_apps()
    tq = target.lower().removesuffix(".exe")
    for pred in (lambda a: a.name.lower() == tq, lambda a: a.name.lower() == q,
                 lambda a: a.name.lower().startswith(q), lambda a: q in a.name.lower()):
        matches = [a for a in apps if pred(a)]
        if matches:
            return min(matches, key=lambda a: len(a.name))
    return None
