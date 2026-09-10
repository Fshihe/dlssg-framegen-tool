"""游戏发现与"真正的渲染 EXE"定位。

工具要为玩家自动找到该把 DLL 放哪儿，需要两步：
  1. 从 Steam / Epic / GOG 的清单里列出已安装游戏
  2. 在游戏目录里找出实际渲染画面的那个 EXE（D3D12）——这是关键，
     放错目录（比如放到启动器或 Engine 目录）Mod 完全不会生效。

定位靠 PE 导入表分析：谁导入了 d3d12.dll、谁旁边有 nvngx_dlssg.dll，
谁就是目标。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import pe
from .paths import log

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

MIN_EXE_BYTES = 200 * 1024          # 小于这个体积的基本不是游戏主体
MAX_EXES_EXAMINED = 80              # 单游戏最多解析多少个 EXE
WALK_MAX_DEPTH = 7

JUNK_PATTERNS = (
    "unins", "setup", "install", "redist", "vcredist", "dxsetup", "dxwebsetup",
    "crashreport", "crashhandler", "crashpad", "unitycrashhandler", "errorreport",
    "easyanticheat", "battleye", "beservice", "beclient", "gameguard", "xigncode",
    "epicwebhelper", "steamerrorreporter", "cleanup", "activation", "helper",
    "diagnostic", "benchmarktool", "detectarchitecture", "adapter_info", "dxinfo",
    "systeminfo", "storageReader".lower(), "report", "updater", "patcher",
    "prereq", "dotnetfx", "oalinst", "python", "java", "test", "sample",
    "dxr_info", "dxinfo", "adapter_info", "systeminfo",
)

SKIP_DIRS = {
    "jre", "java", "redist", "redistributables", "directx", "vcredist", "dotnet",
    "node_modules", ".git", "__pycache__", "sdk", "python", "engine\\extras",
    "extras", "installers", "installer", "backup", "crashdumps", "logs",
}

# steamapps\common 下这些不是游戏
_NON_GAME_DIRS = {
    "steamworks shared", "steamworks common redistributables", "steamlinuxruntime",
    "steamlinuxruntime_sniper", "steamlinuxruntime_soldier", "proton experimental",
    "steam controller configs", "steamvr", "steamvr drivers",
}

# 旁边有这些文件说明游戏本身支持 DLSS / 帧生成
DLSSG_FILES = ("nvngx_dlssg.dll",)
DLSS_FILES = ("nvngx_dlss.dll",)
NGX_FILES = ("nvngx.dll",)


@dataclass
class ExeCandidate:
    path: Path
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    uses_d3d12: bool = False
    uses_d3d11: bool = False
    uses_vulkan: bool = False
    is_x64: bool = False
    has_dlssg: bool = False
    has_dlss: bool = False
    has_ngx: bool = False
    size: int = 0
    error: str = ""

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def directory(self) -> Path:
        return self.path.parent

    @property
    def eligible(self) -> bool:
        """能不能装：必须 64 位且走 D3D12（本 Mod 仅支持 D3D12）。"""
        return self.is_x64 and self.uses_d3d12

    @property
    def already_installed(self) -> bool:
        return any((self.directory / n).exists() for n in ("dlssg_sm86.ini",))


@dataclass
class Game:
    name: str
    root: Path
    source: str = ""
    appid: str = ""
    candidates: list[ExeCandidate] = field(default_factory=list)
    scan_error: str = ""

    @property
    def best(self) -> ExeCandidate | None:
        ok = [c for c in self.candidates if c.eligible]
        if ok:
            return max(ok, key=lambda c: c.score)
        return self.candidates[0] if self.candidates else None

    @property
    def key(self) -> str:
        return f"{self.source}:{self.appid or self.root.name}"


# --------------------------------------------------------------------------
# 游戏库扫描
# --------------------------------------------------------------------------

def _steam_roots() -> list[Path]:
    """找出所有 Steam 安装位置（注册表 + 常见路径）。"""
    roots: set[Path] = set()
    try:
        import winreg

        for hive, key in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
        ):
            try:
                with winreg.OpenKey(hive, key) as k:
                    for value in ("SteamPath", "InstallPath"):
                        try:
                            p = winreg.QueryValueEx(k, value)[0]
                            if p:
                                roots.add(Path(str(p)))
                        except OSError:
                            pass
            except OSError:
                pass
    except Exception:
        pass

    for guess in (
        r"C:\Program Files (x86)\Steam",
        r"C:\Program Files\Steam",
        r"D:\Steam",
        r"E:\Steam",
        r"D:\SteamLibrary",
        r"E:\SteamLibrary",
        r"F:\SteamLibrary",
    ):
        p = Path(guess)
        if p.is_dir():
            roots.add(p)
    return [r for r in roots if r.is_dir()]


def _parse_vdf_paths(text: str) -> list[Path]:
    """从 libraryfolders.vdf 里抠出所有 path 字段。"""
    out = []
    for m in re.finditer(r'"path"\s*"([^"]+)"', text):
        raw = m.group(1).replace("\\\\", "\\")
        p = Path(raw)
        if p.is_dir():
            out.append(p)
    return out


def scan_steam() -> list[Game]:
    games: list[Game] = []
    seen: set[str] = set()
    for root in _steam_roots():
        steamapps = {root / "steamapps"}
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if vdf.is_file():
            try:
                steamapps.update(p / "steamapps" for p in _parse_vdf_paths(vdf.read_text("utf-8", "ignore")))
            except Exception as exc:
                log(f"解析 {vdf} 失败: {exc}", "warn")
        for sa in steamapps:
            if not sa.is_dir():
                continue
            for acf in sorted(sa.glob("appmanifest_*.acf")):
                try:
                    text = acf.read_text("utf-8", "ignore")
                except Exception:
                    continue
                appid = re.sub(r"\D", "", acf.stem)
                name = (re.search(r'"name"\s+"([^"]*)"', text) or [None, ""])[1]
                installdir = (re.search(r'"installdir"\s+"([^"]*)"', text) or [None, ""])[1]
                if not installdir:
                    continue
                gdir = sa / "common" / installdir
                if not gdir.is_dir():
                    continue
                key = str(gdir).lower()
                if key in seen:
                    continue
                seen.add(key)
                games.append(Game(name=name or installdir, root=gdir, source="Steam", appid=appid))
            # 兜底：common 下存在但缺 appmanifest 的游戏（手动搬运 / 清单损坏）也要能列出来。
            # 注意：不能只在前两层找 EXE —— 悟空这类游戏主程序在 b1\Binaries\Win64\ 第三层。
            common = sa / "common"
            if common.is_dir():
                for child in sorted(common.iterdir()):
                    try:
                        if not child.is_dir() or child.name.startswith("."):
                            continue
                        if child.name.lower() in _NON_GAME_DIRS:
                            continue
                        key = str(child).lower()
                        if key in seen:
                            continue
                        seen.add(key)
                        games.append(
                            Game(name=child.name, root=child, source="Steam（无清单）", appid="")
                        )
                    except OSError:
                        continue
    return games


def scan_epic() -> list[Game]:
    games: list[Game] = []
    manifests = [
        Path(os.environ.get("ProgramData", r"C:\ProgramData"))
        / "Epic" / "UnrealEngineLauncher" / "LauncherInstalled.dat",
        Path(os.environ.get("ProgramData", r"C:\ProgramData"))
        / "Epic" / "EpicGamesLauncher" / "Data" / "LauncherInstalled.dat",
    ]
    for mf in manifests:
        if not mf.is_file():
            continue
        try:
            data = json.loads(mf.read_text("utf-8", "ignore"))
        except Exception:
            continue
        for item in data.get("InstallationList", []) or []:
            loc = item.get("InstallLocation")
            if not loc:
                continue
            p = Path(loc)
            if p.is_dir():
                games.append(
                    Game(name=p.name, root=p, source="Epic", appid=str(item.get("AppName", "")))
                )
        break
    return games


def scan_gog() -> list[Game]:
    games: list[Game] = []
    try:
        import winreg

        for hive, key in (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\GOG.com\Games"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\GOG.com\Games"),
        ):
            try:
                with winreg.OpenKey(hive, key) as root:
                    i = 0
                    while True:
                        try:
                            sub = winreg.EnumKey(root, i)
                        except OSError:
                            break
                        i += 1
                        try:
                            with winreg.OpenKey(root, sub) as gk:
                                path = winreg.QueryValueEx(gk, "path")[0]
                                name = winreg.QueryValueEx(gk, "gameName")[0]
                            p = Path(str(path))
                            if p.is_dir():
                                games.append(Game(name=str(name), root=p, source="GOG", appid=sub))
                        except OSError:
                            continue
            except OSError:
                pass
    except Exception:
        pass
    return games


def scan_libraries(include_generic: bool = True) -> list[Game]:
    """扫描所有能找到的游戏库。"""
    games: list[Game] = []
    for fn in (scan_steam, scan_epic, scan_gog):
        try:
            games.extend(fn())
        except Exception as exc:
            log(f"{fn.__name__} 失败: {exc}", "warn")

    if include_generic:
        games.extend(_scan_generic())

    # 去重
    uniq: dict[str, Game] = {}
    for g in games:
        k = str(g.root).lower()
        if k not in uniq:
            uniq[k] = g
    out = list(uniq.values())
    out.sort(key=lambda g: g.name.lower())
    return out


# 粗扫几个常见自建游戏目录（只扫一层，避免拖慢）
_GENERIC_ROOTS = (
    r"C:\Games", r"D:\Games", r"E:\Games", r"F:\Games",
    r"D:\Program Files\Games", r"E:\Program Files\Games",
    r"D:\PCGames", r"E:\PCGames", r"D:\游戏", r"E:\游戏",
    r"D:\SteamLibrary\steamapps\common", r"E:\SteamLibrary\steamapps\common",
)


def _scan_generic() -> list[Game]:
    out: list[Game] = []
    for gr in _GENERIC_ROOTS:
        base = Path(gr)
        if not base.is_dir():
            continue
        if base.name == "common":  # Steam 库已单独处理
            continue
        try:
            for child in base.iterdir():
                if child.is_dir() and not child.name.startswith("."):
                    out.append(Game(name=child.name, root=child, source="本地目录", appid=""))
        except Exception:
            continue
    return out


# --------------------------------------------------------------------------
# 定位渲染 EXE
# --------------------------------------------------------------------------

def _is_junk(name: str) -> bool:
    low = name.lower()
    return any(p in low for p in JUNK_PATTERNS)


def _iter_exes(root: Path):
    """有界深度遍历游戏目录里的候选 EXE。"""
    root = Path(root)
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        cur, depth = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except Exception:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if depth + 1 <= WALK_MAX_DEPTH and e.name.lower() not in SKIP_DIRS:
                        stack.append((Path(e.path), depth + 1))
                elif e.is_file(follow_symlinks=False):
                    if not e.name.lower().endswith(".exe"):
                        continue
                    try:
                        size = e.stat().st_size
                    except OSError:
                        continue
                    if size >= MIN_EXE_BYTES:
                        yield Path(e.path), size
            except OSError:
                continue


def find_render_exes(game_root: str | Path, deep: bool = True) -> list[ExeCandidate]:
    """在游戏目录里找出所有可能的渲染 EXE，按可信度打分排序。"""
    root = Path(game_root)
    out: list[ExeCandidate] = []
    if not root.is_dir():
        return out

    examined = 0
    for path, size in _iter_exes(root):
        if examined >= MAX_EXES_EXAMINED:
            break
        name = path.name
        junk = _is_junk(name)
        # 垃圾名 + 体积不大 → 不浪费解析时间
        if junk and size < 5 * 1024 * 1024:
            continue
        examined += 1

        d = path.parent
        has_dlssg = any((d / f).exists() for f in DLSSG_FILES)
        has_dlss = any((d / f).exists() for f in DLSS_FILES)
        has_ngx = any((d / f).exists() for f in NGX_FILES)

        info = pe.inspect(path)
        cand = ExeCandidate(
            path=path,
            size=size,
            is_x64=info.is_x64,
            uses_d3d12=info.uses_d3d12,
            uses_d3d11=info.uses_d3d11,
            uses_vulkan=info.uses_vulkan,
            has_dlssg=has_dlssg,
            has_dlss=has_dlss,
            has_ngx=has_ngx,
            error=info.error,
        )

        s = 0
        r = cand.reasons
        if info.is_x64:
            s += 20
        else:
            s -= 200
            r.append(f"不是 64 位程序（{info.arch}）")

        if info.uses_d3d12:
            s += 100
            r.append("导入 d3d12.dll：D3D12 渲染，符合本 Mod 要求")
        elif info.char_hits:
            s += 60
            r.append("代码中动态引用 d3d12：疑似 D3D12")
        else:
            s -= 120
            if info.uses_d3d11 and not info.uses_vulkan:
                r.append("仅 D3D11：本 Mod 只支持 D3D12")
            elif info.uses_vulkan:
                r.append("Vulkan 渲染：当前版本不支持")
            else:
                r.append("未发现 D3D12 引用")

        if has_dlssg:
            s += 50
            r.append("同目录有 nvngx_dlssg.dll：游戏原生支持 DLSS 帧生成")
        if has_dlss:
            s += 30
            r.append("同目录有 nvngx_dlss.dll：支持 DLSS 超分")
        if has_ngx:
            s += 10

        low_path = str(path).lower()
        if "shipping" in name.lower():
            s += 25
            r.append("UE 打包命名（*-Shipping.exe），通常是游戏主程序")
        if "binaries" in low_path and "win64" in low_path:
            s += 20
            r.append("位于 Binaries\\Win64：UE 游戏主体目录")
        if "bin\\x64" in low_path or "bin64" in low_path:
            s += 10

        if junk:
            s -= 60
            r.append("文件名像安装器/辅助程序")

        cand.score = s
        out.append(cand)

    out.sort(key=lambda c: (-c.score, c.name.lower()))
    return out


def inspect_game(game: Game) -> Game:
    """给一个 Game 填上候选 EXE 列表。"""
    try:
        game.candidates = find_render_exes(game.root)
    except Exception as exc:
        game.scan_error = f"{type(exc).__name__}: {exc}"
        log(f"扫描 {game.root} 失败: {exc}", "warn")
    return game


def attach_candidates(games: list[Game], only_d3d12: bool = True) -> list[Game]:
    """批量补全候选 EXE，并（可选）只保留真有 D3D12 主程序的游戏。"""
    result = []
    for g in games:
        inspect_game(g)
        if only_d3d12 and not any(c.eligible for c in g.candidates):
            continue
        result.append(g)
    return result


__all__ = [
    "Game",
    "ExeCandidate",
    "scan_steam",
    "scan_epic",
    "scan_gog",
    "scan_libraries",
    "find_render_exes",
    "inspect_game",
    "attach_candidates",
]
