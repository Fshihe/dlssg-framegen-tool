"""验证阶段的脚本 —— 分级判定与扫描性能。

每完成一个阶段都跑这个，确保没有回归。
用法：python tools/verify_stage1.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dgcore import gfxapi, pe  # noqa: E402
from dgcore.selftest import synth_pe  # noqa: E402

FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global FAIL
    if not cond:
        FAIL += 1
    print(f"  [{'OK ' if cond else 'FAIL'}] {label}" + (f"   {detail}" if detail and not cond else ""))


def main() -> int:
    print("=" * 74)
    print("1. 合成 PE 的各种图形 API 组合，判定是否准确")
    print("=" * 74)

    cases = [
        # (名称, 导入列表, 期望等级)
        ("纯 D3D12 游戏", ("KERNEL32.dll", "d3d12.dll", "dxgi.dll"),
         gfxapi.ApiLevel.CONFIRMED),
        ("D3D11+D3D12 并存（UE 典型）", ("KERNEL32.dll", "d3d11.dll", "d3d12.dll", "d3d9.dll", "dxgi.dll"),
         gfxapi.ApiLevel.CONFIRMED),
        ("只有 d3d12core", ("KERNEL32.dll", "d3d12core.dll"),
         gfxapi.ApiLevel.CONFIRMED),
        ("仅 D3D11（真不支持）", ("KERNEL32.dll", "d3d11.dll", "dxgi.dll"),
         gfxapi.ApiLevel.D3D11_ONLY),
        ("纯 DXGI 无 D3D（存疑）", ("KERNEL32.dll", "dxgi.dll"),
         gfxapi.ApiLevel.HINTED),
        ("什么都没有", ("KERNEL32.dll",),
         gfxapi.ApiLevel.UNKNOWN),
    ]

    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="gfxapi-test-"))
    for label, imports, want in cases:
        p = tmp / f"{label[:8]}.exe"
        p.write_bytes(synth_pe(imports))
        info = pe.inspect(p)
        v = gfxapi.judge(info.static_imports, info.delayed_imports, info.text_hits, p.parent, info.is_x64)
        ok = v.level == want
        check(f"{label} → {v.level.value}", ok, f"期望 {want.value}，实际 {v.level.value}（{v.evidence}）")

    print()
    print("=" * 74)
    print("2. 关键修复：同时导入 d3d11 不再导致误判")
    print("=" * 74)
    p = tmp / "both.exe"
    p.write_bytes(synth_pe(("KERNEL32.dll", "d3d11.dll", "d3d12.dll", "dxgi.dll")))
    info = pe.inspect(p)
    v = gfxapi.judge(info.static_imports, info.delayed_imports, info.text_hits, p.parent, info.is_x64)
    check("D3D11+D3D12 并存时判为可安装", v.can_install, f"level={v.level.value}")
    check("给出了「同时导入属正常」的说明",
          any("同时导入" in e for e in v.evidence), f"evidence={v.evidence}")

    print()
    print("=" * 74)
    print("3. 同目录旁证")
    print("=" * 74)
    gdir = tmp / "game"
    (gdir / "D3D12").mkdir(parents=True)
    (gdir / "D3D12" / "D3D12Core.dll").write_bytes(b"x" * 1000)
    (gdir / "nvngx_dlssg.dll").write_bytes(b"x" * 1000)
    # 一个导入表里完全没有 d3d12 的 exe（模拟动态加载）
    dyn = gdir / "Dynamic.exe"
    dyn.write_bytes(synth_pe(("KERNEL32.dll", "dxgi.dll")))
    info = pe.inspect(dyn)
    v = gfxapi.judge(info.static_imports, info.delayed_imports, info.text_hits, gdir, info.is_x64)
    check("靠旁证把动态加载的游戏判为可安装", v.can_install, f"level={v.level.value} score={v.score}")
    check("旁证被列出来了", any("D3D12" in e or "dlssg" in e for e in v.evidence), f"{v.evidence}")

    print()
    print("=" * 74)
    print("4. 真实游戏 + 扫描性能")
    print("=" * 74)
    real = [
        Path(r"D:\steam\steamapps\common\Black Myth Wukong Benchmark Tool\b1\Binaries\Win64\b1-Win64-Shipping.exe"),
        Path(r"D:\steam\steamapps\common\Palworld\Pal\Binaries\Win64\Palworld-Win64-Shipping.exe"),
    ]
    for rp in real:
        if not rp.is_file():
            print(f"  (跳过 {rp.name} —— 本机不存在)")
            continue
        t = time.time()
        info = pe.inspect(rp)
        dt = time.time() - t
        v = gfxapi.judge(info.static_imports, info.delayed_imports, info.text_hits, rp.parent, info.is_x64)
        print(f"\n  {rp.name}  ({rp.stat().st_size / 1048576:.0f} MB)")
        print(f"    解析耗时 {dt * 1000:.0f} ms，实际扫描 {info.scanned_bytes / 1048576:.2f} MB")
        print(f"    静态导入数 {len(info.static_imports)}  延迟导入数 {len(info.delayed_imports)}")
        for line in v.detail_lines():
            print(f"    {line}")
        check(f"{rp.name} 判定为可安装", v.can_install, f"level={v.level.value}")
        check(f"{rp.name} 解析在 1 秒内", dt < 1.0, f"{dt*1000:.0f} ms")

    print()
    print("=" * 74)
    if FAIL:
        print(f"结果：{FAIL} 项失败")
    else:
        print("结果：全部通过")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
