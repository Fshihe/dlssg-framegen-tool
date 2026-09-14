"""OptiScaler 引擎 —— XeSS 帧生成 / DLSS 5 神经网络渲染。

和 DLSSG 引擎的关系
-------------------
本工具现在带**两个互斥的引擎**，同一个游戏目录只能装一个：

    dlssg        上游 DLSSG Native/代理，走 NVIDIA 的 DLSS 帧生成
    optiscaler   OptiScaler，走 Intel 的 XeSS 帧生成（FGOutput=xefg）

两个都从代理 DLL 钩住渲染路径，同时装上去必然互相打架 ——
所以在预检阶段就硬性拦截，而不是"装上试试看"。

为什么单独一个模块
------------------
DLSSG 引擎的模型是"1 个 DLL + 1 个 INI"，落盘范围是扁平的。
OptiScaler 是"十来个子目录文件 + 生成的 INI"，落盘范围带子树。
把两套塞进同一个函数只会让已验证的 DLSSG 流程变得不可信，
所以这里独立实现，只复用底层原语（哈希、备份根目录、状态库、进程探测）。

安全语义与 DLSSG 完全一致
-------------------------
  · 只写用户选定的游戏目录
  · 覆盖任何已有文件前先备份到游戏目录之外
  · 每个文件落盘后复核 SHA256，任何一步失败整体回滚
  · 卸载逐个复核；有残留就报失败并保留记录，绝不谎报成功
  · 绝不动不在清单里的文件 —— 清单同时就是卸载白名单
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import proc
from .optiscaler_bundles import BUNDLES, GENERATED_AT
from .paths import backups_root, journal, log, payload_base
from .profiles import OUR_INI_MARKER, TOOL_NAME_LINE

ENGINE = "optiscaler"

# 落盘的配置文件名（OptiScaler 认这名字）
INI_NAME = "OptiScaler.ini"

# 随二进制分发的第三方许可，我们不往游戏根目录的 Licenses\ 里掺和，
# 统一收在 D3D12_Optiscaler\ 这个完全属于我们的子树下。
LICENSE_DIR = "D3D12_Optiscaler/Licenses"

# 代理入口候选，按 OptiScaler 自己的兼容性建议排序。
# 内容与文件名无关 —— OptiScaler 会识别自己被改名成什么。
PROXY_CANDIDATES = [
    "dxgi.dll",
    "winmm.dll",
    "version.dll",
    "dbghelp.dll",
    "dinput8.dll",
]

TMP_SUFFIX = ".dlssgtool.tmp"

# 倍率 -> XeFG 插值帧数（InterpolationCount = 插值帧数；N 插值 + 1 真实 = (N+1)X）
#
# v2 构建自带的 INI 模板只写了 1..3（2X..4X），但它的 dxgi.dll 里有完整的
# XeFGUnlock 代码路径，解锁后菜单档位由 MaxInterpolatedFrames 决定。
# 5X/6X 因此标为实验性：写得进去，但能不能真的跑起来取决于 provider。
MULTIPLIERS: tuple[tuple[int, str, bool], ...] = (
    (2, "2X（最稳，不需要解锁）", False),
    (3, "3X", False),
    (4, "4X（包内默认）", False),
    (5, "5X（实验性）", True),
    (6, "6X（实验性）", True),
)

# 超过这个倍率就需要解锁 provider。
#
# 为什么 2X 以上都算"解锁"：XeSS 帧生成的公开能力在非 Ada 显卡上只报 2X，
# 多帧档位（3X 起）是 provider 内部被门控住的。这个 bundle 存在的意义
# 就是那段 XeFGUnlock 代码 —— 所以 3X/4X 是它的正常射程，
# 5X/6X 则超出了它自带模板写明的档位（模板只写到 3 = 4X），更没把握。
UNLOCK_ABOVE = 2
TEMPLATE_MAX_MULT = 4


# --------------------------------------------------------------------------
# bundle 规格
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class BundleSpec:
    key: str
    short: str
    display_name: str
    proxy: str
    ini_rel: str
    template_rel: str
    source_archive: str
    total_bytes: int
    files: tuple[tuple[str, str, int], ...]     # (payload 相对路径, sha256, 大小)

    def file_map(self) -> dict[str, tuple[str, int]]:
        return {rel: (h, size) for rel, h, size in self.files}

    @property
    def ini_template(self) -> str:
        for rel, _h, _s in self.files:
            if rel == self.template_rel:
                return rel
        return ""

    def install_rel(self, payload_rel: str) -> str:
        """payload 内的相对路径 -> 游戏目录内的相对路径。

        只有许可文件被搬家：从根目录的 Licenses\\ 挪到
        D3D12_Optiscaler\\Licenses\\，避免和游戏自带的 Licenses\\ 目录混在一起。
        """
        if payload_rel.startswith("Licenses/"):
            return f"{LICENSE_DIR}/{payload_rel[len('Licenses/'):]}"
        return payload_rel

    def installed_files(self) -> tuple[str, ...]:
        """实际落进游戏目录的文件（不含生成的 INI）。

        模板本身不落盘 —— 它只是我们生成配置的底稿。
        """
        out = []
        for rel, _h, _s in self.files:
            if rel == self.template_rel:
                continue
            out.append(self.install_rel(rel))
        return tuple(out)


def _spec(key: str) -> BundleSpec:
    raw = BUNDLES[key]
    return BundleSpec(
        key=key,
        short=raw["short"],
        display_name=raw["display_name"],
        proxy=raw["proxy"],
        ini_rel=raw["ini_rel"],
        template_rel=raw["template_rel"],
        source_archive=raw.get("source_archive", ""),
        total_bytes=raw.get("total_bytes", 0),
        files=tuple(tuple(f) for f in raw["files"]),
    )


def all_bundles() -> dict[str, BundleSpec]:
    return {k: _spec(k) for k in BUNDLES}


def bundle_keys() -> list[str]:
    return list(BUNDLES)


def get_bundle(key: str) -> BundleSpec | None:
    return _spec(key) if key in BUNDLES else None


def default_bundle() -> str:
    return "optiscaler-xess" if "optiscaler-xess" in BUNDLES else bundle_keys()[0]


def bundle_options() -> list[tuple[str, str]]:
    """给 UI 用的 (key, 显示名) 列表。"""
    return [(k, _spec(k).display_name) for k in BUNDLES]


# --------------------------------------------------------------------------
# payload 定位与校验
# --------------------------------------------------------------------------

def payload_root(key: str | None = None) -> Path:
    return payload_base() / (key or default_bundle())


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    """流式算哈希。

    bundle 里有 158 MB 的文件，read_bytes() 会一次性吃掉内存；
    这里必须走分块读取。
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def try_hash(path: str | Path) -> str:
    """读不到就返回空串 —— 文件被游戏锁定时不能打断卸载流程。"""
    try:
        return sha256_file(path)
    except OSError:
        return ""


def resolve_file(key: str, rel: str) -> tuple[Path, bool, str]:
    """取一个文件并核对基线与体积。"""
    spec = get_bundle(key)
    if spec is None:
        return Path(), False, f"未知 bundle：{key}"
    path = payload_root(key) / rel
    expect = spec.file_map().get(rel)
    if expect is None:
        return path, False, f"{rel} 不在 {key} 的清单内"
    if not path.is_file():
        return path, False, f"找不到内置文件：{path}"
    size = path.stat().st_size
    if size != expect[1]:
        return path, False, f"{rel} 体积不符：{size} != {expect[1]}"
    digest = sha256_file(path)
    if digest != expect[0]:
        return path, False, f"{rel} SHA256 不符（{digest[:16]}…）"
    return path, True, ""


def bundle_available(key: str) -> tuple[bool, str]:
    spec = get_bundle(key)
    if spec is None:
        return False, f"未知 bundle：{key}"
    bad = []
    for rel, _h, _s in spec.files:
        _p, ok, msg = resolve_file(key, rel)
        if not ok:
            bad.append(msg)
    if bad:
        return False, "；".join(bad[:3])
    mb = spec.total_bytes / 1048576
    return True, f"{len(spec.files)} 个文件齐全（{mb:.1f} MB）"


# --------------------------------------------------------------------------
# INI 生成：在 bundle 自带模板上做定点修改
# --------------------------------------------------------------------------
#
# 为什么不从零生成：OptiScaler 的配置有上千行、上百个键，而且键名和取值
# 随构建变化。手写一份"最小配置"等于赌它缺省值永远不变。保留上游模板、
# 只改必须改的那几项，既不会丢键，也不会猜错默认值。

def _set_in_section(text: str, section: str, key: str, value: str) -> str:
    """在指定 [section] 内把 key 设为 value；没有就插到该节末尾。

    必须按节定位 —— 像 Enabled 这种键在 OptiScaler.ini 里出现了十几次，
    全局替换一定会改错地方。
    """
    lines = text.splitlines()
    out: list[str] = []
    cur = ""
    sec_start = -1
    sec_end = -1          # 该节最后一行之后的位置
    key_line = -1

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if cur.lower() == section.lower() and sec_end < 0:
                sec_end = i
            cur = stripped[1:-1].strip()
            if cur.lower() == section.lower():
                sec_start = i
            out.append(line)
            continue
        if cur.lower() == section.lower():
            m = re.match(r"^\s*([A-Za-z0-9_]+)\s*=", line)
            if m and m.group(1).lower() == key.lower():
                key_line = len(out)
        out.append(line)

    if sec_start < 0:
        # 整节都不存在：补一节到文件末尾
        out.append("")
        out.append(f"[{section}]")
        out.append(f"{key}={value}")
        return "\n".join(out) + "\n"

    if key_line >= 0:
        # 保留原有的缩进风格
        orig = out[key_line]
        indent = orig[: len(orig) - len(orig.lstrip())]
        eq = " = " if " = " in orig else "="
        out[key_line] = f"{indent}{key}{eq}{value}"
        return "\n".join(out) + "\n"

    # 键不在该节里：插到该节末尾（sec_end 是本文件里该节之后的第一行）
    at = sec_end if sec_end > 0 else len(out)
    # 往前跳过空行，插在正文后面
    while at > 0 and not out[at - 1].strip():
        at -= 1
    out.insert(at, f"{key}={value}")
    return "\n".join(out) + "\n"


def multiplier_label(mult: int) -> str:
    for m, label, _exp in MULTIPLIERS:
        if m == mult:
            return label
    return f"{mult}X"


def multiplier_options(experimental: bool = True) -> list[tuple[int, str]]:
    return [(m, label) for m, label, exp in MULTIPLIERS if experimental or not exp]


def normalize_multiplier(mult: int) -> int:
    valid = [m for m, _l, _e in MULTIPLIERS]
    if mult in valid:
        return mult
    return min(valid, key=lambda v: abs(v - mult))


def build_ini(key: str, multiplier: int = 4, extra_note: str = "",
              log_level: int | None = 2, high_res_mv: bool | None = True) -> str:
    """生成 OptiScaler.ini：模板 + 定点修改 + 我们自己的署名头。

    log_level:
        0..4  → 打开文件日志并设到该级别（默认 2 = Info）
        None  → 不打开文件日志（跟随上游默认 false）

    high_res_mv:
        True  → XeFG.HighResMV=true
        False → XeFG.HighResMV=false
        None  → 不覆盖（跟随上游 auto）

    为什么要显式设 HighResMV：XeFG 要求运动矢量与深度缓冲的**分辨率一致**。
    UE5 游戏（黑神话就是）常见的情况是深度在渲染分辨率、而 MV 在显示分辨率，
    上游默认的 auto 解析成 false，于是每一帧都报
    "motion vector and depth resource resolutions must match"，
    帧生成完全不生效 —— 而且不报错给用户看，只是"没效果"。

    实测（黑神话基准测试，2560x1440 / 渲染 1708x964）：
        HighResMV=auto/false → 3694 次分辨率不匹配报错，帧生成无效
        HighResMV=true       → 0 报错，XeFG 正常建立并工作
    """
    spec = get_bundle(key)
    if spec is None:
        raise ValueError(f"未知 bundle：{key}")

    tpl_path = payload_root(key) / spec.template_rel
    try:
        text = tpl_path.read_text("utf-8", "ignore")
    except OSError as exc:
        raise RuntimeError(f"读不到配置模板 {tpl_path}：{exc}") from exc

    mult = normalize_multiplier(int(multiplier))
    interp = mult - 1                     # 插值帧数 = 倍率 - 1
    unlock = mult > UNLOCK_ABOVE

    # 帧生成：输入用超分（不要求游戏自带 DLSSG），输出用 XeSS 帧生成
    text = _set_in_section(text, "FrameGen", "Enabled", "true")
    text = _set_in_section(text, "FrameGen", "FGInput", "upscaler")
    text = _set_in_section(text, "FrameGen", "FGOutput", "xefg")
    text = _set_in_section(text, "FrameGen", "FGNvngxReplacement", "None")

    # 倍率与解锁
    text = _set_in_section(text, "XeFG", "InterpolationCount", str(interp))
    text = _set_in_section(text, "XeFG", "UnlockMFG", "true" if unlock else "false")
    text = _set_in_section(text, "XeFG", "MaxInterpolatedFrames", str(interp))

    # 运动矢量分辨率（见上面 docstring：不设的话黑神话这类游戏完全不生效）
    if high_res_mv is not None:
        text = _set_in_section(text, "XeFG", "HighResMV", "true" if high_res_mv else "false")

    if key == "optiscaler-dlss5":
        text = _set_in_section(text, "DlssNr", "Enabled", "true")

    # 让 OptiScaler 不要去找我们没装的 provider（少一次无用探测）
    text = _set_in_section(text, "Plugins", "LoadAsiPlugins", "false")
    text = _set_in_section(text, "Plugins", "LoadSpecialK", "false")
    text = _set_in_section(text, "Plugins", "LoadReshade", "false")

    # 日志：默认落盘。
    # 上游默认是不写日志的 —— 但"装了没效果"是最常见的反馈，没有日志就只能靠猜。
    # 写成固定文件名 OptiScaler.log，卸载时由 clean_runtime_leftovers 一并清掉。
    if log_level is None:
        text = _set_in_section(text, "Log", "LogToFile", "false")
    else:
        lvl = max(0, min(4, int(log_level)))
        text = _set_in_section(text, "Log", "LogToFile", "true")
        text = _set_in_section(text, "Log", "LogLevel", str(lvl))
        text = _set_in_section(text, "Log", "SingleFile", "true")

    header = [
        "; " + "=" * 66,
        f"{TOOL_NAME_LINE}（OptiScaler 引擎）",
        f"; 引擎包：{spec.display_name}",
        f"; 倍率：{mult}X（InterpolationCount={interp}，"
        f"解锁 provider：{'开' if unlock else '关'}）",
        ";",
        "; 本文件是在该 bundle 自带的 OptiScaler.ini 模板上定点修改生成的，",
        "; 上游的其余配置项一律保持原样。卸载时本文件会被删除，",
        "; 如果安装前这里本来就有 OptiScaler.ini，那一份会被还原回来。",
        ";",
    ]
    if extra_note:
        for ln in extra_note.splitlines():
            header.append(f"; {ln}")
    if mult > TEMPLATE_MAX_MULT:
        header.append(";")
        header.append(f"; 注意：{mult}X 超出了该构建自带模板写明的档位（最高 4X）。")
        header.append("; 它是靠运行时给 provider 打补丁解锁的 —— 可能无效、可能画质")
        header.append("; 异常、也可能崩溃。出问题就退回 2X/3X/4X。")
    if key == "optiscaler-dlss5":
        header.append(";")
        header.append("; DLSS 5 神经网络渲染：实验性。所用 nvngx_dlssnr.dll 为未签名、")
        header.append("; 且不在你当前驱动里的预览版组件，请自行判断是否使用。")
    header.append("; " + "=" * 66)
    header.append("")

    # marker 必须在，卸载时靠它判断"这份配置是本工具生成的"
    assert OUR_INI_MARKER in "\n".join(header)

    return "\n".join(header) + text


def is_our_ini(text: str) -> bool:
    return bool(text) and OUR_INI_MARKER in text


def read_multiplier(text: str) -> int:
    """从生成的 INI 里反读倍率（用于体检/展示）。"""
    m = re.search(r"^\s*InterpolationCount\s*=\s*(\d+)", text, re.M)
    if not m:
        return 0
    return normalize_multiplier(int(m.group(1)) + 1)


def read_engine_summary(text: str) -> dict[str, str]:
    """从署名头里读回我们写进去的信息。"""
    info: dict[str, str] = {}
    for line in text.splitlines()[:14]:
        s = line.strip()
        if not s.startswith(";"):
            continue
        body = s.lstrip("; ").strip()
        for label, field in (("引擎包：", "bundle"), ("倍率：", "multiplier")):
            if body.startswith(label):
                info[field] = body[len(label):].strip()
    return info


# --------------------------------------------------------------------------
# 代理入口选择
# --------------------------------------------------------------------------

_PAYLOAD_HASHES: dict[str, str] = {}


def all_payload_hashes() -> dict[str, str]:
    """全部 bundle 的 sha256 -> 安装后相对路径。用于"这文件是不是我们放的"。"""
    if not _PAYLOAD_HASHES:
        for spec in all_bundles().values():
            for rel, h, _size in spec.files:
                if rel == spec.template_rel:
                    continue
                _PAYLOAD_HASHES.setdefault(h, spec.install_rel(rel))
    return _PAYLOAD_HASHES


def is_our_file(path: str | Path) -> bool:
    """按内容判断是不是本工具放进来的文件。

    不能只看文件名 —— OptiScaler 的代理会被改名，我们的文件也一样。
    按哈希认，改名了也认得出来。
    """
    p = Path(path)
    if not p.is_file():
        return False
    h = try_hash(p)
    return bool(h) and h in all_payload_hashes()


def pick_proxy(target_dir: Path, bundle_key: str, prev_proxy: str = "") -> tuple[str, str]:
    """挑一个合适的代理入口名。

    关键点：如果某个候选名已经被**我们自己**上次安装的文件占着，
    要沿用那个名字，而不是跳到下一个 —— 否则每装一次就多留一个孤儿代理，
    几个代理同时钩渲染路径必然出事。
    """
    target_dir = Path(target_dir)
    spec = get_bundle(bundle_key)
    ship_name = spec.proxy if spec else "dxgi.dll"

    if prev_proxy and prev_proxy.lower().endswith(".dll"):
        return prev_proxy, f"沿用上次安装的入口 {prev_proxy}"

    for name in PROXY_CANDIDATES:
        p = target_dir / name
        if not p.exists():
            if name.lower() == ship_name.lower():
                return name, f"使用 OptiScaler 推荐入口 {name}"
            return name, f"{ship_name} 已被占用，改用 {name}（内容相同，OptiScaler 按内容识别自己）"
        if is_our_file(p):
            return name, f"沿用本工具上次安装的入口 {name}"

    # 全被别的文件占用：退而覆盖推荐入口（会先备份）
    return ship_name, f"所有代理入口名都被占用，将覆盖 {ship_name}（会先备份）"


def detect_ours(target_dir: str | Path) -> dict[str, str]:
    """按内容扫出这个目录里**本工具装过**的文件。

    用于状态记录丢失后的恢复，以及清理"上次装到另一个代理名下"的孤儿。
    返回 {安装后相对路径: sha256}。
    """
    target_dir = Path(target_dir)
    known = all_payload_hashes()
    found: dict[str, str] = {}

    # 1) 代理入口：名字会是候选之一
    for name in PROXY_CANDIDATES + ["d3d12.dll", "winhttp.dll", "wininet.dll"]:
        p = target_dir / name
        if not p.is_file():
            continue
        h = try_hash(p)
        if h and h in known:
            found[name] = h

    # 2) 固定路径的伴随文件（D3D12_Optiscaler\ 子树等）
    for spec in all_bundles().values():
        for rel, h, _size in spec.files:
            if rel == spec.template_rel:
                continue
            p = target_dir / spec.install_rel(rel)
            if p.is_file() and try_hash(p) == h:
                found[spec.install_rel(rel)] = h

    # 3) 生成的 INI：只有确认其它文件是我们的，才连带认它
    if found:
        ini = target_dir / INI_NAME
        if ini.is_file():
            try:
                if is_our_ini(ini.read_text("utf-8", "ignore")):
                    found[INI_NAME] = try_hash(ini)
            except OSError:
                pass

    return found


def orphan_proxies(target_dir: str | Path, keep: str) -> list[str]:
    """找出上次装在别的代理名下、这次不再需要的入口文件。"""
    target_dir = Path(target_dir)
    out = []
    for name in PROXY_CANDIDATES + ["d3d12.dll", "winhttp.dll", "wininet.dll"]:
        if name == keep:
            continue
        p = target_dir / name
        if p.is_file() and is_our_file(p):
            out.append(name)
    return out


# --------------------------------------------------------------------------
# 外来 OptiScaler 检测
# --------------------------------------------------------------------------
#
# OptiScaler 的 DLL 被改名后，PE 里的 OriginalFilename 仍然是 OptiScaler.dll。
# 按这个特征认，比按文件名猜可靠得多 —— 这也是它自己安装脚本的判据。

_KNOWN_OPTISCALER_FILES = ("libxess_fg.dll", "OptiScaler.ini", "fakenvapi.dll")


def detect_foreign(target_dir: str | Path) -> dict[str, object]:
    """看看这个目录里是不是已经有一份**不是本工具装的** OptiScaler。"""
    target_dir = Path(target_dir)
    hits: list[str] = []
    marked: list[str] = []

    for name in PROXY_CANDIDATES + ["d3d12.dll", "winhttp.dll", "wininet.dll",
                                    "OptiScaler.dll", "OptiScaler.asi"]:
        p = target_dir / name
        if not p.is_file():
            continue
        hits.append(name)
        try:
            if _pe_original_filename(p).lower() == "optiscaler.dll":
                marked.append(name)
        except Exception:
            pass

    extras = [n for n in _KNOWN_OPTISCALER_FILES if (target_dir / n).is_file()]
    return {
        "proxies": hits,
        "confirmed": marked,
        "extras": extras,
        "found": bool(marked or extras),
    }


def _pe_original_filename(path: Path) -> str:
    """从 PE 资源里读 OriginalFilename（纯 stdlib，不依赖 pywin32）。"""
    import ctypes
    from ctypes import wintypes

    ver = ctypes.WinDLL("version", use_last_error=True)
    ver.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    ver.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    ver.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                        wintypes.DWORD, ctypes.c_void_p]
    ver.GetFileVersionInfoW.restype = wintypes.BOOL
    ver.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                   ctypes.POINTER(ctypes.c_void_p),
                                   ctypes.POINTER(wintypes.UINT)]
    ver.VerQueryValueW.restype = wintypes.BOOL

    size = ver.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return ""
    buf = ctypes.create_string_buffer(size)
    if not ver.GetFileVersionInfoW(str(path), 0, size, buf):
        return ""
    # 语言/代码页要枚举才准，这里走固定的翻译表
    for lang in ("040904b0", "040904e4", "080404b0", "000004b0"):
        sub = f"\\StringFileInfo\\{lang}\\OriginalFilename"
        val = ctypes.c_void_p()
        ln = wintypes.UINT()
        if ver.VerQueryValueW(buf, sub, ctypes.byref(val), ctypes.byref(ln)) and val.value:
            return ctypes.wstring_at(val.value)
    return ""


# --------------------------------------------------------------------------
# 安装计划
# --------------------------------------------------------------------------

@dataclass
class OptiPlanItem:
    action: str                 # copy / remove / skip
    rel: str                    # 游戏目录内的相对路径（POSIX 风格）
    src: Path | None
    note: str = ""


@dataclass
class OptiPlan:
    target_dir: Path
    exe_path: Path | None
    bundle: str
    proxy: str
    multiplier: int
    items: list[OptiPlanItem] = field(default_factory=list)
    ini_text: str = ""
    game_name: str = ""
    cleanup: list[str] = field(default_factory=list)   # 上一轮安装、这轮不再需要的相对路径

    @property
    def writes(self) -> list[OptiPlanItem]:
        return [i for i in self.items if i.action in ("copy", "remove")]

    @property
    def total_bytes(self) -> int:
        spec = get_bundle(self.bundle)
        return spec.total_bytes if spec else 0


def make_plan(
    target_dir: str | Path,
    exe_path: Path | None,
    bundle: str,
    multiplier: int = 4,
    game_name: str = "",
    prev_proxy: str = "",
    high_res_mv: bool | None = True,
) -> OptiPlan:
    target_dir = Path(target_dir)
    spec = get_bundle(bundle)
    if spec is None:
        raise ValueError(f"未知 bundle：{bundle}")

    proxy, _why = pick_proxy(target_dir, bundle, prev_proxy)
    mult = normalize_multiplier(multiplier)
    ini_text = build_ini(bundle, mult, high_res_mv=high_res_mv)

    plan = OptiPlan(
        target_dir=target_dir,
        exe_path=exe_path,
        bundle=bundle,
        proxy=proxy,
        multiplier=mult,
        ini_text=ini_text,
        game_name=game_name,
    )

    # 把 bundle 里的代理文件按选定的入口名落盘
    ship_proxy = spec.proxy
    for rel, _h, _s in spec.files:
        if rel == spec.template_rel:
            continue
        target_rel = proxy if rel == ship_proxy else spec.install_rel(rel)
        src = payload_root(bundle) / rel
        dst = target_dir / target_rel
        if dst.is_file() and dst.stat().st_size == _s and _same(src, dst):
            plan.items.append(OptiPlanItem("skip", target_rel, src, "内容已一致，无需写入"))
        else:
            plan.items.append(
                OptiPlanItem("copy", target_rel, src,
                             "备份后覆盖" if dst.exists() else "新建")
            )

    # 生成的 INI
    ini_dst = target_dir / INI_NAME
    if ini_dst.is_file() and ini_dst.read_text("utf-8", "ignore") == ini_text:
        plan.items.append(OptiPlanItem("skip", INI_NAME, None, "内容已一致，无需写入"))
    else:
        plan.items.append(
            OptiPlanItem("copy", INI_NAME, None,
                         "备份后覆盖" if ini_dst.exists() else "新建")
        )

    # 上次装在别的代理名下的孤儿入口：这次顺手清掉（会先备份）。
    # 不清理的话，两个代理同时钩渲染路径必然冲突 —— 而且用户根本看不出来。
    plan.cleanup = orphan_proxies(target_dir, proxy)

    return plan


def _same(a: Path, b: Path) -> bool:
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        return sha256_file(a) == sha256_file(b)
    except OSError:
        return False


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------

@dataclass
class OptiResult:
    success: bool
    message: str = ""
    backup_dir: Path | None = None
    installed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    rolled_back: bool = False
    errors: list[str] = field(default_factory=list)
    # 安装后完整的"安装前原始文件"映射（rel -> 备份路径）。
    # 必须回传给调用方写进状态记录 —— 否则卸载时不知道要还原什么。
    originals: dict[str, str] = field(default_factory=dict)


def _flat(rel: str) -> str:
    """把相对路径压成备份目录里的安全文件名。"""
    return rel.replace("/", "__").replace("\\", "__")


def _backup_dir_for(target_dir: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    uniq = hashlib.sha256(f"{target_dir}{time.time()}{os.getpid()}".encode()).hexdigest()[:6]
    safe = re.sub(r"[^A-Za-z0-9._\u4e00-\u9fff-]+", "_", str(target_dir).lstrip("\\/"))[:120]
    d = backups_root() / (safe or "root") / f"{stamp}-{uniq}"
    d.mkdir(parents=True, exist_ok=False)
    return d


def execute_plan(plan: OptiPlan, dry_run: bool = False, originals: dict[str, str] | None = None) -> OptiResult:
    """执行安装。任何一步失败 -> 完整回滚。

    originals 是"安装前原始文件"的映射（rel -> 备份路径），由调用方从状态记录
    里带进来，保证卸载还原的是**安装本工具之前**的样子，而不是中间某一版。
    """
    target_dir = Path(plan.target_dir)
    res = OptiResult(success=False)

    if dry_run:
        res.success = True
        res.message = "预演完成，未做任何改动"
        return res

    if not target_dir.is_dir():
        res.message = f"目标目录不存在：{target_dir}"
        return res

    for p in target_dir.glob(f"*{TMP_SUFFIX}"):
        try:
            p.unlink()
        except OSError:
            pass

    originals = dict(originals or {})
    backup_dir = _backup_dir_for(target_dir)
    res.backup_dir = backup_dir

    undo: list[tuple[str, Path, Path | None]] = []
    made_dirs: list[Path] = []

    def ensure_dir(d: Path) -> None:
        if d.is_dir():
            return
        d.mkdir(parents=True, exist_ok=True)
        made_dirs.append(d)

    def do_backup(rel: str, src: Path) -> Path:
        dst = backup_dir / _flat(rel)
        if dst.exists():
            return dst
        shutil.copy2(src, dst)
        if not _same(src, dst):
            raise RuntimeError(f"备份校验失败：{src}")
        return dst

    def rollback() -> None:
        log("OptiScaler 安装失败，开始回滚", "warn")
        for kind, path, backup in reversed(undo):
            try:
                if kind == "created":
                    path.unlink(missing_ok=True)
                elif kind in ("replaced", "removed"):
                    if backup and backup.is_file():
                        path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(backup, path)
            except Exception as exc:
                res.errors.append(f"回滚 {path} 失败：{exc}")
        for d in sorted(made_dirs, key=lambda x: -len(x.parts)):
            try:
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                pass
        res.rolled_back = True

    spec = get_bundle(plan.bundle)
    if spec is None:
        res.message = f"未知 bundle：{plan.bundle}"
        return res
    baseline = spec.file_map()

    try:
        installed: list[str] = []

        # 1) 先清掉上一轮装过、这一轮不再需要的入口文件
        for rel in plan.cleanup:
            p = target_dir / rel
            if not p.is_file():
                continue
            if rel not in originals:
                originals[rel] = str(do_backup(rel, p))
            undo.append(("removed", p, Path(originals[rel])))
            p.unlink()
            journal("opti_cleanup", path=str(p))

        # 2) 逐个文件处理
        for item in plan.items:
            if item.action == "remove":
                continue
            dst = target_dir / item.rel

            if item.action == "skip":
                installed.append(item.rel)
                continue

            # 写入前复核源文件（防计划生成后被换掉）
            if item.src is not None:
                want = baseline.get(item.src.relative_to(payload_root(plan.bundle)).as_posix())
                if want is None:
                    raise RuntimeError(f"{item.rel} 不在 {plan.bundle} 的完整性基线内，拒绝安装")
                got = sha256_file(item.src)
                if got != want[0]:
                    raise RuntimeError(
                        f"{item.rel} 源文件校验失败（{got[:12]}… != {want[0][:12]}…），已中止"
                    )

            ensure_dir(dst.parent)
            existed = dst.is_file()

            # 只记录第一次被覆盖时的原始内容
            if existed and item.rel not in originals:
                originals[item.rel] = str(do_backup(item.rel, dst))

            tmp = dst.with_name(dst.name + TMP_SUFFIX)
            if item.src is not None:
                shutil.copy2(item.src, tmp)
                expect = sha256_file(item.src)
            else:
                tmp.write_text(plan.ini_text, encoding="utf-8", newline="\n")
                expect = hashlib.sha256(plan.ini_text.encode("utf-8")).hexdigest()

            got = sha256_file(tmp)
            if got != expect:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(f"{item.rel} 写入校验失败（{got[:12]}… != {expect[:12]}…）")

            os.replace(tmp, dst)

            if sha256_file(dst) != expect:
                raise RuntimeError(f"{item.rel} 落盘后校验失败")

            undo.append((
                "replaced" if existed else "created",
                dst,
                Path(originals[item.rel]) if item.rel in originals else None,
            ))
            installed.append(item.rel)

        res.installed = installed
        res.originals = dict(originals)
        res.success = True
        res.message = f"已安装 {len(installed)} 个文件到 {target_dir}"
        return res

    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        log(f"OptiScaler 安装失败，回滚：{msg}", "error")
        res.errors.append(msg)
        rollback()
        res.message = f"安装失败，已回滚：{msg}"
        try:
            if backup_dir.is_dir() and not any(backup_dir.iterdir()):
                backup_dir.rmdir()
                res.backup_dir = None
        except OSError:
            pass
        return res


# --------------------------------------------------------------------------
# 卸载
# --------------------------------------------------------------------------

def uninstall(target_dir: str | Path, record: dict, force: bool = False) -> OptiResult:
    """删掉本工具装的每一个文件，并还原安装前的原始文件。

    删除后逐个复核；只要还有残留就报失败并保留记录。
    """
    target_dir = Path(target_dir)
    res = OptiResult(success=False)

    recorded = {f["name"]: f.get("sha256", "") for f in record.get("files", [])}
    originals: dict[str, str] = record.get("originals") or {}
    proxy = record.get("proxy", "")

    if not recorded:
        res.message = "该目录没有 OptiScaler 引擎的安装记录，未做任何改动"
        return res

    # 游戏在运行时不要动文件
    names = {Path(record.get("exe", "")).name} if record.get("exe") else set()
    try:
        names.update(p.name for p in target_dir.glob("*.exe"))
    except OSError:
        pass
    running = sorted(n for n in names if n and proc.is_running(n))
    if running and not force:
        res.message = (
            "游戏/相关进程正在运行，已中止卸载：" + "、".join(running)
            + "。\n请完全退出游戏后重试 —— 运行中删除文件可能让游戏加载到残缺的代理 DLL。"
        )
        return res

    for rel, exp in recorded.items():
        p = target_dir / rel
        if not p.is_file():
            continue
        try:
            digest = try_hash(p)
        except OSError:
            res.message += f"{rel} 无法读取（可能被占用），未能删除；"
            continue
        if not digest:
            res.message += f"{rel} 无法读取（可能被占用），未能删除；"
            continue
        if exp and digest != exp and not force and rel not in originals:
            res.message += f"{rel} 已被改动，为安全起见未删除；"
            continue
        try:
            p.unlink()
            res.removed.append(rel)
        except Exception as exc:
            res.message += f"删除 {rel} 失败：{exc}；"

    remaining = sorted(n for n in recorded if (target_dir / n).is_file())
    if remaining:
        res.message += (
            f" 有 {len(remaining)} 个文件仍未能删除：{'、'.join(remaining[:6])}。"
            "通常是游戏正在运行占用了文件，请完全退出游戏后再次卸载。"
        )
        journal("opti_uninstall_incomplete", target_dir=str(target_dir), remaining=remaining)
        return res

    # 还原原始文件
    for rel, raw in originals.items():
        b = Path(raw)
        if not b.is_file():
            res.message += f"原始备份已丢失：{rel}；"
            continue
        if rel == INI_NAME:
            try:
                if is_our_ini(b.read_text("utf-8", "ignore")):
                    journal("opti_skip_restore_own_ini", path=str(b))
                    continue
            except OSError:
                pass
        dst = target_dir / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(b, dst)
            res.restored.append(rel)
        except Exception as exc:
            res.message += f"还原 {rel} 失败：{exc}；"

    # 清掉我们建的空目录（只删空的，游戏自己的内容一律不动）
    cleaned: list[str] = []
    for rel in (LICENSE_DIR, "D3D12_Optiscaler"):
        d = target_dir / rel
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
                cleaned.append(rel)
        except OSError:
            pass

    res.success = True
    res.message = (res.message or "卸载完成") + (
        f" 删除 {len(res.removed)} 个文件"
        + (f"，还原 {len(res.restored)} 个" if res.restored else "")
        + (f"，清除空目录 {len(cleaned)} 个" if cleaned else "")
    )
    journal("opti_uninstall_ok", target_dir=str(target_dir), proxy=proxy)
    return res


# --------------------------------------------------------------------------
# 体检
# --------------------------------------------------------------------------

@dataclass
class OptiVerify:
    installed: bool
    healthy: bool
    details: list[tuple[str, str, str]] = field(default_factory=list)


def verify(target_dir: str | Path, record: dict) -> OptiVerify:
    target_dir = Path(target_dir)
    out: list[tuple[str, str, str]] = []
    healthy = True

    for f in record.get("files", []):
        rel = f["name"]
        p = target_dir / rel
        if not p.is_file():
            out.append(("error", f"{rel} 缺失", "文件不存在，可能被删除或还原"))
            healthy = False
            continue
        try:
            digest = try_hash(p)
        except OSError:
            out.append(("error", f"{rel} 无法读取", "可能正被游戏占用"))
            healthy = False
            continue
        if not digest:
            out.append(("error", f"{rel} 无法读取", "可能正被游戏占用"))
            healthy = False
            continue
        exp = f.get("sha256") or ""
        if exp and digest != exp:
            # OptiScaler 每次运行都会把自己的配置回写一遍（它会把当前生效值落盘），
            # 所以生成的那份 INI 哈希必然变化。只要署名还在，就说明还是我们那份，
            # 不该当成异常来吓用户。
            if rel == INI_NAME:
                try:
                    if is_our_ini(p.read_text("utf-8", "ignore")):
                        out.append(("ok", f"{rel} 完好（引擎运行时回写过，仍是本工具生成的）", ""))
                        continue
                except OSError:
                    pass
            out.append(("warn", f"{rel} 已非原始文件", f"当前 {digest[:16]}…"))
        else:
            out.append(("ok", f"{rel} 完好", ""))

    ini = target_dir / INI_NAME
    if ini.is_file():
        text = ini.read_text("utf-8", "ignore")
        if is_our_ini(text):
            info = read_engine_summary(text)
            mult = read_multiplier(text)
            out.append(("ok", "配置是本工具生成的",
                        f"{info.get('bundle', '')}  倍率 {mult}X" if mult else info.get("bundle", "")))
        else:
            out.append(("warn", "OptiScaler.ini 不是本工具生成的",
                        "可能被别的工具或你手动改过"))

    names = {Path(record.get("exe", "")).name} if record.get("exe") else set()
    try:
        names.update(p.name for p in target_dir.glob("*.exe"))
    except OSError:
        pass
    running = sorted(n for n in names if n and proc.is_running(n))
    if running:
        out.append(("warn", "游戏正在运行", "请退出游戏后再卸载：" + "、".join(running)))

    return OptiVerify(True, healthy, out)


# --------------------------------------------------------------------------
# 运行时产物清理
# --------------------------------------------------------------------------
#
# OptiScaler 会在游戏目录旁写日志。卸载时顺手清掉"只有我们产物"的目录，
# 但里面只要有游戏自己的东西就一律保留。

# 运行时会写日志的文件名（clean_runtime_leftovers 用通配匹配，这里只作文档说明）
_RUNTIME_LOGS = ("OptiScaler.log", "fakenvapi.log", "dlssg_to_fsr3.log")


def clean_runtime_leftovers(target_dir: str | Path) -> list[str]:
    """清掉引擎运行时自己产生的日志/空目录。

    只删日志和"只有我们产物"的空目录 —— 里面但凡有游戏自己的东西就一律保留。
    卸载后游戏目录必须回到安装前的样子，这些运行产物也算残留。
    """
    target_dir = Path(target_dir)
    gone: list[str] = []

    try:
        candidates = list(target_dir.glob("OptiScaler*.log"))
        candidates += list(target_dir.glob("fakenvapi*.log"))
        candidates += list(target_dir.glob("dlssg_to_fsr3*.log"))
        candidates += list(target_dir.glob("XeSSMFG*.log"))
    except OSError:
        candidates = []

    for p in candidates:
        try:
            if p.is_file():
                p.unlink()
                gone.append(p.name)
        except OSError:
            pass

    for d in (target_dir / "DlssOverrides", target_dir / "plugins",
              target_dir / "dlssnr-capture"):
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
                gone.append(d.name)
        except OSError:
            pass
    return gone


# --------------------------------------------------------------------------
# 编排层：预检 / 安装 / 卸载 / 体检（带状态记录与互斥）
# --------------------------------------------------------------------------
#
# 下面这几个函数是 CLI 和 GUI 实际调用的入口。它们负责：
#   · 引擎互斥拦截
#   · 外来 Mod 拦截
#   · 把安装记录写进共享状态库（卸载时才有据可依）


@dataclass
class Check:
    level: str      # ok / warn / error
    title: str
    detail: str = ""


def preflight(
    target_dir: str | Path,
    bundle: str,
    multiplier: int = 4,
    running_names: list[str] | None = None,
    anticheat=None,
) -> list[Check]:
    """OptiScaler 引擎的安装前预检。有任何 error 就不该继续。"""
    from .state import ENGINE_OPTISCALER, conflict_message, other_engine_install

    target_dir = Path(target_dir)
    out: list[Check] = []

    spec = get_bundle(bundle)
    if spec is None:
        return [Check("error", "未知引擎包", f"{bundle} 不在内置清单里")]

    if not target_dir.is_dir():
        return [Check("error", "目标目录不存在", str(target_dir))]

    # 1) 引擎互斥 —— 这条最要紧，放最前面
    other = other_engine_install(target_dir, ENGINE_OPTISCALER)
    if other:
        out.append(Check("error", "该目录已安装另一个引擎",
                         conflict_message(other, ENGINE_OPTISCALER)))
    else:
        out.append(Check("ok", "没有与本引擎冲突的安装", "同一目录只允许一个帧生成引擎"))

    # 2) 外来 Mod
    from .installer import foreign_mods

    foreign = foreign_mods(target_dir)
    if foreign:
        out.append(Check(
            "error", "检测到别的帧生成 Mod",
            "这些文件同样会钩住渲染路径，与本引擎叠加会互相打架：\n  · "
            + "\n  · ".join(foreign)
            + "\n\n请先用它们自带的卸载方式清理干净再装。",
        ))
    else:
        out.append(Check("ok", "未检测到其他帧生成 Mod"))

    # 3) 写权限
    probe = target_dir / f".dlssgtool_write_test{TMP_SUFFIX}"
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
        out.append(Check("ok", "目录可写", str(target_dir)))
    except Exception as exc:
        try:
            probe.unlink()
        except Exception:
            pass
        out.append(Check(
            "error", "没有写入权限",
            f"{target_dir}\n{type(exc).__name__}: {exc}\n"
            "该目录可能位于 Program Files 等受保护位置，请用管理员身份重跑本工具。",
        ))

    # 4) 磁盘空间（bundle 体积 + 200MB 余量）
    try:
        free = shutil.disk_usage(str(target_dir)).free
        need = spec.total_bytes + 200 * 1024 * 1024
        if free < need:
            out.append(Check("error", "磁盘空间不足",
                             f"剩余 {free/1048576:.0f} MB，本次需要约 {need/1048576:.0f} MB"))
        else:
            out.append(Check("ok", "磁盘空间充足", f"剩余 {free/1073741824:.1f} GB"))
    except Exception:
        pass

    # 5) 游戏是否在运行
    names: set[str] = set(running_names or [])
    try:
        names.update(p.name for p in target_dir.glob("*.exe"))
    except OSError:
        pass
    hit = sorted(n for n in names if n and proc.is_running(n))
    if hit:
        out.append(Check("error", "游戏/相关进程正在运行",
                         "请完全退出游戏后再安装：" + "、".join(hit)))
    else:
        out.append(Check("ok", "游戏未在运行"))

    # 6) 反作弊
    if anticheat is not None and getattr(anticheat, "risky", False):
        out.append(Check(
            "error", "检测到反作弊组件",
            f"{anticheat.summary}\n给带反作弊的游戏注入 DLL 可能导致封号，本工具默认阻止。",
        ))
    elif anticheat is not None:
        out.append(Check("ok", "未检测到反作弊组件"))

    # 7) payload 完整性
    ok, msg = bundle_available(bundle)
    if ok:
        out.append(Check("ok", "内置引擎包完整性校验通过", f"{spec.display_name}：{msg}"))
    else:
        out.append(Check("error", "内置引擎包校验失败", msg))

    # 8) 倍率
    mult = normalize_multiplier(multiplier)
    if mult > TEMPLATE_MAX_MULT:
        out.append(Check(
            "warn", f"{mult}X 是实验性档位",
            f"该构建自带模板只写到 {TEMPLATE_MAX_MULT}X，更高档位靠运行时给 provider "
            "打补丁解锁，可能无效或画质异常。",
        ))
    else:
        out.append(Check("ok", f"倍率 {mult}X", multiplier_label(mult)))

    if bundle == "optiscaler-dlss5":
        out.append(Check(
            "warn", "DLSS 5 神经网络渲染是实验性的",
            "所用 nvngx_dlssnr.dll 是未签名、且不在你当前驱动里的预览版组件。"
            "本工具只是把它装进游戏目录，不对此文件做任何修改；出问题卸载即可还原。",
        ))

    return out


def install(
    target_dir: str | Path,
    exe_path: Path | None,
    bundle: str,
    multiplier: int = 4,
    game_name: str = "",
    dry_run: bool = False,
    high_res_mv: bool | None = True,
) -> OptiResult:
    """完整安装流程：读状态 -> 生成计划 -> 执行 -> 记录。"""
    from .state import ENGINE_OPTISCALER, engine_of, find_install, record_install

    target_dir = Path(target_dir)
    prev = find_install(target_dir)
    prev_proxy = ""
    originals: dict[str, str] = {}
    if prev and engine_of(prev) == ENGINE_OPTISCALER:
        prev_proxy = prev.get("proxy", "")
        originals = dict(prev.get("originals") or {})

    plan = make_plan(
        target_dir=target_dir,
        exe_path=exe_path,
        bundle=bundle,
        multiplier=multiplier,
        game_name=game_name,
        prev_proxy=prev_proxy,
        high_res_mv=high_res_mv,
    )

    if dry_run:
        return OptiResult(True, f"预演完成，未做任何改动（将写入 {len(plan.writes)} 项）")

    res = execute_plan(plan, originals=originals)
    if not res.success:
        return res

    # 记录状态。originals 用执行结果里那份 —— 它包含了本轮新备份的原始文件，
    # 落掉的话卸载时就不知道该还原什么了。
    record_install({
        "engine": ENGINE_OPTISCALER,
        "game_name": game_name,
        "target_dir": str(target_dir),
        "exe": str(exe_path) if exe_path else "",
        "bundle": bundle,
        "multiplier": normalize_multiplier(multiplier),
        "proxy": plan.proxy,
        "files": [
            {"name": rel, "sha256": try_hash(target_dir / rel)}
            for rel in res.installed
        ],
        "originals": dict(res.originals),
        "backup_dir": str(res.backup_dir) if res.backup_dir else "",
        "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    return res


def uninstall_installed(target_dir: str | Path, force: bool = False) -> OptiResult:
    """按状态记录卸载 OptiScaler 引擎。"""
    from .state import ENGINE_OPTISCALER, forget_install, find_install

    target_dir = Path(target_dir)
    rec = find_install(target_dir, engine=ENGINE_OPTISCALER)

    if not rec:
        # 记录丢了：按内容认一遍，能认出来也照样能干净卸载
        detected = detect_ours(target_dir)
        if not detected:
            return OptiResult(False, "没有该目录的 OptiScaler 安装记录，未做任何改动")
        rec = {
            "engine": ENGINE_OPTISCALER,
            "files": [{"name": n, "sha256": h} for n, h in detected.items()],
            "originals": {},
            "proxy": next((n for n in detected if n.lower().endswith(".dll")), ""),
        }

    res = uninstall(target_dir, rec, force=force)
    if res.success:
        gone = clean_runtime_leftovers(target_dir)
        if gone:
            res.message += f"，并清理运行产物 {len(gone)} 项"
        forget_install(target_dir)
    return res


def verify_installed(target_dir: str | Path) -> OptiVerify:
    from .state import ENGINE_OPTISCALER, find_install

    target_dir = Path(target_dir)
    rec = find_install(target_dir, engine=ENGINE_OPTISCALER)
    if not rec:
        detected = detect_ours(target_dir)
        if not detected:
            return OptiVerify(False, False, [("warn", "没有安装记录", "该目录不是本工具装的 OptiScaler")])
        rec = {
            "files": [{"name": n, "sha256": h} for n, h in detected.items()],
            "exe": "",
        }
        out = verify(target_dir, rec)
        out.details.insert(0, ("warn", "检测到本工具安装的文件，但没有安装记录",
                               "状态文件可能被清理过；仍可正常卸载（按内容识别）"))
        return out
    return verify(target_dir, rec)


__all__ = [
    "ENGINE",
    "INI_NAME",
    "LICENSE_DIR",
    "PROXY_CANDIDATES",
    "MULTIPLIERS",
    "BundleSpec",
    "OptiPlan",
    "OptiPlanItem",
    "OptiResult",
    "OptiVerify",
    "Check",
    "all_bundles",
    "bundle_keys",
    "get_bundle",
    "default_bundle",
    "bundle_options",
    "bundle_available",
    "payload_root",
    "resolve_file",
    "sha256_file",
    "try_hash",
    "build_ini",
    "is_our_ini",
    "read_multiplier",
    "read_engine_summary",
    "multiplier_options",
    "multiplier_label",
    "normalize_multiplier",
    "pick_proxy",
    "detect_foreign",
    "detect_ours",
    "is_our_file",
    "all_payload_hashes",
    "orphan_proxies",
    "make_plan",
    "execute_plan",
    "uninstall",
    "verify",
    "clean_runtime_leftovers",
    "preflight",
    "install",
    "uninstall_installed",
    "verify_installed",
    "GENERATED_AT",
]
