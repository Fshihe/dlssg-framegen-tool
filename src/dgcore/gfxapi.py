"""图形 API 分级判定 —— 解决「明明是 DX12 却提示 DX11」的问题。

为什么需要分级
--------------
现代游戏（尤其 UE 引擎）会**同时导入** d3d11.dll、d3d12.dll、甚至 d3d9.dll，
运行时才决定用哪个。所以「导入了 d3d11」根本不能说明游戏是用 D3D11 跑的，
更不能因此判定它不能用本 Mod —— 这正是之前误报的来源。

另外有一类游戏通过 LoadLibrary("d3d12.dll") 动态加载，导入表里根本没有，
只能靠字节扫描或旁证判断。

判定等级
--------
CONFIRMED  确认 D3D12 —— 静态导入或延迟导入 d3d12.dll，证据确凿
LIKELY     疑似 D3D12 —— 有较强旁证（同目录 D3D12 组件 / nvngx_dlssg / 字节命中）
HINTED     有迹象     —— 只命中 DXGI 且未发现 D3D11 专有特征，存疑
D3D11_ONLY 确认仅 D3D11 —— 导入 d3d11 且完全没有 D3D12 的任何迹象
UNKNOWN    无法判断
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class ApiLevel(str, Enum):
    CONFIRMED = "confirmed"    # 确认 D3D12
    LIKELY = "likely"          # 疑似 D3D12（有旁证）
    HINTED = "hinted"          # 有迹象，存疑
    D3D11_ONLY = "d3d11_only"  # 确认仅 D3D11
    VULKAN = "vulkan"          # 确认走 Vulkan（当前版本不支持）
    UNKNOWN = "unknown"

    @property
    def can_install(self) -> bool:
        """能不能装：确认和疑似都放行，其余不放。"""
        return self in (ApiLevel.CONFIRMED, ApiLevel.LIKELY)

    @property
    def label(self) -> str:
        return {
            ApiLevel.CONFIRMED: "D3D12（已确认）",
            ApiLevel.LIKELY: "D3D12（疑似，有旁证）",
            ApiLevel.HINTED: "可能支持 D3D12（存疑）",
            ApiLevel.D3D11_ONLY: "仅 D3D11",
            ApiLevel.VULKAN: "Vulkan（暂不支持）",
            ApiLevel.UNKNOWN: "无法判断",
        }[self]


@dataclass
class ApiVerdict:
    level: ApiLevel = ApiLevel.UNKNOWN
    evidence: list[str] = field(default_factory=list)   # 支持 D3D12 的依据
    counter: list[str] = field(default_factory=list)    # 反向依据
    score: int = 0

    @property
    def can_install(self) -> bool:
        return self.level.can_install

    @property
    def label(self) -> str:
        return self.level.label

    def summary(self) -> str:
        if self.evidence:
            return self.evidence[0]
        return "无明确依据"

    def detail_lines(self) -> list[str]:
        out = [f"判定：{self.label}"]
        for e in self.evidence:
            out.append(f"  支持：{e}")
        for c in self.counter:
            out.append(f"  反证：{c}")
        return out


# 同级目录里这些文件是 D3D12 的强旁证
D3D12_SIBLING_DIRS = ("D3D12",)                       # 目录名
D3D12_SIBLING_FILES = (
    "d3d12core.dll",
    "nvngx_dlssg.dll",       # 帧生成组件只在 D3D12 下存在
    "nvngx_dlss.dll",
    "sl.dlss_g.dll",         # Streamline 帧生成插件
    "sl.interposer.dll",
)

# 只属于 D3D11 的符号（如果 EXE 里出现，倾向 D3D11）
D3D11_MARKERS = (
    b"D3D11CreateDevice",
    b"D3D11CreateDeviceAndSwapChain",
    b"d3d11.dll",
)
D3D12_MARKERS = (
    b"D3D12CreateDevice",
    b"d3d12.dll",
    b"d3d12core.dll",
    b"D3D12SerializeVersionedRootSignature",
)


def _imports_lower(imports) -> set[str]:
    return {str(i).lower() for i in imports}


def judge(
    imports: tuple[str, ...] | list[str] = (),
    delayed_imports: tuple[str, ...] | list[str] = (),
    text_hits: dict[str, int] | None = None,
    exe_dir: str | Path | None = None,
    is_x64: bool = True,
) -> ApiVerdict:
    """综合所有证据给出分级判定。

    text_hits: 在 .text 段里搜到的标记 -> 命中次数
    exe_dir  : EXE 所在目录，用于查找旁证文件
    """
    v = ApiVerdict()
    imp = _imports_lower(imports)
    dimp = _imports_lower(delayed_imports)
    hits = text_hits or {}

    score = 0

    # ---- 1. 静态导入（最硬的证据）----
    if "d3d12.dll" in imp:
        score += 100
        v.evidence.append("静态导入 d3d12.dll")
    if "d3d12core.dll" in imp:
        score += 90
        v.evidence.append("静态导入 d3d12core.dll")

    # ---- 2. 延迟导入（也很硬）----
    if "d3d12.dll" in dimp:
        score += 80
        v.evidence.append("延迟导入 d3d12.dll（运行时加载）")

    # ---- 3. 代码/数据段里的符号 ----
    if "D3D12CreateDevice" in hits:
        score += 60
        v.evidence.append("代码段含 D3D12CreateDevice 符号")
    elif "d3d12.dll" in hits:
        score += 45
        v.evidence.append("代码段含字符串 d3d12.dll（动态加载）")
    if "d3d12core.dll" in hits and "D3D12CreateDevice" not in hits:
        score += 30
        v.evidence.append("代码段含字符串 d3d12core.dll")

    # ---- 4. 同目录旁证 ----
    # 注意权重：光有个空的 D3D12\ 目录说明不了什么（实测有些 Vulkan 游戏
    # 也会带一个空目录），所以目录只给很小的分；真正的组件文件才给高分。
    sibling_hits: list[str] = []
    if exe_dir:
        d = Path(exe_dir)
        for sd in D3D12_SIBLING_DIRS:
            try:
                sub = d / sd
                if sub.is_dir() and any(sub.iterdir()):
                    score += 15
                    sibling_hits.append(f"同目录 {sd}\\ 里有文件")
            except OSError:
                pass
        for fn in D3D12_SIBLING_FILES:
            try:
                if (d / fn).is_file():
                    # 帧生成/DLSS 组件是最强旁证
                    w = 55 if ("dlssg" in fn or "dlss_g" in fn) else 35
                    score += w
                    sibling_hits.append(f"同目录存在 {fn}")
            except OSError:
                pass
    v.evidence.extend(sibling_hits)

    # ---- 5. 反向证据与弱信号 ----
    has_d3d11 = "d3d11.dll" in imp or "d3d11.dll" in dimp
    d3d11_sym = hits.get("D3D11CreateDevice", 0) > 0
    if has_d3d11:
        v.counter.append("导入 d3d11.dll")

    # Vulkan：如果游戏明显走 Vulkan 而 D3D12 证据很弱，就该判死，
    # 不能让"同目录有个文件夹"之类的弱旁证把它抬进可安装
    has_vulkan = (
        "vulkan-1.dll" in imp
        or "vulkan-1.dll" in dimp
        or hits.get("vulkan-1.dll", 0) > 0
    )
    strong_d3d12 = (
        "d3d12.dll" in imp
        or "d3d12core.dll" in imp
        or "d3d12.dll" in dimp
        or "D3D12CreateDevice" in hits
    )
    if has_vulkan and not strong_d3d12:
        v.counter.append("有 Vulkan 证据且无强 D3D12 证据")
        v.level = ApiLevel.VULKAN
        v.score = score
        return v

    # DXGI 是 D3D11 和 D3D12 共用的，单独出现只能算弱信号。
    # 关键：它**不能**把"明确的 D3D11 游戏"抬成可安装 —— 那样会让用户
    # 对着一个纯 DX11 游戏白折腾。
    has_dxgi = "dxgi.dll" in imp or "dxgi.dll" in dimp or hits.get("dxgi.dll", 0) > 0
    if has_dxgi and not has_d3d11:
        score += 10
        v.evidence.append("导入 dxgi.dll（D3D11/D3D12 共用，仅作参考）")
    elif has_dxgi and not strong_d3d12:
        # 只有在确实没有 D3D12 证据时，这句"没有迹象"才成立；
        # 否则会出现"已确认 D3D12"和"没有 D3D12 迹象"并列的矛盾说法
        v.counter.append("只有 dxgi.dll + d3d11.dll，没有 D3D12 的任何迹象")

    # ---- 6. 定级 ----
    if score >= 80:
        v.level = ApiLevel.CONFIRMED
    elif score >= 45:
        v.level = ApiLevel.LIKELY
    elif score > 0:
        v.level = ApiLevel.HINTED
    elif has_d3d11:
        v.level = ApiLevel.D3D11_ONLY
        v.counter.append("没有任何 D3D12 迹象")
    else:
        v.level = ApiLevel.UNKNOWN

    # 特别说明：同时导入 d3d11 不影响 D3D12 判定，这正是以前误报的根源
    if has_d3d11 and v.level in (ApiLevel.CONFIRMED, ApiLevel.LIKELY):
        v.evidence.append("注：同时导入 d3d11.dll 属正常现象（引擎运行时择一），不影响 D3D12 判定")

    if d3d11_sym and v.level == ApiLevel.CONFIRMED:
        v.counter.append("代码段也含 D3D11CreateDevice（同样属正常）")

    v.score = score
    return v


__all__ = ["ApiLevel", "ApiVerdict", "judge", "D3D12_SIBLING_FILES"]
