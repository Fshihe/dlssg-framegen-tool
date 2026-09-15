"""一键构建：校验 payload → 生成图标 → PyInstaller 打包 → 输出哈希。

用法：
    python build.py                    # 完整构建（DLSSG 两个版本 + OptiScaler 两个引擎包）
    python build.py --no-opti          # 不带 OptiScaler 引擎包（体积小一半以上）
    python build.py --slim             # 精简版：只内置 0.3.0 的 version.dll
    python build.py --no-verify        # 跳过构建后的自检

体积提示：OptiScaler 的 DLSS 5 引擎包里有 158 MB 的 nvngx_dlssnr.dll，
带上它产物会明显变大。只想测 XeSS 的话用 --no-opti 再单独放也行。
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
from dgcore import optiscaler  # noqa: E402
from dgcore import profiles  # noqa: E402
from dgcore import VERSION as APP_VERSION  # noqa: E402

# 产物名一律用 ASCII —— 中文名在 GitHub Release、部分解压工具和国外网盘上会乱码
APP_BASENAME = "framegen-unlock"


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


def dlssg_plan(slim: bool) -> list[tuple[str, str]]:
    if slim:
        return [("0.3.0", "version.dll")]
    return [
        (v, n)
        for v in profiles.all_versions()
        for n in profiles.get(v).proxy_names
    ]


def verify_payload(slim: bool, with_opti: bool) -> None:
    step("① 校验 payload 完整性")
    bad: list[str] = []
    n_files = 0

    # DLSSG
    plan = dlssg_plan(slim)
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
        print(f"  {'OK  ' if ok else 'FAIL'} {ver}/{name:16} {got[:20]}…  {p.stat().st_size:,} bytes")
        if not ok:
            bad.append(f"{ver}/{name}: SHA256 不符")
        n_files += 1

    # OptiScaler 引擎包
    n_opti = 0
    if with_opti:
        for key in optiscaler.bundle_keys():
            spec = optiscaler.get_bundle(key)
            ok, msg = optiscaler.bundle_available(key)
            print(f"  {'OK  ' if ok else 'FAIL'} {key:16} {spec.total_bytes:,} bytes  {msg}")
            if not ok:
                bad.append(f"{key}: {msg}")
            n_files += len(spec.files)
            n_opti += 1

    if bad:
        print("\n  payload 校验失败：")
        for b in bad:
            print(f"    - {b}")
        if with_opti:
            print("\n  提示：DLSSG 缺文件跑 python tools\\fetch_payload.py；")
            print("        OptiScaler 缺文件跑 python tools\\fetch_optiscaler.py")
        else:
            print("\n  提示：先跑 python tools\\fetch_payload.py 获取缺失的 DLL")
        raise SystemExit(1)

    extra = f"，另有 {n_opti} 个 OptiScaler 引擎包" if with_opti else ""
    print(f"\n  全部 {n_files} 个文件校验通过{extra}")


def make_icon() -> Path:
    step("② 生成程序图标")
    ico = ROOT / "assets" / "icon.ico"
    subprocess.run([sys.executable, str(ROOT / "tools" / "make_icon.py")], check=True)
    print(f"  图标已生成：{ico} ({ico.stat().st_size:,} bytes)")
    return ico


def pyinstaller(slim: bool, with_opti: bool) -> Path:
    step("③ PyInstaller 打包")
    ico = ROOT / "assets" / "icon.ico"
    sep = ";" if os.name == "nt" else ":"

    stage = WORK / "payload-stage"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    # 按 payload/<版本或引擎包>/<文件> 的目录结构打包进 exe
    plan = dlssg_plan(slim)
    total = 0
    for ver, name in plan:
        dst_dir = stage / ver
        dst_dir.mkdir(parents=True, exist_ok=True)
        src = PAYLOAD / ver / name
        shutil.copy2(src, dst_dir / name)
        total += src.stat().st_size
    n_vers = len({v for v, _ in plan})
    print(f"  内置 DLSSG payload：{len(plan)} 个文件 / {n_vers} 个版本，共 {total:,} bytes")

    n_opti_files = 0
    if with_opti:
        for key in optiscaler.bundle_keys():
            spec = optiscaler.get_bundle(key)
            root = optiscaler.payload_root(key)
            for rel, _h, _size in spec.files:
                src = root / rel
                dst = stage / key / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                total += src.stat().st_size
                n_opti_files += 1
            print(f"  内置引擎包 {key}：{len(spec.files)} 个文件，"
                  f"{spec.total_bytes:,} bytes")
    else:
        print("  （--no-opti：不内置 OptiScaler 引擎包）")

    print(f"  payload 合计：{total:,} bytes ({total / 1048576:.1f} MB)")

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


def write_checksums(exe: Path) -> Path:
    """生成发布用的 SHA256SUMS.txt。

    必须在每次构建后重新生成 —— PyInstaller 会嵌入构建时间戳，
    同一个源码两次构建出来的哈希是不同的，手工维护的校验和一定会过期。
    """
    step("④ 生成校验和文件")
    digest = sha256(exe)
    out = DIST / "SHA256SUMS.txt"
    lines = [
        f"# framegen-unlock  v{APP_VERSION}",
        "# 引擎一 DLSSG：RTX 30 系走 0.3.0（6X），RTX 20 系走 0.2.4（4X）",
        "# 引擎二 OptiScaler：XeSS 多帧生成 / DLSS 5 神经网络渲染（与引擎一互斥）",
        "# 上游：https://github.com/sdli1995/dlssg_for_sm86",
        f'# 校验：certutil -hashfile "{exe.name}" SHA256',
        "",
        f"{digest}  {exe.name}",
    ]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  {out}")
    print(f"  {digest}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slim", action="store_true", help="只内置 version.dll")
    ap.add_argument("--no-opti", action="store_true",
                    help="不内置 OptiScaler 引擎包（XeSS / DLSS 5），产物小很多")
    ap.add_argument("--no-verify", action="store_true", help="跳过产物自检")
    args = ap.parse_args()

    with_opti = not args.no_opti

    print(f"构建目录：{ROOT}")
    verify_payload(args.slim, with_opti)
    make_icon()
    exe = pyinstaller(args.slim, with_opti)
    if not args.no_verify:
        verify_build(exe)

    step("⑤ 完成")
    print(f"  文件   : {exe}")
    print(f"  体积   : {exe.stat().st_size:,} bytes ({exe.stat().st_size/1048576:.1f} MB)")
    digest = sha256(exe)
    print(f"  SHA256 : {digest}")

    if not args.no_verify:
        write_checksums(exe)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
