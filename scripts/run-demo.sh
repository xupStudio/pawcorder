#!/usr/bin/env bash
# Convenience wrapper for running the pawcorder admin in demo mode on
# a Mac / Linux laptop. Used during UI development — no Docker, no real
# Frigate, no real cameras. Just spin up the FastAPI admin against a
# tmp data directory pre-seeded with demo fixtures.
#
# Usage:  ./scripts/run-demo.sh
#
# Idempotent: kills any prior demo process, pulls the latest commits,
# (re)installs python deps if missing, starts python -m app.demo.

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ADMIN_DIR="$REPO_ROOT/admin"
VENV="$ADMIN_DIR/.venv"

bold()  { printf '\033[1m%s\033[0m\n'    "$*"; }
green() { printf '\033[0;32m%s\033[0m\n' "$*"; }

cd "$REPO_ROOT"

# 1. Stop any prior demo so we don't end up with two on the same port.
bold "▶ Stopping any prior demo process"
pkill -f "app.demo" 2>/dev/null || true
# Wait a beat for the port to release.
sleep 1

# 2. Pull the latest source IF there's an upstream remote — otherwise
#    skip silently (e.g. a local-only checkout, or commits sitting in
#    the working tree that haven't been pushed yet).
# shellcheck disable=SC1083  # @{u} is git's upstream syntax, not bash brace expansion
if [[ -d .git ]] && git rev-parse @{u} >/dev/null 2>&1; then
  bold "▶ git pull"
  git pull --ff-only || echo "  (skipped — local changes or non-fast-forward)"
else
  echo "▶ Skipping git pull — no upstream tracked"
fi
bold "▶ HEAD: $(git --no-pager log -1 --oneline 2>/dev/null || echo 'unknown')"

# 3. Make sure the venv exists and deps are current.
if [[ ! -x "$VENV/bin/python" ]]; then
  bold "▶ Creating venv"
  python3 -m venv "$VENV"
fi
bold "▶ Installing requirements"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$ADMIN_DIR/requirements.txt"

# 4. Run the demo. We run in the foreground so Ctrl-C works as expected.
green ""
green "✓ Starting demo at http://localhost:8080"
green "  Password: demo"
green "  Force-refresh the browser with Cmd+Shift+R after every code change."
green ""

cd "$ADMIN_DIR"
exec "$VENV/bin/python" -m app.demo
