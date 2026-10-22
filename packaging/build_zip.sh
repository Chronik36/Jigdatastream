#!/bin/bash
# Builds OrderTracker.zip: the installer, the uninstaller, a readme, and the app code from git HEAD.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$ROOT/dist/OrderTracker.zip}"
STAGE="$(mktemp -d)/OrderTracker"
mkdir -p "$STAGE/app" "$(dirname "$OUT")"
git -C "$ROOT" archive HEAD jigstream run.py requirements.txt | tar -x -C "$STAGE/app"
git -C "$ROOT" rev-parse HEAD > "$STAGE/app/VERSION"  # the in-app updater compares this with GitHub
cp "$ROOT/packaging/Install Order Tracker.command" "$ROOT/packaging/Uninstall Order Tracker.command" \
   "$ROOT/packaging/READ ME FIRST.txt" "$STAGE/"
chmod +x "$STAGE"/*.command
rm -f "$OUT"
(cd "$(dirname "$STAGE")" && zip -qr -X "$OUT" OrderTracker)
echo "$OUT"
