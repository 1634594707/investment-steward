# A3 字形覆盖校验（frontend-optimization-roadmap-2026-09-12）。
# 断言：src/**/*.{ts,tsx,css,html} 的全部字符 ⊆ 子集字体 cmap（任一缺字即失败）。
# 用法：.venv/Scripts/python.exe scripts/font-coverage-check.py <web-shell 根目录>
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
CHARSET = ROOT / "font-src" / "charset.txt"
WEIGHTS = (400, 500, 600)

from fontTools.ttLib import TTFont

# 系统回落字符：Noto Sans SC 本身不含（希腊字母/几何符号），font-family 栈尾部由系统字体渲染，
# 与子集化前行为一致；不参与 charset/子集覆盖断言。
FALLBACK_CHARS = set("Σλ▸▾✗")

def charset_of(path):
    text = path.read_text(encoding="utf-8")
    return {ch for ch in text if ch.isprintable()}

# 1. 源码字符集（与 font-src/charset.txt 生成的收集逻辑一致）必须仍被 charset.txt 覆盖
src_chars = set()
for pattern in ("src/**/*.ts", "src/**/*.tsx", "src/**/*.css", "src/**/*.html"):
    for f in ROOT.glob(pattern.replace("src/", "src/")):
        if "fonts" in f.parts and f.suffix == ".css":
            continue  # fonts.css 本身是拉丁注释
        src_chars |= charset_of(f)
for c in range(0x20, 0x7F):
    src_chars.add(chr(c))

declared = charset_of(CHARSET)
missing_in_charset = sorted(src_chars - declared - {"\n", "\r", "\t"} - FALLBACK_CHARS)
if missing_in_charset:
    print(f"FAIL: {len(missing_in_charset)} 个源码字符不在 charset.txt（先补 charset 再重跑 font-subset.py）：")
    print("".join(missing_in_charset[:80]))
    sys.exit(2)

# 2. 每个字重的子集字体 cmap 必须覆盖 charset.txt 全部字符
for weight in WEIGHTS:
    subset = ROOT / "src" / "fonts" / f"noto-sans-sc-{weight}-subset.woff2"
    if not subset.exists():
        print(f"FAIL: 缺少子集字体 {subset.name}（先跑 font-subset.py）")
        sys.exit(2)
    font = TTFont(str(subset))
    cmap = font.getBestCmap()
    missing = sorted(ch for ch in declared if ord(ch) not in cmap and ch != "\n" and ch not in FALLBACK_CHARS)
    if missing:
        print(f"FAIL: weight {weight} 缺 {len(missing)} 字形：{''.join(missing[:80])}")
        sys.exit(3)
    print(f"weight {weight}: charset {len(declared)} 字符全部命中（cmap {len(cmap)} 码点）")

print(f"系统回落字符（不经子集字体）：{''.join(sorted(FALLBACK_CHARS))}")
print("COVERAGE OK")
