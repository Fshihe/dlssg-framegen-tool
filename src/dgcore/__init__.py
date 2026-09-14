"""DLSSG 帧生成一键开启工具 —— 核心库（支持 RTX 20 / 30 系列）。

把 https://github.com/sdli1995/dlssg_for_sm86 的代理 DLL + INI
按正确的显卡路由安装到游戏真正的渲染 EXE 目录。

内置两个上游版本，按显卡自动选（版本差异见 profiles.py）：
  RTX 30 系列 → 0.3.0（代理模式，最高 6X）
  RTX 20 系列 → 0.2.4（最后一个支持 SM75 的版本，最高 4X，实验性）

设计原则（安全第一）
--------------------
1. 只写两类位置：用户选定的游戏目录（仅 2 个文件）、%LOCALAPPDATA% 下的工具目录（备份/状态/日志）。
2. 安装/卸载流程绝不写注册表、不装驱动、不改系统设置、不动杀软、不改 PATH。
3. 覆盖任何已有文件前先备份，且备份放在游戏目录之外，永不污染游戏文件夹。
4. 每个落盘文件做 SHA256 校验，校验失败立刻回滚。
5. 全流程可预览（dry-run）、可卸载、可完整还原。
"""

from __future__ import annotations

APP_NAME = "DLSSG 帧生成一键开启工具"
APP_ID = "dlssg-sm86-tool"
VERSION = "1.1.0"

# 上游 Mod 版本（payload 来源）
UPSTREAM_NAME = "DLSSG Native"
UPSTREAM_VERSION = "0.2.4"
UPSTREAM_REPO = "https://github.com/sdli1995/dlssg_for_sm86"

# 我们落盘的两个文件名
INI_NAME = "dlssg_sm86.ini"

# 可用代理入口 -> 上游包内位置
PROXY_ENTRIES = {
    "version.dll": "根目录 version.dll（默认入口）",
    "winmm.dll": "altnative/winmm.dll",
    "dinput8.dll": "altnative/dinput8.dll",
    "winhttp.dll": "altnative/winhttp.dll",
    "dxgi.dll": "altnative/dxgi.dll（仍为 D3D12 管线）",
}

DEFAULT_PROXY = "version.dll"

__all__ = [
    "APP_NAME",
    "APP_ID",
    "VERSION",
    "UPSTREAM_NAME",
    "UPSTREAM_VERSION",
    "UPSTREAM_REPO",
    "INI_NAME",
    "PROXY_ENTRIES",
    "DEFAULT_PROXY",
]
