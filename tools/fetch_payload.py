"""从上游仓库获取 5 个代理 DLL，并逐个校验 SHA256。

为什么不在仓库里直接放 DLL：
  1. 仓库体积（5 × 15 MB = 78 MB）不适合进 git
  2. 更重要的 —— 这些二进制内嵌了 NVIDIA 的模型/kernel 资产，
     上游明确声明「not relicensed」。让构建者自己从上游取，
     来源链最清楚，本项目也不做这些文件的分发者。

用法：
    python tools/fetch_payload.py            # 下载缺失的文件
    python tools/fetch_payload.py --force    # 全部重新下载
    python tools/fetch_payload.py --check    # 只校验，不下载
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dgcore.payload_manifest import MANIFEST, PAYLOAD_VERSION  # noqa: E402

RAW = "https://raw.githubusercontent.com/sdli1995/dlssg_for_sm86/main"
# 上游包内位置 -> 落盘文件名
SOURCES = {
    "version.dll": "version.dll",
    "winmm.dll": "altnative/winmm.dll",
    "dinput8.dll": "altnative/dinput8.dll",
    "winhttp.dll": "altnative/winhttp.dll",
    "dxgi.dll": "altnative/dxgi.dll",
}
PAYLOAD = ROOT / "payload"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def verify(path: Path, name: str) -> tuple[bool, str]:
    exp_hash, exp_size = MANIFEST[name]
    if not path.is_file():
        return False, "文件不存在"
    size = path.stat().st_size
    if size != exp_size:
        return False, f"体积不符：{size} != {exp_size}"
    got = sha256(path)
    if got != exp_hash:
        return False, f"SHA256 不符：{got[:16]}… != {exp_hash[:16]}…"
    return True, f"{got[:16]}…  {size:,} bytes"


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "dlssg-tool-fetch"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if total:
                pct = done * 100 // total
                print(f"\r    {pct:3d}%  {done/1048576:6.1f} / {total/1048576:.1f} MB", end="")
    print()
    tmp.replace(dest)


def main() -> int:
    ap = argparse.ArgumentParser(description="获取上游代理 DLL")
    ap.add_argument("--force", action="store_true", help="已存在也重新下载")
    ap.add_argument("--check", action="store_true", help="只校验，不下载")
    args = ap.parse_args()

    print(f"目标目录：{PAYLOAD}")
    print(f"上游：sdli1995/dlssg_for_sm86  (DLSSG Native {PAYLOAD_VERSION})")
    print("=" * 70)

    bad: list[str] = []
    for name, rel in SOURCES.items():
        dest = PAYLOAD / name

        if not args.check and (args.force or not dest.is_file()):
            url = f"{RAW}/{rel}"
            print(f"[下载] {name}  <-  {rel}")
            try:
                download(url, dest)
            except Exception as exc:
                print(f"   失败：{exc}")
                bad.append(name)
                continue
        elif args.check and not dest.is_file():
            print(f"[缺失] {name}")
            bad.append(name)
            continue
        else:
            print(f"[跳过] {name}（已存在）")

        ok, msg = verify(dest, name)
        print(f"   校验：{'通过' if ok else '失败'}  {msg}")
        if not ok:
            bad.append(name)

    print("=" * 70)
    if bad:
        print(f"有 {len(bad)} 个文件未就绪：{'、'.join(bad)}")
        print()
        print("如果网络连不上 GitHub，可以手动从上游仓库下载同名文件放进 payload/：")
        for name, rel in SOURCES.items():
            print(f"  {name:14} <- {RAW}/{rel}")
        return 1

    print(f"全部 {len(SOURCES)} 个文件就绪且哈希校验通过。")
    print("其中 version.dll 的哈希与上游 README 公布的 0.2.4 官方值一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
