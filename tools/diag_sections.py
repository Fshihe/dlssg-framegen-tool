"""看真实游戏 EXE 的段分布，确定该扫哪几段。"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dgcore.pe import _read, _rva_to_offset  # noqa: E402

TARGETS = [
    Path(r"D:\steam\steamapps\common\Black Myth Wukong Benchmark Tool\b1\Binaries\Win64\b1-Win64-Shipping.exe"),
    Path(r"D:\steam\steamapps\common\Palworld\Pal\Binaries\Win64\Palworld-Win64-Shipping.exe"),
]


def sections_of(p: Path):
    with open(p, "rb") as fp:
        e = struct.unpack("<I", _read(fp, 0x3C, 4))[0]
        coff = _read(fp, e + 4, 20)
        nsec = struct.unpack("<H", coff[2:4])[0]
        size_opt = struct.unpack("<H", coff[16:18])[0]
        raw = _read(fp, e + 24 + size_opt, 40 * nsec)
        out = []
        for i in range(nsec):
            s = raw[i * 40 : i * 40 + 40]
            if len(s) < 40:
                break
            nm = s[:8].rstrip(b"\x00").decode("ascii", "ignore")
            vs, va, rs, rp = struct.unpack("<IIII", s[8:24])
            chars = struct.unpack("<I", s[36:40])[0]
            out.append((nm, vs, va, rs, rp, chars))
        return out


def scan_section(p: Path, rp: int, rs: int, needles: dict[str, bytes], cap: int):
    hits: dict[str, int] = {}
    with open(p, "rb") as fp:
        fp.seek(rp)
        left = min(rs, cap)
        tail = b""
        while left > 0:
            ch = fp.read(min(4 << 20, left))
            if not ch:
                break
            left -= len(ch)
            buf = (tail + ch).lower()
            for k, n in needles.items():
                if n in buf:
                    hits[k] = hits.get(k, 0) + 1
            tail = buf[-64:]
    return hits


def main() -> int:
    NEEDLES = {
        "d3d12.dll": b"d3d12.dll",
        "D3D12CreateDevice": b"d3d12createdevice",
        "d3d11.dll": b"d3d11.dll",
        "D3D11CreateDevice": b"d3d11createdevice",
    }
    for p in TARGETS:
        if not p.is_file():
            print(f"(跳过 {p.name})")
            continue
        print("=" * 74)
        print(f"{p.name}  ({p.stat().st_size / 1048576:.0f} MB)")
        print("=" * 74)
        print(f"  {'段名':<10} {'虚拟大小':>12} {'原始大小':>12}  特征")
        for nm, vs, va, rs, rp, ch in sections_of(p):
            flags = []
            if ch & 0x20000000:
                flags.append("可执行")
            if ch & 0x40000000:
                flags.append("可读")
            if ch & 0x80000000:
                flags.append("可写")
            print(f"  {nm:<10} {vs:>12,} {rs:>12,}  {'|'.join(flags)}")

        print("\n  各段命中情况（每段最多扫 32MB）：")
        for nm, vs, va, rs, rp, ch in sections_of(p):
            if rs <= 0:
                continue
            hits = scan_section(p, rp, rs, NEEDLES, 32 << 20)
            if hits:
                cap_note = "（已达上限）" if rs > (32 << 20) else ""
                print(f"    {nm:<10} {hits}{cap_note}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
