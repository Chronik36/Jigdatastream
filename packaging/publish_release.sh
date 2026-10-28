#!/bin/bash
# Publish a build of main as a release in the public releases-only repository, where the app checks for updates:
# patch notes since the last release, with README.md and the Manual attached. Used by the Mac job, and by the
# code-only release when GitHub has no Mac machine free. Files: dist/ (dmg, code zip, manual), apk/, win/.
# Usage: publish_release.sh <run number> <releases repo> [line shown above the patch notes]
set -euo pipefail
run="$1"; repo="$2"; extra="${3:-}"
tag="build-$run-${GITHUB_SHA::7}"
# Patch notes: every change since the last release (its tag ends in that build's commit id).
prev=$(gh release list -R "$repo" -L 1 --json tagName -q '.[0].tagName' 2>/dev/null || true)
range="-1"
if [ -n "$prev" ] && git cat-file -e "${prev##*-}^{commit}" 2>/dev/null; then range="${prev##*-}..HEAD"; fi
git log $range --no-merges --pretty='- %s' | sed -E 's/ \(#[0-9]+\)$//' | head -40 > notes.txt
[ -s notes.txt ] || git log -1 --pretty='- %s' > notes.txt
# The page shows how to install first, then what's new; README.md and the Manual are attached too.
{ echo "**New to BotTrack?** Read **README.md** below for how to install (Mac, Windows, Android, iPhone), and open **BotTrack-Manual.html** for the full manual."
  [ -n "$extra" ] && { echo; echo "$extra"; }
  echo; echo "### What's new"; cat notes.txt; } > body.txt
cp packaging/README.md README.md
shopt -s nullglob
gh release create "$tag" dist/BotTrack*.dmg dist/bottrack-code.zip apk/*.apk win/*.exe README.md dist/BotTrack-Manual*.html \
  -R "$repo" --title "BotTrack build $run" --notes-file body.txt
# The releases repository's front page shows the same README.
sha=$(gh api "repos/$repo/contents/README.md" -q .sha 2>/dev/null || true)
gh api -X PUT "repos/$repo/contents/README.md" -f message="README for build $run" \
  -f content="$(base64 < README.md | tr -d '\n')" ${sha:+-f sha="$sha"} >/dev/null || echo "README update failed"
