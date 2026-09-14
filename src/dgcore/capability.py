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
# 来源：评论区反馈 + 上游 README。用文件名/游戏名做宽松匹配。
KNOWN_NO_FG = {
    "palworld": "《幻兽帕鲁》只有 DLSS 超分，没有帧生成功能",
    "stardew": "《星露谷物语》是像素游戏，不涉及 DLSS",
    "goose goose": "《鹅鸭杀》走 Vulkan，且带反作弊",
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


FG_COMPONENTS = ("nvngx_dlssg.dll", "sl.dlss_g.dll")
SR_COMPONENTS = ("nvngx_dlss.dll", "sl.dlss.dll", "nvngx_dlssd.dll")


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

    fg = _dir_has(d, FG_COMPONENTS)
    sr = _dir_has(d, SR_COMPONENTS)

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

    # ---- 有帧生成组件：最好情况 ----
    if fg:
        p.level = Support.GOOD
        p.headline = "这个游戏自带 DLSS 帧生成组件，装上就能开"
        p.reasons.append(f"同目录有 {', '.join(fg)}")
        p.advice = "装完完全退出游戏再启动，在画面设置里打开「帧生成」。"
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


__all__ = ["Support", "Prediction", "predict", "FG_COMPONENTS", "SR_COMPONENTS"]
