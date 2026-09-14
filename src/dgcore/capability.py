"""游戏能力预判 —— 在用户安装**之前**就告诉他这个游戏行不行。

评论区里很大一部分反馈是「装完了发现游戏没有帧生成选项」。这些其实
在扫描阶段就能预判，没必要让用户白折腾一趟。

判断依据（按可靠性排序）：
  1. 同目录有没有 nvngx_dlssg.dll / sl.dlss_g.dll  ← 帧生成组件，最硬
  2. 图形 API 是不是 D3D12                          ← 硬性前提
  3. 是否为已知的「只有 DLSS 超分、没有帧生成」的游戏
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Support(str, Enum):
    GOOD = "good"          # 大概率可以
    MAYBE = "maybe"        # 有希望，但要试
    UNLIKELY = "unlikely"  # 大概率不行
    NO = "no"              # 确定不行

    @property
    def label(self) -> str:
        return {
            Support.GOOD: "大概率可以开启",
            Support.MAYBE: "可以试一下",
            Support.UNLIKELY: "大概率没有帧生成功能",
            Support.NO: "不支持",
        }[self]

    @property
    def color(self) -> str:
        """给 GUI 用的语义色。"""
        return {
            Support.GOOD: "ok",
            Support.MAYBE: "dim",
            Support.UNLIKELY: "warn",
            Support.NO: "err",
        }[self]


@dataclass
class Prediction:
    level: Support = Support.MAYBE
    headline: str = ""
    reasons: list[str] = field(default_factory=list)
    advice: str = ""

    @property
    def installable(self) -> bool:
        """要不要允许继续安装（不是"行不行"，是"值不值得试"）。"""
        return self.level != Support.NO


# 已知只提供 DLSS 超分、没有帧生成功能的游戏
# 来源：评论区反馈 + 实测确认。用文件名/游戏名做宽松匹配。
#
# 注意这里记的是「游戏没开放功能」，不是「文件不存在」——
# 《幻兽帕鲁》就是反例：组件一应俱全，但游戏 UI 没给帧生成开关。
KNOWN_NO_FG = {
    "palworld": "《幻兽帕鲁》只有 DLSS 超分，没有开放帧生成",
    "stardew": "《星露谷物语》是像素游戏，不涉及 DLSS",
    "goose goose": "《鹅鸭杀》走 Vulkan，且带反作弊",
}

# 已知「帧生成只有开/关、不提供倍率选择」的游戏 —— 这类游戏固定跑 2X。
# 用户会以为选了 4X 没生效，其实游戏根本没请求那么多帧。
# 匹配串尽量用可执行文件名，避免中文/英文名不一致。
KNOWN_NO_MULTIPLIER = {
    "b1-win64-shipping": "《黑神话：悟空》",
    "blackmythwukong": "《黑神话：悟空》",
}


def _dir_has(d: Path, names) -> list[str]:
    out = []
    for n in names:
        try:
            if (d / n).is_file():
                out.append(n)
        except OSError:
            pass
    return out


def _find_components(root: Path | None, names) -> list[str]:
    """在 EXE 所在目录**及其所属游戏目录**里找组件。

    为什么不能只看同目录：UE 游戏（黑神话、帕鲁）把插件放在
        <游戏>\\<项目>\\Plugins\\...\\Binaries\\ThirdParty\\Win64\\
    这类深层目录里，只看 EXE 旁边会漏判成"没有组件"。

    但也不能随便往上翻 —— 曾经用 `**/名字` 递归查找，结果会翻到游戏目录
    之外（测试时甚至串到了别的临时目录），既慢又会误判。

    所以改成**只认已知的插件布局**：
      · EXE 同目录
      · 从 EXE 往上找「游戏根」，特征是同级有 Engine 目录
      · 只在这些根下的 <项目>/Plugins、Engine/Plugins 里按固定层级找
    找到就返回，不做无界递归。
    """
    if root is None:
        return []
    root = Path(root)
    found: set[str] = set()

    def scan(d: Path) -> None:
        for n in names:
            try:
                if (d / n).is_file():
                    found.add(n)
            except OSError:
                pass

    # 1) EXE 同目录
    scan(root)

    # 2) 找游戏根候选：往上最多 4 层，边走边收集。
    #
    #    识别特征有两个，命中任一即认为是根：
    #      · 同级有 Engine 目录（标准 UE 布局）
    #      · 同级有 <某项目>/Plugins 目录（有些打包方式没有 Engine）
    #    找不到就只用 EXE 同目录的结果 —— 不能无界往上翻。
    roots: list[Path] = []
    cur = root
    for _ in range(4):
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
        roots.append(cur)

    game_root: Path | None = None
    plugin_roots: list[Path] = []

    for cand in roots:
        try:
            has_engine = (cand / "Engine").is_dir()
            # 找同级的 <项目>/Plugins
            proj_plugins: list[Path] = []
            for entry in cand.iterdir():
                if not entry.is_dir():
                    continue
                p = entry / "Plugins"
                if p.is_dir():
                    proj_plugins.append(p)
                # 也覆盖 <项目>/Plugins 直接在候选目录下的情况
            if has_engine or proj_plugins:
                game_root = cand
                plugin_roots.extend(proj_plugins)
                eng = cand / "Engine" / "Plugins"
                if eng.is_dir():
                    plugin_roots.append(eng)
                break
            # 候选目录本身就是 Plugins 的父级
            if (cand / "Plugins").is_dir():
                plugin_roots.append(cand / "Plugins")
        except OSError:
            continue

    # 没有识别到游戏根，但 EXE 目录上方就是 Plugins 时也扫一下
    if not plugin_roots:
        for cand in roots:
            try:
                p = cand / "Plugins"
                if p.is_dir():
                    plugin_roots.append(p)
                    break
            except OSError:
                continue
        # 还有一种布局：EXE 在 .../Binaries/Win64，插件在 .../Plugins
        for cand in roots:
            try:
                if cand.name.lower() in ("win64", "binaries"):
                    continue
                if (cand / "Plugins").is_dir():
                    plugin_roots.append(cand / "Plugins")
            except OSError:
                continue

    if not plugin_roots:
        return sorted(found)

    # 去重并保持顺序（识别根时可能同时收进相同的 Plugins 路径）
    deduped: list[Path] = []
    for p in plugin_roots:
        if p not in deduped:
            deduped.append(p)
    plugin_roots = deduped

    # 3) 在插件根下按固定层级找。
    #
    #    实测两种布局的深度：
    #      帕鲁      : Plugins/StreamlineCore/Binaries/ThirdParty/Win64/  → 4 层
    #      黑神话    : Engine/Plugins/Runtime/Nvidia/Streamline/Binaries/ThirdParty/Win64/ → 6 层
    #    所以深度要给够（这里到 7），但仍然是有界的 —— 不用 ** 无界递归，
    #    那样会翻出游戏目录，既慢又会串到别的游戏。
    seen: set[Path] = set()
    for pr in plugin_roots[:12]:
        if len(found) == len(names):
            break
        if pr in seen:
            # 注意：这里必须 continue 而不是 break ——
            # plugin_roots 里可能混有重复项（根目录识别时会同时收
            # <项目>/Plugins 和 Engine/Plugins，某些布局下两者相同），
            # 用 break 会在遇到重复项时直接放弃后面所有候选。
            continue
        seen.add(pr)
        for n in names:
            if n in found:
                continue
            try:
                for depth in range(1, 8):
                    pat = "/".join(["*"] * depth) + f"/{n}"
                    hit = next((x for x in pr.glob(pat) if x.is_file()), None)
                    if hit is not None:
                        found.add(n)
                        break
            except OSError:
                continue

    return sorted(found)


def _known_no_multiplier(exe_name: str, game_name: str) -> str:
    """这个游戏是不是已知「只有开/关、不给倍率选择」。是则返回游戏名。"""
    low = f"{exe_name or ''} {game_name or ''}".lower()
    for key, label in KNOWN_NO_MULTIPLIER.items():
        if key in low:
            return label
    return ""


FG_COMPONENTS = ("nvngx_dlssg.dll", "sl.dlss_g.dll")
SR_COMPONENTS = ("nvngx_dlss.dll", "sl.dlss.dll", "nvngx_dlssd.dll")

# 「倍率上限」这个坑值得在每个提示里说一遍。
#
# 真实反馈：用户在工具里选了 4X，进游戏发现只有 2 倍效果（60 -> 100）。
# 原因不是工具没生效，而是《黑神话：悟空》的帧生成只有「开/关」，
# 游戏自己固定请求 1 个生成帧（2X）。MaxGeneratedFrames 是**上限**，
# 游戏不请求，写多少都没用。
#
# 按上游说明：实际生成帧数由游戏请求，并钳到运行库上限。
MULTIPLIER_NOTE = (
    "注意：「最高倍率」只是个上限，实际用几倍由游戏决定。\n"
    "游戏只有「开/关」没有倍率选项的话（黑神话就是这样），"
    "它固定按 2X 跑，改上限不会有变化 —— 这是游戏侧的限制。"
)


def predict(
    exe_name: str,
    exe_dir: str | Path,
    api_level: str = "unknown",
    api_can_install: bool = True,
    game_name: str = "",
) -> Prediction:
    """给出这个游戏开帧生成的前景判断。"""
    d = Path(exe_dir)
    p = Prediction()

    # 先看同目录（快），没有再往插件目录里找（UE 游戏都在深层）
    fg = _dir_has(d, FG_COMPONENTS) or _find_components(d, FG_COMPONENTS)
    sr = _dir_has(d, SR_COMPONENTS) or _find_components(d, SR_COMPONENTS)

    # ---- 硬性阻断：图形 API 不对 ----
    if not api_can_install:
        p.level = Support.NO
        if api_level == "vulkan":
            p.headline = "这个游戏走 Vulkan，当前版本不支持"
            p.advice = "上游把 Vulkan 支持排在后续版本，本工具暂时无能为力。"
        elif api_level == "d3d11_only":
            p.headline = "这个游戏只支持 D3D11"
            p.advice = "帧生成需要 D3D12。可以看看游戏设置里有没有 DX12 模式，有的话切过去再试。"
        else:
            p.headline = "无法确认这个游戏用 D3D12 渲染"
            p.advice = "可以试着安装看看，但成功率不高。"
        p.reasons.append(f"图形 API 判定：{api_level}")
        return p

    # ---- 已知不支持的游戏 ----
    low_exe = (exe_name or "").lower()
    low_game = (game_name or "").lower()
    for key, note in KNOWN_NO_FG.items():
        if not note:
            continue
        if key in low_exe or key in low_game:
            p.level = Support.UNLIKELY
            p.headline = note
            p.advice = (
                "这类游戏可以试试本工具，但游戏没做的功能工具也变不出来。\n"
                "如果游戏设置里本来就有「帧生成」选项（哪怕灰着），成功率会高很多。"
            )
            p.reasons.append(note)
            return p

    # ---- 有帧生成组件 ----
    #
    # 注意：组件存在 ≠ 游戏开放了这个功能。
    # 反例《幻兽帕鲁》：nvngx_dlssg.dll / sl.dlss_g.dll 一应俱全，
    # EXE 里也有 slDLSSGSetOptions 的调用链，但游戏 UI 根本没给帧生成开关。
    # 所以措辞要留余地，别让用户以为"文件在就一定能开"。
    if fg:
        p.level = Support.GOOD
        p.headline = "这个游戏带了帧生成组件"
        p.reasons.append(f"同目录有 {', '.join(fg)}")
        # 已知这个游戏不给倍率选择的话，明确点出来，免得用户以为工具没生效
        no_mult = _known_no_multiplier(exe_name, game_name)
        if no_mult:
            p.headline = f"{no_mult}带了帧生成组件，但只有「开/关」没有倍率选择"
            p.reasons.append(f"{no_mult}的帧生成是二选一开关，固定按 2X 跑")
            p.advice = (
                "装完完全退出游戏再启动，在画面设置里打开「帧生成」。\n"
                f"这个游戏只给「开/关」，所以最高倍率选什么都没区别 —— "
                "它只会生成 1 帧（2X）。\n"
                "想跑更高倍率需要游戏自己支持倍率选择。"
            )
        else:
            p.advice = (
                "组件齐全说明游戏技术上支持，但能不能开还取决于游戏有没有"
                "把这个开关放到画面上。\n"
                "进去看画面设置里有没有「帧生成 / Frame Generation」这一项："
                "有的话打开就行；没有的话就是游戏没开放，工具无法强行打开。\n"
                + MULTIPLIER_NOTE
            )
        return p

    # ---- 有超分但没帧生成组件 ----
    if sr:
        p.level = Support.UNLIKELY
        p.headline = "这个游戏有 DLSS 超分，但没看到帧生成组件"
        p.reasons.append(f"同目录有 {', '.join(sr)}，但没有 {'/'.join(FG_COMPONENTS)}")
        p.advice = (
            "游戏可能只有超分没有帧生成。\n"
            "可以装上去试试 —— 有些游戏把帧生成插件放在别的目录，扫描不到。\n"
            "判断标准：进游戏看画面设置里有没有「帧生成」这一项。"
        )
        return p

    # ---- 什么都没有 ----
    p.level = Support.MAYBE
    p.headline = "没在游戏目录里找到 DLSS 相关组件"
    p.reasons.append("同目录既没有 nvngx_dlssg.dll 也没有 nvngx_dlss.dll")
    p.advice = (
        "有些游戏把插件放在子目录或引擎目录里，所以没扫到不代表一定不行。\n"
        "可以装上去碰碰运气；进游戏后看画面设置里有没有「帧生成」。"
    )
    return p


__all__ = [
    "Support",
    "Prediction",
    "predict",
    "FG_COMPONENTS",
    "SR_COMPONENTS",
    "MULTIPLIER_NOTE",
    "KNOWN_NO_FG",
    "KNOWN_NO_MULTIPLIER",
]
