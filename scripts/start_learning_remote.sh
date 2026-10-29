#!/usr/bin/env bash
# Independent workspace, process and simulation champion. No live-server restart.
set -euo pipefail
rounds=${1:-3}
[[ "$rounds" =~ ^[0-9]+$ ]] && ((rounds >= 1 && rounds <= 20)) || exit 2
cd /srv/operator/pvz-learning
export UV_CACHE_DIR=/srv/operator/pvz-learning/.cache/uv
export UV_PYTHON_INSTALL_DIR=/srv/operator/pvz-learning/.cache/python
export PIP_CACHE_DIR=/srv/operator/pvz-learning/.cache/pip
export TMPDIR=/srv/operator/pvz-learning/.cache/tmp
mkdir -p "$TMPDIR"
test -f /srv/pvz-harness/.env
if tmux has-session -t pvz-learning 2>/dev/null; then
  echo 'Learning is already running; inspect it instead of starting a duplicate.' >&2
  exit 1
fi
uv sync
.venv/bin/python scripts/install_emulator.py
.venv/bin/python -m pytest -q
mkdir -p logs
tmux new-session -d -s pvz-learning "cd /srv/operator/pvz-learning && .venv/bin/python -u -m rsi.code_loop --rounds $rounds --settings-env /srv/pvz-harness/.env > logs/learning.log 2>&1"
echo "Started $rounds rounds in tmux pvz-learning; logs: /srv/operator/pvz-learning/logs/learning.log"
