"""把 v1（dashdogy / OptiScaler + XeSSMFG）装进指定游戏目录 —— 共存试验用。

这是**试验脚手架**，不是工具的一部分：它直接把 v1 压缩包里的白名单文件
拷进目标目录，并按共存场景写 OptiScaler.ini。

为什么需要它：工具对两个引擎是硬互斥的（怕两个 mod 同时挂 D3D12），
但 v1 的文档明确说它不碰 dlssg_sm86 目录，作者是按共存设计的。
要验证这个说法，就得绕开工具的互斥检查手工装。

用法：
    python tools/install_xess_v1.py "D:\\...\\Win64"            # 装
    python tools/install_xess_v1.py "D:\\...\\Win64" --fg-input upscaler
    python tools/install_xess_v1.py "D:\\...\\Win64" --uninstall
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

V1 = ROOT / "work" / "v1-extract" / "①推荐版本" / "OptiScaler-XeSSMFG-build"

# 只放运行必需的白名单。amd_fidelityfx_*（FSR 输出路径用）、libxess.dll（77 MB
# 的 XeSS 超分本体）、XeSSMFGProbe.exe、setup 脚本、docx 一律不放。
FILES = ["dxgi.dll", "XeSSMFG.dll", "libxess_fg.dll", "libxell.dll",
         "fakenvapi.dll", "fakenvapi.ini"]
DIRS = ["D3D12_Optiscaler", "Licenses", "XeSSMFG"]

# 装完留在游戏目录里的运行产物，卸载时一并清掉
RUNTIME_LEFTOVERS = ["OptiScaler.ini", "OptiScaler.log", "OptiScaler.ini.bak",
                     "fakenvapi.log", "XeSSMFG-UI-layout.ini", "XeSSMFG.log"]


def build_ini(fg_input: str, interpolation: int) -> str:
    from dgcore.optiscaler import _set_in_section

    text = (V1 / "OptiScaler.ini").read_text("utf-8", "ignore")
    edits = [
        ("Log", "LogToFile", "true"),
        ("Log", "LogLevel", "2"),
        ("FrameGen", "Enabled", "true"),
        ("FrameGen", "FGInput", fg_input),
        ("FrameGen", "FGOutput", "XeFG"),
        ("XeFG", "MFGUnlock", "true"),
        ("XeFG", "ForceBorderless", "true"),
        ("XeFG", "InterpolationCount", str(interpolation)),
    ]
    for sec, key, val in edits:
        text = _set_in_section(text, sec, key, val)
    return text


def uninstall(target: Path) -> int:
    removed = []
    for n in FILES + RUNTIME_LEFTOVERS:
        p = target / n
        if p.is_file():
            p.unlink()
            removed.append(n)
    for d in DIRS:
        p = target / d
        if p.is_dir():
            shutil.rmtree(p)
            removed.append(d + "/")
    print(f"已移除 {len(removed)} 项：{'、'.join(removed)}")
    return 0


def install(target: Path, fg_input: str, interpolation: int) -> int:
    if not V1.is_dir():
        print(f"找不到 v1 解压目录：{V1}")
        return 2
    if not target.is_dir():
        print(f"目标目录不存在：{target}")
        return 2

    for n in FILES:
        shutil.copy2(V1 / n, target / n)
    for d in DIRS:
        dst = target / d
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(V1 / d, dst)

    (target / "OptiScaler.ini").write_text(
        build_ini(fg_input, interpolation), "utf-8", newline="\n")

    print(f"目标     : {target}")
    print(f"FGInput  : {fg_input}")
    print(f"插值帧数 : {interpolation}（{interpolation + 1}X）")
    print(f"放入     : {'、'.join(FILES)} + {'、'.join(DIRS)} + OptiScaler.ini")
    print("保留原有的：version.dll / dlssg_sm86.ini / dlssg_sm86\\（引擎一）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("target")
    ap.add_argument("--fg-input", default="DLSSG", choices=["DLSSG", "upscaler"])
    ap.add_argument("--interpolation", type=int, default=1, choices=[1, 2, 3])
    ap.add_argument("--uninstall", action="store_true")
    args = ap.parse_args()

    target = Path(args.target)
    if args.uninstall:
        return uninstall(target)
    return install(target, args.fg_input, args.interpolation)


if __name__ == "__main__":
    raise SystemExit(main())
