"""File management tools (Phase 2): find, info, mkdir, copy, move/rename, recycle, zip.

All paths go through files.resolve_safe (allowed roots + credential block list).
Deletion only ever goes to the Recycle Bin; SALU has no permanent-delete tool.
"""

from __future__ import annotations

import asyncio
import ctypes
import fnmatch
import os
import shutil
import time
import zipfile
from datetime import datetime
from pathlib import Path

from ...security.permissions import Risk
from ...winos import IS_WINDOWS
from ..base import ToolResult, salu_tool
from .files import PathDenied, _human, expand_path, is_sensitive, resolve_safe

FIND_MAX_SCANNED = 60_000
FIND_TIME_LIMIT = 20
ZIP_MAX_FILES = 10_000
ZIP_MAX_BYTES = 2_000_000_000
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "$recycle.bin", "system volume information"}


def _tree_stats(p: Path, limit: int = 5000) -> tuple[int, int]:
    if p.is_file():
        return 1, p.stat().st_size
    files = size = 0
    for root, _, names in os.walk(p):
        for n in names:
            files += 1
            try:
                size += (Path(root) / n).stat().st_size
            except OSError:
                pass
            if files >= limit:
                return files, size
    return files, size


# ---- find / info / mkdir --------------------------------------------------------------
@salu_tool("find_files",
           "Search for files by name pattern (glob, e.g. '*.pdf' or '*invoice*') under a folder, optionally "
           "only files containing some text. Skips .git/node_modules/venv. Read-only.",
           risk="low", category="files",
           properties={"root": {"type": "string"}, "pattern": {"type": "string"},
                       "contains": {"type": "string", "description": "Optional text that must appear in the file"},
                       "max_results": {"type": "integer", "minimum": 1, "maximum": 500}},
           required=["root", "pattern"], timeout_seconds=FIND_TIME_LIMIT + 10)
async def find_files(args, ctx):
    try:
        root = resolve_safe(args["root"], ctx)
    except PathDenied as e:
        return ToolResult.failure(str(e))
    if not root.is_dir():
        return ToolResult.failure(f"Not a directory: {root}")
    pattern = args["pattern"].lower()
    needle = (args.get("contains") or "").lower()
    limit = args.get("max_results") or 100

    def walk():
        hits, scanned, start = [], 0, time.time()
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS and not d.startswith(".")]
            for name in filenames:
                scanned += 1
                if fnmatch.fnmatch(name.lower(), pattern):
                    p = Path(dirpath) / name
                    if is_sensitive(p):
                        continue
                    if needle:
                        try:
                            if p.stat().st_size > 5_000_000 or needle not in p.read_text("utf-8", "ignore").lower():
                                continue
                        except OSError:
                            continue
                    try:
                        st = p.stat()
                        hits.append(f"{datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M}  {_human(st.st_size):>7}  {p}")
                    except OSError:
                        hits.append(str(p))
                    if len(hits) >= limit:
                        return hits, scanned, "result limit reached"
                if scanned >= FIND_MAX_SCANNED:
                    return hits, scanned, f"stopped after scanning {FIND_MAX_SCANNED} files"
                if time.time() - start > FIND_TIME_LIMIT:
                    return hits, scanned, f"stopped after {FIND_TIME_LIMIT}s"
        return hits, scanned, None

    hits, scanned, note = await asyncio.to_thread(walk)
    head = f"{len(hits)} match(es) in {root} (scanned {scanned} files{'; ' + note if note else ''})"
    return ToolResult.success(head + ("\n" + "\n".join(hits) if hits else ""))


@salu_tool("file_info", "Details about a file or folder: type, size (folders: file count/total), dates.",
           risk="low", category="files", properties={"path": {"type": "string"}}, required=["path"])
async def file_info(args, ctx):
    try:
        p = resolve_safe(args["path"], ctx)
    except PathDenied as e:
        return ToolResult.failure(str(e))
    if not p.exists():
        return ToolResult.failure(f"Not found: {p}")
    st = p.stat()
    files, size = await asyncio.to_thread(_tree_stats, p, 50_000)
    kind = "folder" if p.is_dir() else (p.suffix.lower() or "file")
    lines = [f"Path: {p}", f"Type: {kind}",
             f"Size: {_human(size)}" + (f" in {files} files" if p.is_dir() else ""),
             f"Modified: {datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M:%S}",
             f"Created: {datetime.fromtimestamp(st.st_ctime):%Y-%m-%d %H:%M:%S}"]
    return ToolResult.success("\n".join(lines))


@salu_tool("create_directory", "Create a folder (and missing parent folders).",
           risk="low", category="files", properties={"path": {"type": "string"}}, required=["path"])
async def create_directory(args, ctx):
    try:
        p = resolve_safe(args["path"], ctx)
    except PathDenied as e:
        return ToolResult.failure(str(e))
    if p.exists():
        return ToolResult.success(f"Already exists: {p}") if p.is_dir() else ToolResult.failure(f"A file exists at {p}")
    await asyncio.to_thread(p.mkdir, parents=True)
    return ToolResult.success(f"Created {p}") if p.is_dir() else ToolResult.failure(f"Could not create {p}")


# ---- copy / move ------------------------------------------------------------------------
def _final_destination(src: Path, dst: Path) -> Path:
    """cp/mv semantics: an existing folder as destination means 'place inside it'."""
    return dst / src.name if dst.is_dir() and dst != src else dst


def _dest_exists(args) -> bool:
    try:
        return _final_destination(expand_path(args.get("source", "")), expand_path(args.get("destination", ""))).exists()
    except (OSError, PathDenied):
        return True  # unknown => treat as the riskier case


def _transfer_risk(args) -> Risk:
    return Risk.HIGH if _dest_exists(args) else Risk.MEDIUM


def _transfer_describe(verb):
    def describe(args):
        lines = [f"{verb}: {args.get('source')}", f"To: {args.get('destination')}"]
        if _dest_exists(args):
            lines.append("Destination EXISTS and will be replaced" if args.get("overwrite")
                         else "Destination exists (will fail unless overwrite=true)")
        try:
            files, size = _tree_stats(expand_path(args.get("source", "")))
            lines.append(f"{files} file(s), {_human(size)}")
        except (OSError, PathDenied):
            pass
        return lines
    return describe


async def _transfer(args, ctx, move: bool) -> ToolResult:
    try:
        src = resolve_safe(args["source"], ctx)
        dst = resolve_safe(args["destination"], ctx)
    except PathDenied as e:
        return ToolResult.failure(str(e))
    if not src.exists():
        return ToolResult.failure(f"Source not found: {src}")
    dst = _final_destination(src, dst)
    if dst == src:
        return ToolResult.failure("Source and destination are the same.")
    if src.is_dir() and (dst == src or src in dst.parents):
        return ToolResult.failure("Cannot copy/move a folder into itself.")
    if dst.exists():
        if not args.get("overwrite"):
            return ToolResult.failure(f"Destination exists: {dst}. Set overwrite=true (high risk) to replace it.")
        if dst.is_dir() != src.is_dir():
            return ToolResult.failure("Cannot replace a folder with a file or vice versa.")

    def do():
        dst.parent.mkdir(parents=True, exist_ok=True)
        if move:
            if dst.exists():
                (shutil.rmtree if dst.is_dir() else os.remove)(dst)
            shutil.move(str(src), str(dst))
        elif src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=bool(args.get("overwrite")))
        else:
            shutil.copy2(src, dst)

    await asyncio.to_thread(do)
    if not dst.exists() or (move and src.exists()):
        return ToolResult.failure(f"Operation did not complete as expected (source exists: {src.exists()}, "
                                  f"destination exists: {dst.exists()}).")
    return ToolResult.success(f"{'Moved' if move else 'Copied'} {src} -> {dst}")


_TRANSFER_PROPS = {"source": {"type": "string"}, "destination": {"type": "string", "description":
                   "Target path. If it is an existing folder, the item is placed inside it."},
                   "overwrite": {"type": "boolean", "default": False}}


@salu_tool("copy_path", "Copy a file or folder. Replacing an existing destination is high risk.",
           risk="medium", category="files", risk_fn=_transfer_risk, describe_fn=_transfer_describe("Copy"),
           properties=_TRANSFER_PROPS, required=["source", "destination"], timeout_seconds=600)
async def copy_path(args, ctx):
    return await _transfer(args, ctx, move=False)


@salu_tool("move_path", "Move or rename a file or folder. Replacing an existing destination is high risk.",
           risk="medium", category="files", risk_fn=_transfer_risk, describe_fn=_transfer_describe("Move"),
           properties=_TRANSFER_PROPS, required=["source", "destination"], timeout_seconds=600)
async def move_path(args, ctx):
    return await _transfer(args, ctx, move=True)


# ---- recycle bin ------------------------------------------------------------------------
def _send_to_recycle_bin(path: Path) -> int:
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
    op = SHFILEOPSTRUCTW(None, FO_DELETE, str(path) + "\0", None,
                         FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI, False, None, None)
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))


def _recycle_describe(args):
    lines = [f"Move to Recycle Bin (recoverable): {args.get('path')}"]
    try:
        p = expand_path(args.get("path", ""))
        files, size = _tree_stats(p)
        lines.append(f"{'Folder with ' if p.is_dir() else ''}{files} file(s), {_human(size)}")
    except (OSError, PathDenied):
        pass
    return lines


@salu_tool("delete_to_recycle_bin",
           "Delete a file or folder by moving it to the Windows Recycle Bin (recoverable). Always requires "
           "explicit confirmation. There is no permanent delete.",
           risk="high", category="files", describe_fn=_recycle_describe,
           properties={"path": {"type": "string"}}, required=["path"])
async def delete_to_recycle_bin(args, ctx):
    if not IS_WINDOWS:
        return ToolResult.failure("Only supported on Windows.")
    try:
        p = resolve_safe(args["path"], ctx)
    except PathDenied as e:
        return ToolResult.failure(str(e))
    if not p.exists():
        return ToolResult.failure(f"Not found: {p}")
    protected = {Path.home().resolve(), *ctx.settings.allowed_roots(), ctx.settings.root.resolve()}
    if p in protected or p.parent == p or len(p.parts) <= 2 and p.is_dir():
        return ToolResult.failure(f"Refusing to delete {p}: it is a drive root, home folder, allowed root or "
                                  "SALU's own folder.")
    code = await asyncio.to_thread(_send_to_recycle_bin, p)
    if code != 0 or p.exists():
        return ToolResult.failure(f"Recycle failed (code {code}); {p} {'still exists' if p.exists() else 'is gone'}.")
    return ToolResult.success(f"Moved {p} to the Recycle Bin.")


# ---- zip --------------------------------------------------------------------------------
@salu_tool("create_zip", "Compress files/folders into a new .zip archive (won't overwrite an existing archive).",
           risk="medium", category="files",
           properties={"sources": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                       "destination": {"type": "string", "description": "Path of the .zip to create"}},
           required=["sources", "destination"], timeout_seconds=900)
async def create_zip(args, ctx):
    try:
        srcs = [resolve_safe(s, ctx) for s in args["sources"]]
        dest = resolve_safe(args["destination"], ctx)
    except PathDenied as e:
        return ToolResult.failure(str(e))
    if dest.suffix.lower() != ".zip":
        dest = dest.with_suffix(".zip")
    if dest.exists():
        return ToolResult.failure(f"{dest} already exists.")
    missing = [str(s) for s in srcs if not s.exists()]
    if missing:
        return ToolResult.failure(f"Not found: {', '.join(missing)}")

    def build():
        count, skipped = 0, 0
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            for s in srcs:
                items = [s] if s.is_file() else [p for p in s.rglob("*") if p.is_file()]
                for f in items:
                    if is_sensitive(f) or f == dest:
                        skipped += 1
                        continue
                    z.write(f, f.relative_to(s.parent))
                    count += 1
        return count, skipped

    count, skipped = await asyncio.to_thread(build)
    note = f" Skipped {skipped} protected file(s)." if skipped else ""
    return ToolResult.success(f"Created {dest} ({count} files, {_human(dest.stat().st_size)}).{note}")


@salu_tool("extract_zip",
           "Extract a .zip archive into a folder. Refuses archives that would write outside the folder, "
           "overwrite existing files, or are suspiciously large.",
           risk="medium", category="files",
           properties={"archive": {"type": "string"}, "destination": {"type": "string"}},
           required=["archive", "destination"], timeout_seconds=900)
async def extract_zip(args, ctx):
    try:
        archive = resolve_safe(args["archive"], ctx)
        dest = resolve_safe(args["destination"], ctx)
    except PathDenied as e:
        return ToolResult.failure(str(e))
    if not archive.is_file():
        return ToolResult.failure(f"Not found: {archive}")

    def extract():
        with zipfile.ZipFile(archive) as z:
            infos = z.infolist()
            if len(infos) > ZIP_MAX_FILES or sum(i.file_size for i in infos) > ZIP_MAX_BYTES:
                return f"Refused: archive too large ({len(infos)} entries).", 0
            conflicts = []
            for i in infos:
                target = (dest / i.filename).resolve()
                if dest != target and dest not in target.parents:
                    return f"Refused: entry '{i.filename}' would be written outside the destination (zip-slip).", 0
                if is_sensitive(target):
                    return f"Refused: entry '{i.filename}' targets a protected credential location.", 0
                if target.exists() and not i.is_dir():
                    conflicts.append(i.filename)
            if conflicts:
                return f"Refused: {len(conflicts)} file(s) already exist, e.g. {conflicts[0]}. Extract into an empty folder.", 0
            dest.mkdir(parents=True, exist_ok=True)
            z.extractall(dest)
            return None, len(infos)

    error, count = await asyncio.to_thread(extract)  # (error message or None, entries extracted)
    if error:
        return ToolResult.failure(error)
    return ToolResult.success(f"Extracted {count} entries from {archive.name} into {dest}")


TOOLS = [find_files, file_info, create_directory, copy_path, move_path, delete_to_recycle_bin, create_zip, extract_zip]
