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
scripts/deploy.sh --test      # from this folder on the Mac: sync, uv sync, pytest, restart
```

Since 2026-10-29 development happens on the Windows game machine only (see docs/env.md);
deploy.sh is the old Mac path and a Windows deploy script is still to be written.
Keep `/root/fkchang/flide-patch`: it guards the shared model service against a request
version mismatch.

On the server the app lives in `/root/fkchang/pvz-agent` (`/root` itself is shared
with other people's tools; `/mnt` and `/data` are full). `/root` is container-local
disk that can be lost if the container is rebuilt, so copy `runs/` back regularly.
`.env` (from `.env.example`) holds `PVZ_TOKEN` and, later,
`ANTHROPIC_API_KEY` with `PVZ_PLANNER=claude`. The server runs in tmux session
`pvz` (`tmux attach -t pvz`), logs to `logs/server.log`, writes runs to `runs/`.

## RSI loop (server, fake game for now)

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
