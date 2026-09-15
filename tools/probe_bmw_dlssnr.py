"""DLSS 5 神经网络渲染（[DlssNr] 通道）在黑神话测试工具上的验证。

为什么用 AutoCapture
--------------------
模板里 [DlssNr] AutoCapture 默认 true：通道首次运行时会在 OptiScaler 旁写
`dlssnr-capture` 文件夹，保存**匹配的前后对比帧**。这让我不用靠肉眼就能判断
模型到底有没有改画面 —— 那批是图片，可以直接看。

隔离：本次把 [FrameGen] Enabled 关掉，只留 [DlssNr]，避免帧生成的日志盖过它。

注意：payload/optiscaler-dlss5 里**没有** libxess_fg/libxell/fakenvapi，
所以这个包做不了 XeSS 帧生成，只能测 DLSS NR。

用法：
    python tools/probe_bmw_dlssnr.py            # 装 + 跑 + 取证据
    python tools/probe_bmw_dlssnr.py --cleanup  # 只清场
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

GAME = Path(r"D:\steam\steamapps\common\Black Myth Wukong Benchmark Tool")
WIN64 = GAME / "b1" / "Binaries" / "Win64"
LOG = WIN64 / "OptiScaler.log"
INI = WIN64 / "OptiScaler.ini"
CAPTURE = WIN64 / "dlssnr-capture"
PAYLOAD = ROOT / "payload" / "optiscaler-dlss5"

FILES = ["dxgi.dll", "nvngx_dlssnr.dll", "nvngx.dll_dlssnr.dll"]
DIRS = ["D3D12_Optiscaler", "Licenses"]
LEFTOVERS = ["OptiScaler.ini", "OptiScaler.log", "fakenvapi.log"]

EDITS = [
    ("Log", "LogToFile", "true"),
    ("Log", "LogLevel", "2"),
    ("Log", "SingleFile", "true"),
    ("Upscalers", "Dx12Upscaler", "dlss"),
    ("FrameGen", "Enabled", "false"),      # 隔离：本次不测帧生成
    ("DlssNr", "AutoCapture", "true"),
    ("DlssNr", "DebugView", "0"),
    ("Plugins", "LoadAsiPlugins", "false"),
    ("Plugins", "LoadSpecialK", "false"),
    ("Plugins", "LoadReshade", "false"),
]


def clean() -> None:
    for n in FILES + LEFTOVERS:
        p = WIN64 / n
        if p.is_file():
            p.unlink()
    for d in DIRS + ["dlssnr-capture"]:
        p = WIN64 / d
        if p.is_dir():
            shutil.rmtree(p)
    print("已清场")


def install(nr_mode: str = "on") -> None:
    for n in FILES:
        shutil.copy2(PAYLOAD / n, WIN64 / n)
    for d in DIRS:
        dst = WIN64 / d
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(PAYLOAD / d, dst)

    from dgcore.optiscaler import _set_in_section

    edits = EDITS + [("DlssNr", "Enabled", nr_mode)]
    text = (PAYLOAD / "OptiScaler.ini.template").read_text("utf-8", "ignore")
    for sec, k, v in edits:
        text = _set_in_section(text, sec, k, v)
    INI.write_text(text, "utf-8", newline="\n")
    print(f"已安装 dlss5 bundle 并写入 OptiScaler.ini（DlssNr.Enabled={nr_mode}）")
    for sec, k, v in edits:
        print(f"   [{sec}] {k} = {v}")


def kill_game() -> None:
    for n in ("b1-Win64-Shipping.exe", "b1_benchmark.exe"):
        subprocess.run(["taskkill", "/F", "/IM", n], capture_output=True, text=True)
    time.sleep(2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cleanup", action="store_true")
    ap.add_argument("--run-seconds", type=int, default=180)
    ap.add_argument("--nr", default="on", choices=["on", "off", "auto"],
                    help="on/off/auto。auto=留上游默认值（工具不动这个键）")
    args = ap.parse_args()

    if args.cleanup:
        kill_game()
        clean()
        return 0

    kill_game()
    LOG.unlink(missing_ok=True)
    install(args.nr)

    print(f"\n启动 b1_benchmark.exe，跑 {args.run_seconds} 秒后结束……", flush=True)
    subprocess.Popen([str(GAME / "b1_benchmark.exe")], cwd=str(GAME))
    time.sleep(args.run_seconds)
    kill_game()

    if not LOG.is_file():
        print("没有 OptiScaler.log —— 代理可能没被加载")
        return 1

    txt = LOG.read_text("utf-8", "ignore")
    (ROOT / "work" / "probe-dlssnr.log").write_text(txt, "utf-8")
    lines = txt.splitlines()
    errs = [ln for ln in lines if "[E]" in ln]

    print(f"\n日志 {len(txt):,} 字节 / {len(lines)} 行 / 报错 {len(errs)} 条")
    print("\n=== DlssNr / dlssnr / NR 相关 ===")
    hit = [ln for ln in lines
           if re.search(r"[Dd]lssNr|dlssnr|DLSSNR|neural|Neural|NR ", ln)]
    for ln in hit[:40]:
        print("  " + ln[:190])
    if not hit:
        print("  （没有任何 DlssNr 相关行）")

    print("\n=== 报错（前 8 条） ===")
    for ln in errs[:8]:
        print("  " + ln[:190])

    print(f"\n=== dlssnr-capture ===")
    if CAPTURE.is_dir():
        files = sorted(CAPTURE.rglob("*"))
        print(f"  存在，{len(files)} 项")
        for f in files[:20]:
            print(f"   {f.relative_to(CAPTURE)}  {f.stat().st_size if f.is_file() else ''}")
    else:
        print("  不存在（说明通道没跑到写捕获那一步）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
