"""帧生成解锁工具 —— 核心库。

本工具带**两个互斥的引擎**，同一个游戏目录只能装一个：

  引擎一  DLSSG（默认）
      把 https://github.com/sdli1995/dlssg_for_sm86 的代理 DLL + INI
      按正确的显卡路由安装到游戏真正的渲染 EXE 目录。
        RTX 30 系列 → 0.3.0（代理模式，最高 6X）
        RTX 20 系列 → 0.2.4（最后一个支持 SM75 的版本，最高 4X，实验性）

  引擎二  OptiScaler（XeSS 帧生成 / DLSS 5）
      走 Intel 的 XeSS 帧生成，倍率由本工具直接下发，不依赖游戏是否
      开放了倍率选项。引擎包：
        optiscaler-xess     XeSS 多帧生成（最高 6X）
        optiscaler-dlss5    额外带 DLSS 5 神经网络渲染（实验性）

  为什么互斥：两者都从代理 DLL 钩住 D3D12 渲染路径，同时装上去不是
  "效果差一点"，而是游戏起不来或画面彻底坏掉。所以在预检阶段就硬拦。

设计原则（安全第一）
--------------------
1. 只写两类位置：用户选定的游戏目录、%LOCALAPPDATA% 下的工具目录（备份/状态/日志）。
2. 安装/卸载流程绝不写注册表、不装驱动、不改系统设置、不动杀软、不改 PATH。
   唯一例外是用户明确点了「开启硬件加速 GPU 计划」——那是 Windows 设置里
   同一个开关，且会单独确认。
3. 覆盖任何已有文件前先备份，且备份放在游戏目录之外，永不污染游戏文件夹。
4. 每个落盘文件做 SHA256 校验，校验失败立刻回滚。
5. 卸载按"安装时登记的清单"逐个删、逐个复核；有残留就报失败，绝不谎报成功。
   不在清单里的文件，卸载时一个都不动。
6. 全流程可预览（dry-run）、可卸载、可完整还原。
"""

from __future__ import annotations

APP_NAME = "帧生成解锁工具"
# 数据目录标识保持不变：改了会让用户机器上现有的安装记录、备份、日志全部失联
# （游戏目录里的文件还在，但工具认不出来，卸载会留下残留）。
APP_ID = "dlssg-sm86-tool"
VERSION = "1.3.0"

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
