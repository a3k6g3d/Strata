"""The web app's home-screen icons (serve/web/icon-*.png): the layer stack of serve/web/favicon.svg, green on the dark
background, drawn into the middle 58% so a phone's round or squircle mask (a "maskable" icon) never cuts it.

Standard library only (zlib + struct), so it runs on the server's Python without an image package:
    python tools/make_web_icons.py
"""
import math
import struct
import zlib
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "serve" / "web"
BG, FG = (0x11, 0x13, 0x15), (0x10, 0xB9, 0x81)
# favicon.svg, in its 32-unit box: one filled diamond and two open chevrons (stroke 3.67, round caps and joins)
DIAMOND = [(16, 2.38), (28.42, 9.08), (16, 15.78), (3.58, 9.08)]
CHEVRONS = [([(3.58, 14.8), (16, 21.5), (28.42, 14.8)], 0.92), ([(3.58, 21.28), (16, 27.98), (28.42, 21.28)], 0.78)]
HALF = 3.67 / 2


def inside(x, y, poly):
    hit = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def near(x, y, line):
    for (x1, y1), (x2, y2) in zip(line, line[1:]):
        dx, dy = x2 - x1, y2 - y1
        t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
        if math.hypot(x - x1 - t * dx, y - y1 - t * dy) <= HALF:
            return True
    return False


def coverage(x, y):
    """How much green the point gets: the shapes are separate, so the first one it is in decides."""
    if inside(x, y, DIAMOND):
        return 1.0
    for line, alpha in CHEVRONS:
        if near(x, y, line):
            return alpha
    return 0.0


def png(n, ss=3):
    s = n * 0.58 / 32                      # pixels per favicon unit
    off = (n - 32 * s) / 2
    rows = []
    for py in range(n):
        row = bytearray([0])               # filter: none
        for px in range(n):
            a = 0.0
            for sy in range(ss):
                for sx in range(ss):
                    a += coverage((px + (sx + 0.5) / ss - off) / s, (py + (sy + 0.5) / ss - off) / s)
            a /= ss * ss
            row += bytes(round(b + (f - b) * a) for b, f in zip(BG, FG))
        rows.append(bytes(row))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    for n in (180, 192, 512):
        out = WEB / f"icon-{n}.png"
        out.write_bytes(png(n))
        print(out.name, out.stat().st_size, "bytes")
