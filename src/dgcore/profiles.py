"""payload 版本适配层 —— 把「上游版本差异」关在这一个文件里。

为什么需要它
------------
上游迭代很快（0.2.4 → 0.3.0 只隔了 3 天），而且两个版本差异是**结构性**的：

                    0.2.4                    0.3.0
  INI 路由键        Router=SM86/SM75         （无，已删除）
  INI 采样键        HardwareBilinear         改为 Optimized
  倍率上限          3（4X）                  5（6X）
  代理入口          +winhttp                 -winhttp, +d3d12/dbghelp
  20 系（SM75）     一等公民                  ❌ 已放弃（发布包无 sm75 内核）
  运行架构          native                   代理模式

**最关键的一点**：0.3.0 不再支持 RTX 20 系列。所以不能简单升级 ——
那会让 20 系用户从"能用"变成"不能用"。正确做法是两个 profile 并存，
按显卡自动选。

这个模块是唯一的版本差异收敛点。以后上游再改版，只动这里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ProxyEntry:
    """一个代理入口。"""
    name: str                 # 落盘文件名，如 version.dll
    upstream_path: str        # 上游包内位置
    kind: str                 # tool=工具类（安全） / render=渲染路径（风险高）
    note: str = ""

    @property
    def is_safe(self) -> bool:
        return self.kind == "tool"


@dataclass(frozen=True)
class Profile:
    """一个上游版本 profile。"""
    version: str
    display_name: str
    architecture: str                       # native / proxy
    routers: tuple[str, ...]                # 支持的计算路由（空 = 无路由概念）
    max_frames: int                         # MaxGeneratedFrames 硬上限
    max_multiplier: int                     # 对应倍率（2/3/4/6）
    supports_sm75: bool
    experimental: bool = False              # 是否属于实验性支持
    explanation: str = ""                   # 给用户看的一句话
    adapters: tuple[ProxyEntry, ...] = ()
    manifest: dict[str, tuple[str, int]] = field(default_factory=dict)

    @property
    def default_proxy(self) -> str:
        for a in self.adapters:
            if a.name == "version.dll":
                return a.name
        return self.adapters[0].name if self.adapters else "version.dll"

    @property
    def proxy_names(self) -> tuple[str, ...]:
        return tuple(a.name for a in self.adapters)

    def proxy(self, name: str) -> ProxyEntry | None:
        for a in self.adapters:
            if a.name == name:
                return a
        return None

    def payload_path(self, name: str, payload_root: Path) -> Path:
        return payload_root / self.version / name


# --------------------------------------------------------------------------
# 0.2.4 —— 当前默认，唯一支持 RTX 20 系列的版本
# --------------------------------------------------------------------------

PROFILE_024 = Profile(
    version="0.2.4",
    display_name="DLSSG Native 0.2.4",
    architecture="native",
    routers=("SM86", "SM75"),
    max_frames=3,
    max_multiplier=4,
    supports_sm75=True,
    experimental=False,
    explanation="支持 RTX 20 / 30 系列。20 系走 SM75 实验性内核。",
    adapters=(
        ProxyEntry("version.dll", "version.dll", "tool", "上游默认入口，绝大多数游戏会加载"),
        ProxyEntry("winmm.dll", "altnative/winmm.dll", "tool", "第二选择，几乎所有游戏都导入"),
        ProxyEntry("dinput8.dll", "altnative/dinput8.dll", "tool", "老一些的输入栈会加载"),
        ProxyEntry("winhttp.dll", "altnative/winhttp.dll", "tool", "网络库，部分游戏加载"),
        ProxyEntry("dxgi.dll", "altnative/dxgi.dll", "render", "渲染路径入口，只在上面都不行时用"),
    ),
    manifest={
        "version.dll": ("c844646d835a7b88ed1382eea80403d38b433f8ac09cf92581c73698c44ae7c2", 15667520),
        "winmm.dll": ("1004dd4ee0edbe4e1af4c8c7b30d4786bea0f5e7c0412566996b4c2543ae7e36", 15678272),
        "dinput8.dll": ("ef3c3d49c5b5c8a17289c24da9b22885570793d72f3db628fa500f9efdb20489", 15666496),
        "winhttp.dll": ("1619839e4d1b6145ce9a587ba807f42e64f2b0984af9e81700d42ccf46ff7253", 15674176),
        "dxgi.dll": ("8d29eddbd7f1c3e272d07f94ab8812a80ef5b7aeb73923320bf9a432ddcf74c0", 15668032),
    },
)


# --------------------------------------------------------------------------
# 0.3.0 —— 上游最新，支持 6X，但**已放弃 RTX 20 系**
# --------------------------------------------------------------------------

PROFILE_030 = Profile(
    version="0.3.0",
    display_name="DLSSG for SM86 0.3.0（代理模式）",
    architecture="proxy",
    routers=(),                       # 0.3.0 的 INI 里没有 Router 键
    max_frames=5,
    max_multiplier=6,
    supports_sm75=False,
    experimental=False,
    explanation=(
        "上游最新版，改用代理模式（native 模式存在难以修复的兼容问题），"
        "新增 6X 帧生成。**仅支持 RTX 30 系列**——0.3.0 已移除 SM75 内核。"
    ),
    adapters=(
        ProxyEntry("version.dll", "version.dll", "tool", "上游默认入口"),
        ProxyEntry("winmm.dll", "alternatives/winmm.dll", "tool", "第二选择，推荐"),
        ProxyEntry("dbghelp.dll", "alternatives/dbghelp.dll", "tool", "崩溃/符号库，多数游戏或反作弊会加载"),
        ProxyEntry("dinput8.dll", "alternatives/dinput8.dll", "tool", "DirectInput8"),
        ProxyEntry("dxgi.dll", "alternatives/dxgi.dll", "render", "渲染路径入口，仅在上面的都不行时用"),
        ProxyEntry("d3d12.dll", "alternatives/d3d12.dll", "render", "渲染路径入口，与 dxgi 二选一"),
    ),
    manifest={
        "version.dll": ("a22d2453f25d7df3fdc0d6d683c21f01769a115439d58f1341183a75faaf8c7d", 17529120),
        "winmm.dll": ("197f97e90541ae688d291ef388b1eddc0c55cb657603eb55dea2d3d178b1e384", 17540896),
        "dbghelp.dll": ("10e2fe2d84b8b674e184891ca5211b01c43c6700e1c8b8c6d4969286f653bff8", 17547040),
        "dinput8.dll": ("01fdd5e77e64045400a2e7b6f35f98f3403335e30cd9def0e996f78240aa65da", 17528608),
        "dxgi.dll": ("ae37150fe056f3388571481ad4aaf78fad720e9b52eb7e6e1e9d2dff85df841e", 17529632),
        "d3d12.dll": ("63e7c3a1ba0b10e37e1a162ccf3aa2e19a0f7359de63787585c09baffd67ad45", 17529632),
    },
)


PROFILES: dict[str, Profile] = {
    "0.2.4": PROFILE_024,
    "0.3.0": PROFILE_030,
}

DEFAULT_VERSION = "0.3.0"     # 新用户默认用最新版
FALLBACK_VERSION = "0.2.4"    # 20 系自动回退到这个


def get(version: str) -> Profile:
    return PROFILES.get(version) or PROFILES[DEFAULT_VERSION]


def for_router(router: str, prefer: str | None = None) -> Profile:
    """按计算路由挑合适的 profile。

    SM75（RTX 20 系）只有 0.2.4 支持 —— 这是硬约束，不是偏好问题。
    router 非法时也要尊重 prefer，否则调用方明确指定了版本却被忽略。
    """
    r = (router or "").upper()
    # 硬约束优先：20 系只能用 0.2.4
    if r == "SM75":
        return PROFILE_024
    if prefer and prefer in PROFILES:
        return PROFILES[prefer]
    if r == "SM86":
        return PROFILES[DEFAULT_VERSION]
    # 路由无法识别（或为空）：用默认版本，但别让调用方拿到一个意外的版本
    return PROFILES[DEFAULT_VERSION]


def all_versions() -> list[str]:
    return list(PROFILES)


def describe_all() -> str:
    lines = []
    for v, p in PROFILES.items():
        sm75 = "支持 20 系" if p.supports_sm75 else "不支持 20 系"
        lines.append(f"  {v}  {p.display_name}  最高 {p.max_multiplier}X  {sm75}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# INI 生成（每个 profile 一套）
# --------------------------------------------------------------------------

TOOL_NAME_LINE = "; 由 DLSSG 帧生成一键开启工具生成"


def build_ini(
    profile: Profile,
    router: str = "SM86",
    optimized: int = 1,
    max_generated_frames: int | None = None,
    log_level: int = 1,
    hardware_bilinear: int = 0,
) -> str:
    """按 profile 生成合法的 INI。

    0.2.4 和 0.3.0 的键结构完全不同，这里分别处理 —— 这也是适配层存在的意义。

    hardware_bilinear 只在 0.2.4 生效（0.3.0 用 Optimized 表达同一件事，
    且没有近似采样开关）。
    """
    frames = profile.max_frames if max_generated_frames is None else int(max_generated_frames)
    frames = max(1, min(profile.max_frames, frames))
    log_level = max(0, min(3, int(log_level)))
    optimized = 1 if int(optimized) else 0
    hardware_bilinear = 1 if int(hardware_bilinear) else 0

    if profile.version == "0.2.4":
        return _ini_024(router, optimized, frames, log_level, hardware_bilinear)
    return _ini_030(optimized, frames, log_level)


def _ini_024(router: str, optimized: int, frames: int, log_level: int, bilinear: int = 0) -> str:
    router = (router or "SM86").upper()
    if router not in ("SM86", "SM75"):
        router = "SM86"
    # SM75 硬件上不支持近似采样 —— 无论用户怎么选都强制归零
    if router == "SM75":
        bilinear = 0
    board = "RTX 30 系列 (Ampere)" if router == "SM86" else "RTX 20 系列 (Turing)"
    return (
        f"{TOOL_NAME_LINE} — 上游 DLSSG Native 0.2.4\n"
        f"; 修改后需要重启游戏才会生效。\n"
        f"; Router={router} 对应 {board}\n"
        "\n"
        "[Compatibility]\n"
        f"Router={router}\n"
        "KernelImage=PTX\n"
        f"HardwareBilinear={bilinear}\n"
        "\n"
        "[FrameGeneration]\n"
        f"MaxGeneratedFrames={frames}\n"
        "\n"
        "[Logging]\n"
        f"Level={log_level}\n"
    )


def _ini_030(optimized: int, frames: int, log_level: int) -> str:
    return (
        f"{TOOL_NAME_LINE} — 上游 0.3.0（代理模式）\n"
        f"; 修改后需要重启游戏才会生效。\n"
        f"; 本版仅支持 RTX 30 系列（SM86）；0.3.0 已移除 SM75 内核。\n"
        "\n"
        "[General]\n"
        "Enabled=1\n"
        "\n"
        "[FrameGeneration]\n"
        f"Optimized={optimized}\n"
        f"MaxGeneratedFrames={frames}\n"
        "\n"
        "[Compatibility]\n"
        "Preset=Auto\n"
        "\n"
        "[Logging]\n"
        f"Level={log_level}\n"
        "Directory=dlssg_sm86\\logs\n"
        "\n"
        "[Runtime]\n"
        "Mode=Bundled\n"
        "CacheDirectory=\n"
    )


def read_ini_router(path, profile: Profile | None = None) -> str:
    """读 INI 里的路由。0.3.0 没有这个键，返回空串。"""
    import re
    from pathlib import Path

    try:
        text = Path(path).read_text("utf-8", "ignore")
    except OSError:
        return ""
    m = re.search(r"^\s*Router\s*=\s*(\w+)", text, re.M)
    return m.group(1).upper() if m else ""


def detect_ini_version(path) -> str:
    """从 INI 内容反推是哪个版本写的。"""
    from pathlib import Path

    try:
        text = Path(path).read_text("utf-8", "ignore")
    except OSError:
        return ""
    if "[Runtime]" in text or "Mode=Bundled" in text:
        return "0.3.0"
    if "Router=" in text or "HardwareBilinear" in text:
        return "0.2.4"
    return ""


__all__ = [
    "ProxyEntry",
    "Profile",
    "PROFILES",
    "PROFILE_024",
    "PROFILE_030",
    "DEFAULT_VERSION",
    "FALLBACK_VERSION",
    "get",
    "for_router",
    "all_versions",
    "describe_all",
    "build_ini",
    "read_ini_router",
    "detect_ini_version",
]
