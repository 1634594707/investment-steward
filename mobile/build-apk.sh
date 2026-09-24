#!/bin/bash
# 手动 APK 构建（零 Gradle/AGP 依赖，全部本机 SDK 工具）。
# 产物: mobile/build/InvestmentSteward-0.3.6.apk
#
# 前置：签名口令必须由环境提供，仓库内不保存任何口令。
#   export KS_PASS='<口令>'            # 方式一
#   cp .env.example .env.local         # 方式二（.env.local 已被 gitignore）
set -e
MOBILE="$(cd "$(dirname "$0")" && pwd)"
SDK="$(cygpath -m "${LOCALAPPDATA}")/Android/Sdk"
BT="$SDK/build-tools/36.1.0"
PLAT="$SDK/platforms/android-34/android.jar"
BLD="$(cygpath -m "$MOBILE/build")"
AROOT="$(cygpath -m "$MOBILE/android")"

rm -rf "$BLD"; mkdir -p "$BLD/gen" "$BLD/classes" "$BLD/dex"
rm -rf "$AROOT/assets"; rm -rf "$AROOT/assets"; mkdir -p "$AROOT/assets"; cp -r "$MOBILE/www" "$AROOT/assets/www"

echo "[1/7] aapt2 compile+link"
"$BT/aapt2" compile --dir "$AROOT/res" -o "$BLD/res.zip"
"$BT/aapt2" link -o "$BLD/base.apk" -I "$PLAT" --manifest "$AROOT/AndroidManifest.xml" \
  -R "$BLD/res.zip" -A "$AROOT/assets" --java "$BLD/gen" --auto-add-overlay

echo "[2/7] 生成内嵌页 + javac"
python "$(cygpath -m "$MOBILE/gen-embedded.py")" || "$(cygpath -m "$MOBILE/../.venv/Scripts/python.exe")" "$(cygpath -m "$MOBILE/gen-embedded.py")"
javac --release 11 -classpath "$PLAT" -d "$BLD/classes" \
  "$BLD/gen/com/investment/steward/mobile/R.java" \
  "$AROOT/src/com/investment/steward/mobile/MainActivity.java" \
  "$AROOT/src/com/investment/steward/mobile/EmbeddedPage.java"

echo "[3/7] d8 dex"
find "$BLD/classes" -name "*.class" > "$BLD/classes.txt"
"$BT/d8.bat" --release --lib "$PLAT" --min-api 24 --output "$BLD/dex" @"$BLD/classes.txt"

echo "[4/7] pack classes.dex"
cd "$BLD/dex"
jar --update --file "$BLD/base.apk" classes.dex
cd "$MOBILE"

echo "[5/7] zipalign"
"$BT/zipalign" -f 4 "$BLD/base.apk" "$BLD/aligned.apk"

echo "[6/7] keystore"
KS="$(cygpath -m "$MOBILE")/steward-debug.keystore"

# 签名口令只从环境变量读取，**绝不写进仓库**。
# 取值优先级：已导出的 $KS_PASS > mobile/.env.local（该文件已被 .gitignore 忽略）。
# 用法：cp mobile/.env.example mobile/.env.local && 填入 KS_PASS，或直接 export KS_PASS='...'
if [ -f "$MOBILE/.env.local" ]; then
  . "$MOBILE/.env.local"
fi
if [ -z "${KS_PASS:-}" ]; then
  echo "ERROR: 未设置 KS_PASS（Android 签名口令）。" >&2
  echo "       请 export KS_PASS='<口令>'，或在 mobile/.env.local 中配置后重试。" >&2
  exit 2
fi

if [ ! -f "$KS" ]; then
  keytool -genkeypair -keystore "$KS" -alias steward -storepass "$KS_PASS" -keypass "$KS_PASS" \
    -dname "CN=Investment Steward" -keyalg RSA -keysize 2048 -validity 10950
fi

echo "[7/7] sign"
"$BT/apksigner.bat" sign --ks "$KS" --ks-pass "pass:$KS_PASS" \
  --out "$BLD/InvestmentSteward-0.3.6.apk" "$BLD/aligned.apk"
"$BT/apksigner.bat" verify "$BLD/InvestmentSteward-0.3.6.apk" && echo "SIGN OK"
ls -l "$BLD/InvestmentSteward-0.3.6.apk"
