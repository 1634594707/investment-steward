# A3 字体子集化管线（frontend-optimization-roadmap-2026-09-12）。
# 流程：fontsource woff2 分片(按 weight) --fontTools.merge--> 整字体 TTF --pyftsubset(UI 字符集)--> 单文件 woff2。
# 用法：.venv/Scripts/python.exe scripts/font-subset.py <web-shell 根目录>
import sys, glob, subprocess
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
CHARSET = ROOT / "font-src" / "charset.txt"
OUT_DIR = ROOT / "src" / "fonts"
OUT_DIR.mkdir(parents=True, exist_ok=True)
CHUNK_GLOB = str(ROOT.parent.parent / "node_modules" / ".pnpm" / "@fontsource+noto-sans-sc@5.3.0" / "node_modules" / "@fontsource" / "noto-sans-sc" / "files" / "noto-sans-sc-*-{w}-normal.woff2")

text = CHARSET.read_text(encoding="utf-8")
print(f"charset: {len(text)} chars")

total = 0
for weight in (400, 500, 600):
    chunks = sorted(glob.glob(CHUNK_GLOB.format(w=weight)))
    if not chunks:
        raise SystemExit(f"no chunks for weight {weight}")
    merged_ttf = OUT_DIR / f"noto-sans-sc-{weight}-full.ttf"
    if merged_ttf.exists():
        merged_ttf.unlink()
    # fontTools.merge：分片合并回整字体（分片按 unicode-range 划分，cmap 互斥）。
    from fontTools.merge import Merger
    merger = Merger()
    merged = merger.merge(chunks)
    merged.save(str(merged_ttf))
    subset = OUT_DIR / f"noto-sans-sc-{weight}-subset.woff2"
    subprocess.run(
        [
            str(ROOT.parent.parent / ".venv" / "Scripts" / "pyftsubset.exe"),
            str(merged_ttf),
            f"--text-file={CHARSET}",
            "--flavor=woff2",
            f"--output-file={subset}",
            "--layout-features=*",
            "--drop-tables+=DSIG",
            "--name-IDs=1,2",
        ],
        check=True,
    )
    size_kb = subset.stat().st_size // 1024
    total += size_kb
    merged_ttf.unlink()
    print(f"weight {weight}: {len(chunks)} chunks -> {subset.name} = {size_kb} KB")

print(f"TOTAL: {total} KB (target <= 2048 KB)")
sys.exit(0 if total <= 2048 else 3)
