#!/usr/bin/env bash
# Server half of scripts/deploy.ps1. Replaces the code with the uploaded tarball, keeping .env, .venv,
# runs, logs, data (playbook versions + ledger) and the uv.lock resolved on the server, then syncs
# dependencies, optionally runs the tests, and restarts the server in tmux session "pvz".
#   bash deploy_remote.sh <remote_dir> <tarball> <stamp> [--test]
set -euo pipefail
dir=$1 tarball=$2 stamp=$3 mode=${4:-}
mkdir -p "$dir"
cd "$dir"
if [ ! -f .env ]; then
  echo "missing $dir/.env (copy .env.example and set PVZ_TOKEN)" >&2
  exit 1
fi
find . -mindepth 1 -maxdepth 1 ! -name .env ! -name .venv ! -name runs ! -name logs ! -name data ! -name uv.lock \
  -exec rm -rf {} +
tar -xzf "$tarball" --no-same-owner
rm -f "$tarball"
echo "$stamp $(date -Is)" > DEPLOYED
uv sync -q
if [ "$mode" = "--test" ]; then
  uv run pytest -q
fi
tmux kill-session -t pvz 2>/dev/null || true
tmux new-session -d -s pvz "bash scripts/run_server.sh"
sleep 3
port=$(grep -E '^PVZ_PORT=' .env | cut -d= -f2 || true)
curl -sS "http://127.0.0.1:${port:-8765}/health"
echo
