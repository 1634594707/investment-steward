"""K00（2026-09-18 排版与研报呈现一致性路线图）：精确的硬编码色清单（**只读**，不改任何文件）。

只输出满足四个条件的裸色：
  1. 在选择器规则内（`prop: value` 形式的声明，非选择器、非 `}`）；
  2. 非 CSS 注释里的色号（先剥掉 /* */）；
  3. 非自定义属性赋值（不统计 `--foo: #hex` / `--rgb-x: r,g,b` 这类原语定义）；
  4. 非 `var(--x, #hex)` 兜底里的 fallback 色。

输出 `文件:行 <属性>: <值>`，供逐条人工点验（styles.css 的 22 处自定义属性赋值与注释不计入）。
"""
from __future__ import annotations

import os
import re
import sys

CSS_ROOT = sys.argv[1] if len(sys.argv) > 1 else "src"

HEX = r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})"
RGB_FN = r"(?:rgba?|hsla?)\([^()]*\)"
COLOR_TOKEN = re.compile(rf"{HEX}|{RGB_FN}")
CUSTOM_PROP = re.compile(r"^\s*--[\w-]+\s*:")       # 自定义属性赋值：排除
DECL = re.compile(r"([A-Za-z-]+)\s*:\s*([^;{}]+)")   # prop: value
VAR_FALLBACK = re.compile(r"var\(\s*--[\w-]+\s*,[^)]*\)")  # var(--x, fallback)


def strip_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", lambda m: " " * len(m.group(0)), text, flags=re.S)


def main() -> None:
    rows = []
    for root, _dirs, names in os.walk(CSS_ROOT):
        for name in sorted(names):
            if not name.endswith(".css"):
                continue
            path = os.path.join(root, name)
            with open(path, "r", encoding="utf-8") as handle:
                raw = handle.read()
            cleaned = strip_comments(raw)
            for lineno, line in enumerate(cleaned.split("\n"), start=1):
                if CUSTOM_PROP.match(line):        # 条件 3：跳过 --token 定义
                    continue
                for prop, value in DECL.findall(line):
                    if prop.startswith("--"):       # 条件 3：也排除写在规则块内的自定义属性赋值（如 body[data-mkt]{--mkt-up}）
                        continue
                    value_wo_var = VAR_FALLBACK.sub("var()", value)  # 条件 4：去掉 var() 兜底色
                    if COLOR_TOKEN.search(value_wo_var):
                        rows.append((path, lineno, prop, value.strip()))
    print(f"真·硬编码色清单：{len(rows)} 处（已排除注释 / 自定义属性赋值 / var() 兜底）")
    by_file: dict[str, int] = {}
    for path, _ln, _prop, _val in rows:
        by_file[path] = by_file.get(path, 0) + 1
    for path in sorted(by_file, key=lambda p: -by_file[p]):
        print(f"  {by_file[path]:4d}  {path}")
    print("--- 逐条 ---")
    for path, lineno, prop, val in rows:
        print(f"{path}:{lineno}  {prop}: {val}")


if __name__ == "__main__":
    main()
