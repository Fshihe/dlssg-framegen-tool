"""进程枚举（ctypes 调 Win32，无第三方依赖）。

用于两件事：
  1. 安装前确认游戏没在运行（避免文件被占用 / 污染运行中的进程）
  2. 识别正在运行的反作弊服务（Vanguard 等不进游戏目录的那种）
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_PATH = 260


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * MAX_PATH),
    ]


def list_process_names() -> set[str]:
    """返回当前所有进程的映像名（小写、含 .exe）。失败时返回空集合。"""
    names: set[str] = set()
    if os.name != "nt":
        return names
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == INVALID_HANDLE_VALUE or not snap:
            return names
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                nm = entry.szExeFile
                if nm:
                    names.add(nm.lower())
                ok = k32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            k32.CloseHandle(snap)
    except Exception:
        pass
    return names


def is_running(exe_name: str) -> bool:
    """指定映像名（可带或不带 .exe）是否在运行。"""
    nm = (exe_name or "").strip().lower()
    if not nm:
        return False
    if not nm.endswith(".exe"):
        nm += ".exe"
    return nm in list_process_names()


def is_locked(path: str) -> bool:
    """尝试独占打开文件：成功说明没被占用。"""
    try:
        with open(path, "r+b"):
            return False
    except PermissionError:
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


__all__ = ["list_process_names", "is_running", "is_locked"]
