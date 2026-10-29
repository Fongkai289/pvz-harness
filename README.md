# pvz-agent

Computer-use agent for Plants vs. Zombies, set up as a "reverse cloud game": the
Windows game machine is a thin client that streams screenshots and executes
clicks; the server runs perception, the fast policy, the vision-model planner,
the judge and the run logs. Design doc: https://claude.ai/artifact/LoFF6d77152w5JRaBon1HC

```
common/protocol.py     wire format shared by both sides
server/                FastAPI WebSocket server (runs on the remote box)
  perception.py        pixels -> observation          (player side)
  policy.py            fast layer, ~1 s, clicks         (player side)
  scene.py             look step: code reads cards + lawn type, Qwen reads sun + zombies per row; a separate,
                       slower Qwen read of the plants per cell (the lawn as seen is authoritative)
  planner.py           big model, ~20 s: decide from the scene (split) or look+decide in one call (end_to_end)
  decider.py           Jev, each second, three layers: OneJev-27B yes/no judgments per row (text facts only)
                       -> playbook tactics (priorities + thresholds, code compares) -> hard rules -> clicks
  judge.py             ground-truth state, scoring      (privileged side)
  session.py           wires the layers for one client
  playbook.py          versioned playbooks: data/playbooks/vNNN.json, data/CURRENT, data/ledger.jsonl
rules/playbook_rules.py  fixed rules the coach cannot change: sections, allowed fields and ranges
rules/hard_rules.py    fixed checks on every placement: free cell, usable card, enough sun, instant kills in reach
                       sections: perception (code) / fast (code) / decider (OneJev) / planner (Qwen)
knowledge/pvz_strategy.md  human-curated strategy from guides; planner block + notes and sources
playbook_seed/v001.json  hand-written first playbook (seeds data/ on first start)
rsi/
  evaluate.py          play a version on fixed seeds; judge scores + per-click diagnostics
  coach.py             Qwen (text) reads dev results incl. ground truth, proposes one change
  loop.py              rounds: baseline -> coach -> candidate -> gate -> promote/rollback; held-out checks
  human_edit.py        a person's edit (e.g. new strategy knowledge) as a validated, logged version
  vision_eval.py       score the look step on labelled screenshots (eval/vision_labels.json)
eval/vision_labels.json  ground truth for 4 official Steam screenshots (images: server data/vision_eval/)
client/
  link.py              client half of the protocol
  mock_client.py       fake games for testing without Windows: --game suns (sun collecting) | lawn
  fake_lawn.py         drawn, simplified PvZ: cards, sun, plants, six zombie types, eating, mowers, win/lose
  windows_client.py    real client (untested until environment verification)
tests/                 includes an import check that keeps state away from the player side
```

## Server (remote)

Listens on 127.0.0.1 only; clients reach it through an SSH tunnel, because the
host only exposes its SSH port.

```bash
powershell -File scripts/deploy.ps1 -Test  # Windows: sync, test and restart the live service
```

Since 2026-10-29 development happens on the Windows game machine only (see docs/env.md);
deploy.sh is the old Mac path; deploy.ps1 is the Windows entry point.
Keep `/root/fkchang/flide-patch`: it guards the shared model service against a request
version mismatch.

On the server the app lives in `/root/fkchang/pvz-agent` (`/root` itself is shared
with other people's tools; `/mnt` and `/data` are full). `/root` is container-local
disk that can be lost if the container is rebuilt, so copy `runs/` back regularly.
`.env` (from `.env.example`) holds `PVZ_TOKEN` and, later,
`ANTHROPIC_API_KEY` with `PVZ_PLANNER=claude`. The server runs in tmux session
`pvz` (`tmux attach -t pvz`), logs to `logs/server.log`, writes runs to `runs/`.

## Code research and native-emulator learning

The new `rsi.code_loop` lets a coach discover failures from completed development
games and rewrite **Python perception and decision/reflex functions**. This is
code evolution, not model-weight training. The old parameter-only loop below
remains available.

The native engine is [dnartz/PvZ-Emulator](https://github.com/dnartz/PvZ-Emulator),
pinned to `bb6347233fbbf9964fa6dd4a15f721262eda15ce`. The installer applies an
explicit training patch: seedable construction, deterministic external spawn
events, a binding syntax repair, and a sunflower/sky-sun timer fix. The timer fix
changes upstream behavior; this benchmark is not an unmodified original game.

The initial benchmark is daytime 5x9 with six plants and three zombie types.
Native combat, projectiles, planting costs, cooldowns and sun production run at
100 ticks per game second. Episodes last at most 180 simulated seconds. The
renderer draws **procedural RGB symbols**, not original-game sprites. Sun is
automatically credited; there are no mowers or sun-click collection tasks. A win
means clearing the benchmark's scheduled zombies, not beating an Adventure level.

Player functions receive only a detached image, their own observations and JSON
memory. The engine and `truth()` stay with the judge/coach. Generated programs
cannot import modules, open files, call the network or change the evaluator.
`rsi/code_runtime.py` implements a restricted Python subset, not an arbitrary-code
security sandbox; workers also have an external timeout and Linux resource limits.

Each candidate is immutable and addressed by its source hash. Fixed rules require:

1. Development: eight paired scenarios, mean gain at least 0.02 and improvement
   in at least 60% of scenarios.
2. Confirmation: twelve fresh pairs, the same minimum gain and a one-sided exact
   sign-test p-value at most 0.05.
3. Audit: twelve additional pairs, no mean-score or win-count regression.

No stage may reduce the number of completed wins. A win scores 1; non-wins score
`0.2 * killed/scheduled + 0.1 * survived_ticks/18000`. Rules and seed counts cannot
be changed by candidates. Confirmation/audit records, including errors, are
withheld from the coach. These are practical promotion checks, not a guarantee
against overfitting across unlimited experiments.

Only `data/code_rsi/pvzemu-symbolic-v1/CURRENT` is promoted. The real game's
`data/CURRENT` is separate: a simulator improvement needs real-screen and
real-client validation before transfer. Both perception and decision source can
evolve here; prompt-only vision experiments and live hot-swapping are not wired
into this loop yet.

### Install and run

```powershell
# Windows: compile the native engine and run a baseline (no model requests).
.\.venv\Scripts\python.exe scripts/install_emulator.py --download-toolchain
.\.venv\Scripts\python.exe -X utf8 -m rsi.code_loop --baseline-only

# Upload to an independent server workspace, test, then run three Qwen rounds.
powershell -File scripts/start_learning.ps1 -Rounds 3
```

`start_learning.ps1` uses `/root/fkchang/pvz-learning`, its own virtual environment,
and tmux session `pvz-learning`. It does not restart the live `pvz` service. Model
settings are read from the existing server `/root/fkchang/pvz-agent/.env`; secrets
are neither downloaded nor inherited by candidate workers. All Qwen requests
remain on the server; this loop does not send images to OneJev.

```bash
# From /root/fkchang/pvz-learning:
.venv/bin/python -m rsi.code_loop --rounds 3 --settings-env /root/fkchang/pvz-agent/.env
.venv/bin/python -m rsi.code_control status
.venv/bin/python -m rsi.code_control pause
.venv/bin/python -m rsi.code_control resume
.venv/bin/python -m rsi.code_control stop
```

Pause stops simulation advancement at the next decision boundary. Stop terminates
the evaluation worker. Pause/stop during a coach call cancels the client request
and ends that run; an already accepted model request may still finish inside the
shared model server. Nothing automatically resumes a paused/stopped run. These
controls apply to the research loop, not the existing Windows game control panel.

Each run saves `experiment.json`, complete episode reports, proposal source and
rationale, model-call latency/usage, `status.json`, and append-only ledger events.
`LATEST_RUN.json` identifies the latest run. To reproduce an external coach's
proposal without making model requests, use `--rounds 1 --candidate-json FILE
--candidate-origin NAME`; FILE follows `CodeProposal` in `rsi/code_coach.py`.
External proposals undergo the identical gates and have explicit provenance.

### First local validation (2026-10-29)

The initial eight native-engine episodes completed with zero wins. With the
server temporarily unreachable, the current Codex session reviewed development
records and proposed a threat-driven defense change. The proposal passed all
three fixed stages over 64 fresh paired episodes and was promoted locally:
confirmation score 0.0987 -> 0.2626 (wins 0/12 -> 1/12); audit score 0.1053 ->
0.2602 (wins 0/12 -> 1/12). This was an externally supplied Codex proposal, **not a
Qwen autonomous-learning result**. The procedural benchmark remains difficult;
these results do not establish real-game success.

## Legacy parameter RSI loop (server, simplified fake game)

```bash
cd /root/fkchang/pvz-agent && uv run python -m rsi.loop --rounds 3
```

First run (2026-10-28): v001 -> v002 (hue upper bound 35 -> 31, drops decoys) -> v003
(min sun area 600 -> 300, catches small suns); held-out mean 0.153 -> 0.955.

## Client

```bash
ssh -N -L 8765:127.0.0.1:8765 -p 32420 -i <key> root@117.186.102.78   # keep open
python -m client.mock_client --token <PVZ_TOKEN> --seconds 30           # any machine
python -m client.windows_client --token <PVZ_TOKEN>                     # Windows + game
```
