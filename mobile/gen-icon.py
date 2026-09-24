#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""v0.3.0 应用图标重绘: 深色圆角方块 + 金色上扬增长曲线 + 箭头。
隐喻「研究 → 收益」: 一条平滑上升的金色曲线收于右上箭头, 配暗夜蓝渐变底。
纯标准库实现(supersample 4x + box 下采样 + zlib PNG), 输出全密度 mipmap。
"""
import os, struct, zlib

MOBILE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(MOBILE, "android", "res")

def hx(s):
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))

BG_TOP, BG_BOT = hx("182433"), hx("0A0E15")
GOLD_BRIGHT, GOLD_DEEP = hx("ffd34d"), hx("e8a20c")
GRID = hx("24303f")

def lerp(c1, c2, t):
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))

def seg_dist(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return ((px - x1) ** 2 + (py - y1) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L2))
    return ((px - x1 - t * dx) ** 2 + (py - y1 - t * dy) ** 2) ** 0.5

# 曲线折线(比例坐标, 左下 → 右上)
POLY = [(0.16, 0.80), (0.36, 0.66), (0.54, 0.47), (0.72, 0.22)]
# 箭头三角: 顶点沿曲线末段方向外推, 底边垂直于方向
_end, _prev = POLY[-1], POLY[-2]
_d = (_end[0] - _prev[0], _end[1] - _prev[1])
_L = (_d[0] ** 2 + _d[1] ** 2) ** 0.5
_dir = (_d[0] / _L, _d[1] / _L)
_perp = (-_dir[1], _dir[0])
TIP = (_end[0] + _dir[0] * 0.19, _end[1] + _dir[1] * 0.19)
BASE = (_end[0] - _dir[0] * 0.01, _end[1] - _dir[1] * 0.01)
C1 = (BASE[0] + _perp[0] * 0.105, BASE[1] + _perp[1] * 0.105)
C2 = (BASE[0] - _perp[0] * 0.105, BASE[1] - _perp[1] * 0.105)
ARROW = (TIP, C1, C2)
LW = 0.042          # 曲线线宽(比例)
RADIUS = 0.225      # 圆角半径(比例)
GRIDLINES = [0.32, 0.52, 0.72]  # 背景横向网格线 y

def in_rounded(px, py, W):
    """圆角方形覆盖测试(0/1), 边缘 AA 由超采样+下采样实现。"""
    r = RADIUS * W
    x0, y0, x1, y1 = 0.02 * W, 0.02 * W, 0.98 * W, 0.98 * W
    if not (x0 <= px < x1 and y0 <= py < y1):
        return False
    cx = min(max(px, x0 + r), x1 - r)
    cy = min(max(py, y0 + r), y1 - r)
    return (px - cx) ** 2 + (py - cy) ** 2 <= r * r

def in_triangle(px, py, tri):
    (ax, ay), (bx, by), (cx, cy) = tri
    d1 = (px - bx) * (ay - by) - (ax - bx) * (py - by)
    d2 = (px - cx) * (by - cy) - (bx - cx) * (py - cy)
    d3 = (px - ax) * (cy - ay) - (cx - ax) * (py - ay)
    neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (neg and pos)

def render(size):
    S = 4
    W = size * S
    buf = bytearray(W * W * 4)
    poly = [(x * W, y * W) for x, y in POLY]
    lw = LW * W
    for py in range(W):
        t = py / (W - 1)
        bg = lerp(BG_TOP, BG_BOT, t)
        row = py * W * 4
        for px in range(W):
            if not in_rounded(px + .5, py + .5, W):
                buf[row + px * 4 + 3] = 0  # 圆角外全透明
                continue
            r, g, b = bg
            # 背景细网格线(极淡, 研究图表感)
            for gy in GRIDLINES:
                if abs(py - gy * W) < 0.006 * W:
                    r, g, b = lerp((r, g, b), GRID, 0.7)
            # 金色曲线(亮度随高度渐变)
            d = min(seg_dist(px + .5, py + .5, *poly[i], *poly[i + 1]) for i in range(len(poly) - 1))
            if d < lw * 1.9:
                a = max(0.0, 1.0 - d / (lw * 1.9)) * 0.28  # 柔光晕
                r = int(r + (GOLD_BRIGHT[0] - r) * a); g = int(g + (GOLD_BRIGHT[1] - g) * a); b = int(b + (GOLD_BRIGHT[2] - b) * a)
            if d < lw:
                gt = py / W
                r, g, b = lerp(GOLD_DEEP, GOLD_BRIGHT, gt)
            # 箭头
            if in_triangle(px + .5, py + .5, [(x * W, y * W) for x, y in ARROW]):
                r, g, b = GOLD_BRIGHT
            o = row + px * 4
            buf[o], buf[o+1], buf[o+2], buf[o+3] = r, g, b, buf[o+3] or 255
    # box 下采样(把 alpha 一并平均 → 圆角平滑)
    out = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            ar = ag = ab = aa = 0
            for sy in range(S):
                for sx in range(S):
                    o = ((y * S + sy) * W + (x * S + sx)) * 4
                    a = buf[o+3]
                    ar += buf[o] * a; ag += buf[o+1] * a; ab += buf[o+2] * a
                    aa += a
            n = S * S
            o2 = (y * size + x) * 4
            if aa == 0:
                out[o2], out[o2+1], out[o2+2], out[o2+3] = 0, 0, 0, 0
            else:
                out[o2], out[o2+1], out[o2+2] = ar // aa, ag // aa, ab // aa
                out[o2+3] = aa // n
    return bytes(out)

def write_png(path, size, raw):
    def chunk(typ, data):
        c = struct.pack(">I", len(data)) + typ + data
        return c + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    rows = b"".join(b"\x00" + raw[y*size*4:(y+1)*size*4] for y in range(size))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)

DENSITIES = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}
for dpi, size in DENSITIES.items():
    d = os.path.join(RES, "mipmap-" + dpi)
    os.makedirs(d, exist_ok=True)
    raw = render(size)
    write_png(os.path.join(d, "ic_launcher.png"), size, raw)
    print("ok", dpi, size)
