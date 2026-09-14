"""可靠版 OptiScaler 试验：必须等到 XeFG 真的被激活才判定。

为什么重写
----------
上一版按"日志超过 200KB"就下结论。问题是：XeFG 报错时会疯狂刷日志，
于是"报错的运行"很快达标被读到，而"没报错的运行"其实只是**帧生成压根没启动**。
结果把"没激活"误判成了"修好了"。

正确判据只能是 XeFG_Dx12::Activate SetEnabled: true 出现过 ——
没出现就说明这一次没有任何结论，必须重试或换条件，不能算通过。
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
EROOT = Path(r"E:\AI project\v4.1f\dlssg-tool")
sys.path.insert(0, str(EROOT / "src"))


def kill_game() -> None:
    for n in ("b1-Win64-Shipping.exe", "b1_benchmark.exe"):
        subprocess.run(["taskkill", "/F", "/IM", n], capture_output=True, text=True)
    time.sleep(2)


def wait_for_activation(timeout: int) -> tuple[bool, float]:
    """等 XeFG 真正激活。返回 (是否激活, 等待秒数)。"""
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


def probe(label: str, edits: list[tuple[str, str, str]],
          launch_timeout: int = 200, sample: int = 20) -> dict:
    from dgcore import optiscaler as O
    from dgcore.optiscaler import _set_in_section

    kill_game()
    INI.write_text(O.build_ini("optiscaler-xess", 4), "utf-8", newline="\n")
    for sec, k, v in edits:
        INI.write_text(_set_in_section(INI.read_text("utf-8", "ignore"), sec, k, v),
                       "utf-8", newline="\n")

    LOG.unlink(missing_ok=True)
    print(f"\n{'='*72}\n试验：{label}")
    for sec, k, v in edits:
        print(f"   [{sec}] {k} = {v}")
    print(f"{'='*72}")

    subprocess.Popen([str(GAME / "b1_benchmark.exe")], cwd=str(GAME))
    activated, waited = wait_for_activation(launch_timeout)
    print(f"   XeFG 激活: {activated}  （等待 {waited:.0f}s）")

    if not activated:
        kill_game()
        print("   ⚠ 未激活 —— 本次没有任何结论（不算通过，也不算失败）")
        return {"label": label, "activated": False, "errors": 0}

    # 激活后再采样一段时间，统计报错
    time.sleep(sample)
    txt = LOG.read_text("utf-8", "ignore")
    kill_game()

    errs = re.findall(r"\[E\] XeFG Log: (.+)", txt)
    mismatch = sum(1 for e in errs if "resolutions must match" in e)
    acts = txt.count("XeFG_Dx12::Activate SetEnabled: true")
    interp = re.findall(r"Interpolation count changed \S+ -> (\d+)", txt)
    unlock = re.search(r"MFG enabled up to (\d+)X", txt)

    info = {
        "label": label, "activated": True,
        "errors": len(errs), "mismatch": mismatch,
        "activations": acts, "interp": interp[-1] if interp else "-",
        "unlock": unlock.group(1) if unlock else "-",
        "other": sorted({e.split(".")[0] for e in errs if "resolutions must match" not in e})[:3],
    }
    print(f"   Activate 次数   : {info['activations']}")
    print(f"   插值帧数        : {info['interp']}（{int(info['interp'])+1 if info['interp'].isdigit() else '?'}X）")
    print(f"   解锁上限        : {info['unlock']}X")
    print(f"   报错总数        : {info['errors']}（MV/Depth 不匹配 {info['mismatch']}）")
    if info["other"]:
        print(f"   其它错误        : {info['other']}")
    print(f"   判定            : {'✓ 正常出帧' if info['errors'] == 0 else '✗ 帧生成失败'}")
    (EROOT / "work" / f"probe-{label.replace(' ', '_').replace('=','')}.log").write_text(txt, "utf-8")
    return info


def main() -> int:
    if not GAME.is_dir():
        print("找不到游戏目录")
        return 2
    cases = {
        "current": [],
        "dlssg-input": [("FrameGen", "FGInput", "dlssg")],
        "no-highresmv": [("XeFG", "HighResMV", "false")],
    }
    which = sys.argv[1:] or ["current"]
    results = [probe(name, cases[name]) for name in which if name in cases]
    print(f"\n{'='*72}\n汇总\n{'='*72}")
    for r in results:
        if not r["activated"]:
            print(f"  {r['label']:16} 未激活 —— 无结论")
        else:
            print(f"  {r['label']:16} 报错 {r['errors']:6}  MV不匹配 {r['mismatch']:6}  "
                  f"插值 {r['interp']}  解锁 {r['unlock']}X")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
