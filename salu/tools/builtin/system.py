"""System tools: status, time, processes, services, clipboard."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime

import psutil

from ...security.permissions import Risk
from ...system_info import full_status
from ...winos import IS_WINDOWS
from ...winos.powershell import ps_quote, run_powershell
from ..base import ToolResult, salu_tool


@salu_tool("system_status",
           "Check this Windows computer's health: CPU, RAM, disks, uptime, top memory processes, network. "
           "Read-only.",
           risk="low", category="system")
async def system_status(args, ctx):
    data = await asyncio.to_thread(full_status)
    return ToolResult.success(json.dumps(data, indent=1), data)


@salu_tool("current_datetime", "Get the current local date, time, weekday and timezone.",
           risk="low", category="system")
async def current_datetime(args, ctx):
    now = datetime.now().astimezone()
    return ToolResult.success(f"{now:%A, %Y-%m-%d %H:%M:%S} (UTC{now:%z}, {now.tzname()})")


@salu_tool("list_processes", "List running processes sorted by CPU or memory, optionally filtered by name.",
           risk="low", category="system",
           properties={"query": {"type": "string"}, "sort": {"type": "string", "enum": ["cpu", "memory"]},
                       "limit": {"type": "integer", "minimum": 1, "maximum": 200}})
async def list_processes(args, ctx):
    q = (args.get("query") or "").lower()

    def sample():
        procs = [p for p in psutil.process_iter(["pid", "name", "memory_info"])
                 if not q or q in (p.info["name"] or "").lower()]
        for p in procs:
            try:
                p.cpu_percent(None)
            except psutil.Error:
                pass
        time.sleep(0.5)
        rows = []
        ncpu = psutil.cpu_count() or 1
        for p in procs:
            try:
                rows.append((p.info["name"], p.info["pid"], p.cpu_percent(None) / ncpu,
                             (p.info["memory_info"].rss if p.info["memory_info"] else 0) / 1e6))
            except psutil.Error:
                continue
        return rows

    rows = await asyncio.to_thread(sample)
    key = 3 if args.get("sort") == "memory" else 2
    rows.sort(key=lambda r: r[key], reverse=True)
    rows = rows[: args.get("limit") or 25]
    if not rows:
        return ToolResult.success("No matching processes.")
    lines = [f"{'NAME':<32} {'PID':>7} {'CPU%':>6} {'MEM MB':>8}"]
    lines += [f"{(n or '?')[:32]:<32} {pid:>7} {cpu:>6.1f} {mem:>8.0f}" for n, pid, cpu, mem in rows]
    return ToolResult.success("\n".join(lines))


@salu_tool("list_services", "List Windows services, optionally filtered by name/display name and status.",
           risk="low", category="system",
           properties={"query": {"type": "string"},
                       "status": {"type": "string", "enum": ["running", "stopped", "any"]}})
async def list_services(args, ctx):
    if not IS_WINDOWS:
        return ToolResult.failure("Only supported on Windows.")
    q = (args.get("query") or "").lower()
    want = args.get("status") or "any"

    def collect():
        out = []
        for s in psutil.win_service_iter():
            try:  # query only what we need; some services have unreadable descriptions/configs
                info = {"name": s.name(), "display_name": s.display_name(), "status": s.status(),
                        "start_type": s.start_type()}
            except (psutil.Error, OSError):
                continue
            if q and q not in info["name"].lower() and q not in (info["display_name"] or "").lower():
                continue
            if want != "any" and info["status"] != want:
                continue
            out.append(f"{info['status']:<8} {info['start_type']:<9} {info['name']:<28} {info['display_name']}")
        return out

    rows = await asyncio.to_thread(collect)
    return ToolResult.success("\n".join(rows[:200]) if rows else "No matching services.")


def _service_risk(args) -> Risk:
    return Risk.HIGH if args.get("action") == "stop" else Risk.MEDIUM


@salu_tool("control_service",
           "Start, stop or restart a Windows service. Usually requires SALU to run as administrator; failures "
           "are reported. Stop is high risk.",
           risk="medium", category="system", risk_fn=_service_risk, timeout_seconds=90,
           describe_fn=lambda a: [f"{a.get('action', '?').upper()} service: {a.get('name')}"],
           properties={"name": {"type": "string", "description": "Service name (not display name)"},
                       "action": {"type": "string", "enum": ["start", "stop", "restart"]}},
           required=["name", "action"])
async def control_service(args, ctx):
    if not IS_WINDOWS:
        return ToolResult.failure("Only supported on Windows.")
    cmdlet = {"start": "Start-Service", "stop": "Stop-Service", "restart": "Restart-Service"}[args["action"]]
    name = ps_quote(args["name"])
    r = await run_powershell(f"{cmdlet} -Name {name} -ErrorAction Stop; (Get-Service -Name {name}).Status", timeout=75)
    if not r.ok:
        hint = " (Access denied usually means SALU must run as administrator.)" if "denied" in r.stderr.lower() \
            or "cannot open" in r.stderr.lower() else ""
        return ToolResult.failure(f"{cmdlet} failed: {r.stderr or r.stdout}{hint}")
    return ToolResult.success(f"{cmdlet} {args['name']}: status is now {r.stdout.strip() or 'unknown'}.")


@salu_tool("clipboard_read", "Read the current text on the clipboard (secrets are redacted).",
           risk="low", category="system")
async def clipboard_read(args, ctx):
    if not IS_WINDOWS:
        return ToolResult.failure("Only supported on Windows.")
    r = await run_powershell("Get-Clipboard -Raw", timeout=15)
    if not r.ok:
        return ToolResult.failure(f"Could not read clipboard: {r.stderr}")
    text = r.stdout.rstrip("\r\n")
    return ToolResult.success(text if text else "(clipboard is empty or contains no text)")


@salu_tool("clipboard_write", "Put text on the clipboard (replaces its current content).",
           risk="low", category="system", properties={"text": {"type": "string"}}, required=["text"])
async def clipboard_write(args, ctx):
    if not IS_WINDOWS:
        return ToolResult.failure("Only supported on Windows.")
    r = await run_powershell(f"Set-Clipboard -Value {ps_quote(args['text'])}", timeout=15)
    if not r.ok:
        return ToolResult.failure(f"Could not set clipboard: {r.stderr}")
    return ToolResult.success(f"Copied {len(args['text'])} characters to the clipboard.")


TOOLS = [system_status, current_datetime, list_processes, list_services, control_service, clipboard_read,
         clipboard_write]
