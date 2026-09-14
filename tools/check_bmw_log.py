"""跑完黑神话之后，一条命令给出结论。

用法（跑完基准测试、关掉游戏之后）：
    python tools/check_bmw_log.py

它只做三件事：确认 XeFG 到底有没有被激活、统计插值是否成功、给出结论。

判据为什么要这么严
------------------
之前吃过亏：按"有没有报错"下结论，结果把"帧生成压根没启动"误判成"修好了"。
所以这里**先看有没有 Activate**：
    没激活  → 本次无结论（不算成功也不算失败，游戏可能只是没进到渲染场景）
    有激活 + 0 报错  → 成功
    有激活 + 一堆报错 → 失败，并告诉你失败在哪
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

GAME = Path(r"D:\steam\steamapps\common\Black Myth Wukong Benchmark Tool")
LOG = GAME / "b1" / "Binaries" / "Win64" / "OptiScaler.log"
INI = GAME / "b1" / "Binaries" / "Win64" / "OptiScaler.ini"


def read(p: Path) -> str:
    try:
        return p.read_text("utf-8", "ignore")
    except OSError:
        return ""


def main() -> int:
    if not LOG.is_file():
        print(f"找不到日志：{LOG}")
        print("先把游戏跑一次。")
        return 2

    txt = read(LOG)
    ini = read(INI)

    print("=" * 68)
    print("黑神话 · XeSS 引擎测试结论")
    print("=" * 68)

    # 日志是不是比配置还旧 —— 那样的话这份结论属于上一次运行，不能算数
    try:
        lag = INI.stat().st_mtime - LOG.stat().st_mtime
    except OSError:
        lag = 0
    if lag > 5:
        print(f"\n⚠ 日志比配置旧了 {lag/60:.1f} 分钟 —— 说明当前配置还没被跑过。")
        print("  下面的结论属于**上一次**运行，不能代表现在的配置。")
        print("  请启动游戏跑一次基准测试，关掉后再运行本检查。")

    # 当前配置
    print("\n【配置】")
    for sec in ("FrameGen", "XeFG"):
        cur = ""
        for line in ini.splitlines():
            s = line.strip()
            if s.startswith("[") and s.endswith("]"):
                cur = s
                continue
            if cur != f"[{sec}]" or s.startswith(";") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            if k.strip() in ("Enabled", "FGInput", "FGOutput", "InterpolationCount",
                             "UnlockMFG", "MaxInterpolatedFrames", "HighResMV"):
                print(f"   {sec}.{k.strip():24} = {v.strip()}")

    # 引擎是否加载 / 解锁
    if "OptiScaler" not in txt:
        print("\n✗ OptiScaler 根本没加载 —— 安装位置不对？")
        return 1

    unlock = re.search(r"MFG enabled up to (\d+)X", txt)
    swap = "XeFG swapchain created" in txt
    maxi = re.search(r"Max supported interpolations: (\d+)", txt)

    print("\n【引擎状态】")
    print(f"   引擎加载        : 是")
    print(f"   MFG 解锁上限    : {unlock.group(1)+'X' if unlock else '无'}")
    print(f"   XeFG 交换链     : {'已建立' if swap else '未建立'}")
    print(f"   支持插值帧数    : {maxi.group(1) if maxi else '-'}"
          f"{'（= ' + str(int(maxi.group(1))+1) + 'X）' if maxi else ''}")

    # 关键：有没有真的激活
    acts = txt.count("XeFG_Dx12::Activate SetEnabled: true")
    interp = re.findall(r"Interpolation count changed \S+ -> (\d+)", txt)

    print("\n【帧生成是否真的跑起来】")
    print(f"   Activate 次数   : {acts}")
    print(f"   插值档位        : {interp[-1] + '（' + str(int(interp[-1])+1) + 'X）' if interp else '-'}")

    if acts == 0:
        print("\n⚠ 无结论：XeFG 从头到尾没有被激活。")
        print("  这不算成功也不算失败 —— 多半是游戏没进到实际渲染的场景。")
        print("  请确认你跑到了「开始测试」那一步，而不是停在设置界面。")
        return 3

    # 报错统计
    errs = re.findall(r"\[E\] XeFG Log: (.+)", txt)
    mismatch = sum(1 for e in errs if "resolutions must match" in e)
    other = sorted({e.split(".")[0] for e in errs if "resolutions must match" not in e})

    print("\n【插值是否成功】")
    print(f"   [E] 报错总数    : {len(errs)}")
    print(f"   其中 MV/深度不匹配: {mismatch}")
    if other:
        print(f"   其它错误        : {other[:3]}")

    print("\n" + "=" * 68)
    if len(errs) == 0:
        print("✓ 成功：XeFG 已激活且全程无报错，帧生成应当正在工作。")
        print("  可以看游戏里的帧数有没有明显上升来确认。")
        return 0
    if mismatch > 0:
        print("✗ 失败：帧生成每帧都中断在 MV / 深度分辨率不匹配。")
        print("  XeFG 要求两者分辨率一致，本游戏的输入不满足。")
        print("  换倍率、开关 MFG、调 HighResMV 都不会改变这一点。")
        return 1
    print("✗ 失败：有其它错误，见上面。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
