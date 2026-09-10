"""SM75（RTX 20 系列）路径的本机可验证部分 —— 去笔记本实测前的预演。

在 2070 笔记本上跑之前，先在这台机器上把不依赖硬件的部分全部验掉：
型号识别、INI 生成、完整安装/校验/卸载链路。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dgcore import gpu, installer  # noqa: E402

FAILED = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global FAILED
    if not cond:
        FAILED += 1
    print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f"   {extra}" if extra and not cond else ""))


def main() -> int:
    print("=" * 74)
    print("1. RTX 20 系列型号识别（含笔记本 / SUPER / Ti / Max-Q 变体）")
    print("=" * 74)
    names = [
        "NVIDIA GeForce RTX 2070",
        "NVIDIA GeForce RTX 2070 Laptop GPU",
        "NVIDIA GeForce RTX 2070 Super",
        "NVIDIA GeForce RTX 2070 SUPER Laptop GPU",
        "NVIDIA GeForce RTX 2070 with Max-Q Design",
        "NVIDIA GeForce RTX 2070 Super with Max-Q Design",
        "NVIDIA GeForce RTX 2060",
        "NVIDIA GeForce RTX 2060 Laptop GPU",
        "NVIDIA GeForce RTX 2060 SUPER",
        "NVIDIA GeForce RTX 2060 Super with Max-Q Design",
        "NVIDIA GeForce RTX 2080",
        "NVIDIA GeForce RTX 2080 Laptop GPU",
        "NVIDIA GeForce RTX 2080 SUPER",
        "NVIDIA GeForce RTX 2080 Ti",
        "Quadro RTX 5000",
        "Quadro RTX 6000",
        "NVIDIA TITAN RTX",
    ]
    bad = []
    for n in names:
        sm, router, fam, why = gpu.classify(n)
        good = router == "SM75" and sm == "SM75"
        if not good:
            bad.append(n)
        print(f"  {'OK ' if good else '!! '} {n:46} -> {fam:22} router={router or '(无)'}")
    check(f"{len(names) - len(bad)}/{len(names)} 正确判为 SM75（{'、'.join(bad)}）", not bad)

    print()
    print("=" * 74)
    print("2. SM75 生成的 INI（对照上游 docs/NATIVE_INI.md）")
    print("=" * 74)
    # 故意传 HardwareBilinear=1，验证 SM75 上会被强制归零
    ini = installer.build_ini("SM75", hardware_bilinear=1, max_generated_frames=3, log_level=1)
    for line in ini.splitlines():
        print(f"    {line}")
    print()
    check("Router=SM75", "Router=SM75" in ini)
    check("KernelImage=PTX（SM75 必须用 PTX，不能用 Cubin）", "KernelImage=PTX" in ini)
    check("HardwareBilinear 被强制归零（SM75 不支持近似采样）", "HardwareBilinear=0" in ini)
    check("不含 HardwareBilinear=1", "HardwareBilinear=1" not in ini)
    check("MaxGeneratedFrames=3", "MaxGeneratedFrames=3" in ini)
    check("注释里写明对应 RTX 20 系列", "RTX 20 系列" in ini)

    print()
    print("=" * 74)
    print("3. 以 router=SM75 走完整安装 → 校验 → 卸载")
    print("=" * 74)
    tmp = Path(tempfile.mkdtemp(prefix="sm75-test-"))
    try:
        ed = tmp / "Game" / "Binaries" / "Win64"
        ed.mkdir(parents=True)
        exe = ed / "TQ2-Win64-Shipping.exe"
        exe.write_bytes(b"MZ" + b"\x00" * 4096)
        before = sorted(p.name for p in ed.iterdir())

        plan = installer.make_plan(ed, exe, "version.dll", "SM75", "SM75 测试游戏")
        res = installer.execute_plan(plan)
        check("安装成功", res.success, res.message)
        check("version.dll 就位", (ed / "version.dll").is_file())
        check("dlssg_sm86.ini 就位", (ed / "dlssg_sm86.ini").is_file())
        check(
            "落盘 INI 的 Router 是 SM75",
            installer.read_ini_router(ed / "dlssg_sm86.ini") == "SM75",
            installer.read_ini_router(ed / "dlssg_sm86.ini"),
        )
        txt = (ed / "dlssg_sm86.ini").read_text("utf-8")
        check("落盘 INI 里 HardwareBilinear=0", "HardwareBilinear=0" in txt)
        check(
            "DLL 与内置 payload 哈希一致",
            installer.sha256_file(ed / "version.dll")
            == installer.MANIFEST["version.dll"][0],
        )

        vr = installer.verify(ed)
        check("体检：已安装且健康", vr.installed and vr.healthy)

        ur = installer.uninstall(ed)
        check("卸载成功", ur.success, ur.message)
        check("目录逐字节还原", sorted(p.name for p in ed.iterdir()) == before)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 74)
    if FAILED:
        print(f"结果：{FAILED} 项失败 —— 先别去笔记本，这里就有问题")
    else:
        print("结果：本机可验证部分全部通过 ✅")
        print()
        print("这意味着工具侧的 SM75 逻辑（识别 / 配置生成 / 安装链路）没问题。")
        print("笔记本上要验证的是【硬件侧】：SM75 的 PTX 内核在真实 Turing 卡上能否跑起来。")
    print("=" * 74)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
