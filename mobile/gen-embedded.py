#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 www/index.html 以 base64 分块内嵌为 Java 常量，作为资产读取失败的兜底。
分块原因:单个 String 常量有 64KB 上限,分块可支持任意大页面。
"""
import base64, os

MOBILE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(MOBILE, "www", "index.html")
OUT = os.path.join(MOBILE, "android", "src", "com", "investment", "steward", "mobile", "EmbeddedPage.java")

with open(SRC, "r", encoding="utf-8") as f:
    html = f.read()
b64 = base64.b64encode(html.encode("utf-8")).decode("ascii")
CHUNK = 4096
chunks = [b64[i:i + CHUNK] for i in range(0, len(b64), CHUNK)]

parts = ",\n        ".join('"%s"' % c for c in chunks)
code = """package com.investment.steward.mobile;

/** 构建期由 gen-embedded.py 生成:www/index.html 的 base64 分块快照(资产读取失败时兜底)。 */
final class EmbeddedPage {
    private static final String[] CHUNKS = {
        %s
    };

    static String html() {
        StringBuilder sb = new StringBuilder();
        for (String c : CHUNKS) sb.append(c);
        try {
            byte[] raw = android.util.Base64.decode(sb.toString(), android.util.Base64.DEFAULT);
            return new String(raw, java.nio.charset.StandardCharsets.UTF_8);
        } catch (Exception e) {
            return null;
        }
    }
}
""" % parts

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    f.write(code)
print("EmbeddedPage.java written, html=%d bytes, b64=%d bytes, chunks=%d" % (len(html), len(b64), len(chunks)))
