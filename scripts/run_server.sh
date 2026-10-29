#!/usr/bin/env bash
# Runs on the server inside tmux (this container has no systemd). Restarts on crash.
set -u
cd "$(dirname "$0")/.."
mkdir -p logs
while true; do
  # deploy.sh already ran `uv sync`; don't re-resolve the venv on every restart.
  uv run --no-sync python -m server.app >> logs/server.log 2>&1
  echo "$(date -Is) server exited with $?, restarting in 3s" >> logs/server.log
  sleep 3
done
