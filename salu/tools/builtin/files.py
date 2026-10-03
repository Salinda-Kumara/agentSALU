"""Sandboxed file tools.

Access is limited to `tools.allowed_roots` (default: your home folder) and
credential locations are always blocked (.ssh, .env, key files, Claude
credentials, ...). Writing is MEDIUM risk; overwriting an existing file is HIGH.
There is deliberately no delete tool in Phase 1.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
from datetime import datetime
from pathlib import Path

from ...security.permissions import Risk
from ..base import ToolResult, salu_tool

MAX_READ_BYTES = 256_000
MAX_WRITE_CHARS = 500_000
MAX_LIST = 300

BLOCKED_DIRS = {".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".password-store", "secrets"}
BLOCKED_FILES = [".env", ".env.*", "*.pem", "*.key", "*.pfx", "*.p12", "*.kdbx", "id_rsa*", "id_ed25519*",
                 "id_ecdsa*", "id_dsa*", ".credentials.json", ".git-credentials", ".netrc", "_netrc",
                 "credentials", "credentials.json", "*.ppk"]
ALLOWED_DESPITE_BLOCK = {".env.example"}


class PathDenied(Exception):
    pass


def is_sensitive(path: Path) -> bool:
    parts = {p.lower() for p in path.parts[:-1]}
    if parts & BLOCKED_DIRS:
        return True
    name = path.name.lower()
    if name in ALLOWED_DESPITE_BLOCK:
        return False
    return any(fnmatch.fnmatch(name, pat) for pat in BLOCKED_FILES)


def expand_path(raw: str) -> Path:
    """User path -> absolute resolved path (relative paths are relative to home)."""
    if not raw or "\x00" in raw:
        raise PathDenied("empty or invalid path")
    p = Path(os.path.expandvars(os.path.expanduser(raw.strip().strip('"'))))
    if not p.is_absolute():
        p = Path.home() / p
    return p.resolve()  # follows symlinks/junctions so they can't escape the roots


def resolve_safe(raw: str, ctx) -> Path:
    p = expand_path(raw)
    roots = ctx.settings.allowed_roots()
    if not any(p == r or r in p.parents for r in roots):
        raise PathDenied(f"{p} is outside the allowed folders ({', '.join(map(str, roots))}). "
                         "Allowed folders are set in config/settings.yaml -> tools.allowed_roots.")
    if is_sensitive(p):
        raise PathDenied(f"{p} is a protected credential location; SALU will not access it.")
    return p


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


@salu_tool("list_directory", "List files and folders in a directory (name, type, size, modified). Read-only.",
           risk="low", category="files",
           properties={"path": {"type": "string", "description": "Directory path; ~ = home folder"},
                       "pattern": {"type": "string", "description": "Optional glob filter, e.g. *.py"}},
           required=["path"])
async def list_directory(args, ctx):
    try:
        p = resolve_safe(args["path"], ctx)
    except PathDenied as e:
        return ToolResult.failure(str(e))
    if not p.is_dir():
        return ToolResult.failure(f"Not a directory: {p}")

    def scan():
        rows = []
        for entry in sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            if args.get("pattern") and not fnmatch.fnmatch(entry.name, args["pattern"]):
                continue
            try:
                st = entry.stat()
                kind = "dir " if entry.is_dir() else "file"
                size = "" if entry.is_dir() else _human(st.st_size)
                rows.append(f"{kind}  {datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M}  {size:>7}  {entry.name}")
            except OSError:
                rows.append(f"?     {entry.name}")
            if len(rows) >= MAX_LIST:
                rows.append(f"... (stopped after {MAX_LIST} entries)")
                break
        return rows

    rows = await asyncio.to_thread(scan)
    return ToolResult.success(f"{p}\n" + ("\n".join(rows) if rows else "(empty)"))


@salu_tool("read_text_file", "Read a text file (up to 256 KB). Read-only.",
           risk="low", category="files",
           properties={"path": {"type": "string"},
                       "max_lines": {"type": "integer", "minimum": 1, "maximum": 5000}},
           required=["path"])
async def read_text_file(args, ctx):
    try:
        p = resolve_safe(args["path"], ctx)
    except PathDenied as e:
        return ToolResult.failure(str(e))
    if not p.is_file():
        return ToolResult.failure(f"File not found: {p}")
    size = p.stat().st_size

    def read_head() -> bytes:
        with p.open("rb") as f:
            return f.read(MAX_READ_BYTES)

    raw = await asyncio.to_thread(read_head)
    if b"\x00" in raw[:4096]:
        return ToolResult.failure(f"{p} looks like a binary file ({_human(size)}); not reading it as text.")
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    max_lines = args.get("max_lines") or 2000
    note = ""
    if len(lines) > max_lines:
        lines, note = lines[:max_lines], f"\n...[showing first {max_lines} lines]"
    if size > MAX_READ_BYTES:
        note += f"\n...[file is {_human(size)}; only first {_human(MAX_READ_BYTES)} read]"
    return ToolResult.success("\n".join(lines) + note)


def _write_risk(args) -> Risk:
    try:
        return Risk.HIGH if expand_path(args.get("path", "")).exists() else Risk.MEDIUM
    except (OSError, PathDenied):
        return Risk.HIGH


def _write_describe(args) -> list[str]:
    content = args.get("content", "")
    exists = _write_risk(args) == Risk.HIGH
    return [f"{'OVERWRITE existing' if exists else 'Create new'} file: {args.get('path')}",
            f"Content: {len(content)} characters, {content.count(chr(10)) + 1} lines"]


@salu_tool("write_text_file",
           "Create a text file. Overwriting an existing file requires overwrite=true and is high risk. "
           "Requires user confirmation.",
           risk="medium", category="files", risk_fn=_write_risk, describe_fn=_write_describe,
           properties={"path": {"type": "string"}, "content": {"type": "string"},
                       "overwrite": {"type": "boolean", "default": False}},
           required=["path", "content"])
async def write_text_file(args, ctx):
    try:
        p = resolve_safe(args["path"], ctx)
    except PathDenied as e:
        return ToolResult.failure(str(e))
    content = args["content"]
    if len(content) > MAX_WRITE_CHARS:
        return ToolResult.failure(f"Content too large ({len(content)} chars, max {MAX_WRITE_CHARS}).")
    if p.exists() and not args.get("overwrite"):
        return ToolResult.failure(f"{p} already exists. Set overwrite=true (high risk) to replace it.")
    if p.exists() and p.is_dir():
        return ToolResult.failure(f"{p} is a directory.")
    await asyncio.to_thread(lambda: (p.parent.mkdir(parents=True, exist_ok=True),
                                     p.write_text(content, encoding="utf-8")))
    written = p.stat().st_size  # verify the result instead of assuming success
    return ToolResult.success(f"Wrote {_human(written)} to {p}")


TOOLS = [list_directory, read_text_file, write_text_file]
