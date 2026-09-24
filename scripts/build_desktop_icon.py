#!/usr/bin/env python3
"""桌面端应用图标生成器（纯标准库，零新增依赖）。

v3（2026-09-10，用户给到 DSH/Obsidian/QQ 等参考后定调）：
- 参考图的共性 = 单一强势轮廓/吉祥物占满画布（不是多元素组合）→ v2 的「线+点+柱」
  组合式构图升级为 **金色鲸** 单主体：呼应用户最初点名的 DeepSeek（鲸），
  金色渐变 + 暗底是产品自己的签名色。
- 鲸姿上扬（尾鳍高位、吻部低位）——鲸身本身就是一条上升趋势线。
- 底板：暗色圆角方（#141C1A→#0B1018 纵向微渐变），产品主题色。

技术：底板/K 线用解析式 SDF 抗锯齿；鲸轮廓（贝塞尔链 → 多边形）用 2048 扫描线
填充 + 盒式降采样得覆盖度；512 主画布合成后降采样到各尺寸。ICO 装 PNG 帧。
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

RESOURCE_DIR = Path(__file__).resolve().parents[1] / "apps" / "desktop-host" / "resources"
MASTER = 512
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
WHALE_SS = 2048  # 鲸多边形超采样分辨率（4x）

# —— 调色（产品主题：暗底 #0B1018、薄荷 #9BE3BC、金 #F1BD74） ——
BG_TOP = (0x14, 0x1C, 0x1A, 255)
BG_BOTTOM = (0x0B, 0x10, 0x18, 255)
GOLD_FROM = (0xF6, 0xD0, 0x8A, 255)
GOLD_TO = (0xD4, 0x82, 0x18, 255)


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def mix(c1, c2, t):
    return (
        int(lerp(c1[0], c2[0], t) + 0.5),
        int(lerp(c1[1], c2[1], t) + 0.5),
        int(lerp(c1[2], c2[2], t) + 0.5),
        255,
    )


def cov_rounded_rect(px, py, x0, y0, x1, y1, radius) -> float:
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    hx, hy = (x1 - x0) / 2.0, (y1 - y0) / 2.0
    qx = abs(px - cx) - (hx - radius)
    qy = abs(py - cy) - (hy - radius)
    dx, dy = max(qx, 0.0), max(qy, 0.0)
    d = (dx * dx + dy * dy) ** 0.5 + min(max(qx, qy), 0.0) - radius
    return clamp(0.5 - d, 0.0, 1.0)


# ---------------------------------------------------------------------------
# 鲸轮廓：设计坐标 64 单位（y 向下），三段贝塞尔链拼一条闭合轮廓。
# 姿态 = 尾鳍高位、吻部低位（上扬），背脊圆润、腹线饱满。
# ---------------------------------------------------------------------------

NOSE = (6.5, 35.5)

WHALE_SEGMENTS = (
    # 圆钝前额（大圆额，DeepSeek 式）
    (NOSE, (7.0, 23.5), (14.0, 15.5), (26.0, 15.8)),
    # 背脊缓弧 → 尾腰上缘
    ((26.0, 15.8), (35.0, 16.0), (43.0, 18.0), (48.5, 20.5)),
    # 上鳍外缘（顶边，强外弓）：尾腰 → 上鳍尖
    ((48.5, 20.5), (51.5, 15.0), (56.5, 10.0), (62.0, 7.0)),
    # 上鳍内缘（明显更低，让鳍有宽度）：鳍尖 → 缺口
    ((62.0, 7.0), (60.0, 13.0), (55.0, 18.5), (50.5, 22.5)),
    # 下鳍内缘：缺口 → 下鳍尖
    ((50.5, 22.5), (53.5, 25.5), (56.0, 28.0), (58.5, 31.0)),
    # 下鳍外缘（底边）：下鳍尖 → 尾腰下缘
    ((58.5, 31.0), (54.0, 30.5), (50.0, 29.0), (46.5, 27.0)),
    # 腹线（饱满）
    ((47.0, 26.5), (39.5, 38.5), (28.0, 42.5), (19.0, 42.5)),
    # 圆下巴合口
    ((19.0, 42.5), (10.5, 42.5), (6.5, 41.0), NOSE),
)

# 整体偏移：左移给鳍尖留呼吸感，下移让鲸垂直居中偏上（上扬姿态留白在下）
WHALE_OFFSET = (-2.0, 6.5)


def bezier_points(p0, p1, p2, p3, steps=14):
    pts = []
    for i in range(steps + 1):
        t = i / steps
        u = 1.0 - t
        x = u * u * u * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * p3[0]
        y = u * u * u * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * p3[1]
        pts.append((x, y))
    return pts


def whale_polygon() -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for p0, p1, p2, p3 in WHALE_SEGMENTS:
        seg = bezier_points(p0, p1, p2, p3)
        if pts:
            seg = seg[1:]
        pts.extend(seg)
    return [(x + WHALE_OFFSET[0], y + WHALE_OFFSET[1]) for x, y in pts]


def polygon_mask_highres(points, res: int) -> bytes:
    """扫描线填充（超采样网格），返回 res*res 的 0/255 掩码。"""
    scaled = [(x * res / 64.0, y * res / 64.0) for x, y in points]
    n = len(scaled)
    edges = [(scaled[i], scaled[(i + 1) % n]) for i in range(n)]
    mask = bytearray(res * res)
    for row in range(res):
        sy = row + 0.5
        xs: list[float] = []
        for (ax, ay), (bx, by) in edges:
            if (ay <= sy < by) or (by <= sy < ay):
                t = (sy - ay) / (by - ay)
                xs.append(ax + t * (bx - ax))
        if not xs:
            continue
        xs.sort()
        base = row * res
        for i in range(0, len(xs) - 1, 2):
            x0 = max(int(xs[i] + 0.5), 0)
            x1 = min(int(xs[i + 1] + 0.5), res)
            if x1 > x0:
                mask[base + x0 : base + x1] = b"\xff" * (x1 - x0)
    return mask


def downsample_mask(mask: bytes, res: int, target: int) -> list[float]:
    """超采样掩码 → 目标分辨率覆盖度 [0,1]。"""
    factor = res // target
    out: list[float] = []
    for ty in range(target):
        for tx in range(target):
            acc = 0
            for sy in range(factor):
                base = (ty * factor + sy) * res
                row = mask[base : base + res]
                acc += sum(row[tx * factor : (tx + 1) * factor])
            out.append(acc / (factor * factor * 255))
    return out


def render_master(whale_cov: list[float]) -> list[list[tuple]]:
    k = MASTER / 64.0
    card = (2.0, 2.0, 62.0, 62.0, 14.0)

    # 鲸包围盒（用于竖向金渐变），与 WHALE_OFFSET 同源
    poly = whale_polygon()
    ys = [p[1] for p in poly]
    wy0, wy1 = min(ys), max(ys)

    rows: list[list[tuple]] = []
    idx = 0
    for py in range(MASTER):
        uy = (py + 0.5) / k
        bg = mix(BG_TOP, BG_BOTTOM, uy / 64.0)
        row = []
        for px in range(MASTER):
            ux = (px + 0.5) / k
            color = (0, 0, 0, 0)
            cov_card = cov_rounded_rect(ux, uy, *card)
            if cov_card > 0:
                color = bg
                wcov = whale_cov[idx]
                if wcov > 0:
                    t = clamp((uy - wy0) / max(wy1 - wy0, 1e-6), 0.0, 1.0)
                    color = mix(color, mix(GOLD_FROM, GOLD_TO, t), wcov)
            idx += 1
            row.append(color)
        rows.append(row)
    return rows


def downsample(rows, target):
    factor = MASTER // target
    out = []
    for ty in range(target):
        row = []
        for tx in range(target):
            r = g = b = a = 0
            for sy in range(factor):
                src = rows[ty * factor + sy]
                for sx in range(factor):
                    pr, pg, pb, pa = src[tx * factor + sx]
                    r += pr
                    g += pg
                    b += pb
                    a += pa
            n = factor * factor
            row.append((r // n, g // n, b // n, a // n))
        out.append(row)
    return out


def png_bytes(rows) -> bytes:
    height, width = len(rows), len(rows[0])
    raw = b"".join(b"\x00" + b"".join(struct.pack("4B", *px) for px in row) for row in rows)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def write_ico(path: Path, frames: list[tuple[int, bytes]]) -> None:
    with path.open("wb") as fh:
        fh.write(struct.pack("<HHH", 0, 1, len(frames)))
        offset = 6 + 16 * len(frames)
        for size, blob in frames:
            dim = 0 if size >= 256 else size
            fh.write(struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset))
            offset += len(blob)
        for _, blob in frames:
            fh.write(blob)


def main() -> int:
    # 2026-09-20：图标改为以 resources/icon-master.png（1024 母图）为源，由 raster 管线出各尺寸。
    # 母图存在时本脚本会用过时的过程化「豆形鲸」覆盖 icon.ico / icon.png，故默认拒绝。
    master = RESOURCE_DIR / "icon-master.png"
    if master.exists() and "--force-v3" not in sys.argv:
        print(
            f"跳过：检测到 raster 图标源 {master.name}。\n"
            "当前 icon.ico / icon.png 由母图裁切生成（裁水印 → 圆角遮罩 → 16…256px → 装 ICO）；\n"
            "确要用旧的过程化 v3 覆盖，请加 --force-v3。"
        )
        return 1
    mask = polygon_mask_highres(whale_polygon(), WHALE_SS)
    cov = downsample_mask(mask, WHALE_SS, MASTER)
    rows = render_master(cov)
    RESOURCE_DIR.mkdir(parents=True, exist_ok=True)
    frames = [(size, png_bytes(downsample(rows, size))) for size in ICO_SIZES]
    write_ico(RESOURCE_DIR / "icon.ico", frames)
    (RESOURCE_DIR / "icon.png").write_bytes(png_bytes(downsample(rows, 512)))
    print(
        f"icon.ico（{'/'.join(str(s) for s in ICO_SIZES)}px）+ icon.png（512 预览）已生成"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
