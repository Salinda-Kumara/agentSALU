"""Application and window tools: open apps/files/URLs, list/focus/close windows."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import urlparse

import psutil

from ...security.permissions import Risk
from ...winos import IS_WINDOWS
from ...winos.apps import list_start_menu_apps, resolve_app
from ...winos.windows import close_window, find_windows, focus_window, list_windows
from ..base import ToolResult, salu_tool
from .files import PathDenied, expand_path, resolve_safe

EXECUTABLE_EXT = {".exe", ".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".msi", ".msix", ".appx",
                  ".lnk", ".url", ".reg", ".scr", ".com", ".pif", ".cpl", ".hta", ".jar", ".py", ".pyw"}
# Never force-killed by SALU.
PROTECTED_PROCESSES = {"system", "registry", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
                       "lsass.exe", "svchost.exe", "dwm.exe", "explorer.exe", "fontdrvhost.exe", "memcompression",
                       "msmpeng.exe", "securityhealthservice.exe", "spoolsv.exe", "audiodg.exe"}


def _is_url(s: str) -> bool:
    return urlparse(s).scheme in ("http", "https")


async def _new_windows_after(before: set[int], wait: float = 3.0) -> list[dict]:
    deadline = asyncio.get_running_loop().time() + wait
    while True:
        await asyncio.sleep(0.5)
        new = [w.to_dict() for w in await asyncio.to_thread(list_windows) if w.hwnd not in before]
        if new or asyncio.get_running_loop().time() > deadline:
            return new


def _open_target_risk(args) -> Risk:
    target = args.get("target") or args.get("path") or ""
    if not target or _is_url(target):
        return Risk.LOW
    try:
        p = expand_path(target)
    except (OSError, PathDenied):
        return Risk.HIGH
    if p.suffix.lower() in EXECUTABLE_EXT:
        return Risk.HIGH  # running a program/script file is code execution
    return Risk.LOW


@salu_tool("open_application",
           "Open an installed application (e.g. Chrome, VS Code, Notepad, Excel, Terminal), optionally with a "
           "file, folder or http(s) URL to open in it. Reports whether a new window appeared.",
           risk="low", category="apps", risk_fn=_open_target_risk,
           properties={"name": {"type": "string"},
                       "target": {"type": "string", "description": "Optional file/folder path or http(s) URL"}},
           required=["name"])
async def open_application(args, ctx):
    if not IS_WINDOWS:
        return ToolResult.failure("Only supported on Windows.")
    app = await asyncio.to_thread(resolve_app, args["name"])
    if app is None:
        return ToolResult.failure(f"Couldn't find an installed application called '{args['name']}'. "
                                  "Try list_installed_apps to see what's available.")
    target = args.get("target")
    if target and not _is_url(target):
        try:
            target = str(resolve_safe(target, ctx))
        except PathDenied as e:
            return ToolResult.failure(str(e))
    before = {w.hwnd for w in await asyncio.to_thread(list_windows)}
    try:
        if target:
            await asyncio.to_thread(os.startfile, app.target, "open", f'"{target}"')
        else:
            await asyncio.to_thread(os.startfile, app.target)
    except OSError as e:
        return ToolResult.failure(f"Failed to launch {app.name} ({app.target}): {e}")
    new = await _new_windows_after(before)
    seen = ", ".join(f"{w['title']} ({w['process']})" for w in new[:3])
    verdict = f"New window: {seen}." if new else ("No new window detected yet — the app may already have been "
                                                   "open (reused its window) or is still starting.")
    return ToolResult.success(f"Launched {app.name} via {app.source} ({app.target}). {verdict}",
                              {"app": app.to_dict(), "new_windows": new})


@salu_tool("open_path",
           "Open a file, folder or http(s) URL with its default Windows handler (e.g. a PDF in the PDF viewer, a "
           "folder in Explorer, a URL in the default browser). Opening program/script files is high risk.",
           risk="low", category="apps", risk_fn=_open_target_risk,
           properties={"path": {"type": "string", "description": "File/folder path or http(s) URL"}},
           required=["path"])
async def open_path(args, ctx):
    if not IS_WINDOWS:
        return ToolResult.failure("Only supported on Windows.")
    target = args["path"].strip()
    if not _is_url(target):
        if "://" in target or target.lower().startswith(("file:", "javascript:", "ms-")):
            return ToolResult.failure("Only file paths and http(s) URLs can be opened.")
        try:
            p = resolve_safe(target, ctx)
        except PathDenied as e:
            return ToolResult.failure(str(e))
        if not p.exists():
            return ToolResult.failure(f"Not found: {p}")
        target = str(p)
    try:
        await asyncio.to_thread(os.startfile, target)
    except OSError as e:
        return ToolResult.failure(f"Failed to open {target}: {e}")
    return ToolResult.success(f"Opened {target}.")


@salu_tool("list_installed_apps", "List applications from the Start Menu, optionally filtered by name.",
           risk="low", category="apps", properties={"query": {"type": "string"}})
async def list_installed_apps(args, ctx):
    apps = await asyncio.to_thread(list_start_menu_apps)
    q = (args.get("query") or "").lower()
    names = [a.name for a in apps if q in a.name.lower()]
    if not names:
        return ToolResult.success("No matching applications.")
    return ToolResult.success(f"{len(names)} apps:\n" + "\n".join(names[:300]))


@salu_tool("list_windows", "List open application windows (title, process, minimized).",
           risk="low", category="apps")
async def list_windows_tool(args, ctx):
    wins = await asyncio.to_thread(list_windows)
    if not wins:
        return ToolResult.success("No open windows found.")
    lines = [f"{'[min] ' if w.minimized else ''}{w.title}  —  {w.process} (pid {w.pid})" for w in wins]
    return ToolResult.success("\n".join(lines), {"windows": [w.to_dict() for w in wins]})


@salu_tool("focus_window", "Bring a window to the front. Match by title text or process name (e.g. 'chrome').",
           risk="low", category="apps", properties={"query": {"type": "string"}}, required=["query"])
async def focus_window_tool(args, ctx):
    matches = await asyncio.to_thread(find_windows, args["query"])
    if not matches:
        return ToolResult.failure(f"No open window matches '{args['query']}'.")
    w = matches[0]
    ok = await asyncio.to_thread(focus_window, w)
    if ok:
        return ToolResult.success(f"Focused: {w.title} ({w.process}).")
    return ToolResult.failure(f"Windows refused to bring '{w.title}' to the front (focus-stealing protection). "
                              "It may be flashing in the taskbar.")


def _close_risk(args) -> Risk:
    return Risk.HIGH if args.get("force") else Risk.MEDIUM


def _close_describe(args) -> list[str]:
    matches = find_windows(args.get("query", ""))
    mode = "FORCE-KILL processes (unsaved work is lost)" if args.get("force") else "Close windows politely (apps may ask to save)"
    lines = [mode]
    lines += [f"{w.title} — {w.process}" for w in matches[:8]] or [f"No window currently matches '{args.get('query')}'"]
    return lines


@salu_tool("close_application",
           "Close application windows matching a title or process name. Default is a polite close (like clicking "
           "X; apps may prompt to save). force=true kills the processes (high risk, unsaved work lost).",
           risk="medium", category="apps", risk_fn=_close_risk, describe_fn=_close_describe,
           properties={"query": {"type": "string"}, "force": {"type": "boolean", "default": False}},
           required=["query"])
async def close_application(args, ctx):
    matches = await asyncio.to_thread(find_windows, args["query"])
    if args.get("force"):
        pids = {w.pid for w in matches}
        if not pids:
            q = args["query"].lower().removesuffix(".exe")
            pids = {p.pid for p in psutil.process_iter(["name"])
                    if (p.info["name"] or "").lower().removesuffix(".exe") == q}
        if not pids:
            return ToolResult.failure(f"Nothing matches '{args['query']}'.")
        killed, refused = [], []
        for pid in pids:
            try:
                proc = psutil.Process(pid)
                name = proc.name()
                if name.lower() in PROTECTED_PROCESSES or pid == os.getpid():
                    refused.append(name)
                    continue
                proc.kill()
                killed.append(f"{name} ({pid})")
            except psutil.Error as e:
                refused.append(f"pid {pid}: {e}")
        msg = f"Killed: {', '.join(killed) or 'none'}."
        if refused:
            msg += f" Refused/failed: {', '.join(refused)}."
        return ToolResult.success(msg) if killed else ToolResult.failure(msg)

    if not matches:
        return ToolResult.failure(f"No open window matches '{args['query']}'.")
    closed, still_open = [], []
    for w in matches:
        (closed if await asyncio.to_thread(close_window, w) else still_open).append(f"{w.title} ({w.process})")
    msg = f"Closed: {', '.join(closed) or 'none'}."
    if still_open:
        msg += f" Still open (probably asking to save, or ignored the request): {', '.join(still_open)}."
    return ToolResult.success(msg) if closed else ToolResult.failure(msg)


TOOLS = [open_application, open_path, list_installed_apps, list_windows_tool, focus_window_tool, close_application]
