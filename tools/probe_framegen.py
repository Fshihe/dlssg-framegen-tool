"""在黑神话 EXE 里找帧生成相关的符号，判断倍率是不是写死的。

只扫有意义的段（.rdata/.text 等），不整个文件硬啃。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dgcore.pe import Section, _read, _rva_to_offset  # noqa: E402

EXE = Path(
    r"D:\steam\steamapps\common\Black Myth Wukong Benchmark Tool"
    r"\b1\Binaries\Win64\b1-Win64-Shipping.exe"
)

# 想找的字符串（都是 GBK/ASCII 可打印的）
NEEDLES = [
    b"InsertFrame",
    b"GeneratedFrames",
    b"MaxGenerated",
    b"DLSSG",
    b"FrameGeneration",
    b"SetDLSSG",
    b"MultiFrameCount",
    b"FrameGenMultiplier",
    b"DLSSGMultiFrame",
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
            out.append((nm, vs, va, rs, rp))
        return out


def scan(p: Path, secs, budget: int = 400 << 20):
    hits: dict[str, list[int]] = {n.decode(): [] for n in NEEDLES}
    total_read = 0

    # 优先只读数据段（字符串常量通常在这里）
    order = sorted(secs, key=lambda s: (
        0 if any(k in s[0].lower() for k in ("rdata", "rodata", "data", "text")) else 1,
        s[3],
    ))

    with open(p, "rb") as fp:
        for nm, vs, va, rs, rp in order:
            if rs <= 0 or rp <= 0 or total_read >= budget:
                continue
            cap = min(rs, budget - total_read, 120 << 20)
            fp.seek(rp)
            data = fp.read(cap).lower()
            total_read += len(data)
            for n in NEEDLES:
                key = n.decode()
                idx = data.find(n.lower())
                while idx >= 0 and len(hits[key]) < 5:
                    hits[key].append(rp + idx)
                    idx = data.find(n.lower(), idx + 1)
    return hits, total_read


def main() -> int:
    if not EXE.is_file():
        print(f"找不到 {EXE}")
        return 1

    print(f"目标：{EXE.name}  ({EXE.stat().st_size / 1048576:.0f} MB)")
    secs = sections_of(EXE)
    print(f"段数：{len(secs)}")
    print()
    hits, total = scan(EXE, secs)
    print(f"共读取 {total / 1048576:.0f} MB")
    print()

    print("=== 命中情况 ===")
    for k, v in hits.items():
        mark = "命中" if v else "未找到"
        loc = ", ".join(f"0x{x:X}" for x in v[:3])
        print(f"  {k:22} {mark:6} {loc}")

    print()
    print("=== 把命中的字符串附近内容 dump 出来（看有没有别的线索）===")
    with open(EXE, "rb") as fp:
        for k, offsets in hits.items():
            if not offsets:
                continue
            off = offsets[0]
            fp.seek(max(0, off - 160))
            chunk = fp.read(400)
            # 只保留可打印部分
            text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            print(f"\n--- {k} @ 0x{off:X} ---")
            print(f"  {text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
