#!/usr/bin/env bash
# Sync code to the server, install deps, (re)start the server in tmux session "pvz".
#   scripts/deploy.sh            deploy and restart
#   scripts/deploy.sh --test     also run the test suite on the server first
set -euo pipefail
cd "$(dirname "$0")/.."

HOST="${PVZ_SSH_HOST:-root@117.186.102.78}"
PORT="${PVZ_SSH_PORT:-32420}"
KEY="${PVZ_SSH_KEY:-../id_ed25519_llm2}"
REMOTE_DIR="${PVZ_REMOTE_DIR:-/root/fkchang/pvz-agent}"
SSH=(ssh -i "$KEY" -p "$PORT" -o BatchMode=yes -q)

# No rsync on the server: stream a tarball, replacing code but keeping .env, .venv, runs, logs,
# data (playbook versions + ledger) and the uv.lock resolved on the server (no uv on the Mac).
COPYFILE_DISABLE=1 tar --no-xattrs --no-fflags --no-mac-metadata -czf - --exclude .env --exclude .venv --exclude runs --exclude logs \
  --exclude data --exclude __pycache__ --exclude .pytest_cache --exclude .DS_Store . |
  "${SSH[@]}" "$HOST" "mkdir -p $REMOTE_DIR && cd $REMOTE_DIR &&
    find . -mindepth 1 -maxdepth 1 ! -name .env ! -name .venv ! -name runs ! -name logs ! -name data ! -name uv.lock -exec rm -rf {} + &&
    tar -xzf - --no-same-owner"

"${SSH[@]}" "$HOST" bash -s -- "$REMOTE_DIR" "${1:-}" <<'EOF'
set -euo pipefail
cd "$1"
if [ ! -f .env ]; then
  echo "missing $1/.env (copy .env.example and set PVZ_TOKEN)" >&2
  exit 1
fi
uv sync -q
if [ "${2:-}" = "--test" ]; then  # ssh drops an empty argument, so $2 may be unset
  uv run pytest -q
fi
tmux kill-session -t pvz 2>/dev/null || true
tmux new-session -d -s pvz "bash scripts/run_server.sh"
sleep 3
port=$(grep -E '^PVZ_PORT=' .env | cut -d= -f2 || true)
curl -sS "http://127.0.0.1:${port:-8765}/health"
echo
EOF
