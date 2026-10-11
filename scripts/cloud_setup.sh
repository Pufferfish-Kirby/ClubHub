#!/bin/bash
# Installs dependencies at the start of a Claude Code cloud session.
# Runs from the SessionStart hook in .claude/settings.json. Does nothing on your own
# machine: CLAUDE_CODE_REMOTE is only "true" inside a cloud session.

if [ "$CLAUDE_CODE_REMOTE" != "true" ]; then
  exit 0
fi

ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"

# Backend: a venv, because Ubuntu 24.04 refuses system-wide pip installs.
(
  cd "$ROOT/backend" || exit 0
  [ -d .venv ] || python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt pytest
) &

# Frontend: skip if packages are already there (resumed session).
(
  cd "$ROOT/frontend" || exit 0
  [ -d node_modules ] || npm ci --no-audit --no-fund --silent
) &

wait

# Enable the repo's pre-commit hook that blocks committing real .env files.
git -C "$ROOT" config core.hooksPath .githooks || true

exit 0
