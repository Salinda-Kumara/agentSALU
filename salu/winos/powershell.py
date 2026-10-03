"""Run PowerShell safely: no profile, non-interactive, UTF-8, timeout with process-tree kill."""

from __future__ import annotations

import asyncio
import base64
import shutil
import subprocess
from dataclasses import dataclass

PREAMBLE = ("$ProgressPreference='SilentlyContinue'; $ErrorActionPreference='Continue'; "
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $OutputEncoding=[Text.Encoding]::UTF8;\n")


@dataclass
class PsResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def powershell_exe() -> str:
    # Windows PowerShell 5.1 ships with Windows; pwsh 7 is used if installed.
    return shutil.which("pwsh.exe") or shutil.which("powershell.exe") or "powershell.exe"


def _kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)


async def run_powershell(script: str, timeout: float = 60, cwd: str | None = None) -> PsResult:
    # -EncodedCommand avoids every quoting problem; the script itself is SALU-built or user-approved.
    encoded = base64.b64encode((PREAMBLE + script).encode("utf-16-le")).decode("ascii")
    proc = await asyncio.create_subprocess_exec(
        powershell_exe(), "-NoProfile", "-NonInteractive", "-NoLogo", "-EncodedCommand", encoded,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL,
        cwd=cwd, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await asyncio.to_thread(_kill_tree, proc.pid)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=5)
        except asyncio.TimeoutError:
            out, err = b"", b""
        return PsResult(-1, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), timed_out=True)
    return PsResult(proc.returncode, out.decode("utf-8", "replace"), _clean_stderr(err.decode("utf-8", "replace")))


def _clean_stderr(err: str) -> str:
    # PowerShell emits CLIXML progress records on stderr when output is redirected.
    if err.startswith("#< CLIXML"):
        import re
        msgs = re.findall(r'<S S="Error">(.*?)</S>', err)
        return "\n".join(m.replace("_x000D__x000A_", "") for m in msgs).strip()
    return err.strip()


def ps_quote(value: str) -> str:
    """Quote a value as a PowerShell single-quoted literal (no interpolation possible)."""
    return "'" + str(value).replace("'", "''") + "'"
