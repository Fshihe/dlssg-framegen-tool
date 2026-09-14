"""本地路径、日志与操作日志（journal）。

本工具只在这两个位置写文件：
  1. 用户选定的游戏目录 —— 仅 <代理>.dll 与 dlssg_sm86.ini
  2. %LOCALAPPDATA%\\DLSSG-SM86-Tool —— 备份、状态、日志
不写注册表、不装驱动、不改系统设置。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

from . import APP_ID

_LOCK = threading.Lock()


def app_root() -> Path:
    """工具的数据根目录（备份 / 状态 / 日志）。"""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if not base:
        base = str(Path.home() / "AppData" / "Local")
    return Path(base) / "DLSSG-SM86-Tool"


def backups_root() -> Path:
    return app_root() / "backups"


def logs_root() -> Path:
    return app_root() / "logs"


def state_file() -> Path:
    """记录所有本工具做过的安装（用于卸载 / 还原 / 体检）。"""
    return app_root() / "state.json"


def reports_root() -> Path:
    return app_root() / "reports"


def ensure_dirs() -> None:
    for d in (app_root(), backups_root(), logs_root(), reports_root()):
        d.mkdir(parents=True, exist_ok=True)


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包出的 exe 中。"""
    return getattr(sys, "frozen", False)


def resource_root() -> Path:
    """随程序发布的只读资源目录（payload/ 所在处）。

    - 打包后：PyInstaller 解包目录（sys._MEIPASS）
    - 源码运行：仓库根（src 的上一级）
    """
    if is_frozen():
        base = getattr(sys, "_MEIPASS", None)
        if base:
            return Path(base)
        return Path(sys.executable).parent
    return Path(__file__).resolve().parents[2]


def exe_dir() -> Path:
    """exe（或 main.py）所在目录，用于查找 exe 旁边的 payload/ 覆盖目录。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def payload_base() -> Path:
    """payload/ 的根目录。

    优先用 exe 旁边的（用户可自行替换上游文件），否则用内置解包目录。
    这样源码运行、打包运行、以及"用户手动放一份 payload 在旁边"三种情况
    走的是同一条查找逻辑。
    """
    for base in (exe_dir(), resource_root()):
        d = base / "payload"
        if d.is_dir():
            return d
    return resource_root() / "payload"


# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------

def _log_path() -> Path:
    ensure_dirs()
    return logs_root() / f"tool-{time.strftime('%Y%m%d')}.log"


def log(message: str, level: str = "info") -> None:
    """写一行日志。任何情况下都不允许因为日志失败而中断主流程。"""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{level.upper():5}] {message}"
    try:
        with _LOCK:
            with open(_log_path(), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass


def journal(event: str, **fields) -> None:
    """结构化操作日志（JSONL），用于事后审计。"""
    try:
        ensure_dirs()
        rec = {"ts": time.time(), "time": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event}
        rec.update(fields)
        with _LOCK:
            with open(logs_root() / "journal.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


__all__ = [
    "app_root",
    "backups_root",
    "logs_root",
    "reports_root",
    "state_file",
    "ensure_dirs",
    "is_frozen",
    "resource_root",
    "exe_dir",
    "payload_base",
    "log",
    "journal",
]
