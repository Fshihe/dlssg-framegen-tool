"""扫描性能验证：三档 preset 在真实游戏上的耗时与结果。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dgcore import games  # noqa: E402

TARGETS = [
    Path(r"D:\steam\steamapps\common\Black Myth Wukong Benchmark Tool"),
    Path(r"D:\steam\steamapps\common\Palworld"),
    Path(r"D:\steam\steamapps\common\3DMark"),
    Path(r"d:\steam\steamapps\common\Counter-Strike Global Offensive"),
]


def main() -> int:
    for preset in ("快速", "标准", "彻底"):
        print("=" * 74)
        print(f"档位：{preset}")
        print("=" * 74)
        total = 0.0
        for t in TARGETS:
            if not t.is_dir():
                print(f"  (跳过 {t.name})")
                continue
            t0 = time.time()
            cands = games.find_render_exes(t, preset=preset)
            dt = time.time() - t0
            total += dt
            elig = [c for c in cands if c.eligible]
            best = max(elig, key=lambda c: c.score) if elig else (cands[0] if cands else None)
            print(f"  {t.name[:38]:<40} {dt:6.2f}s  候选 {len(cands):>3}  可装 {len(elig):>2}")
            if best:
                print(f"    → {best.name}  [{best.confidence}] score={best.score}")
            if cands:
                scanned = sum(c.scanned_bytes for c in cands)
                print(f"    → 总扫描字节 {scanned/1048576:.1f} MB")
        print(f"  合计 {total:.2f}s")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
