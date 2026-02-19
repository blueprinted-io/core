#!/usr/bin/env bash
set -euo pipefail
REPO="/home/claw/work/project-loom"
OUT_DIR="$REPO/.snapshots"
mkdir -p "$OUT_DIR"
cd "$REPO"

stamp=$(date -u +"%Y%m%d-%H%M%S")
branch=$(git branch --show-current || echo detached)
base="$OUT_DIR/${stamp}-${branch}"

git status --short > "${base}.status.txt"
git diff > "${base}.diff.patch"
git diff --cached > "${base}.staged.diff.patch"

# Save untracked files as a tarball if present
untracked=$(git ls-files --others --exclude-standard)
if [ -n "$untracked" ]; then
  tar -czf "${base}.untracked.tar.gz" $untracked
fi

echo "$base"
