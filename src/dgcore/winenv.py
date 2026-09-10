"""Windows 图形环境前置条件检查 —— HAGS（硬件加速 GPU 计划）。

DLSS 帧生成不只看显卡，还硬性依赖操作系统的「硬件加速 GPU 计划」。
这一项关闭时，即使 RTX 40 系也用不了帧生成，而游戏通常只给一句
「您的显卡不支持 DLSS 帧生成技术」—— 极容易被误判成 Mod 失效。

本项目只在**用户显式同意**时才会修改这一项，且改动内容与 Windows
设置界面里的开关完全一致（仅一个注册表值），不碰任何其他系统设置。
"""

from __future__ import annotations

import os

HAGS_KEY = r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers"
HAGS_VALUE = "HwSchMode"
HAGS_ON = 2
HAGS_OFF = 1

HAGS_GUI_PATH = "设置 → 系统 → 屏幕 → 显示卡 → 更改默认图形设置 → 硬件加速 GPU 计划"


def read_hags() -> tuple[bool | None, int | None]:
    """返回 (是否开启, 原始值)。读不到时返回 (None, None)。"""
    if os.name != "nt":
        return None, None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, HAGS_KEY, 0, winreg.KEY_READ) as k:
            raw, _ = winreg.QueryValueEx(k, HAGS_VALUE)
        raw = int(raw)
        return (raw == HAGS_ON), raw
    except FileNotFoundError:
        # 值不存在：老系统上等同于关闭
        return False, None
    except Exception:
        return None, None


def describe(enabled: bool | None, raw: int | None) -> str:
    if enabled is None:
        return "无法读取（可能需要管理员权限）"
    if enabled:
        return "已开启"
    return "已关闭" + (f"（HwSchMode={raw}）" if raw is not None else "（注册表项不存在）")


def needs_enabling() -> bool | None:
    """True = 需要用户去开；False = 已经开着；None = 读不到，无法判断。"""
    enabled, _ = read_hags()
    if enabled is None:
        return None
    return not enabled


def is_admin() -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def set_hags(enable: bool = True) -> tuple[bool, str]:
    """开启/关闭 HAGS。需要管理员权限。

    只写 HwSchMode 这一个值 —— 与 Windows 设置界面里的开关是同一件事。
    改完必须重启电脑才会生效。
    """
    if os.name != "nt":
        return False, "仅支持 Windows"
    if not is_admin():
        return False, "需要管理员权限才能修改系统设置，请以管理员身份重新运行本工具"

    target = HAGS_ON if enable else HAGS_OFF
    try:
        import winreg

        with winreg.CreateKeyEx(
            winreg.HKEY_LOCAL_MACHINE, HAGS_KEY, 0, winreg.KEY_SET_VALUE
        ) as k:
            before, _ = read_hags()
            winreg.SetValueEx(k, HAGS_VALUE, 0, winreg.REG_DWORD, target)
        after, raw = read_hags()
        if after is None:
            return False, "写入后无法回读，请到 Windows 设置里确认"
        if after != enable:
            return False, f"写入未生效（当前 HwSchMode={raw}）"
        return True, (
            f"已{'开启' if enable else '关闭'}硬件加速 GPU 计划（HwSchMode {HAGS_OFF if enable else HAGS_ON} → {target}）。"
            "必须重启电脑后才会生效。"
        )
    except PermissionError:
        return False, "权限不足，请以管理员身份重新运行本工具"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


__all__ = [
    "read_hags",
    "describe",
    "needs_enabling",
    "set_hags",
    "is_admin",
    "HAGS_GUI_PATH",
    "HAGS_ON",
    "HAGS_OFF",
]
