"""从上游仓库获取代理 DLL，并逐个校验 SHA256。

为什么不在仓库里直接放 DLL：
  1. 仓库体积（两个版本共 11 个文件、约 170 MB）不适合进 git
  2. 更重要的 —— 这些二进制内嵌了 NVIDIA 的模型/kernel 资产，
     上游明确声明「not relicensed」。让构建者自己从上游取，
     来源链最清楚，本项目也不做这些文件的分发者。

会同时取两个版本的 payload：
  payload/0.2.4/  ← RTX 20 系用（最后一个支持 SM75 的版本）
  payload/0.3.0/  ← RTX 30 系用（支持 6X）

用法：
    python tools/fetch_payload.py                    # 下载缺失的文件
    python tools/fetch_payload.py --force            # 全部重新下载
    python tools/fetch_payload.py --check            # 只校验，不下载
    python tools/fetch_payload.py --version 0.3.0    # 只处理某个版本
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dgcore import profiles  # noqa: E402

RAW = "https://raw.githubusercontent.com/sdli1995/dlssg_for_sm86/main"
PAYLOAD = ROOT / "payload"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def verify(path: Path, name: str, prof) -> tuple[bool, str]:
    expect = prof.manifest.get(name)
    if expect is None:
        return False, f"不在 {prof.version} 的基线里"
    exp_hash, exp_size = expect
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
    with urllib.request.urlopen(req, timeout=180) as resp, open(tmp, "wb") as fh:
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
    ap.add_argument("--version", help="只处理某个版本（0.2.4 / 0.3.0）")
    args = ap.parse_args()

    print(f"目标目录：{PAYLOAD}")
    print("上游：sdli1995/dlssg_for_sm86")
    print("=" * 70)

    versions = [args.version] if args.version else profiles.all_versions()
    bad: list[str] = []

    for ver in versions:
        prof = profiles.get(ver)
        print(f"\n【{prof.version}】{prof.display_name}")
        print(f"  {prof.explanation}")
        print(f"  支持路由：{'/'.join(prof.routers) or '(无路由概念)'}   最高 {prof.max_multiplier}X")
        print()

        for name in prof.proxy_names:
            dest = PAYLOAD / prof.version / name
            entry = prof.proxy(name)
            rel = entry.upstream_path if entry else name

            if args.check:
                if not dest.is_file():
                    print(f"  [缺失] {name}")
                    bad.append(f"{prof.version}/{name}")
                    continue
            elif args.force or not dest.is_file():
                print(f"  [下载] {name}  <-  {rel}")
                try:
                    download(f"{RAW}/{rel}", dest)
                except Exception as exc:
                    print(f"     失败：{exc}")
                    bad.append(f"{prof.version}/{name}")
                    continue
            else:
                print(f"  [跳过] {name}（已存在）")

            ok, msg = verify(dest, name, prof)
            print(f"     校验：{'通过' if ok else '失败'}  {msg}")
            if not ok:
                bad.append(f"{prof.version}/{name}")

    print()
    print("=" * 70)
    if bad:
        print(f"有 {len(bad)} 个文件未就绪：")
        for b in bad:
            print(f"  - {b}")
        print()
        print("网络连不上 GitHub 时，可以手动从上游下载后按下面的路径放置：")
        for ver in versions:
            prof = profiles.get(ver)
            for name in prof.proxy_names:
                e = prof.proxy(name)
                print(f"  payload/{ver}/{name:14} <- {RAW}/{e.upstream_path if e else name}")
        return 1

    n = sum(len(profiles.get(v).proxy_names) for v in versions)
    print(f"全部 {n} 个文件就绪且哈希校验通过。")
    print("其中 0.2.4/version.dll 的哈希与上游 README 公布的官方值一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
