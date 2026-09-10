"""生成程序图标（assets/icon.ico）—— 纯标准库手写 ICO，不依赖 Pillow。

图案：深色圆角底 + 亮绿闪电，表示"帧率翻倍"。
"""

from __future__ import annotations

import struct
from pathlib import Path

SIZES = [16, 24, 32, 48, 64, 128, 256]

BG = (18, 22, 33, 255)        # 深蓝黑
BG2 = (30, 38, 56, 255)
BOLT = (60, 220, 120, 255)    # 亮绿
BOLT2 = (140, 255, 180, 255)

# 闪电多边形（归一化坐标 0..1）
BOLT_POLY = [
    (0.58, 0.06), (0.26, 0.55), (0.46, 0.55),
    (0.38, 0.94), (0.74, 0.42), (0.52, 0.42),
]


def _in_poly(x: float, y: float, poly) -> bool:
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            xint = (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi
            if x < xint:
                inside = not inside
        j = i
    return inside


def _rounded_alpha(x: float, y: float, r: float, size: int) -> bool:
    """圆角矩形判定（归一化坐标）。"""
    if r <= 0:
        return True
    cx = min(max(x, r), 1 - r)
    cy = min(max(y, r), 1 - r)
    dx, dy = x - cx, y - cy
    return dx * dx + dy * dy <= r * r + 1e-9


def render(size: int) -> bytes:
    """渲染一张 size×size 的 BGRA 位图（自下而上，ICO 要求）。"""
    r = 6.0 / 128.0 if size >= 48 else 2.0 / 48.0
    rows = []
    for py in range(size - 1, -1, -1):  # bottom-up
        row = bytearray()
        for px in range(size):
            # 2x2 超采样抗锯齿
            acc = [0, 0, 0, 0]
            for sy in (0.25, 0.75):
                for sx in (0.25, 0.75):
                    x = (px + sx) / size
                    y = (py + sy) / size
                    if not _rounded_alpha(x, y, r, size):
                        c = (0, 0, 0, 0)
                    else:
                        t = y
                        base = tuple(int(BG[i] + (BG2[i] - BG[i]) * t) for i in range(3)) + (255,)
                        if _in_poly(x, y, BOLT_POLY):
                            g = (x + y) / 2
                            c = tuple(int(BOLT[i] + (BOLT2[i] - BOLT[i]) * g) for i in range(3)) + (255,)
                        else:
                            c = base
                    for i in range(4):
                        acc[i] += c[i]
            row += bytes(int(v / 4) for v in acc)
        rows.append(bytes(row))
    return b"".join(rows)


def build_ico(path: Path) -> Path:
    images = []
    for s in SIZES:
        bmp = render(s)
        # BITMAPINFOHEADER：高度写两倍（XOR + AND）
        header = struct.pack(
            "<IiiHHIIiiII",
            40, s, s * 2, 1, 32, 0, len(bmp), 0, 0, 0, 0,
        )
        # AND 掩码：32 位图带 alpha，掩码全 0 即可，但必须按 4 字节对齐存在
        mask_row = ((s + 31) // 32) * 4
        mask = b"\x00" * (mask_row * s)
        images.append(header + bmp + mask)

    dir_size = 6 + 16 * len(SIZES)
    offset = dir_size
    entries = b""
    for s, img in zip(SIZES, images):
        w = 0 if s >= 256 else s
        h = 0 if s >= 256 else s
        entries += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(img), offset)
        offset += len(img)

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(struct.pack("<HHH", 0, 1, len(SIZES)))
        fh.write(entries)
        for img in images:
            fh.write(img)
    return path


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    p = build_ico(root / "assets" / "icon.ico")
    print(f"icon written: {p} ({p.stat().st_size} bytes)")
