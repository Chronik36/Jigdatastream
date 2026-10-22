#!/bin/bash
# BotTrack.app's executable. Picks the newest code (an in-app update, or the code shipped in the app)
# and starts it with the app's own Python. Everything BotTrack stores lives in one folder.
B="$(cd "$(dirname "$0")/../.." && pwd)"
D="$HOME/Library/Application Support/BotTrack"
mkdir -p "$D/logs"
SHIPPED="$(cat "$B/Contents/Resources/app/VERSION" 2>/dev/null)"
# A newer BotTrack.dmg replaces code that in-app updates installed for the previous one.
if [ "$(cat "$D/shipped-version" 2>/dev/null)" != "$SHIPPED" ]; then
  rm -rf "$D/app" "$D/pydeps" "$D/backups"
  echo "$SHIPPED" > "$D/shipped-version"
fi
CODE="$B/Contents/Resources/app"
[ -f "$D/app/jigstream/macapp.py" ] && CODE="$D/app"
export PYTHONPATH="$CODE:$D/pydeps" BOTTRACK_BUNDLE="$B" BOTTRACK_CODE_DIR="$D/app" BOTTRACK_PIP_TARGET="$D/pydeps"
export PYTHONDONTWRITEBYTECODE=1
exec "$B/Contents/Resources/python/bin/python3" -m jigstream.macapp >> "$D/logs/launch.log" 2>&1
