"""反作弊识别 —— 安全护栏。

给游戏目录塞 DLL 代理是"注入"行为。带反作弊的联机游戏可能因此
判定为作弊而导致封号。这不是"损坏电脑"，但代价同样严重，
所以默认拦截，必须用户显式确认才放行。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import proc

# 标记文件名（小写子串） -> 反作弊名称
MARKERS: dict[str, str] = {
    "easyanticheat": "Easy Anti-Cheat",
    "easyanticheat_eos": "Easy Anti-Cheat (EOS)",
    "eac_launcher": "Easy Anti-Cheat",
    "beservice": "BattlEye",
    "beclient": "BattlEye",
    "battleye": "BattlEye",
    "gameguard": "nProtect GameGuard",
    "npggnt": "nProtect GameGuard",
    "npgmup": "nProtect GameGuard",
    "xigncode": "XIGNCODE3",
    "x3.xem": "XIGNCODE3",
    "denuvoanticheat": "Denuvo Anti-Cheat",
    "pnkbstra": "PunkBuster",
    "pnkbstrb": "PunkBuster",
    "pbcl": "PunkBuster",
    "anticheatexpert": "ACE 反作弊（腾讯）",
    "sguard": "ACE 反作弊（腾讯）",
    "ace-base": "ACE 反作弊（腾讯）",
    "ace-game": "ACE 反作弊（腾讯）",
    "tenprotect": "TP 反作弊（腾讯）",
    "blackcipher": "BlackCipher（Nexon）",
    "mhyprot": "mhyprot（米哈游）",
    "wellbia": "Wellbia XIGNCODE",
    "ricochet": "Ricochet（使命召唤）",
    "faceit": "FACEIT AC",
    "equ8": "EQU8",
    "warden": "Warden（暴雪）",
    "fairfight": "FairFight",
}

# 常驻系统服务型反作弊（不在游戏目录里，靠进程名识别）
SERVICE_PROCS: dict[str, str] = {
    "vgc.exe": "Riot Vanguard",
    "vgk.exe": "Riot Vanguard",
    "esetservice.exe": "ESEA Client",
    "faceitclient.exe": "FACEIT AC",
    "battleye.exe": "BattlEye",
    "beservice.exe": "BattlEye",
    "easyanticheat.exe": "Easy Anti-Cheat",
    "easyanticheat_eos.exe": "Easy Anti-Cheat (EOS)",
}

_SKIP_DIRS = {
    "jre",
    "java",
    "node_modules",
    ".git",
    "__pycache__",
    "sdk",
    "python",
    "redist",
    "redistributables",
    "directx",
    "vcredist",
}


@dataclass
class AntiCheatReport:
    found: list[str]
    running: list[str]

    @property
    def risky(self) -> bool:
        return bool(self.found or self.running)

    @property
    def summary(self) -> str:
        items = list(dict.fromkeys(self.found + self.running))
        if not items:
            return "未发现反作弊组件"
        return "检测到：" + "、".join(items)


def scan_directory(root: str | Path, max_entries: int = 60000) -> list[str]:
    """扫描游戏目录里的反作弊组件（目录名/文件名匹配）。"""
    hits: list[str] = []
    seen: set[str] = set()
    root = Path(root)
    if not root.is_dir():
        return hits
    count = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS]
            for token in list(dirnames) + list(filenames):
                count += 1
                if count > max_entries:
                    return hits
                low = token.lower()
                for marker, name in MARKERS.items():
                    if marker in low and name not in seen:
                        seen.add(name)
                        hits.append(name)
    except Exception:
        pass
    return hits


def scan_running() -> list[str]:
    """检查正在运行的反作弊服务进程。"""
    names = proc.list_process_names()
    hits = []
    for exe, label in SERVICE_PROCS.items():
        if exe in names:
            hits.append(label)
    return list(dict.fromkeys(hits))


def scan(root: str | Path | None = None) -> AntiCheatReport:
    found = scan_directory(root) if root else []
    running = scan_running()
    return AntiCheatReport(found=list(dict.fromkeys(found)), running=running)


__all__ = ["AntiCheatReport", "scan", "scan_directory", "scan_running", "MARKERS"]
