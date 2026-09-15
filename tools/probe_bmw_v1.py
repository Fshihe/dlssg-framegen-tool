"""v1（dashdogy / OptiScaler + XeSSMFG）在黑神话测试工具上的验证。

判据沿用 probe_bmw_opti.py：必须出现 `XeFG_Dx12::Activate SetEnabled: true`，
否则本次没有任何结论 —— 不算通过也不算失败。

两个用例把两件事分开：
  as-shipped   —— 原样配置（FGInput=DLSSG）。黑神话没有 OptiPatcher，
                  预期拿不到 DLSSG 输入。
  upscaler     —— 只把 FGInput 换成 upscaler。这是我们已知能跑通的输入路径，
                  用来单独判断 XeSSMFG 那套「拆 Ada 门槛」是否真的有效。

用法：
    python tools/probe_bmw_v1.py                 # 跑全部用例
    python tools/probe_bmw_v1.py upscaler        # 只跑某个
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

GAME = Path(r"D:\steam\steamapps\common\Black Myth Wukong Benchmark Tool")
WIN64 = GAME / "b1" / "Binaries" / "Win64"
LOG = WIN64 / "OptiScaler.log"
INI = WIN64 / "OptiScaler.ini"
TPL = WIN64 / "OptiScaler.ini.v1template"
XLOG = WIN64 / "XeSSMFG" / "XeSSMFG.log"
EROOT = Path(r"E:\AI project\v4.1f\dlssg-tool")
sys.path.insert(0, str(EROOT / "src"))

# 每个用例都需要的固定项：留日志、强制无边框（XeFG 不支持独占全屏）、开 MFG 解锁
BASE_EDITS = [
    ("Log", "LogToFile", "true"),
    ("Log", "LogLevel", "2"),
    ("XeFG", "ForceBorderless", "true"),
    ("XeFG", "MFGUnlock", "true"),
    ("XeFG", "InterpolationCount", "1"),
]

CASES = {
    "as-shipped": [],
    "upscaler": [("FrameGen", "FGInput", "upscaler")],
}


def kill_game() -> None:
    for n in ("b1-Win64-Shipping.exe", "b1_benchmark.exe"):
        subprocess.run(["taskkill", "/F", "/IM", n], capture_output=True, text=True)
    time.sleep(2)


def wait_for_activation(timeout: int) -> tuple[bool, float]:
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(4)
        if not LOG.is_file():
            continue
        try:
            txt = LOG.read_text("utf-8", "ignore")
        except OSError:
            continue
        if "XeFG_Dx12::Activate SetEnabled: true" in txt:
            return True, time.time() - t0
    return False, time.time() - t0


def write_ini(edits: list[tuple[str, str, str]]) -> None:
    from dgcore.optiscaler import _set_in_section

    text = TPL.read_text("utf-8", "ignore")
    for sec, k, v in BASE_EDITS + edits:
        text = _set_in_section(text, sec, k, v)
    INI.write_text(text, "utf-8", newline="\n")


def probe(label: str, edits: list[tuple[str, str, str]],
          launch_timeout: int = 200, sample: int = 20) -> dict:
    kill_game()
    write_ini(edits)

    LOG.unlink(missing_ok=True)
    if XLOG.is_file():
        XLOG.unlink()
    print(f"\n{'=' * 72}\n用例：{label}")
    for sec, k, v in BASE_EDITS + edits:
        print(f"   [{sec}] {k} = {v}")
    print("=" * 72, flush=True)

    subprocess.Popen([str(GAME / "b1_benchmark.exe")], cwd=str(GAME))
    activated, waited = wait_for_activation(launch_timeout)
    print(f"   XeFG 激活: {activated}  （等待 {waited:.0f}s）", flush=True)

    res = {"label": label, "activated": activated}

    if activated:
        time.sleep(sample)
    txt = LOG.read_text("utf-8", "ignore") if LOG.is_file() else ""
    kill_game()

    res["log_bytes"] = len(txt)
    res["xessmfg_log"] = XLOG.is_file()
    res["activate"] = txt.count("XeFG_Dx12::Activate SetEnabled: true")
    errs = re.findall(r"\[E\] ([^\n]+)", txt)
    res["errors"] = len(errs)
    res["err_kinds"] = sorted({e.split(":")[-1].strip()[:70] for e in errs})[:4]
    interp = re.findall(r"Interpolation count changed \S+ -> (\d+)", txt)
    res["interp"] = interp[-1] if interp else "-"
    mfg = re.findall(r"MFG enabled up to (\d+)X", txt)
    res["mfg_unlock"] = mfg[-1] if mfg else "-"

    # XeSSMFG / XeLL / 解锁相关的关键行
    keys = [ln for ln in txt.splitlines()
            if re.search(r"XeSSMFG|XeSSMfg|MFGUnlock|MFG|XeLL|maximum|Available", ln)
            and "TVRAM" not in ln]
    res["key_lines"] = keys[:12]

    # 先落盘再打印 —— 控制台编码问题不该把证据弄丢
    safe = label.replace(" ", "_")
    if txt:
        (EROOT / "work" / f"probe-v1-{safe}.log").write_text(txt, "utf-8")
    if XLOG.is_file():
        (EROOT / "work" / f"probe-v1-{safe}.xessmfg.log").write_text(
            XLOG.read_text("utf-8", "ignore"), "utf-8")

    print(f"   日志大小        : {res['log_bytes']:,} 字节")
    print(f"   XeSSMFG 自己的日志: {'有' if res['xessmfg_log'] else '没有'}")
    print(f"   Activate 次数   : {res['activate']}")
    print(f"   插值帧数        : {res['interp']}")
    print(f"   MFG 解锁上限    : {res['mfg_unlock']}X")
    print(f"   报错总数        : {res['errors']}")
    if res["err_kinds"]:
        for k in res["err_kinds"]:
            print(f"       · {k}")
    if res["key_lines"]:
        print("   XeSSMFG/MFG 关键行：")
        for ln in res["key_lines"]:
            print(f"       {ln[:160]}")
    if activated:
        print(f"   判定            : {'✓ 出帧' if res['errors'] == 0 else '✗ 出帧但报错'}")
    else:
        print("   判定            : ⚠ 未激活 —— 无结论")

    safe = label.replace(" ", "_")
    if txt:
        (EROOT / "work" / f"probe-v1-{safe}.log").write_text(txt, "utf-8")
    if XLOG.is_file():
        (EROOT / "work" / f"probe-v1-{safe}.xessmfg.log").write_text(
            XLOG.read_text("utf-8", "ignore"), "utf-8")
    return res


def main() -> int:
    if not GAME.is_dir():
        print("找不到游戏目录")
        return 2
    which = sys.argv[1:] or list(CASES)
    results = [probe(n, CASES[n]) for n in which if n in CASES]

    print(f"\n{'=' * 72}\n汇总\n{'=' * 72}")
    for r in results:
        if not r["activated"]:
            print(f"  {r['label']:14} 未激活 —— 无结论")
        else:
            print(f"  {r['label']:14} Activate {r['activate']:3}  插值 {r['interp']}  "
                  f"MFG {r['mfg_unlock']}X  报错 {r['errors']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
