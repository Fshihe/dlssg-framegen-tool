"""安装 / 校验 / 卸载 / 回滚引擎 —— 本工具的安全核心。

写盘范围被死死限制在：
  A. 用户选定的游戏目录里，恰好 2 个文件：<代理>.dll 与 dlssg_sm86.ini
  B. %LOCALAPPDATA%\\DLSSG-SM86-Tool\\ 下的备份与状态

不写注册表、不装驱动、不改系统设置、不动杀软、不改 PATH。
每一步都先备份、再原子替换、再校验哈希，任何一步失败立即整体回滚。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import DEFAULT_PROXY, INI_NAME, PROXY_ENTRIES, UPSTREAM_VERSION
from . import proc
from . import profiles
from . import state
from . import winenv
from .profiles import is_our_ini
from .gpu import VERDICT_OK, VERDICT_NOT_NEEDED
from .paths import (
    backups_root,
    ensure_dirs,
    exe_dir,
    journal,
    log,
    resource_root,
    state_file,
)
from .payload_manifest import MANIFEST, PAYLOAD_VERSION

# 代理入口优先级：version.dll 是上游默认，其余是备用
PROXY_ORDER = ["version.dll", "winmm.dll", "dinput8.dll", "winhttp.dll", "dxgi.dll"]

# 倍率上限选项（INI 里的 MaxGeneratedFrames 是"最多额外生成几帧"）
#   3 = 4X，2 = 3X，1 = 2X（0.2.4）
#   5 = 6X，4 = 5X，3 = 4X，2 = 3X，1 = 2X（0.3.0）
#
# 注意：这只是**上限**，实际用几倍由游戏决定。游戏只给开关不给选择时，
# 写多少效果都一样 —— 这是评论区里最容易误解的一点。
FRAME_OPTIONS = [
    "6X（上限，仅 0.3.0）",
    "4X（上限）",
    "3X",
    "2X",
]
FRAME_VALUE = {
    "6X（上限，仅 0.3.0）": 5,
    "5X（仅 0.3.0）": 4,
    "4X（上限）": 3,
    "3X": 2,
    "2X": 1,
}

# 按 profile 过滤可选项（0.2.4 最高 4X，0.3.0 才到 6X）
FRAME_VALUES_BY_MAX = {
    4: ["4X（上限）", "3X", "2X"],
    6: ["6X（上限，仅 0.3.0）", "5X（仅 0.3.0）", "4X（上限）", "3X", "2X"],
}


def frame_options_for(profile) -> list[str]:
    return list(FRAME_VALUES_BY_MAX.get(profile.max_multiplier, FRAME_VALUES_BY_MAX[4]))


def frame_option_to_value(text: str, fallback: int = 3) -> int:
    return FRAME_VALUE.get(text, fallback)

TMP_SUFFIX = ".dlssgtool.tmp"

# 动作类型
ACT_BACKUP = "backup"
ACT_COPY = "copy"
ACT_SKIP = "skip"
ACT_REMOVE = "remove"
ACT_RESTORE = "restore"


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def sha256_file(path: str | Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def try_hash(path: str | Path) -> str:
    """读不到就返回空串 —— 文件被游戏独占锁定时读哈希会抛 PermissionError，
    卸载/体检流程绝不能被这个异常打断。"""
    try:
        return sha256_file(path)
    except OSError:
        return ""


def same_content(a: Path, b: Path) -> bool:
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        return sha256_file(a) == sha256_file(b)
    except OSError:
        return False


def sanitize_key(path: str | Path) -> str:
    s = str(path)
    s = re.sub(r"^[A-Za-z]:[\\/]", "", s)
    s = re.sub(r"[^A-Za-z0-9._\u4e00-\u9fff-]+", "_", s)
    return s.strip("_")[:120] or "root"


# --------------------------------------------------------------------------
# payload 解析与校验
# --------------------------------------------------------------------------

@dataclass
class PayloadFile:
    name: str
    path: Path
    sha256: str
    size: int
    ok: bool
    message: str = ""


def payload_dir(version: str | None = None) -> Path:
    """payload 目录。

    结构：payload/<版本>/xxx.dll
    优先用 exe 旁边的（便于用户自行更新），否则用内置解包目录。
    """
    ver = version or profiles.DEFAULT_VERSION

    def pick(base: Path) -> Path | None:
        d = base / "payload" / ver
        if d.is_dir() and any(d.glob("*.dll")):
            return d
        # 兼容旧的扁平结构（payload/xxx.dll）
        flat = base / "payload"
        if flat.is_dir() and any(flat.glob("*.dll")):
            return flat
        return None

    side = pick(exe_dir())
    if side is not None:
        return side
    got = pick(resource_root())
    return got if got is not None else (resource_root() / "payload" / ver)


def resolve_payload(proxy: str, version: str | None = None, profile=None) -> PayloadFile:
    """取出指定代理 DLL 并做完整性校验。

    version 缺省 = 当前默认 profile；20 系会自动走 0.2.4。
    """
    prof = profile or profiles.get(version or profiles.DEFAULT_VERSION)
    name = proxy if proxy.lower().endswith(".dll") else proxy + ".dll"
    path = payload_dir(prof.version) / name
    expect = prof.manifest.get(name)

    if not path.is_file():
        return PayloadFile(name, path, "", 0, False, f"找不到内置 payload：{path}")
    size = path.stat().st_size
    digest = sha256_file(path)
    if expect is None:
        return PayloadFile(
            name, path, digest, size, False,
            f"{name} 不在 {prof.version} 的完整性基线内",
        )
    exp_hash, exp_size = expect
    if size != exp_size:
        return PayloadFile(name, path, digest, size, False, f"体积不符：{size} != {exp_size}")
    if digest != exp_hash:
        return PayloadFile(
            name, path, digest, size, False,
            f"SHA256 不符，文件可能已损坏或被篡改（{digest[:16]}…）",
        )
    return PayloadFile(name, path, digest, size, True, "完整性校验通过")


def available_payloads(version: str | None = None) -> dict[str, bool]:
    prof = profiles.get(version or profiles.DEFAULT_VERSION)
    return {p: resolve_payload(p, profile=prof).ok for p in prof.proxy_names}


def profile_available(version: str) -> tuple[bool, str]:
    """某个 profile 的 payload 是否齐全可用。"""
    prof = profiles.get(version)
    missing = []
    for name in prof.proxy_names:
        r = resolve_payload(name, profile=prof)
        if not r.ok:
            missing.append(f"{name}({r.message})")
    if missing:
        return False, "；".join(missing)
    return True, f"{len(prof.proxy_names)} 个文件齐全"


# --------------------------------------------------------------------------
# INI 生成 —— 委托给 profiles（版本差异只在那里处理）
# --------------------------------------------------------------------------

def build_ini(
    router: str,
    hardware_bilinear: int = 0,
    max_generated_frames: int = 3,
    log_level: int = 1,
    version: str | None = None,
) -> str:
    """按 profile 生成配置。

    version 缺省时按 router 自动选：SM86 → 0.3.0，SM75 → 0.2.4。
    hardware_bilinear 只对 0.2.4 有意义（0.3.0 没有这个开关）。
    """
    prof = profiles.for_router(router, prefer=version)
    frames = max(1, min(prof.max_frames, int(max_generated_frames)))
    return profiles.build_ini(
        prof,
        router=router,
        max_generated_frames=frames,
        log_level=log_level,
        hardware_bilinear=hardware_bilinear,
    )


def read_ini_router(path: str | Path) -> str:
    try:
        text = Path(path).read_text("utf-8", "ignore")
        m = re.search(r"^\s*Router\s*=\s*(\w+)", text, re.M)
        return m.group(1).upper() if m else ""
    except OSError:
        return ""


# --------------------------------------------------------------------------
# 状态存储 —— 实现已移到 state.py（两个引擎共用，且避免循环依赖）
# --------------------------------------------------------------------------

from .state import (          # noqa: E402  (放在这里是为了贴近原有调用位置)
    ENGINE_DLSSG,
    ENGINE_OPTISCALER,
    all_installs,
    engine_of,
    engine_label,
    find_install,
    forget_install,
    load_state,
    other_engine_install,
    record_install,
    save_state,
)


# --------------------------------------------------------------------------
# 预检
# --------------------------------------------------------------------------

@dataclass
class Check:
    level: str      # ok / warn / error
    title: str
    detail: str = ""


@dataclass
class Preflight:
    checks: list[Check] = field(default_factory=list)

    def add(self, level: str, title: str, detail: str = "") -> None:
        self.checks.append(Check(level, title, detail))

    @property
    def errors(self) -> list[Check]:
        return [c for c in self.checks if c.level == "error"]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.level == "warn"]

    @property
    def ok(self) -> bool:
        return not self.errors


def check_write_access(directory: Path) -> tuple[bool, str]:
    """真写一个临时文件来确认有写权限（不猜）。"""
    probe = directory / f".dlssgtool_write_test{TMP_SUFFIX}"
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
        return True, ""
    except Exception as exc:
        try:
            probe.unlink()
        except Exception:
            pass
        return False, f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------
# 外来帧生成 Mod 识别
# --------------------------------------------------------------------------
#
# 这些包会往游戏目录塞代理 DLL，和我们抢同一条渲染路径。用户往往不记得
# 自己装过 —— 所以要在安装前替他认出来，而不是等游戏起不来了再猜。
#
# 判据分两级：
#   1. 特征文件名 —— 只要存在就说明大概率有（libxess_fg / XeSSMFG / fakenvapi ...）
#   2. PE 版本信息里的 OriginalFilename —— OptiScaler 改名后这一项仍是
#      OptiScaler.dll，这是它自己安装脚本的判据，可靠得多

_FOREIGN_MARKERS: tuple[tuple[str, str], ...] = (
    ("libxess_fg.dll", "OptiScaler / XeSS 多帧生成"),
    ("XeSSMFG.dll", "XeSSMFG 多帧生成（外部 loader）"),
    ("OptiScaler.ini", "OptiScaler 的配置文件"),
    ("OptiScaler.dll", "OptiScaler 本体"),
    ("fakenvapi.dll", "fakenvapi（Nukem/OptiScaler 系）"),
    ("dlssg_to_fsr3_amd_is_better.dll", "dlssg_to_fsr3（DLSS→FSR3 插帧）"),
    ("dlssg_to_fsr3_amd_is_better-3-0.dll", "dlssg_to_fsr3 3.0"),
    ("dlss-enabler.dll", "DLSS Enabler"),
    ("dlss-enabler-headless.dll", "DLSS Enabler (headless)"),
    ("nvngx.dll_dlssnr.dll", "DLSS 5 神经网络渲染转发器"),
    ("nvngx_dlssnr.dll", "DLSS 5 神经网络渲染模型"),
    ("DlssOverrides", "OptiScaler DLSS 覆盖目录"),
)

# 这些入口名被改名后靠内容认
_FOREIGN_PROXY_NAMES = (
    "dxgi.dll", "winmm.dll", "version.dll", "dbghelp.dll", "dinput8.dll",
    "d3d12.dll", "wininet.dll", "winhttp.dll",
)


def foreign_mods(target_dir: str | Path) -> list[str]:
    """扫出目录里**第三方**的帧生成/超分 Mod。返回人类可读的条目。

    必须把我们自己装的东西排除掉 —— 否则用户装完 OptiScaler 引擎再来装
    DLSSG，会被提示"检测到别的 Mod"，而那个"别的 Mod"其实是我们自己刚放的。
    这种误导会让人去删根本不该删的文件。

    只报告，不改动任何东西 —— 识别不等于有权限替用户删。
    """
    target_dir = Path(target_dir)
    hits: list[str] = []

    # 先把"属于本工具"的文件算出来
    mine: set[str] = set()
    try:
        from . import optiscaler

        mine |= set(optiscaler.detect_ours(target_dir).keys())
    except Exception:
        pass
    try:
        mine |= set(detect_ours(target_dir).keys())
    except Exception:
        pass

    def is_mine(rel: str) -> bool:
        return rel.replace("\\", "/") in {m.replace("\\", "/") for m in mine}

    for marker, label in _FOREIGN_MARKERS:
        p = target_dir / marker
        if not p.exists():
            continue
        if is_mine(marker):
            continue
        # 目录型标记（DlssOverrides）不是我们建的，单独判断
        if p.is_file() and is_our_file_any(p):
            continue
        hits.append(f"{marker}（{label}）")

    # 按 PE 里的原始文件名认被改名的代理
    try:
        from .optiscaler import _pe_original_filename
    except Exception:
        _pe_original_filename = None  # type: ignore[assignment]

    if _pe_original_filename is not None:
        seen_names = {h.split("（")[0] for h in hits}
        for name in _FOREIGN_PROXY_NAMES:
            if name in seen_names or is_mine(name):
                continue
            p = target_dir / name
            if not p.is_file():
                continue
            if is_our_file_any(p):
                continue
            try:
                orig = _pe_original_filename(p)
            except Exception:
                continue
            low = (orig or "").lower()
            if low == "optiscaler.dll":
                hits.append(f"{name}（实为 OptiScaler 改名，PE 原始名 OptiScaler.dll）")

    return hits


def is_our_file_any(path: str | Path) -> bool:
    """按内容判断这个文件是不是本工具任何一个引擎放的。"""
    p = Path(path)
    if not p.is_file():
        return False
    try:
        digest = try_hash(p)
    except Exception:
        return False
    if not digest:
        return False
    from .payload_manifest import ALL_HASHES

    if digest in ALL_HASHES:
        return True
    try:
        from . import optiscaler

        return digest in optiscaler.all_payload_hashes()
    except Exception:
        return False


def preflight(
    target_dir: Path,
    exe_path: Path | None,
    proxy: str,
    router: str,
    env=None,
    game_name: str = "",
    running_names: list[str] | None = None,
    anticheat=None,
    version: str | None = None,
    coexist: bool = False,
) -> Preflight:
    """安装前把所有风险点查一遍，一条都不让过。

    coexist:
        True 表示允许与 OptiScaler 引擎共存（详见下面第 8.5 条）。
    """
    pf = Preflight()
    target_dir = Path(target_dir)
    prof = profiles.for_router(router, prefer=version)

    # 0. profile 自检：20 系只能用 0.2.4（0.3.0 删了 SM75 内核）
    if (router or "").upper() == "SM75" and not prof.supports_sm75:
        pf.add(
            "error",
            "版本与显卡不匹配",
            f"{prof.version} 不支持 RTX 20 系列（SM75）。\n"
            "程序内部的 profile 选择出现不一致。",
        )
    else:
        pf.add("ok", f"使用上游 {prof.version}", prof.explanation)

    # 1. 目录
    if not target_dir.is_dir():
        pf.add("error", "目标目录不存在", str(target_dir))
        return pf

    # 2. 写权限
    writable, why = check_write_access(target_dir)
    if writable:
        pf.add("ok", "目录可写", str(target_dir))
    else:
        pf.add(
            "error",
            "没有写入权限",
            f"{target_dir}\n{why}\n该目录不可写；Program Files 等受保护位置需要管理员权限。",
        )

    # 3. 磁盘空间（留 200MB 余量）
    try:
        free = shutil.disk_usage(str(target_dir)).free
        need = 200 * 1024 * 1024
        if free < need:
            pf.add("error", "磁盘空间不足", f"剩余 {free/1048576:.0f} MB，本次需要 200 MB")
        else:
            pf.add("ok", "磁盘空间充足", f"剩余 {free/1073741824:.1f} GB")
    except Exception:
        pass

    # 4. 显卡与路由
    if router == "SM86":
        pf.add("ok", "计算路由 SM86", "对应 RTX 30 系列 (Ampere)")
    elif router == "SM75":
        pf.add("ok", "计算路由 SM75", "对应 RTX 20 系列 (Turing)")
        pf.add(
            "warn",
            "20 系是实验性支持",
            "上游从 0.3.0 起已移除 SM75 内核，本工具自动改用 0.2.4（最后一个支持 20 系的版本）。\n"
            "实测可能出现画面闪烁、拖影或退出；卸载会还原安装前的文件。",
        )
    else:
        pf.add("error", "计算路由无法确定", f"检测到的 Router = {router!r}")

    if env is not None:
        g = getattr(env, "primary", None)
        if g is None:
            pf.add("error", "未检测到 NVIDIA 显卡", "本 Mod 依赖 NVIDIA 驱动提供的 NGX/NVAPI/CUDA 接口")
        else:
            if g.verdict == VERDICT_OK:
                pf.add("ok", "显卡受支持", f"{g.name}（{g.family}），驱动 {g.driver or '未知'}")
            elif g.verdict == VERDICT_NOT_NEEDED:
                pf.add("warn", "这台机器的显卡原生支持帧生成", f"{g.name}：{g.reason}")
            else:
                pf.add("error", "显卡不受支持", f"{g.name}：{g.reason}")
            if not g.driver:
                pf.add("warn", "读不到驱动版本", "未能从 NVIDIA 驱动读到版本号")

        # 硬件加速 GPU 计划：帧生成的硬性系统前置条件
        hags = getattr(env, "hags", None)
        if hags is True:
            pf.add("ok", "硬件加速 GPU 计划已开启", "DLSS 帧生成的前置条件满足")
        elif hags is False:
            pf.add(
                "warn",
                "硬件加速 GPU 计划（HAGS）已关闭",
                "DLSS 帧生成硬性依赖这一项。关闭状态下即使 Mod 装好，"
                "游戏也不会提供帧生成选项，并会提示「您的显卡不支持 DLSS 帧生成技术」。\n"
                f"该项位于：{winenv.HAGS_GUI_PATH}（修改后需重启电脑）",
            )
        else:
            pf.add("warn", "读不到硬件加速 GPU 计划状态",
                   "该项是 DLSS 帧生成的前置条件；注册表读数不可用")

    # 5. 目标 EXE
    if exe_path is not None:
        p = Path(exe_path)
        if p.is_file():
            pf.add("ok", "已定位游戏主程序", str(p))
        else:
            pf.add("error", "游戏主程序不存在", str(p))

    # 6. 游戏是否在运行
    #    除了目标 EXE，还要看同目录其他 EXE 和调用方额外给的候选名。
    cand_names = set()
    if exe_path is not None:
        cand_names.add(Path(exe_path).name)
    try:
        cand_names.update(p.name for p in Path(target_dir).glob("*.exe"))
    except OSError:
        pass
    cand_names.update(running_names or [])
    hit = sorted(n for n in cand_names if n and proc.is_running(n))
    if hit:
        pf.add(
            "error",
            "游戏/相关进程正在运行",
            "占用待写入文件的进程：" + "、".join(hit),
        )
    else:
        pf.add("ok", "游戏未在运行")

    # 7. 反作弊
    if anticheat is not None and getattr(anticheat, "risky", False):
        pf.add(
            "error",
            "检测到反作弊组件",
            f"{anticheat.summary}\n向带反作弊的游戏注入 DLL 存在封号风险，本工具默认阻止。",
        )
    elif anticheat is not None:
        pf.add("ok", "未检测到反作弊组件")

    # 8. 代理入口冲突
    dll_name = proxy if proxy.lower().endswith(".dll") else proxy + ".dll"
    dest = target_dir / dll_name
    ini = target_dir / INI_NAME
    prev = find_install(target_dir)
    ours = bool(prev and engine_of(prev) == ENGINE_DLSSG and prev.get("proxy") == dll_name)
    if dest.exists():
        if ours:
            pf.add("ok", "该入口是本工具此前安装的", f"{dll_name}（会先备份再覆盖）")
        else:
            pf.add(
                "warn",
                "目标文件名已被占用",
                f"{dll_name} 已存在，来源不是本工具的安装记录。\n"
                "该文件会先备份到工具目录再被覆盖，卸载时还原。",
            )
    else:
        pf.add("ok", "代理入口可用", dll_name)

    if ini.exists() and not ours:
        pf.add("warn", "已存在 dlssg_sm86.ini", "该文件会先备份到工具目录再被覆盖（当前内容不是本工具生成的）")

    # 8.5 引擎互斥：这个目录是不是已经装了 OptiScaler 引擎
    #      共存模式（coexist）下放行这一对：两者各用各的代理入口
    #      （DLSSG 用 version.dll、OptiScaler 用 dxgi.dll），实测能在同一个进程里
    #      同时工作 —— 帕鲁上跑了 2 分半、黑神话上 3 分钟，都没互相打架。
    _other = other_engine_install(target_dir, ENGINE_DLSSG)
    if _other and coexist and engine_of(_other) == ENGINE_OPTISCALER:
        pf.add(
            "warn",
            "共存模式：本目录同时装有 OptiScaler 引擎",
            "DLSSG 引擎替换 nvngx_dlssg.dll，使游戏自身的 DLSS 帧生成通道产出真实帧；"
            "OptiScaler 以该通道为输入源，倍率由 OptiScaler 决定。\n\n"
            "OptiScaler 一侧的帧生成输入源为 dlssg 时，UE 游戏不会触发"
            "「运动矢量与深度分辨率不一致」；upscaler 输入会触发该冲突，倍率不生效。\n\n"
            "两个引擎各自独立：卸载任意一个都不改动另一个的文件。",
        )
    elif _other:
        pf.add("error", "该目录已安装另一个引擎", state.conflict_message(_other, ENGINE_DLSSG))
    else:
        # 记录可能被清理过，但文件还在 —— 按内容再认一遍。
        # 少了这一步，状态文件一丢，互斥保护就静默失效了。
        try:
            from . import optiscaler

            if optiscaler.detect_ours(target_dir):
                pf.add(
                    "error",
                    "该目录已安装另一个引擎（按文件内容识别）",
                    "按内容识别到本工具装的 OptiScaler 引擎文件，但没有对应的安装记录"
                    "（状态文件可能被清理过）。\n\n"
                    "两个引擎都会从代理 DLL 钩住同一条渲染路径；"
                    "「卸载」会按文件内容识别并清理这些文件。",
                )
        except Exception:
            pass

    # 8.6 外来 Mod：认得出名字的那些，比"文件名被占用"说得更清楚
    _foreign = foreign_mods(target_dir)
    if _foreign:
        pf.add(
            "error",
            "检测到别的帧生成 Mod",
            "这些第三方帧生成/超分 Mod 同样会钩住渲染路径，"
            "与本工具叠加会互相冲突：\n  · " + "\n  · ".join(_foreign),
        )
    else:
        pf.add("ok", "未检测到其他帧生成 Mod")

    # 9. payload 完整性（按当前 profile 校验）
    pl = resolve_payload(dll_name, profile=prof)
    if pl.ok:
        pf.add(
            "ok",
            "内置 DLL 完整性校验通过",
            f"{prof.version}/{dll_name}  SHA256 {pl.sha256[:16]}…",
        )
    else:
        pf.add("error", "内置 DLL 校验失败", pl.message)

    # 10. 目录里是否有别的代理冲突（同一游戏装多个代理会互相打架）
    others = [p for p in prof.proxy_names if p != dll_name and (target_dir / p).exists()]
    if others:
        pf.add(
            "warn",
            "同目录还有其他代理 DLL",
            "这些代理 DLL 位于同一目录：" + "、".join(others)
            + "\n帧生成类代理会与本 Mod 冲突，它们同样会钩住渲染路径。",
        )

    return pf


# --------------------------------------------------------------------------
# 计划（dry-run）
# --------------------------------------------------------------------------

@dataclass
class PlanItem:
    action: str
    name: str
    src: Path | None
    dst: Path
    note: str = ""


@dataclass
class InstallPlan:
    target_dir: Path
    exe_path: Path | None
    proxy: str
    router: str
    items: list[PlanItem] = field(default_factory=list)
    ini_text: str = ""
    game_name: str = ""
    cleanup_proxy: str = ""
    version: str = ""          # 使用的上游 profile 版本

    @property
    def writes(self) -> list[PlanItem]:
        return [i for i in self.items if i.action in (ACT_COPY, ACT_REMOVE)]


def auto_pick_proxy(target_dir: Path, profile=None) -> tuple[str, str]:
    """挑一个没被占用的代理入口。返回 (proxy, 说明)。

    只从当前 profile 支持的入口里挑 —— 0.3.0 没有 winhttp，0.2.4 没有 dbghelp。
    """
    target_dir = Path(target_dir)
    prof = profile or profiles.get(profiles.DEFAULT_VERSION)

    prev = find_install(target_dir)
    if prev and prev.get("proxy") and prev["proxy"] in prof.proxy_names:
        # 继续用之前那个，避免留下孤儿文件
        return prev["proxy"], f"沿用上次安装的入口 {prev['proxy']}"

    for p in prof.proxy_names:
        if not (target_dir / p).exists():
            entry = prof.proxy(p)
            if p == prof.default_proxy:
                return p, f"使用上游默认入口 {p}"
            extra = f"（{entry.note}）" if entry and entry.note else ""
            return p, f"{prof.default_proxy} 已被占用，改用备用入口 {p}{extra}"
    return prof.default_proxy, f"所有入口名都被占用，将覆盖 {prof.default_proxy}（会先备份）"


def make_plan(
    target_dir: Path,
    exe_path: Path | None,
    proxy: str,
    router: str,
    game_name: str = "",
    hardware_bilinear: int = 0,
    max_generated_frames: int = 3,
    log_level: int = 1,
    version: str | None = None,
) -> InstallPlan:
    target_dir = Path(target_dir)
    prof = profiles.for_router(router, prefer=version)
    dll_name = proxy if proxy.lower().endswith(".dll") else proxy + ".dll"

    # 用户指定的入口不在当前 profile 里 → 回退到该 profile 的默认入口
    if dll_name not in prof.proxy_names:
        dll_name = prof.default_proxy

    ini_text = build_ini(
        router, hardware_bilinear, max_generated_frames, log_level, version=prof.version
    )
    plan = InstallPlan(
        target_dir=target_dir,
        exe_path=exe_path,
        proxy=dll_name,
        router=router,
        ini_text=ini_text,
        game_name=game_name,
    )
    plan.version = prof.version

    # 若之前用的是别的代理入口，顺手清理掉旧的那个（会先备份）
    prev = find_install(target_dir)
    if prev and prev.get("proxy") and prev["proxy"] != dll_name:
        old = target_dir / prev["proxy"]
        if old.exists():
            plan.cleanup_proxy = prev["proxy"]
            plan.items.append(
                PlanItem(ACT_REMOVE, prev["proxy"], None, old, "移除本工具上次安装的旧入口")
            )

    pl = resolve_payload(dll_name, profile=prof)
    dst_dll = target_dir / dll_name
    if dst_dll.exists() and pl.ok and same_content(dst_dll, pl.path):
        plan.items.append(PlanItem(ACT_SKIP, dll_name, pl.path, dst_dll, "内容已一致，无需写入"))
    else:
        note = "备份后覆盖" if dst_dll.exists() else "新建"
        plan.items.append(PlanItem(ACT_COPY, dll_name, pl.path, dst_dll, note))

    dst_ini = target_dir / INI_NAME
    if dst_ini.exists() and dst_ini.read_text("utf-8", "ignore") == ini_text:
        plan.items.append(PlanItem(ACT_SKIP, INI_NAME, None, dst_ini, "内容已一致，无需写入"))
    else:
        note = "备份后覆盖" if dst_ini.exists() else "新建"
        plan.items.append(PlanItem(ACT_COPY, INI_NAME, None, dst_ini, note))

    return plan


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------

@dataclass
class InstallResult:
    success: bool
    message: str = ""
    backup_dir: Path | None = None
    installed: list[str] = field(default_factory=list)
    rolled_back: bool = False
    errors: list[str] = field(default_factory=list)


def _backup_dir_for(target_dir: Path) -> Path:
    """每次安装一个独立的备份目录（时间戳 + 短随机后缀），避免互相覆盖。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    uniq = hashlib.sha256(f"{target_dir}{time.time()}{os.getpid()}".encode()).hexdigest()[:6]
    d = backups_root() / sanitize_key(target_dir) / f"{stamp}-{uniq}"
    d.mkdir(parents=True, exist_ok=False)
    return d


def clean_stale_temps(target_dir: Path) -> None:
    """清理上次异常退出留下的临时文件。"""
    try:
        for p in Path(target_dir).glob(f"*{TMP_SUFFIX}"):
            try:
                p.unlink()
            except OSError:
                pass
    except Exception:
        pass


def execute_plan(plan: InstallPlan, dry_run: bool = False) -> InstallResult:
    """执行安装计划。任何一步失败 → 完整回滚，游戏目录回到原样。

    备份语义（重要）：
      只在一个文件"第一次"被本工具覆盖时记录它的原始内容，之后重复安装
      不再覆盖这份原始备份。这样卸载时还原的一定是**安装本工具之前**的样子，
      而不是中间某一版本工具自己的文件。
    """
    target_dir = Path(plan.target_dir)
    res = InstallResult(success=False)

    if dry_run:
        res.success = True
        res.message = "预演完成，未做任何改动"
        return res

    if not target_dir.is_dir():
        res.message = f"目标目录不存在：{target_dir}"
        return res

    clean_stale_temps(target_dir)

    # 继承上一轮记录的"原始文件"备份，避免链式覆盖
    prev = find_install(target_dir)
    originals: dict[str, str] = dict((prev or {}).get("originals") or {})

    backup_dir = _backup_dir_for(target_dir)
    res.backup_dir = backup_dir

    undo: list[tuple[str, Path, Path | None]] = []  # (kind, path, backup)

    def do_backup(src: Path) -> Path:
        dst = backup_dir / src.name
        if dst.exists():  # 同一轮里同名文件只备份一次
            return dst
        shutil.copy2(src, dst)
        if not same_content(src, dst):
            raise RuntimeError(f"备份校验失败：{src}")
        return dst

    def rollback() -> None:
        log("开始回滚", "warn")
        for kind, path, backup in reversed(undo):
            try:
                if kind == "created":
                    if path.exists():
                        path.unlink()
                elif kind in ("replaced", "removed"):
                    if backup and backup.exists():
                        shutil.copy2(backup, path)
            except Exception as exc:
                res.errors.append(f"回滚 {path} 失败：{exc}")
                log(f"回滚 {path} 失败: {exc}", "error")
        res.rolled_back = True

    try:
        installed: list[str] = []

        # 1) 先清理旧入口
        if plan.cleanup_proxy:
            old = target_dir / plan.cleanup_proxy
            if old.exists():
                if plan.cleanup_proxy not in originals:
                    originals[plan.cleanup_proxy] = str(do_backup(old))
                b = Path(originals[plan.cleanup_proxy])
                undo.append(("removed", old, b))
                old.unlink()
                journal("cleanup_old_proxy", path=str(old), backup=str(b))

        # 2) 逐个文件处理
        for item in plan.items:
            if item.action in (ACT_REMOVE, ACT_SKIP):
                if item.action == ACT_SKIP:
                    installed.append(item.name)
                continue

            dst: Path = item.dst
            existed = dst.exists()

            # 写入前再次复核 payload 完整性（防止计划生成后源文件被换掉）
            # 注意：必须按 plan 对应的 profile 取基线 —— 0.2.4 和 0.3.0 的
            # DLL 哈希不同，用错基线会把正常的安装判成"被篡改"。
            if item.src is not None:
                _prof = profiles.get(plan.version) if plan.version else profiles.get(
                    profiles.DEFAULT_VERSION
                )
                base = _prof.manifest.get(item.name)
                if base is None:
                    raise RuntimeError(
                        f"{item.name} 不在 {_prof.version} 的完整性基线内，拒绝安装"
                    )
                got_hash = sha256_file(item.src)
                if got_hash != base[0]:
                    raise RuntimeError(
                        f"{item.name} 源文件完整性校验失败（{got_hash[:12]}… != {base[0][:12]}…），已中止"
                    )

            # 只记录第一次被覆盖时的原始内容
            if existed and item.name not in originals:
                originals[item.name] = str(do_backup(dst))

            tmp = dst.with_name(dst.name + TMP_SUFFIX)
            if item.src is not None:
                shutil.copy2(item.src, tmp)
                expect_hash = sha256_file(item.src)
            else:
                tmp.write_text(plan.ini_text, encoding="utf-8", newline="\n")
                expect_hash = hashlib.sha256(plan.ini_text.encode("utf-8")).hexdigest()

            got = sha256_file(tmp)
            if got != expect_hash:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(f"{item.name} 写入校验失败（{got[:12]}… != {expect_hash[:12]}…）")

            # 原子替换：不会留下写了一半的文件
            os.replace(tmp, dst)

            if sha256_file(dst) != expect_hash:
                raise RuntimeError(f"{item.name} 落盘后校验失败")

            undo.append(("replaced" if existed else "created", dst, Path(originals[item.name]) if item.name in originals else None))
            installed.append(item.name)
            journal(
                "file_installed",
                path=str(dst),
                sha256=expect_hash,
                existed_before=existed,
                original_backup=originals.get(item.name, ""),
            )

        res.installed = installed

        # 3) 记录状态
        rec = {
            "engine": ENGINE_DLSSG,
            "game_name": plan.game_name,
            "target_dir": str(target_dir),
            "exe": str(plan.exe_path) if plan.exe_path else "",
            "proxy": plan.proxy,
            "router": plan.router,
            "files": [
                {
                    "name": n,
                    "sha256": sha256_file(target_dir / n) if (target_dir / n).is_file() else "",
                }
                for n in installed
            ],
            "originals": originals,
            "backup_dir": str(backup_dir),
            "payload_version": plan.version or PAYLOAD_VERSION,
            "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        record_install(rec)

        res.success = True
        res.message = f"已安装 {len(installed)} 个文件到 {target_dir}"
        journal("install_ok", target_dir=str(target_dir), proxy=plan.proxy, router=plan.router)
        return res

    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        log(f"安装失败，回滚：{msg}", "error")
        res.errors.append(msg)
        rollback()
        res.message = f"安装失败，已回滚：{msg}"
        journal("install_failed", target_dir=str(target_dir), error=msg)
        # 回滚成功的话备份目录已无意义
        try:
            if backup_dir.is_dir() and not any(backup_dir.iterdir()):
                backup_dir.rmdir()
                res.backup_dir = None
        except OSError:
            pass
        return res


# --------------------------------------------------------------------------
# 体检
# --------------------------------------------------------------------------

def detect_ours(target_dir: str | Path) -> dict[str, str]:
    """不依赖状态记录，按内置 payload 的 SHA256 识别目录里哪些文件是本工具放的。

    用于状态文件丢失 / 被清理后的恢复，也能识别"孤儿安装"。
    """
    target_dir = Path(target_dir)
    found: dict[str, str] = {}
    for name, (h, size) in MANIFEST.items():
        p = target_dir / name
        try:
            if p.is_file() and p.stat().st_size == size and sha256_file(p) == h:
                found[name] = h
        except OSError:
            continue
    # 只有确认 DLL 是我们的，才连带认下同目录的 INI
    if found and (target_dir / INI_NAME).is_file():
        found[INI_NAME] = ""
    return found


def _running_candidates(target_dir: Path, extra: str = "") -> set[str]:
    names: set[str] = set()
    if extra:
        names.add(Path(extra).name)
    try:
        names.update(p.name for p in Path(target_dir).glob("*.exe"))
    except OSError:
        pass
    return names


@dataclass
class VerifyResult:
    installed: bool
    healthy: bool
    details: list[Check] = field(default_factory=list)
    record: dict | None = None


def verify(target_dir: str | Path) -> VerifyResult:
    """体检：确认文件都在、哈希都对、INI 与当前环境匹配。"""
    target_dir = Path(target_dir)
    out: list[Check] = []
    rec = find_install(target_dir)
    orphan = False
    if not rec:
        detected = detect_ours(target_dir)
        if not detected:
            return VerifyResult(
                False, False, [Check("warn", "没有安装记录", "该目录不是本工具安装的")], None
            )
        orphan = True
        rec = {
            "files": [{"name": n, "sha256": h} for n, h in detected.items()],
            "originals": {},
            "router": "",
            "proxy": next((n for n in detected if n.lower().endswith(".dll")), ""),
        }
        out.append(
            Check("warn", "检测到本工具安装的文件，但没有安装记录",
                  "状态文件可能被清理过。卸载按内置哈希识别这些文件。")
        )

    healthy = True
    for f in rec.get("files", []):
        p = target_dir / f["name"]
        if not p.is_file():
            out.append(Check("error", f"{f['name']} 缺失", "文件不存在，可能被删除或还原"))
            healthy = False
            continue
        digest = try_hash(p)
        if not digest:
            out.append(Check("error", f"{f['name']} 无法读取",
                             "文件可能正被游戏占用"))
            healthy = False
            continue
        exp = f.get("sha256") or ""
        if exp and digest != exp:
            out.append(Check("error", f"{f['name']} 哈希不符", f"当前 {digest[:16]}… 期望 {exp[:16]}…"))
            healthy = False
        elif f["name"].lower().endswith(".dll"):
            # 用记录里写的版本对基线；记录没有就查所有版本
            _ver = (rec or {}).get("payload_version", "")
            base = profiles.get(_ver).manifest.get(f["name"]) if _ver else None
            if base is None:
                from .payload_manifest import ALL_HASHES

                known = digest in ALL_HASHES
                if known:
                    out.append(Check("ok", f"{f['name']} 完好（原始文件）", f"SHA256 {digest[:16]}…"))
                else:
                    out.append(Check("warn", f"{f['name']} 已非原始文件", "可能被更新或被其他程序替换"))
            elif digest != base[0]:
                out.append(Check("warn", f"{f['name']} 已非原始文件", "可能被更新或被其他程序替换"))
            else:
                out.append(Check("ok", f"{f['name']} 完好", f"SHA256 {digest[:16]}…"))
        else:
            out.append(Check("ok", f"{f['name']} 完好", ""))

    ini = target_dir / INI_NAME
    if ini.is_file():
        r = read_ini_router(ini)
        want = rec.get("router", "")
        if want and r and r != want:
            out.append(Check("warn", "INI 路由与安装记录不一致", f"文件里是 {r}，记录是 {want}"))
        else:
            out.append(Check("ok", "INI 路由正确", f"Router={r}" if r else ""))

    # 运行状态提示：文件被占用时卸载会失败
    running = sorted(n for n in _running_candidates(target_dir, rec.get("exe", "")) if proc.is_running(n))
    if running:
        out.append(Check("warn", "游戏正在运行", "文件被占用的进程：" + "、".join(running)))

    return VerifyResult(True, healthy, out, rec)


# --------------------------------------------------------------------------
# 卸载
# --------------------------------------------------------------------------

@dataclass
class UninstallResult:
    success: bool
    message: str = ""
    removed: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)


def uninstall(target_dir: str | Path, force: bool = False) -> UninstallResult:
    """卸载：删掉本工具放的文件，并把**安装前的原始文件**还原回去。

    force=False 时：
      · 游戏在运行 → 直接拒绝（文件被占用，删一半最危险）
      · 只删除哈希与安装记录一致的文件 —— 用户后来自己换过的不动
    还原只按记录里显式的 originals 映射做，绝不会把备份目录里的东西
    一股脑倒回游戏目录。

    删除后会**逐个复核**；只要还有文件没删掉，就报失败并保留记录，
    绝不在留下残留的情况下谎报成功。
    """
    target_dir = Path(target_dir)
    res = UninstallResult(success=False)
    rec = find_install(target_dir)
    synthesized = False

    if not rec:
        detected = detect_ours(target_dir)
        if not detected:
            res.message = "没有该目录的安装记录，未做任何改动"
            return res
        synthesized = True
        rec = {
            "files": [{"name": n, "sha256": h} for n, h in detected.items()],
            "originals": {},
            "router": "",
            "proxy": next((n for n in detected if n.lower().endswith(".dll")), ""),
        }
        res.message = "状态记录已丢失，已按内置 DLL 哈希识别出本工具安装的文件；"

    recorded = {f["name"]: f.get("sha256", "") for f in rec.get("files", [])}
    originals: dict[str, str] = rec.get("originals") or {}

    # 0) 游戏在运行时不要动文件：删一半会让游戏加载到残缺代理
    running = sorted(n for n in _running_candidates(target_dir, rec.get("exe", "")) if proc.is_running(n))
    if running and not force:
        res.message = (
            "游戏/相关进程正在运行，已中止卸载：" + "、".join(running)
            + "。\n运行中删除文件会让游戏加载到残缺的代理 DLL。"
        )
        return res

    # 1) 删除我们放进去的文件（原文件会被后面的还原步骤补回来）
    for name, exp in recorded.items():
        p = target_dir / name
        if not p.is_file():
            continue
        digest = try_hash(p)
        if not digest:
            res.message += f"{name} 无法读取（很可能被游戏占用），未能删除；"
            log(f"读取 {p} 失败，跳过删除", "warn")
            continue
        if exp and digest != exp and not force and name not in originals:
            res.message += f"{name} 已被改动，未删除；"
            log(f"跳过删除 {p}：哈希与记录不符", "warn")
            continue
        if exp and digest != exp and name in originals:
            log(f"{p} 内容已被改动，将由原始备份还原", "warn")
        try:
            p.unlink()
            res.removed.append(name)
            journal("file_removed", path=str(p))
        except Exception as exc:
            res.message += f"删除 {name} 失败：{exc}；"
            log(f"删除 {p} 失败: {exc}", "error")

    # 2) 逐个复核，确认真删掉了 —— 没删掉就不许报成功
    remaining = sorted(n for n in recorded if (target_dir / n).is_file())
    if remaining:
        res.success = False
        res.message += (
            f" 有 {len(remaining)} 个文件仍未能删除：{'、'.join(remaining)}。"
            "这些文件通常被正在运行的进程占用。"
        )
        if not synthesized:
            # 记录保留，下次还能继续卸载
            pass
        journal("uninstall_incomplete", target_dir=str(target_dir), remaining=remaining)
        return res

    # 3) 还原"第一次覆盖前"记录的原始文件
    for name, raw in originals.items():
        b = Path(raw)
        if not b.is_file():
            res.message += f"原始备份已丢失：{name}；"
            continue

        # 防护：如果这份"原始备份"本身就是我们工具生成的产物，
        # 还原它等于把我们的文件又放回去 —— 那不是"恢复原状"。
        # 这种情况出现在：用户反复安装/卸载，或上一次卸载没清干净。
        if name == INI_NAME:
            try:
                text = b.read_text("utf-8", "ignore")
                if is_our_ini(text):
                    journal("skip_restore_own_ini", path=str(b))
                    log(f"备份的 {name} 是本工具生成的，不再还原", "warn")
                    continue
            except OSError:
                pass
        else:
            # 备份的是不是我们自己某个版本的 DLL —— 查全部版本的哈希
            from .payload_manifest import ALL_HASHES

            if try_hash(b) in ALL_HASHES:
                journal("skip_restore_own_dll", path=str(b))
                log(f"备份的 {name} 是本工具的 DLL，不再还原", "warn")
                continue

        dst = target_dir / name
        try:
            shutil.copy2(b, dst)
            res.restored.append(name)
            journal("file_restored", path=str(dst), source=str(b))
        except Exception as exc:
            res.message += f"还原 {name} 失败：{exc}；"

    forget_install(target_dir, ENGINE_DLSSG)
    res.success = True
    res.message = (res.message or "卸载完成") + (
        f" 删除 {len(res.removed)} 个文件"
        + (f"，还原 {len(res.restored)} 个：{'、'.join(res.restored)}" if res.restored else "")
    )

    # 4) 顺手清掉 Mod 运行时自己产生的日志目录（纯运行产物，不含用户数据）
    logdir = target_dir / "dlssg_sm86"
    if logdir.is_dir():
        try:
            leftovers = {p.name.lower() for p in logdir.iterdir()}
            if leftovers <= {"logs"}:
                shutil.rmtree(logdir)
                res.message += "，并清除了 Mod 运行日志目录 dlssg_sm86\\"
                journal("mod_logs_removed", path=str(logdir))
            else:
                res.message += f"，但 {logdir} 里还有非日志内容，已保留"
        except Exception as exc:
            log(f"清理 {logdir} 失败: {exc}", "warn")

    journal("uninstall_ok", target_dir=str(target_dir))
    return res


def restore_backup(backup_dir: str | Path, target_dir: str | Path) -> UninstallResult:
    """从指定备份目录还原到目标目录。"""
    backup_dir = Path(backup_dir)
    target_dir = Path(target_dir)
    res = UninstallResult(success=False)
    if not backup_dir.is_dir():
        res.message = f"备份目录不存在：{backup_dir}"
        return res
    for b in sorted(backup_dir.iterdir()):
        if not b.is_file():
            continue
        dst = target_dir / b.name
        try:
            shutil.copy2(b, dst)
            res.restored.append(b.name)
        except Exception as exc:
            res.message += f"还原 {b.name} 失败：{exc}；"
    res.success = True
    res.message = res.message or f"已还原 {len(res.restored)} 个文件"
    return res


# --------------------------------------------------------------------------
# 引擎调度
# --------------------------------------------------------------------------
#
# 卸载和体检必须按"当初装的是哪个引擎"路由。用户不该关心这件事 ——
# 他只知道"我装过这个工具的东西，现在想卸掉"。
#
# 顺序：先看安装记录；记录丢了就按内容认（两个引擎都支持按哈希识别）。

def engines_present(target_dir: str | Path) -> list[str]:
    """这个目录当前装了哪些引擎 —— 共存模式下可能两个都有。

    先看安装记录，再看文件内容：记录被清理过时，靠内容也能认出来。
    """
    key = state.path_key(target_dir)
    found: set[str] = set()
    for rec in all_installs():
        if (rec.get("target_dir_key") or state.path_key(rec.get("target_dir", ""))) == key:
            found.add(engine_of(rec))

    from . import optiscaler

    if optiscaler.detect_ours(target_dir):
        found.add(ENGINE_OPTISCALER)
    if detect_ours(target_dir):
        found.add(ENGINE_DLSSG)
    return sorted(found)


def engine_present(target_dir: str | Path) -> str:
    """这个目录当前装的是哪个引擎。没装就返回空串。

    共存模式下可能两个都在 —— 这里只返回第一个，要完整清单用 engines_present()。
    """
    found = engines_present(target_dir)
    return found[0] if found else ""


def uninstall_any(target_dir: str | Path, force: bool = False):
    """按引擎路由卸载。

    共存模式下两个引擎可能同时装在同一个目录里，所以这里**两个都卸** ——
    「卸载并还原」的语义就是把这个目录恢复成安装前的样子。
    """
    from . import optiscaler

    which = engines_present(target_dir)
    if not which:
        which = [ENGINE_DLSSG]          # 没有任何记录与痕迹：仍走一次兜底路径

    results = []
    if ENGINE_DLSSG in which:
        res = uninstall(target_dir, force=force)

        # 补一刀：OptiScaler 引擎会往游戏 Engine.ini 写一行配置。如果记录丢了、
        # 或者用户是从 DLSSG 引擎那边点的卸载，那一行没人负责清理，
        # 就会永远留在用户的游戏配置里。这里按标记行兜底清掉。
        if ENGINE_OPTISCALER not in which:
            try:
                from . import ueconfig

                r = ueconfig.strip_orphan(target_dir)
                if r.changed:
                    res.message += f"；{r.detail}"
            except Exception:
                pass
        results.append(res)

    if ENGINE_OPTISCALER in which:
        results.append(optiscaler.uninstall_installed(target_dir, force=force))

    if len(results) == 1:
        return results[0]

    first = results[0]
    first.success = all(r.success for r in results)
    first.message = "；".join(r.message for r in results if r.message)
    for r in results[1:]:
        first.removed.extend(r.removed)
        first.restored.extend(r.restored)
    return first


def verify_any(target_dir: str | Path) -> VerifyResult:
    """按引擎路由体检，统一成 VerifyResult。共存模式下两个引擎都检。"""
    from . import optiscaler

    which = engines_present(target_dir)
    if not which:
        return verify(target_dir)

    parts: list[VerifyResult] = []
    if ENGINE_DLSSG in which:
        parts.append(verify(target_dir))
    if ENGINE_OPTISCALER in which:
        vr = optiscaler.verify_installed(target_dir)
        parts.append(VerifyResult(
            installed=vr.installed,
            healthy=vr.healthy,
            details=[Check(lv, t, d) for lv, t, d in vr.details],
            record=find_install(target_dir, ENGINE_OPTISCALER),
        ))

    if len(parts) == 1:
        return parts[0]

    # 两个都在：合并成一条结果。healthy 只统计真正装了的那部分，
    # 免得"一个装了且健康、另一个没记录"被算成不健康。
    installed_parts = [p for p in parts if p.installed]
    return VerifyResult(
        installed=bool(installed_parts),
        healthy=all(p.healthy for p in installed_parts) if installed_parts else False,
        details=[d for p in parts for d in p.details],
        record=find_install(target_dir),
    )


__all__ = [
    "sha256_file",
    "resolve_payload",
    "available_payloads",
    "build_ini",
    "read_ini_router",
    "preflight",
    "make_plan",
    "execute_plan",
    "auto_pick_proxy",
    "verify",
    "detect_ours",
    "uninstall",
    "restore_backup",
    "foreign_mods",
    "engine_present",
    "engines_present",
    "uninstall_any",
    "verify_any",
    "find_install",
    "all_installs",
    "load_state",
    "state",
    "ENGINE_DLSSG",
    "ENGINE_OPTISCALER",
    "engine_of",
    "engine_label",
    "other_engine_install",
    "Check",
    "Preflight",
    "InstallPlan",
    "PlanItem",
    "InstallResult",
    "VerifyResult",
    "UninstallResult",
    "PROXY_ORDER",
]
