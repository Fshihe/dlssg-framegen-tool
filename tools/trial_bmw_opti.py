"""在黑神话基准测试里做一次 OptiScaler 配置试验。

流程：改 INI 的一个键 → 启动基准测试 → 等它初始化 → 统计 XeFG 的报错数
→ 杀掉进程。用来判断哪个开关能解决 "motion vector and depth resource
resolutions must match"。

只做一次性试验，不做持久化改动。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

GAME = Path(r"D:\steam\steamapps\common\Black Myth Wukong Benchmark Tool")
WIN64 = GAME / "b1" / "Binaries" / "Win64"
LOG = WIN64 / "OptiScaler.log"
INI = WIN64 / "OptiScaler.ini"

EROOT = Path(r"E:\AI project\v4.1f\dlssg-tool")
sys.path.insert(0, str(EROOT / "src"))


def kill_game() -> None:
    for name in ("b1-Win64-Shipping.exe", "b1_benchmark.exe"):
        subprocess.run(["taskkill", "/F", "/IM", name],
                       capture_output=True, text=True)
    time.sleep(2)


def set_key(section: str, key: str, value: str) -> None:
    from dgcore.optiscaler import _set_in_section
    text = INI.read_text("utf-8", "ignore")
    INI.write_text(_set_in_section(text, section, key, value), "utf-8", newline="\n")


def run_trial(label: str, edits: list[tuple[str, str, str]], wait: int = 75) -> dict:
    kill_game()
    # 每次从"干净生成"的配置开始，保证试验之间可比
    from dgcore import optiscaler as O
    INI.write_text(O.build_ini("optiscaler-xess", 4), "utf-8", newline="\n")
    for sec, key, val in edits:
        set_key(sec, key, val)

    LOG.unlink(missing_ok=True)
    print(f"\n{'='*70}\n试验：{label}")
    for sec, key, val in edits:
        print(f"   [{sec}] {key} = {val}")
    print(f"{'='*70}")

    proc = subprocess.Popen([str(GAME / "b1_benchmark.exe")], cwd=str(GAME))
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(5)
        if LOG.is_file() and LOG.stat().st_size > 200_000:
            break

    time.sleep(5)
    text = LOG.read_text("utf-8", "ignore") if LOG.is_file() else ""
    kill_game()

    errs = re.findall(r"\[E\] XeFG Log: (.+)", text)
    mv_mismatch = sum(1 for e in errs if "resolutions must match" in e)
    created = "XeFG swapchain created" in text
    unlocked = re.search(r"MFG enabled up to (\d+)X", text)
    produced = re.findall(r"FrameGen.*?(?:generated|interpolat\w+).*", text, re.I)

    info = {
        "label": label,
        "swapchain": created,
        "unlock": unlocked.group(1) if unlocked else "-",
        "errors_total": len(errs),
        "mv_mismatch": mv_mismatch,
        "other_errors": sorted({e.split(".")[0] for e in errs if "resolutions must match" not in e})[:4],
    }
    print(f"   swapchain 建立 : {info['swapchain']}")
    print(f"   解锁上限       : {info['unlock']}X")
    print(f"   报错总数       : {info['errors_total']}（其中 MV/Depth 不匹配 {mv_mismatch}）")
    if info["other_errors"]:
        print(f"   其它错误       : {info['other_errors']}")
    verdict = "✓ 可用" if (created and mv_mismatch == 0 and len(errs) < 20) else "✗ 仍在报错"
    print(f"   判定           : {verdict}")
    # 把日志留档，便于事后比对
    if text:
        (EROOT / "work" / f"trial-{label.replace(' ', '_').replace('/', '_')}.log").write_text(
            text, "utf-8")
    return info


def main() -> int:
    if not GAME.is_dir():
        print("找不到黑神话基准测试目录")
        return 2

    trials = [
        ("baseline", []),
        ("HighResMV=true", [("XeFG", "HighResMV", "true")]),
        ("MakeDepthCopy=true", [("OptiFG", "MakeDepthCopy", "true")]),
        ("HighResMV+MakeDepthCopy", [("XeFG", "HighResMV", "true"),
                                     ("OptiFG", "MakeDepthCopy", "true")]),
        ("EnableDepthScale=true", [("Inputs", "EnableDepthScale", "true")]),
    ]
    which = sys.argv[1:] or [t[0] for t in trials]
    results = []
    for label, edits in trials:
        if label not in which:
            continue
        results.append(run_trial(label, edits))

    print(f"\n{'='*70}\n汇总\n{'='*70}")
    for r in results:
        print(f"  {r['label']:26} swapchain={str(r['swapchain']):5} "
              f"unlock={r['unlock']:>2}X  MV不匹配={r['mv_mismatch']:<4} 总错误={r['errors_total']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
