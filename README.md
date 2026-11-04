# pvz-harness

English | [简体中文](README.zh-CN.md)

An agent that plays **Plants vs. Zombies** (PC, GOTY 1.2.0.1096) from screenshots, with general-purpose
models instead of a trained game AI, and improves between games by editing its own strategy and
player code, never model weights.

The game machine (Windows) is a thin client: it streams screenshots and executes clicks. Everything
else runs on a remote GPU server: vision, planning, action choice, the judge, logs and learning.

**Status (2026-11-04).** The live pipeline plays real games end to end, including choosing seeds,
collecting sun and digging with the shovel. Training on the Portable re-implementation has won
levels 1-2 to 1-7 without lawn mowers; live games have been won on 1-7 and 1-8. There is no claim
yet that the agent completes the Adventure mode.

## How it works

```
 Windows game machine                     GPU server (FastAPI, 127.0.0.1:8765)
┌──────────────────────────┐  SSH tunnel ┌──────────────────────────────────────────────┐
│ client/windows_client     │  WebSocket  │ server/session.py: per-game async loops      │
│  screenshots (JPEG)  ─────┼────────────▶│  vision ×3 (Qwen) ─▶ Planner (Qwen)          │
│  clicks         ◀─────────┼─────────────│  System0 player code ─▶ Jev (OneJev) ─▶ clicks│
│  read-only game memory:   │             │  sun collector (code)                        │
│  outcome, mowers, clock   │             │  judge (the only reader of true state)       │
│ control center (Edge app) │             │ rsi/: post-game Coach, training, promotion   │
└──────────────────────────┘             └──────────────────────────────────────────────┘
```

| Role | What it does | Model / code |
|---|---|---|
| Vision | Three independent reads per frame: sun and cards, zombies, plant layout | Qwen3.8-27B; cards, lawn type, shovel and zombie positions by code |
| Planner | Objective, priority rows, budget advice, threat notes; called about every 25 s and on events | Qwen3.8-27B |
| System0 | Versioned player code: derives context, offers Jev a named decision menu, turns a choice into actions (plant, shovel), reflexes | Python, edited by the Coach |
| Jev | Picks one decision from the menu, or waits; text only | AutoTrust-JEV-27B (OneJev) |
| Sun collector | Clicks suns confirmed on two frames | Code |
| Seed choice | Picks the loadout from the previewed zombies | Qwen |
| Coach | Reviews games that cost a lawn mower; edits playbook and player code in training | Codex (gpt-6-astra) or Qwen |
| Judge | Scores outcome and lawn-mower timing from read-only game state | Code |

## Design rules

These are enforced by code and tests, not by convention.

- **Information isolation.** True game state (outcome, mowers, clock) goes only to the judge, the
  Coach and vision labelling. Player-side modules cannot import the judge (`tests/test_isolation.py`).
- **Authority.** Only an accepted Jev decision (or gated System0 code) may plant or dig; the Planner
  advises and cannot click. Every click is logged with its decision id.
- **Fixed rules.** Hard rules (`rules/hard_rules.py`), playbook ranges and promotion gates cannot be
  changed by the Coach.
- **Traceable versions.** Playbooks and player code are immutable versions; every change is logged.
- **Shared model pools.** The Windows machine never calls the models directly, and Jev never
  receives images.

## Repository layout

| Path | Contents |
|---|---|
| `client/` | Windows client: capture, clicks, read-only memory, menu detection, control center window |
| `server/` | Live session, vision, Planner, Jev, action gate, seed choice, shovel, reviews, diagnostics |
| `rules/` | Fixed hard rules, playbook schema, replan bounds, promotion gate |
| `rsi/` | Learning: post-game review, Coach, player-code runtime, Portable/pvz_rl training executors |
| `common/` | Wire protocol shared by client and server |
| `knowledge/` | Strategy notes, static game reference, card and shovel templates |
| `scripts/` | Launchers, deployment, control center server, training tools |
| `tests/` | ~720 tests; run them all before deploying |
| `docs/` | Architecture, project status, operations (see below) |

## Getting started

### Server

1. Copy `.env.example` to `.env` and fill it in (`PVZ_TOKEN`, model endpoints). `.env` is never
   synced or committed.
2. Run the server under its restart loop in tmux: `scripts/run_server.sh` (listens on
   `127.0.0.1:8765`; reach it through an SSH tunnel).
3. Deploy from Windows with `scripts/deploy.ps1 -Test`, which syncs, tests and restarts.

### Game machine (Windows)

1. Optionally open PvZ in windowed mode. The client also starts without the game and attaches
   when it opens.
2. Double-click `scripts/start_client.cmd`, or press **启动客户端** in the control center. The
   launcher opens the SSH tunnel, reads the token from the server into memory only, and starts the
   client.
3. Use the control center window, `http://127.0.0.1:8766/client`:
   - **开局前准备 (before a game):** *测试连接* times the tunnel, upload bandwidth, Qwen and OneJev.
   - **Client controls:** *开始 / 暂停 / 继续 / 结束* control the client only; game menus stay manual.
   - **游戏进度:** *选植物* fills the seed bank on the chooser screen; you press "Let's Rock".
   - **赛后复盘:** reviews of recent games, with each game's result.
4. For post-game reviews, start the Codex Coach worker: `scripts/codex_coach.ps1 -Mode Start`.

### Training

Start and stop training on the control center's training overview page (`http://127.0.0.1:8766/`).
It plays levels 1-2 to 1-10 on the Portable engine, which pauses while the models think, so model
latency does not affect the game. Setup: [docs/portable-rsi.md](docs/portable-rsi.md).

### Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests
```

## Learning loop

1. A finished live game that cost a lawn mower (every loss does) gets a Coach review: diagnosis,
   evidence, one measurable goal. A win without a mower is skipped.
2. Training rounds let the Coach edit the playbook (strategy notes, lessons, timing, settings) and
   the System0 player code, then evaluate the candidate on fixed seeds against the current version.
3. A candidate is promoted only if it passes the fixed gates. Live play and training share the
   promoted version.

## Documentation

| Document | Topic |
|---|---|
| [docs/PROJECT.md](docs/PROJECT.md) | Project overview, status, comparison with JueWu (Chinese) |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Architecture, data flow, permissions (Chinese) |
| [docs/HANDOFF_windows.md](docs/HANDOFF_windows.md) | Windows development and operations |
| [docs/portable-rsi.md](docs/portable-rsi.md) | Portable training environment |
| [docs/native-rsi.md](docs/native-rsi.md) | pvz_rl training environment (legacy) |
| [docs/codex-coach.md](docs/codex-coach.md) | Codex Coach worker |
| [docs/sun-collection.md](docs/sun-collection.md) | Automatic sun collection |
| [docs/game-mechanics.md](docs/game-mechanics.md) | Static game reference |
| [docs/mower-evaluation.md](docs/mower-evaluation.md) | Lawn-mower scoring and read-only state |
| [docs/IMPROVEMENTS.md](docs/IMPROVEMENTS.md) | Improvement log |
