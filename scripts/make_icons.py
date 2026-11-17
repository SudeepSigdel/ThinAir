"""Draw Thinair's icon (three hairlines of decreasing length, the top one teal, on the dark ground)
as icon.svg and the PNG sizes the PWA needs. Standard library only: `python scripts/make_icons.py`.

The shapes are rounded rectangles and capsules rendered with signed distances and 3x3
supersampling, then written as RGBA PNGs with zlib. Colours follow the design tokens."""

import struct
import sys
import zlib
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src" / "thinair" / "web" / "static"
BG = (0x0A, 0x11, 0x1C)
TEAL = (0x2D, 0xD4, 0xBF)
MID = (0x8E, 0x9A, 0xAB)
LOW = (0x5F, 0x6B, 0x7A)
# On a 512 grid: (x1, x2, y, colour). Lengths decrease downward, like air thinning with altitude.
LINES = [(110, 402, 170, TEAL), (110, 322, 256, MID), (110, 242, 342, LOW)]
STROKE = 28
RADIUS = 96

SVG = (
    f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect width="512" height="512" rx="{RADIUS}" fill="#{BG[0]:02x}{BG[1]:02x}{BG[2]:02x}"/>
  <g stroke-width="{STROKE}" stroke-linecap="round" fill="none">
"""
    + "".join(
        f'    <line x1="{a}" y1="{y}" x2="{b}" y2="{y}" stroke="#{c[0]:02x}{c[1]:02x}{c[2]:02x}"/>\n'  # noqa: E501
        for a, b, y, c in LINES
    )
    + """  </g>
</svg>
"""
)


def inside_round_rect(x: float, y: float, size: float, r: float) -> bool:
    cx = min(max(x, r), size - r)
    cy = min(max(y, r), size - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def on_capsule(x: float, y: float, a: float, b: float, cy: float, half: float) -> bool:
    cx = min(max(x, a), b)
    return (x - cx) ** 2 + (y - cy) ** 2 <= half * half


def render(px: int, ss: int = 3) -> bytes:
    scale = 512 / px
    rows = []
    for j in range(px):
        row = bytearray([0])  # PNG filter: none
        for i in range(px):
            acc = [0.0, 0.0, 0.0]
            cover = 0
            for sj in range(ss):
                for si in range(ss):
                    x = (i + (si + 0.5) / ss) * scale
                    y = (j + (sj + 0.5) / ss) * scale
                    if not inside_round_rect(x, y, 512, RADIUS):
                        continue
                    cover += 1
                    colour = BG
                    for a, b, ly, c in LINES:
                        if on_capsule(x, y, a + STROKE / 2, b - STROKE / 2, ly, STROKE / 2):
                            colour = c
                            break
                    for k in range(3):
                        acc[k] += colour[k]
            n = ss * ss
            if cover == 0:
                row += bytes([0, 0, 0, 0])
            else:
                row += bytes(
                    [
                        round(acc[0] / cover),
                        round(acc[1] / cover),
                        round(acc[2] / cover),
                        round(255 * cover / n),
                    ]
                )
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (
            struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", px, px, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def main() -> int:
    (STATIC / "icon.svg").write_text(SVG, encoding="utf-8", newline="\n")
    for px in (192, 512):
        (STATIC / f"icon-{px}.png").write_bytes(render(px))
        print(f"wrote icon-{px}.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
