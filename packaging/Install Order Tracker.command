#!/bin/bash
# Installs (or updates) Order Tracker on this Mac and starts it.
# Double-click to run. Safe to run again: it keeps your inboxes and data.
set -euo pipefail

APP_NAME="Order Tracker"
HOME_DIR="$HOME/Library/Application Support/OrderTracker"
LABEL="com.jigdatastream.ordertracker"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT="${PORT:-8765}"
MODEL="${LLM_MODEL:-llama3.1:8b}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say() { printf '\n==> %s\n' "$1"; }
fail() { printf '\n%s\n\nPress Return to close.' "$1"; read -r _; exit 1; }

# Keep a copy of everything this installer prints, for troubleshooting.
mkdir -p "$HOME_DIR/logs"
exec > >(tee -a "$HOME_DIR/logs/install.log") 2>&1
echo "---- install started $(date) from $HERE (macOS $(sw_vers -productVersion 2>/dev/null || echo ?), $(uname -m))"

[ -d "$HERE/app/jigstream" ] || fail "Keep this file next to its 'app' folder (unzip the whole download first)."

say "Checking for Python 3"
PY=""
for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if [ -x "$candidate" ] && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
    PY="$candidate"; break
  fi
done
if [ -z "$PY" ]; then
  xcode-select --install >/dev/null 2>&1 || true
  fail "macOS needs its free developer tools for Python. A window should have opened: click Install, wait for it to finish, then double-click this installer again."
fi
echo "Using $("$PY" --version)"

say "Copying the app"
mkdir -p "$HOME_DIR/data" "$HOME_DIR/logs"
rm -rf "$HOME_DIR/app.new"
cp -R "$HERE/app" "$HOME_DIR/app.new"
rm -rf "$HOME_DIR/app"
mv "$HOME_DIR/app.new" "$HOME_DIR/app"

say "Setting up Python packages"
if [ ! -x "$HOME_DIR/venv/bin/python" ]; then
  "$PY" -m venv "$HOME_DIR/venv"
fi
"$HOME_DIR/venv/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade -r "$HOME_DIR/app/requirements.txt" \
  || "$HOME_DIR/venv/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade "flask>=3.0"

say "Checking Ollama"
OLLAMA="$(command -v ollama || true)"
[ -z "$OLLAMA" ] && [ -x /Applications/Ollama.app/Contents/Resources/ollama ] && OLLAMA=/Applications/Ollama.app/Contents/Resources/ollama
if [ -z "$OLLAMA" ]; then
  echo "Ollama isn't installed. Downloading it (free) from ollama.com..."
  TMP="$(mktemp -d)"
  curl -fL --progress-bar -o "$TMP/Ollama.zip" https://ollama.com/download/Ollama-darwin.zip
  ditto -x -k "$TMP/Ollama.zip" /Applications
  rm -rf "$TMP"
  OLLAMA=/Applications/Ollama.app/Contents/Resources/ollama
fi
if ! curl -fs http://127.0.0.1:11434/api/version >/dev/null; then
  open -a Ollama || true
  printf 'Waiting for Ollama to start'
  for _ in $(seq 1 60); do
    curl -fs http://127.0.0.1:11434/api/version >/dev/null && break
    printf '.'; sleep 2
  done
  echo
  curl -fs http://127.0.0.1:11434/api/version >/dev/null || fail "Ollama didn't start. Open the Ollama app yourself, then run this installer again."
fi

say "Downloading the AI model $MODEL (about 5 GB the first time; later runs skip this)"
"$OLLAMA" pull "$MODEL" || fail "Couldn't download $MODEL. Check your internet connection and run this installer again."

say "Saving settings"
ENV_FILE="$HOME_DIR/.env"
touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
set_default() { grep -q "^$1=" "$ENV_FILE" || echo "$1=$2" >> "$ENV_FILE"; }
set_default LLM_MODEL "$MODEL"
set_default PORT "$PORT"
set_default SYNC_EVERY_HOURS 4
PORT="$(grep '^PORT=' "$ENV_FILE" | tail -1 | cut -d= -f2)"

say "Starting $APP_NAME (it will also start by itself when you log in)"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$HOME_DIR/venv/bin/python</string><string>$HOME_DIR/app/run.py</string>
  </array>
  <key>WorkingDirectory</key><string>$HOME_DIR</string>
  <key>EnvironmentVariables</key><dict><key>PYTHONPATH</key><string>$HOME_DIR/app</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME_DIR/logs/server.log</string>
  <key>StandardErrorPath</key><string>$HOME_DIR/logs/server.log</string>
</dict></plist>
PLIST
launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
# launchd's own output file isn't rotated, so keep only its recent part.
if [ -f "$HOME_DIR/logs/server.log" ]; then tail -n 2000 "$HOME_DIR/logs/server.log" > "$HOME_DIR/logs/server.log.tmp" && mv "$HOME_DIR/logs/server.log.tmp" "$HOME_DIR/logs/server.log"; fi
launchctl bootstrap "gui/$(id -u)" "$PLIST"

URL="http://127.0.0.1:$PORT"
for _ in $(seq 1 30); do
  curl -fs "$URL/orders" >/dev/null && break
  sleep 1
done
curl -fs "$URL/orders" >/dev/null || fail "$APP_NAME didn't start. Details are in $HOME_DIR/logs (server.log and app.log)"

say "Adding $APP_NAME to Applications"
# Its own app with a Dock icon: the dashboard in a native window, not a browser tab.
BUNDLE="$HOME/Applications/$APP_NAME.app"
rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources"
cat > "$BUNDLE/Contents/MacOS/OrderTracker" <<LAUNCH
#!/bin/bash
cd "$HOME_DIR"
export PYTHONPATH="$HOME_DIR/app"
"$HOME_DIR/venv/bin/python" -m jigstream.window >> "$HOME_DIR/logs/window.log" 2>&1 || open "$URL/orders"
LAUNCH
chmod +x "$BUNDLE/Contents/MacOS/OrderTracker"
cat > "$BUNDLE/Contents/Info.plist" <<INFO
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleDisplayName</key><string>$APP_NAME</string>
  <key>CFBundleIdentifier</key><string>$LABEL.app</string>
  <key>CFBundleExecutable</key><string>OrderTracker</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
INFO
ICON_PNG="$HOME_DIR/app/jigstream/static/icon.png"
if [ -f "$ICON_PNG" ]; then
  ICONSET="$(mktemp -d)/AppIcon.iconset"; mkdir -p "$ICONSET"
  for s in 16 32 128 256 512; do
    sips -z $s $s "$ICON_PNG" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null 2>&1 || true
    sips -z $((s*2)) $((s*2)) "$ICON_PNG" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null 2>&1 || true
  done
  iconutil -c icns "$ICONSET" -o "$BUNDLE/Contents/Resources/AppIcon.icns" >/dev/null 2>&1 || true
fi
# The old browser shortcut from earlier versions, if any, is replaced by the bundle above.
touch "$BUNDLE"

open "$BUNDLE" || open "$URL/orders"

say "Done. $APP_NAME is running at $URL"
echo "Open it any time from Spotlight: type \"$APP_NAME\"."
echo "Logs for troubleshooting: the Logs page in the dashboard, or $HOME_DIR/logs"
echo "You can close this window."
