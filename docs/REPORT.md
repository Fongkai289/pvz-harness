# pvz-harness technical report (draft)

English | [简体中文](REPORT.zh-CN.md)

General-purpose models playing Plants vs. Zombies from screenshots, improving without weight updates

> Draft; data as of 2026-11-04. Every number comes from the project's records (sources in the appendix).
> Samples are small: most results are a single game, and there is no fixed-seed controlled comparison.
> **Read them as preliminary observations, not conclusions.**

## Summary

General-purpose models (Qwen3.8-27B for vision and planning, AutoTrust-JEV-27B for action choice) play the PC
version of Plants vs. Zombies, GOTY 1.2.0.1096, in real time through screenshots and mouse clicks only. Model
weights stay fixed. Improvement comes from a Coach role that, after games, edits the strategy text and a versioned
piece of player code ("System0"). A candidate version is retested in a training environment before promotion.

Main observations:

1. **The pipeline completes real games.** Screenshots, vision, planning, action choice, clicks, sun collection
   and seed selection all run automatically. Both live games with a confirmed result were wins: 1-7 with no lawn
   mower used, and 1-8 with 2.
2. **The training campaign reached 1-8.** In the Portable training environment, the current playbook v013 (and
   its predecessor v010) won 1-2, 1-3, 1-4, 1-6 and 1-7 at the first attempt without a lawn mower, one game each.
3. **One clear case of the Coach fixing code.** On 1-5 (wall-nut bowling) the original playbook won but used all
   5 mowers. The Coach diagnosed that the generic tactics did not understand rolling nuts and wrote dedicated
   decision menus; the new version won its retest without a mower. The retest was a single game.
4. **The main bottleneck is engineering, not strategy.** Of 16 training or evaluation runs recorded on the game
   machine, 1 completed normally and 7 stopped because a model service was unreachable. In live play a Planner
   call takes about 11 s (median), and on a slow network the client uploads about one screenshot per second.

## 1. Background and goal

The question: without training a game-specific AI, using off-the-shelf general models plus a self-modifying
"harness" (strategy, code, process), how far can a real-time strategy game be played?

Constraints:

- **Screenshots in, mouse clicks out**, like a person. Game memory is read-only and only the judge and Coach use it
  (outcome, lawn mowers); player-side models never see it.
- **Real time.** In live play the game does not wait: the slower the models, the older the situation they act on.
- **Shared model pools.** Models run on a server shared with other teams; the Windows game machine never calls them
  directly and Jev never receives images.
- **No weight updates.** All "learning" happens in the playbook (strategy text and parameters) and System0 code;
  every version is immutable and traceable.

## 2. System

Full description: [ARCHITECTURE.md](ARCHITECTURE.md) (Chinese). Only what matters for the results is listed here.

| Role | Responsibility | Implementation |
|---|---|---|
| Vision | Three reads per frame: sun and cards, zombies, plant layout | Qwen; cards, lawn type, shovel and zombie positions by code |
| Planner | Guidance: objective, priority rows, budget advice, threat notes; cannot click | Qwen, routinely about every 25 s, events at most every 8 s |
| System0 | Versioned player code: derives context, offers Jev a decision menu, turns a choice into plant or shovel actions | Python, editable by the Coach |
| Jev | Picks one menu item or waits; text only | AutoTrust-JEV-27B |
| Sun collector | Clicks suns confirmed on two frames | Code |
| Seed choice | Picks the loadout from the previewed zombies | Qwen |
| Coach | Reviews games lost or that used a mower; edits playbook and System0 in training | Codex (gpt-6-astra), Qwen earlier |
| Judge | Outcome, progress and lawn-mower timing score | Code |

**Authority.** Only an accepted Jev decision, or System0 code passing the action gate, can trigger planting or
digging clicks, and every click carries its decision id. Hard rules (cell free, card usable, enough sun), playbook
ranges and promotion gates are fixed in `rules/`; the Coach cannot change them.

**Score.** A win scores 1, a loss 0.2 × progress; each lawn mower subtracts 0.1 + 0.1·e^(−t/60), where t is its
game-time trigger second, so earlier mowers cost more; the minimum is 0 (`rsi/native_gate.py`). A "clean win" is a
win without a lawn mower.

## 3. Setup

| Item | Setting |
|---|---|
| Game | PvZ GOTY 1.2.0.1096 (Steam), English, windowed, Adventure area 1 (day) |
| Live environment | Windows client over an SSH tunnel to the server; 5 screenshots/s target, network-limited |
| Training environment | PvZ-Portable (open-source re-implementation) with this project's harness patch and owned game resources; the game pauses while models think |
| Training levels | 1-2 to 1-10 (1-1 skipped), in order; a level counts as passed only without a mower |
| Models | Qwen3.8-27B (vision, Planner, seed choice), AutoTrust-JEV-27B-v1.1 (Jev), Codex gpt-6-astra (Coach) |
| Promotion | Per-level training: after each Coach edit the level is replayed once; a pass makes it that level's playbook (`level-v1`), with no multi-game gate |

## 4. Results

### 4.1 Training campaign

| Level | Playbook | Result | Mowers | Score | Wall time |
|---|---|---|---|---|---|
| 1-2 | v010 | win | 0 | 1.0 | 222 s |
| 1-3 | v010 | win | 0 | 1.0 | 444 s |
| 1-4 | v010 | win | 0 | 1.0 | 453 s |
| 1-5 | v010 → v011 | win (5 mowers) → win | 5 → 0 | 0.48 → 1.0 | 405 s → 113 s |
| 1-6 | v010 | win | 0 | 1.0 | 396 s |
| 1-7 | v013 | win | 0 | 1.0 | 871 s |
| 1-8 | v013 | training stopped manually on this level | — | — | — |

Each level was played once, without repeats, so the table shows that a level *can* be passed, not how often.
Wall time includes model thinking (the game pauses meanwhile). These training runs were each interrupted after
their level by the infrastructure problems listed in section 5, and new runs continued from there.

### 4.2 Case: the Coach fixes wall-nut bowling (1-5)

1-5 is a special level: a conveyor belt delivers nuts that the player rolls into zombies; explode-o-nuts burst.

- **Original playbook v010:** won, but used all 5 lawn mowers; score 0.48.
- **Coach diagnosis** (paraphrased): the generic day-level tactics ignored explode-o-nuts and treated rolling
  wall-nuts as permanent walls; they hoarded explosive ammunition while waiting for a sun economy this level does
  not have; 20 missed chances were recorded; rolling-nut goals also caused needless replanning.
- **Coach change:** dedicated legal menus and execution for both nut types, ranking threats by proximity, armor
  and clustering, plus adjusted Planner wording. This is v011.
- **Retest:** v011 won with no mower, score 1.0, in 113 s. A second candidate variant, v012, also won without a mower.

This is so far the only case in the real training environment where the Coach changed code on its own and the
change was promoted. With a single retest and a large change, it shows that the Coach can read a failure and write
working level-specific code; it does not show how reliable the improvement is.

### 4.3 Live play

The server recorded 39 live connections (10-29 to 11-04). A connection is not a game: pauses and reconnects split
one game into several segments, many only seconds long; segments of one game share the game's board id.
**Only 2 games have a confirmed outcome:**

| Time | Level | Result | Mowers | Duration | Planner calls | Jev plantings |
|---|---|---|---|---|---|---|
| 11-03 21:50 | 1-7 | win | 0 | 472 s game time (two segments after a reconnect) | 27 | 37 |
| 11-04 10:25 | 1-8 | win | 2 | 281 s game time (one segment) | 13 | 14 |

For the 1-8 game, the Coach's post-game review concluded that the emergency defense of rows 3 and 4 came too late.
It found three verifiable problems:
- the Planner still advised a sunflower while those rows had no attacker;
- a 15 s planning call worked from a screenshot 20 s old;
- the menu placed a wall-nut behind a zombie that had already passed it.

**Seed choice** was used 3 times live, 2.4–3.5 s each. All three times the preview showed normal, conehead and
buckethead zombies, and the choice was sunflower, peashooter, snow pea, wall-nut, cherry bomb and chomper.

**Shovel:** connected, but the current System0 code does not use it yet; no live dig so far.

### 4.4 Playbook history

| Version | Source | Change |
|---|---|---|
| v001 | Hand-written | Initial parameters |
| v002, v003, v005 | Coach (early simplified game) | Sun detection hue and area thresholds |
| v004 | Human | Strategy knowledge from community guides |
| v006 | Human | Comparison experiment (attack off), not promoted |
| v007–v009 | Human | Sun hue fix after the first real game; vision and planning periods; danger-triggered replanning |
| v010 | Human | Seed System0 code: per-row estimates, replan checks, fallback planting; shared by live play and training |
| v011 | **Coach (training)** | 1-5 wall-nut bowling code (see 4.2) |
| v013 | Human | System0 fix: a goal already met when a plan arrives no longer requests a replan (was every 8 s) |

The current version is v013 (1-5 uses v011). In the real-game environments, v011 is the only version promoted by
the Coach on its own.

### 4.5 Latency and cost

Measured on live games after 11-03 21:00, when the Planner shortening change was live:

| Stage | Calls | Median | 90th percentile |
|---|---|---|---|
| Sun and card read | 209 | 0.26 s | 0.50 s |
| Zombie read | 198 | 3.19 s | 4.23 s |
| Plant layout read | 197 | 3.53 s | 4.58 s |
| Planner | 46 | 11.17 s | 15.34 s |
| Screenshot capture to server arrival | 1995 | 0.33 s | 0.74 s |

- Planner output is 518 tokens at the median, 657 at most. Early versions on 10-29 and 10-30 produced tens to
  about two hundred tokens in 1–3 s; as plans grew richer, calls rose to 7–17 s. Capping the free-text fields did
  not noticeably shorten them: most output tokens come from structured fields, not prose.
- Jev decisions take 0.06–0.8 s (per-game medians).
- **Coach cost:** one live review is about 110k tokens (108,360 in, 1,409 out). One training edit round on 1-5 took
  14–15 calls per variant, about 370k–420k tokens.

### 4.6 Earlier stages

- **Simplified sun game** (2026-10-28): parameter-level self-improvement raised the held-out mean from 0.153 to
  0.955, tuning only sun-detection thresholds.
- **PvZ-Emulator procedural benchmark** (2026-10-29): the only promotion raised wins from 0/12 to 1/12, but the
  candidate came from an external Codex proposal, not from Qwen learning on its own. This route is retired.

## 5. Engineering findings

1. **Infrastructure failures dominate.** Of the 16 training or evaluation runs recorded on the game machine:

   | How the run ended | Runs |
   |---|---|
   | Model service unreachable | 7 |
   | Invalid Planner output | 2 |
   | Stopped by the operator | 2 |
   | Coach request expired | 1 |
   | Codex session conflict | 1 |
   | Another parallel evaluation lane failed | 1 |
   | Local file permission | 1 |
   | Completed normally | 1 |

   Before self-improvement can run for real, long runs have to be reliable: reconnecting, visible failures,
   resumable work.

2. **The network sets the pace of live play.** On a slow network, screenshots once queued up to 21 s behind.
   Sending the next frame only after the previous one is acknowledged removed the backlog, but the rate can drop to
   about one frame per second. That exposed two pieces of logic that polled at fixed times:
   - Vision required a frame younger than 2 s or skipped until the next tick, so it kept missing frames; it now
     runs as soon as a new frame arrives.
   - A sun needed two frames within 0.8 s to be confirmed, so a landed sun could stay unconfirmed for its whole
     7.5 s life; the window is now 2 s, since a landed sun does not move.
3. **The Planner is the latency bottleneck**, and longer outputs are slower. Following related work on Honor of
   Kings, routine planning now happens about every 25 s, with System0 requesting earlier plans on danger. A call
   still takes about 11 s, so plans still rest on old screenshots.
4. **Coach reviews land on concrete problems.** Findings such as "used a 20 s old screenshot" or "wall-nut placed
   behind the zombie" translate directly into code changes; this is one of the project's most valuable parts.
5. **Operational problems silently ruin evaluation.** For example, when the Coach worker exited unexpectedly, review
   requests went unclaimed and were cancelled after 4 minutes, invisibly at the time. The control center now shows
   each game's review state and outcome.

## 6. Limitations

- **No controlled comparison.** Every number comes from single games. Without repeated fixed-seed games, version
  differences cannot be separated from luck.
- **Weak promotion gate.** Per-level promotion rests on a single replay; `level-v1` requires no multi-game significance.
- **Component contributions are untested.** There is no ablation of the Planner, Jev and System0. Related Honor of
  Kings work did not show that a planner improves win rates either.
- **Incomplete live results.** Of 39 connections only 2 games have a confirmed outcome; the rest are unfinished
  games, segments without a result, or pieces of split games. Lost games were not explicitly recorded, so these
  records cannot estimate a live win rate.
- **Narrow coverage.** Only area 1 (day) is tested. Memory reads are tied to the English 1.2.0.1096 build. A seed
  chooser with a 7th row is not supported yet.
- **Model-specific.** Jev uses AutoTrust-JEV-27B's dedicated readout protocol; other models need a new adapter.

## 7. Next steps

1. **Controlled evaluation.** On 1-2 to 1-7, with fixed seeds and several games per level, compare three setups:
   Planner + Jev + code, Planner + code, code only.
2. **A stronger promotion gate.** Require several replays for per-level training too, and check that levels
   already passed do not regress.
3. **Continue the campaign.** 1-8 to 1-10, with attention to 1-10 (a conveyor-belt level).
4. **Lower planning latency.** Shorten the Planner's structured output, or move more immediate judgments to System0.
5. **Let the Coach use the shovel**, and measure whether it helps.

## Appendix: data sources

| Content | Source |
|---|---|
| Training campaign, 1-5 case | `data/portable_rsi/worker/levels/*.json`, `training.json`, `coach-recheck-20261103-135446-summary.json` |
| How training runs ended | `data/portable_rsi/worker/*-summary.json` (16 files) |
| Live games and latency | Server `runs/2026*-windows-*/events.jsonl` |
| Seed choices | Server `data/seed_choices/` |
| Playbook versions | Server `data/playbooks/v*.json`, `data/ledger.jsonl` |
| Live reviews | Server `data/live_reviews/` |
| Coach tokens | Server Codex queue statistics (the control center's Coach token table) |
| Score formula | `episode_score` in `rsi/native_gate.py` |
