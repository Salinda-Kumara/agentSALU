"""PowerShell tool. Risk is computed per command (see salu/security/command_risk.py)."""

from __future__ import annotations

from pathlib import Path

from ...security.command_risk import classify_powershell
from ...winos import IS_WINDOWS
from ...winos.powershell import run_powershell
from ..base import ToolResult, salu_tool
from .files import PathDenied, resolve_safe

MAX_TIMEOUT = 300


def _risk(args):
    return classify_powershell(args.get("command", "")).risk


def _describe(args):
    a = classify_powershell(args.get("command", ""))
    lines = [f"PowerShell: {args.get('command', '')[:600]}"]
    lines += [f"Why {a.risk.label}: {r}" for r in a.reasons[:5]]
    if args.get("cwd"):
        lines.append(f"In folder: {args['cwd']}")
    return lines


@salu_tool("run_powershell",
           "Run a Windows PowerShell command and return its output. Risk is assessed per command: read-only "
           "commands (Get-*, ipconfig, git status, ...) run immediately; anything that changes the system needs "
           "user confirmation; destructive commands are high risk. Prefer SALU's dedicated tools when one fits. "
           "Never use it to bypass a denied action. Output is capped.",
           risk="low", category="system", risk_fn=_risk, describe_fn=_describe, timeout_seconds=MAX_TIMEOUT + 15,
           properties={"command": {"type": "string", "description": "PowerShell command or short script"},
                       "cwd": {"type": "string", "description": "Working directory (default: home folder)"},
                       "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_TIMEOUT}},
           required=["command"])
async def run_ps(args, ctx):
    if not IS_WINDOWS:
        return ToolResult.failure("PowerShell is only available on Windows.")
    cwd = str(Path.home())
    if args.get("cwd"):
        try:
            cwd = str(resolve_safe(args["cwd"], ctx))
        except PathDenied as e:
            return ToolResult.failure(str(e))
        if not Path(cwd).is_dir():
            return ToolResult.failure(f"Not a directory: {cwd}")
    timeout = min(int(args.get("timeout_seconds") or 60), MAX_TIMEOUT)
    r = await run_powershell(args["command"], timeout=timeout, cwd=cwd)
    parts = []
    if r.timed_out:
        parts.append(f"TIMED OUT after {timeout}s (process tree terminated).")
    parts.append(f"exit code: {r.exit_code}")
    if r.stdout.strip():
        parts.append(r.stdout.rstrip())
    if r.stderr.strip():
        parts.append("stderr:\n" + r.stderr.rstrip())
    if not r.stdout.strip() and not r.stderr.strip():
        parts.append("(no output)")
    text = "\n".join(parts)
    return ToolResult.success(text) if r.ok else ToolResult.failure(text)


TOOLS = [run_ps]
