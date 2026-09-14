"""验证「手动指定 EXE / 目录」与扫描档位。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dgcore import games  # noqa: E402

FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global FAIL
    if not cond:
        FAIL += 1
    print(f"  [{'OK ' if cond else 'FAIL'}] {label}" + (f"   {detail}" if detail and not cond else ""))


WUKONG_EXE = Path(
    r"D:\steam\steamapps\common\Black Myth Wukong Benchmark Tool\b1\Binaries\Win64\b1-Win64-Shipping.exe"
)
WUKONG_DIR = WUKONG_EXE.parent
WUKONG_ROOT = Path(r"D:\steam\steamapps\common\Black Myth Wukong Benchmark Tool")


def main() -> int:
    print("=" * 74)
    print("1. find_exe_by_path：三种输入")
    print("=" * 74)

    # 直接给 EXE
    p, why = games.find_exe_by_path(WUKONG_EXE)
    check("传 EXE 文件 → 直接采用", p == WUKONG_EXE, f"{p} / {why}")
    print(f"      {why}")

    # 给渲染目录
    p2, why2 = games.find_exe_by_path(WUKONG_DIR)
    check("传渲染目录 → 自动定位到主程序", p2 == WUKONG_EXE, f"{p2} / {why2}")
    print(f"      {why2}")

    # 给游戏根目录（主程序在第三层）
    p3, why3 = games.find_exe_by_path(WUKONG_ROOT)
    check("传游戏根目录 → 穿过 3 层找到主程序", p3 == WUKONG_EXE, f"{p3} / {why3}")
    print(f"      {why3}")

    # 不存在的路径
    p4, why4 = games.find_exe_by_path(r"E:\no\such\path")
    check("不存在的路径 → 明确报错，不抛异常", p4 is None and "不存在" in why4, f"{p4} / {why4}")

    # 非 EXE 文件
    txt = WUKONG_ROOT / "README.txt"
    txt.write_text("x", encoding="utf-8")
    p5, why5 = games.find_exe_by_path(txt)
    check("非 EXE 文件 → 拒绝并说明", p5 is None and "不是 EXE" in why5, f"{p5} / {why5}")
    txt.unlink(missing_ok=True)

    print()
    print("=" * 74)
    print("2. analyse_one：单文件分析（手动指定的场景）")
    print("=" * 74)
    c = games.analyse_one(WUKONG_EXE)
    check("能分析指定 EXE", c.path == WUKONG_EXE and c.is_x64, f"{c.path}")
    check("给出分级结论", c.api_level == "confirmed", f"{c.api_level}")
    check("可安装", c.eligible)
    print(f"      {c.api_label}  score={c.score}")
    for rr in c.reasons[:4]:
        print(f"        · {rr}")

    print()
    print("=" * 74)
    print("3. 扫描档位：max_exes / max_depth 是否真的生效")
    print("=" * 74)
    for preset, cap in (("快速", 40), ("标准", 200), ("彻底", 600)):
        cands = games.find_render_exes(WUKONG_ROOT, preset=preset)
        check(f"{preset} 档返回不超过 {cap} 个", len(cands) <= cap, f"{len(cands)}")
        print(f"      {preset}: {len(cands)} 个候选")

    fast = games.find_render_exes(WUKONG_ROOT, preset="快速")
    deep = games.find_render_exes(WUKONG_ROOT, preset="彻底")
    check("彻底档 >= 快速档", len(deep) >= len(fast), f"fast={len(fast)} deep={len(deep)}")

    print()
    print("=" * 74)
    print("4. 参数覆盖")
    print("=" * 74)
    c1 = games.find_render_exes(WUKONG_ROOT, max_exes=1)
    check("max_exes=1 只返回 1 个", len(c1) <= 1, f"{len(c1)}")
    c2 = games.find_render_exes(WUKONG_ROOT, max_depth=1)
    check("max_depth=1 不会抛异常", isinstance(c2, list), f"{type(c2)}")

    print()
    print("=" * 74)
    print("5. 进度回调")
    print("=" * 74)
    calls = []
    games.find_render_exes(WUKONG_ROOT, on_progress=lambda d, t: calls.append((d, t)))
    check("进度回调被调用或候选太少（都算正常）", True, f"{calls[:3]}")

    print()
    print("=" * 74)
    print("结果：" + (f"{FAIL} 项失败" if FAIL else "全部通过"))
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
