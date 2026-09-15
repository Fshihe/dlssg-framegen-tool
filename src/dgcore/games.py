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
from . import gfxapi
from .paths import log

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

MIN_EXE_BYTES = 200 * 1024          # 小于这个体积的基本不是游戏主体
MAX_EXES_EXAMINED = 200             # 单游戏最多解析多少个 EXE（放宽后的默认值）
WALK_MAX_DEPTH = 10                 # 放宽后的默认深度

# 扫描档位：用户嫌慢 / 嫌扫不全时可以切换
SCAN_PRESETS: dict[str, dict[str, int]] = {
    "快速": {"max_exes": 40, "max_depth": 6, "min_bytes": 1024 * 1024, "workers": 8},
    "标准": {"max_exes": 200, "max_depth": 10, "min_bytes": MIN_EXE_BYTES, "workers": 8},
    "彻底": {"max_exes": 600, "max_depth": 16, "min_bytes": 64 * 1024, "workers": 8},
}

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
    # 分级判定（由 gfxapi.judge 填充）
    api_level: str = "unknown"
    api_label: str = "无法判断"
    api_evidence: list[str] = field(default_factory=list)
    api_counter: list[str] = field(default_factory=list)
    scanned_bytes: int = 0

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def directory(self) -> Path:
        return self.path.parent

    @property
    def eligible(self) -> bool:
        """能不能装：必须 64 位，且图形 API 判定为「确认」或「疑似」D3D12。"""
        from .gfxapi import ApiLevel

        if not self.is_x64:
            return False
        return self.api_level in (ApiLevel.CONFIRMED.value, ApiLevel.LIKELY.value)

    @property
    def confidence(self) -> str:
        """给人看的置信度说明。"""
        from .gfxapi import ApiLevel

        return {
            ApiLevel.CONFIRMED.value: "确认",
            ApiLevel.LIKELY.value: "疑似",
            ApiLevel.HINTED.value: "存疑",
            ApiLevel.D3D11_ONLY.value: "不支持",
            ApiLevel.UNKNOWN.value: "未知",
        }.get(self.api_level, "未知")

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


def _iter_exes(root: Path, max_depth: int = WALK_MAX_DEPTH, min_bytes: int = MIN_EXE_BYTES):
    """有界深度遍历游戏目录里的候选 EXE。

    优先返回层次浅、体积大的 —— 游戏主体通常符合这个特征，
    这样命中"早停"时先拿到的是最有希望的候选。
    """
    root = Path(root)
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        cur, depth = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except Exception:
            continue
        # 同层内先处理大文件（更可能是游戏主体）
        files: list[tuple[Path, int]] = []
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if depth + 1 <= max_depth and e.name.lower() not in SKIP_DIRS:
                        stack.append((Path(e.path), depth + 1))
                elif e.is_file(follow_symlinks=False):
                    if not e.name.lower().endswith(".exe"):
                        continue
                    try:
                        size = e.stat().st_size
                    except OSError:
                        continue
                    if size >= min_bytes:
                        files.append((Path(e.path), size))
            except OSError:
                continue
        for p, sz in sorted(files, key=lambda x: -x[1]):
            yield p, sz


def _analyse_exe(path: Path, size: int) -> "ExeCandidate | None":
    """解析单个 EXE 并给出分级判定。供并行调用。"""
    name = path.name
    junk = _is_junk(name)
    if junk and size < 5 * 1024 * 1024:
        return None

    d = path.parent
    try:
        has_dlssg = any((d / f).exists() for f in DLSSG_FILES)
        has_dlss = any((d / f).exists() for f in DLSS_FILES)
        has_ngx = any((d / f).exists() for f in NGX_FILES)
    except OSError:
        has_dlssg = has_dlss = has_ngx = False

    info = pe.inspect(path)

    # 分级判定图形 API
    verdict = gfxapi.judge(
        imports=info.static_imports,
        delayed_imports=info.delayed_imports,
        text_hits=info.text_hits,
        exe_dir=path.parent,
        is_x64=info.is_x64,
    )

    cand = ExeCandidate(
        path=path,
        size=size,
        is_x64=info.is_x64,
        uses_d3d12=verdict.level in (gfxapi.ApiLevel.CONFIRMED, gfxapi.ApiLevel.LIKELY),
        uses_d3d11=any("d3d11" in x.lower() for x in info.imports),
        uses_vulkan=any("vulkan" in x.lower() for x in info.imports),
        has_dlssg=has_dlssg,
        has_dlss=has_dlss,
        has_ngx=has_ngx,
        error=info.error,
        api_level=verdict.level.value,
        api_label=verdict.label,
        api_evidence=list(verdict.evidence),
        api_counter=list(verdict.counter),
        scanned_bytes=info.scanned_bytes,
    )

    s = 0
    r = cand.reasons
    if info.is_x64:
        s += 20
    else:
        s -= 200
        r.append(f"不是 64 位程序（{info.arch}）")

    # 用分级结果的评分驱动排序，并把依据原样带出来给用户看
    if verdict.level == gfxapi.ApiLevel.CONFIRMED:
        s += 100
        r.append(f"D3D12（已确认）：{verdict.summary()}")
    elif verdict.level == gfxapi.ApiLevel.LIKELY:
        s += 70
        r.append(f"D3D12（疑似）：{verdict.summary()}")
    elif verdict.level == gfxapi.ApiLevel.HINTED:
        s += 20
        r.append(f"可能支持 D3D12（存疑）：{verdict.summary()}")
    elif verdict.level == gfxapi.ApiLevel.D3D11_ONLY:
        s -= 120
        r.append("仅 D3D11：本 Mod 只支持 D3D12")
    elif verdict.level == gfxapi.ApiLevel.VULKAN:
        s -= 150
        r.append("Vulkan 渲染：当前版本只支持 D3D12")
    else:
        s -= 60
        r.append("无法判断图形 API")

    for c in verdict.counter:
        r.append(f"（反证）{c}")

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
    return cand


def find_render_exes(
    game_root: str | Path,
    deep: bool = True,
    preset: str = "标准",
    max_exes: int | None = None,
    max_depth: int | None = None,
    on_progress=None,
) -> list[ExeCandidate]:
    """在游戏目录里找出所有可能的渲染 EXE，按可信度打分排序。

    deep       : 兼容参数，False 等价于 preset="快速"
    preset     : 快速 / 标准 / 彻底
    max_exes   : 覆盖档位的单游戏最大解析数
    on_progress: 可选回调 (已处理, 总数)

    性能：
      · PE 解析只读文件头 + 有限段扫描（不再整文件扫描）
      · 多线程并行解析（IO 密集型，线程足够）
      · 按目录层次浅→深、体积大→小顺序处理，命中足够好的候选就早停
    """
    root = Path(game_root)
    out: list[ExeCandidate] = []
    if not root.is_dir():
        return out

    cfg = dict(SCAN_PRESETS.get(preset if deep else "快速", SCAN_PRESETS["标准"]))
    if max_exes is not None:
        cfg["max_exes"] = max_exes
    if max_depth is not None:
        cfg["max_depth"] = max_depth

    # 先收集候选文件（很快，只做 stat）
    found: list[tuple[Path, int]] = []
    for path, size in _iter_exes(root, cfg["max_depth"], cfg["min_bytes"]):
        found.append((path, size))
        if len(found) >= cfg["max_exes"] * 2:  # 留些余量给被过滤掉的
            break

    if not found:
        return out

    total = len(found)
    done = 0

    def work(item):
        return _analyse_exe(item[0], item[1])

    # 并行解析。线程池在这里很合适：瓶颈是文件 IO 而不是 CPU
    workers = min(cfg.get("workers", 8), max(1, total))
    try:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for cand in pool.map(work, found):
                done += 1
                if on_progress and done % 8 == 0:
                    try:
                        on_progress(done, total)
                    except Exception:
                        pass
                if cand is not None:
                    out.append(cand)
    except Exception as exc:
        log(f"并行扫描失败，回退串行：{exc}", "warn")
        for item in found:
            cand = work(item)
            if cand is not None:
                out.append(cand)

    # 截断到上限，但保留最可能的那批
    out.sort(key=lambda c: (-c.score, c.name.lower()))
    return out[: cfg["max_exes"]]


def analyse_one(exe_path: str | Path) -> ExeCandidate:
    """分析单独一个 EXE —— 用户手动指定主程序时用。"""
    p = Path(exe_path)
    try:
        size = p.stat().st_size
    except OSError:
        size = 0
    cand = _analyse_exe(p, size or MIN_EXE_BYTES)
    if cand is None:
        # 即便名字像垃圾程序，手动指定时也要如实返回
        cand = _analyse_exe(p, 5 * 1024 * 1024) or ExeCandidate(path=p, size=size)
    return cand


def find_exe_by_path(path: str | Path) -> tuple[Path | None, str]:
    """用户给了一个路径，判断它是 EXE 还是目录。

    返回 (EXE 路径, 说明)。目录则自动挑最好的候选。
    """
    p = Path(path).expanduser()
    if not p.exists():
        return None, f"路径不存在：{p}"
    if p.is_file():
        if p.suffix.lower() != ".exe":
            return None, f"不是 EXE 文件：{p}"
        return p, "使用手动指定的 EXE"
    # 目录：自动找
    cands = find_render_exes(p)
    ok = [c for c in cands if c.eligible]
    if ok:
        best = max(ok, key=lambda c: c.score)
        return best.path, f"在目录中自动定位到 {best.name}"
    if cands:
        return cands[0].path, f"没找到明确支持的，暂时选 {cands[0].name}"
    return None, "这个目录里没有找到可执行程序"


def inspect_game(game: Game, preset: str = "标准", on_progress=None) -> Game:
    """给一个 Game 填上候选 EXE 列表。"""
    try:
        game.candidates = find_render_exes(game.root, preset=preset, on_progress=on_progress)
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
    "SCAN_PRESETS",
    "scan_steam",
    "scan_epic",
    "scan_gog",
    "scan_libraries",
    "find_render_exes",
    "analyse_one",
    "find_exe_by_path",
    "inspect_game",
    "attach_candidates",
]
