"""Top-level window enumeration / focus / close via the Win32 API (ctypes, no extra deps)."""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass

import psutil

from . import IS_WINDOWS

if IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
    user32.GetWindow.restype = wintypes.HWND
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
    user32.IsWindow.argtypes = [wintypes.HWND]

GW_OWNER = 4
SW_RESTORE = 9
WM_CLOSE = 0x0010


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    process: str
    minimized: bool

    def to_dict(self) -> dict:
        return {"title": self.title, "process": self.process, "pid": self.pid, "minimized": self.minimized}


def list_windows() -> list[WindowInfo]:
    """Visible, titled, un-owned top-level windows (what the taskbar shows)."""
    if not IS_WINDOWS:
        return []
    found: list[WindowInfo] = []

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, GW_OWNER):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            pname = psutil.Process(pid.value).name()
        except psutil.Error:
            pname = "?"
        found.append(WindowInfo(int(hwnd), buf.value, pid.value, pname, bool(user32.IsIconic(hwnd))))
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return found


def find_windows(query: str) -> list[WindowInfo]:
    q = query.lower().removesuffix(".exe")
    return [w for w in list_windows() if q in w.title.lower() or q == w.process.lower().removesuffix(".exe")]


def focus_window(w: WindowInfo) -> bool:
    if not IS_WINDOWS:
        return False
    if user32.IsIconic(w.hwnd):
        user32.ShowWindow(w.hwnd, SW_RESTORE)
    user32.SetForegroundWindow(w.hwnd)
    time.sleep(0.15)
    return int(user32.GetForegroundWindow() or 0) == w.hwnd


def close_window(w: WindowInfo, wait: float = 3.0) -> bool:
    """Polite close (like clicking X). Apps may show a 'save changes?' prompt instead of closing."""
    if not IS_WINDOWS:
        return False
    user32.PostMessageW(w.hwnd, WM_CLOSE, 0, 0)
    deadline = time.time() + wait
    while time.time() < deadline:
        if not user32.IsWindow(w.hwnd):
            return True
        time.sleep(0.2)
    return not user32.IsWindow(w.hwnd)
