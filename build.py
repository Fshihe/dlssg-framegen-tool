"""一键构建：校验 payload → 生成图标 → PyInstaller 打包 → 输出哈希。

用法：
    python build.py              # 完整构建（内置两个上游版本的代理 DLL，离线自包含）
    python build.py --slim       # 精简版：只内置 0.3.0 的 version.dll
    python build.py --no-verify  # 跳过构建后的自检
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
PAYLOAD = ROOT / "payload"
DIST = ROOT / "dist"
WORK = ROOT / "work"

sys.path.insert(0, str(SRC))
from dgcore import profiles  # noqa: E402

# 产物名一律用 ASCII —— 中文名在 GitHub Release、部分解压工具和国外网盘上会乱码
APP_BASENAME = "dlssg-cn"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def step(msg: str) -> None:
    print()
    print("=" * 70)
    print(f"  {msg}")
    print("=" * 70)


def verify_payload(slim: bool) -> None:
    step("① 校验 payload 完整性")
    if slim:
        # 精简版只带 0.3.0 的 version.dll
        plan = [("0.3.0", "version.dll")]
    else:
        plan = [
            (v, n)
            for v in profiles.all_versions()
            for n in profiles.get(v).proxy_names
        ]

    bad = []
    for ver, name in plan:
        prof = profiles.get(ver)
        p = PAYLOAD / ver / name
        exp = prof.manifest.get(name)
        if not p.is_file():
            bad.append(f"{ver}/{name}: 文件缺失")
            continue
        exp_hash, exp_size = exp
        if p.stat().st_size != exp_size:
            bad.append(f"{ver}/{name}: 体积 {p.stat().st_size} != {exp_size}")
            continue
        got = sha256(p)
        ok = got == exp_hash
        print(f"  {'OK  ' if ok else 'FAIL'} {ver}/{name:14} {got[:20]}…  {p.stat().st_size:,} bytes")
        if not ok:
            bad.append(f"{ver}/{name}: SHA256 不符")

    if bad:
        print("\n  payload 校验失败：")
        for b in bad:
            print(f"    - {b}")
        print("\n  提示：先跑 python tools\\fetch_payload.py 获取缺失的 DLL")
        raise SystemExit(1)

    n_vers = len({v for v, _ in plan})
    print(f"\n  全部 {len(plan)} 个文件校验通过（覆盖 {n_vers} 个上游版本）")


def make_icon() -> Path:
    step("② 生成程序图标")
    ico = ROOT / "assets" / "icon.ico"
    subprocess.run([sys.executable, str(ROOT / "tools" / "make_icon.py")], check=True)
    print(f"  图标已生成：{ico} ({ico.stat().st_size:,} bytes)")
    return ico


def pyinstaller(slim: bool) -> Path:
    step("③ PyInstaller 打包")
    ico = ROOT / "assets" / "icon.ico"
    sep = ";" if os.name == "nt" else ":"

    stage = WORK / "payload-stage"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    # 按 payload/<版本>/<文件> 的目录结构打包进 exe
    if slim:
        plan = [("0.3.0", "version.dll")]
    else:
        plan = [
            (v, n)
            for v in profiles.all_versions()
            for n in profiles.get(v).proxy_names
        ]
    total = 0
    for ver, name in plan:
        dst_dir = stage / ver
        dst_dir.mkdir(parents=True, exist_ok=True)
        src = PAYLOAD / ver / name
        shutil.copy2(src, dst_dir / name)
        total += src.stat().st_size
    n_vers = len({v for v, _ in plan})
    print(f"  内置 payload：{len(plan)} 个文件 / {n_vers} 个版本，共 {total:,} bytes")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile",
        "--windowed",              # 双击不弹黑框
        "--noupx",                 # 不用 UPX：压缩壳会显著提高杀软误报率
        "--name", APP_BASENAME,
        "--icon", str(ico),
        "--add-data", f"{stage}{sep}payload",
        "--paths", str(SRC),
        "--distpath", str(DIST),
        "--workpath", str(WORK / "build"),
        "--specpath", str(WORK),
        str(SRC / "main.py"),
    ]
    print("  " + " ".join(cmd[:8]) + " …")
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        raise SystemExit(f"PyInstaller 失败，退出码 {r.returncode}")

    built = DIST / f"{APP_BASENAME}.exe"
    if not built.is_file():
        raise SystemExit(f"没有找到产物：{built}")

    print(f"\n  产物：{built}")
    return built


def verify_build(exe: Path) -> None:
    step("④ 构建产物自检（跑 exe 内置的 selftest）")
    r = subprocess.run([str(exe), "selftest"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300)
    tail = (r.stdout or "").strip().splitlines()
    for line in tail[-6:]:
        print("  " + line)
    if r.returncode != 0:
        print("  自检输出：")
        print(r.stdout)
        print(r.stderr)
        raise SystemExit(f"构建产物自检失败，退出码 {r.returncode}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slim", action="store_true", help="只内置 version.dll")
    ap.add_argument("--no-verify", action="store_true", help="跳过产物自检")
    args = ap.parse_args()

    print(f"构建目录：{ROOT}")
    verify_payload(args.slim)
    make_icon()
    exe = pyinstaller(args.slim)
    if not args.no_verify:
        verify_build(exe)

    step("⑤ 完成")
    print(f"  文件   : {exe}")
    print(f"  体积   : {exe.stat().st_size:,} bytes ({exe.stat().st_size/1048576:.1f} MB)")
    print(f"  SHA256 : {sha256(exe)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
