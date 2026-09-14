"""验证「游戏能力预判」在真实游戏上的表现。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dgcore import capability, games  # noqa: E402

FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global FAIL
    if not cond:
        FAIL += 1
    print(f"  [{'OK ' if cond else 'FAIL'}] {label}" + (f"   {detail}" if detail and not cond else ""))


CASES = [
    (r"D:\steam\steamapps\common\Black Myth Wukong Benchmark Tool", "黑神话基准测试"),
    (r"D:\steam\steamapps\common\Palworld", "幻兽帕鲁"),
    (r"D:\steam\steamapps\common\3DMark", "3DMark"),
    (r"d:\steam\steamapps\common\Goose Goose Duck", "鹅鸭杀（Vulkan）"),
]


def main() -> int:
    print("=" * 74)
    print("真实游戏的能力预判")
    print("=" * 74)
    for path, label in CASES:
        root = Path(path)
        if not root.is_dir():
            print(f"  (跳过 {label})")
            continue
        cands = games.find_render_exes(root, preset="标准")
        elig = [c for c in cands if c.eligible]
        best = max(elig, key=lambda c: c.score) if elig else (cands[0] if cands else None)
        if best is None:
            print(f"  {label}: 没有候选")
            continue
        pred = capability.predict(
            exe_name=best.name,
            exe_dir=best.directory,
            api_level=best.api_level,
            api_can_install=best.eligible,
            game_name=label,
        )
        print(f"\n  【{label}】{best.name}")
        print(f"    {pred.level.label} —— {pred.headline}")
        for r in pred.reasons:
            print(f"      · {r}")
        print(f"    建议：{pred.advice.replace(chr(10), chr(10) + '          ')}")

    print()
    print("=" * 74)
    print("单元断言")
    print("=" * 74)

    import tempfile

    tmp = Path(tempfile.mkdtemp())

    # 有帧生成组件 → GOOD
    d1 = tmp / "g1"
    d1.mkdir()
    (d1 / "nvngx_dlssg.dll").write_bytes(b"x")
    p1 = capability.predict("game.exe", d1, "confirmed", True)
    check("有 nvngx_dlssg.dll → 大概率可以", p1.level == capability.Support.GOOD, p1.level.value)

    # 只有超分 → UNLIKELY
    d2 = tmp / "g2"
    d2.mkdir()
    (d2 / "nvngx_dlss.dll").write_bytes(b"x")
    p2 = capability.predict("game.exe", d2, "confirmed", True)
    check("只有超分组件 → 不太可能", p2.level == capability.Support.UNLIKELY, p2.level.value)

    # 什么都没有 → MAYBE
    d3 = tmp / "g3"
    d3.mkdir()
    p3 = capability.predict("game.exe", d3, "confirmed", True)
    check("没有 DLSS 组件 → 可以试", p3.level == capability.Support.MAYBE, p3.level.value)

    # Vulkan → NO
    p4 = capability.predict("game.exe", d3, "vulkan", False)
    check("Vulkan → 不支持", p4.level == capability.Support.NO, p4.level.value)
    check("Vulkan 的建议提到后续版本", "Vulkan" in p4.advice, p4.advice)

    # 仅 D3D11 → NO
    p5 = capability.predict("game.exe", d3, "d3d11_only", False)
    check("仅 D3D11 → 不支持", p5.level == capability.Support.NO, p5.level.value)
    check("D3D11 的建议提到切 DX12", "DX12" in p5.advice, p5.advice)

    # 已知游戏
    p6 = capability.predict("Palworld-Win64-Shipping.exe", d3, "confirmed", True, "Palworld")
    check("帕鲁被识别为已知无帧生成", p6.level == capability.Support.UNLIKELY, p6.level.value)
    check("给出了具体原因", "帕鲁" in p6.headline, p6.headline)

    # installable 语义
    check("GOOD 可安装", p1.installable)
    check("UNLIKELY 仍允许试", p2.installable)
    check("NO 不允许安装", not p4.installable)

    print()
    print("=" * 74)
    print("结果：" + (f"{FAIL} 项失败" if FAIL else "全部通过"))
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
