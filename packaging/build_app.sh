#!/bin/bash
# Builds BotTrack.app and BotTrack.dmg on a Mac (Apple Silicon). Used by .github/workflows/build-mac.yml.
# Everything the app needs goes inside the bundle: its own Python, the app code, the Ollama engine.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$ROOT/dist}"
PY_URL="${PY_URL:-https://github.com/astral-sh/python-build-standalone/releases/download/20241016/cpython-3.12.7+20241016-aarch64-apple-darwin-install_only.tar.gz}"
OLLAMA_URL="${OLLAMA_URL:-https://github.com/ollama/ollama/releases/latest/download/ollama-darwin.tgz}"
WORK="$(mktemp -d)"
APP="$WORK/BotTrack.app"
RES="$APP/Contents/Resources"
mkdir -p "$APP/Contents/MacOS" "$RES/app" "$OUT"

echo "==> Python"
curl -fsSL "$PY_URL" | tar -xz -C "$RES"   # unpacks to Resources/python
"$RES/python/bin/python3" -m pip install --quiet --disable-pip-version-check --no-compile \
  "flask>=3.0" "pywebview>=5.1,<6" "cryptography>=42" "mlx-lm>=0.25"
find "$RES/python" -name "__pycache__" -prune -exec rm -rf {} +
rm -rf "$RES/python/lib/python3.12/test" "$RES/python/lib/python3.12/idlelib" "$RES/python/lib/python3.12/tkinter" \
       "$RES/python/lib/python3.12/ensurepip" "$RES/python/share"

echo "==> App code"
git -C "$ROOT" archive HEAD jigstream run.py requirements.txt | tar -x -C "$RES/app"
git -C "$ROOT" rev-parse HEAD > "$RES/app/VERSION"

echo "==> AI engine"
mkdir -p "$RES/ollama"
curl -fsSL "$OLLAMA_URL" | tar -xz -C "$RES/ollama" || true
if [ ! -x "$RES/ollama/ollama" ]; then  # fall back to the engine inside Ollama's own app download
  curl -fsSL -o "$WORK/Ollama.zip" https://github.com/ollama/ollama/releases/latest/download/Ollama-darwin.zip
  ditto -x -k "$WORK/Ollama.zip" "$WORK/ollama-app"
  rm -rf "$RES/ollama"; cp -R "$WORK/ollama-app/Ollama.app/Contents/Resources" "$RES/ollama"
fi
[ -x "$RES/ollama/ollama" ] || { echo "ollama binary missing"; ls -R "$RES/ollama" | head; exit 1; }

echo "==> Icon"
ICONSET="$WORK/AppIcon.iconset"; mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s "$ROOT/jigstream/static/icon.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$ROOT/jigstream/static/icon.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$RES/AppIcon.icns"

echo "==> Launcher"
cp "$ROOT/packaging/launcher.sh" "$APP/Contents/MacOS/BotTrack"
chmod +x "$APP/Contents/MacOS/BotTrack"
VERSION_SHORT="1.0.$(git -C "$ROOT" rev-list --count HEAD)"
sed "s/@VERSION@/$VERSION_SHORT/" "$ROOT/packaging/Info.plist" > "$APP/Contents/Info.plist"

echo "==> Sign (ad hoc; free, no Apple developer account)"
codesign --force --deep --sign - "$APP"

echo "==> Disk image"
DMG_DIR="$WORK/dmg"; mkdir -p "$DMG_DIR"
cp -R "$APP" "$DMG_DIR/"
ln -s /Applications "$DMG_DIR/Applications"
cp "$ROOT/packaging/READ ME FIRST.txt" "$DMG_DIR/"
rm -f "$OUT/BotTrack.dmg"
hdiutil create -volname BotTrack -srcfolder "$DMG_DIR" -fs HFS+ -format UDZO -ov "$OUT/BotTrack.dmg" >/dev/null
du -sh "$APP" "$OUT/BotTrack.dmg"
