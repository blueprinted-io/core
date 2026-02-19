#!/usr/bin/env bash
set -euo pipefail
REPO="/home/claw/work/project-loom"
cd "$REPO"

# Only autosave on a real branch (never detached HEAD)
branch=$(git branch --show-current || true)
if [ -z "$branch" ]; then
  exit 0
fi

# No-op if nothing changed
if git diff --quiet && git diff --cached --quiet && [ -z "$(git ls-files --others --exclude-standard)" ]; then
  exit 0
fi

# Stage and commit
git add -A
if ! git diff --cached --quiet; then
  ts=$(date -u +"%Y-%m-%d %H:%M:%S UTC")
  git commit -m "wip(autosave): ${ts}" >/dev/null 2>&1 || true
fi
