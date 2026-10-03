"""Locating the Claude Code CLI and enforcing the configured auth mode.

SALU never reads, copies or stores Claude credentials. It runs the unmodified
Claude Code CLI, which uses whatever login you created with Claude Code's own
sign-in flow, and only asks the CLI for its auth *status*.
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("salu.claude")

# Variables that make Claude Code bill an API key / other provider instead of the subscription.
API_BILLING_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
                    "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "ANTHROPIC_PROFILE")


def find_cli(configured: str | None = None) -> str | None:
    """configured path -> PATH -> native installer -> SDK bundle -> newest VS Code extension."""
    if configured:
        p = Path(os.path.expandvars(os.path.expanduser(configured)))
        return str(p) if p.is_file() else None
    if os.name == "nt":
        hit = shutil.which("claude.exe")
    else:
        hit = shutil.which("claude")
    if hit:
        return hit
    candidates = [Path.home() / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude")]
    try:
        import claude_agent_sdk
        candidates.append(Path(claude_agent_sdk.__file__).parent / "_bundled" /
                          ("claude.exe" if os.name == "nt" else "claude"))
    except ImportError:
        pass
    for c in candidates:
        if c.is_file():
            return str(c)
    exe = "claude.exe" if os.name == "nt" else "claude"
    pattern = str(Path.home() / ".vscode" / "extensions" / "anthropic.claude-code-*" / "resources" / "native-binary" / exe)
    found = sorted(glob.glob(pattern), key=_ext_version, reverse=True)
    return found[0] if found else None


def _ext_version(path: str) -> tuple:
    name = Path(path).parents[2].name  # anthropic.claude-code-2.1.238-win32-x64
    ver = name.removeprefix("anthropic.claude-code-").split("-")[0]
    try:
        return tuple(int(x) for x in ver.split("."))
    except ValueError:
        return (0,)


def enforce_auth_mode(auth_mode: str) -> list[str]:
    """In subscription mode, remove API-billing variables from SALU's own process.

    The Agent SDK passes os.environ to the CLI, and Claude Code prefers an API
    key over the subscription login. Removing them guarantees SALU can never
    silently switch to API billing. Returns the names removed (never values).
    """
    removed = []
    if auth_mode == "subscription":
        for var in API_BILLING_VARS:
            if os.environ.pop(var, None) is not None:
                removed.append(var)
    if removed:
        log.warning("subscription mode: removed %s from SALU's environment to prevent API billing",
                    ", ".join(removed))
    return removed


@dataclass
class CliStatus:
    path: str | None
    version: str | None
    logged_in: bool
    auth_method: str | None
    subscription: str | None
    provider: str | None
    error: str | None = None


async def _run(cli: str, *args: str, timeout: float = 30) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(cli, *args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def cli_status(configured_path: str | None) -> CliStatus:
    cli = find_cli(configured_path)
    if not cli:
        return CliStatus(None, None, False, None, None, None,
                         "Claude Code CLI not found. Install it (see README) or set claude.cli_path.")
    try:
        _, out, _ = await _run(cli, "--version")
        version = out.strip().split()[0] if out.strip() else None
        code, out, err = await _run(cli, "auth", "status")
        data = json.loads(out) if out.strip().startswith("{") else {}
    except Exception as e:
        return CliStatus(cli, None, False, None, None, None, f"Could not run Claude Code CLI: {e}")
    if not data:
        return CliStatus(cli, version, False, None, None, None,
                         f"Unexpected 'claude auth status' output (exit {code}): {(err or out)[:200]}")
    # Only non-identifying fields are kept (no email / org ids).
    return CliStatus(cli, version, bool(data.get("loggedIn")), data.get("authMethod"),
                     data.get("subscriptionType"), data.get("apiProvider"))


def is_api_billing(method: str | None) -> bool:
    m = (method or "").lower()
    return "api" in m or "key" in m or "console" in m
