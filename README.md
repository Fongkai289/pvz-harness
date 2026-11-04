# pvz-agent

Computer-use agent for Plants vs. Zombies, set up as a "reverse cloud game": the
Windows game machine is a thin client that streams screenshots and executes
clicks; the server runs perception, the fast policy, the vision-model planner,
the judge and the run logs. Design doc: https://claude.ai/artifact/LoFF6d77152w5JRaBon1HC

```
common/protocol.py     wire format shared by both sides
server/                FastAPI WebSocket server (runs on the remote box)
  perception.py        pixels -> observation          (player side)
  policy.py            enumerate sun targets and compile authorized Jev clicks (player side)
  scene.py             separate Qwen resource, zombie and plant-layout reads; code reads cards + lawn type
  planner.py           Qwen, target 6 s plus danger events: objectives, priorities and budget; no placements
  action_selector.py   Jev, ~1 s: choose plant+row then column, or wait; text-only
  sun_collector.py     independent pixel-only sun confirmation for automatic collection
  decider.py           legacy tactics, only for the explicit rules comparison baseline
  judge.py             ground-truth state, scoring      (privileged side)
  session.py           wires the layers for one client
  playbook.py          versioned playbooks: data/playbooks/vNNN.json, data/CURRENT, data/ledger.jsonl
rules/playbook_rules.py  fixed rules the coach cannot change: sections, allowed fields and ranges
rules/hard_rules.py    fixed checks on every placement: free cell, usable card, enough sun, instant kills in reach
                       sections: perception / fast / jev (rules baseline only) / planner / timing
knowledge/pvz_strategy.md  human-curated strategy from guides; planner block + notes and sources
knowledge/pvz_mechanics.json  public static HP, damage, movement and timing reference; no live HP tracking
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
  windows_client.py    real client: screenshot capture, input execution and privileged state reporting
  app_window.py        the only window: the control center page (8766/client) in a pinned Edge app window
  operator_view.py     read-only planning, review, playbook and game reference tabs
tests/                 includes an import check that keeps state away from the player side
```

Static reference scope, sources and dashboard semantics: [docs/game-mechanics.md](docs/game-mechanics.md).

## Server (remote)

Listens on 127.0.0.1 only; clients reach it through an SSH tunnel, because the
host only exposes its SSH port.

```bash
powershell -File scripts/deploy.ps1 -Test  # Windows: sync, test and restart the live service
```

Development and deployment happen on the Windows game machine (see docs/HANDOFF_windows.md).
Use deploy.ps1; the obsolete Mac deployment entry point has been removed.
Keep `/srv/operator/flide-patch`: it guards the shared model service against a request
version mismatch.

On the server the app lives in `/srv/pvz-harness` (`/root` itself is shared
with other people's tools; `/mnt` and `/data` are full). `/root` is container-local
disk that can be lost if the container is rebuilt, so copy `runs/` back regularly.
`.env` (from `.env.example`) holds `PVZ_TOKEN` and, later,
`ANTHROPIC_API_KEY` with `PVZ_PLANNER=claude`. The server runs in tmux session
`pvz` (`tmux attach -t pvz`), logs to `logs/server.log`, writes runs to `runs/`.

## Planner guidance and Jev action selection

调用周期也是版本化策略的一部分。`timing.plants_seconds=4`、`timing.planner_seconds=6` 为本次设置；
资源和僵尸默认也是 4 秒。`timing` 中显式数值覆盖服务端环境变量，null 才继承环境设置。
识图允许 2–30 秒，Planner 允许 4–60 秒；按调用开始到下一次开始计时，同一路不重叠、不补发积压调用。
Planner 会在指令失效前提前刷新，实际频率也受推理时长、暂停与新画面是否可用影响。
每次连接固定一个策略版本；新版本在下一次连接生效，控制台和日志记录有效周期。

手工修改使用服务器上的版本管理入口，例如：

```bash
python -m rsi.human_edit --set timing.plants_seconds=4 --set timing.planner_seconds=6 --set timing.frame_log_seconds=4 --note "adjust call periods"
```

参数 Coach 的候选 schema 也包含 `timing`，沿用评测后晋升流程。真实赛后 Coach 仍只输出复盘和待验证目标，
不会自行部署周期；原生模拟器直接运行 Python 玩家，不能用其分数证明远程模型调用周期更优。
OneJev 模式的策略页和 Coach 输入不再包含仅用于 rules 基线的 `jev` 战术字段。

真实客户端默认上传 5 帧/秒（`--fps` 可覆盖）；普通截图每 4 秒保存一次，危险触发和终局另存关键帧。
历史截图尚无自动过期删除机制；磁盘上限只停止新增图片，不会删除旧对局，文字事件仍记录。
提前规划来自观察变化：僵尸类型/数量变化、向房屋移动、覆盖变化，以及阳光/可用卡片变化。
不再依据代码危险等级或预算拦截触发。`timing.danger_replan` 可关闭，`danger_min_seconds` 默认 4 秒。
每次规划开始之间保持最小间隔，推理中只合并待处理原因，不并发、不无限排队、不绕过 Jev 点击。
危险等级由 Planner 的 threat_assessment 消息给出；代码只提供逐行事实，不估算实时血量或精确突破时间。
“规划与决策”页显示原始规划、最近 Jev 选择、拦截原因、实际下发点击和输入回执；回执不等于种植成功。

真实游戏检测到明确胜利/失败后，会自动封存对局并触发 coach。控制台的“赛后复盘”页显示进度、
诊断、证据和待验证目标；“当前策略”显示正在生效的完整 playbook。暂停或断线不会触发复盘。
复盘完成后等待操作员确认下一局，不自动重试。若需补做一次复盘（先暂停，让日志完整落盘）：

```powershell
.\scripts\review_last_game.ps1
# 指定一局：.\scripts\review_last_game.ps1 -Session 20261030-122936-windows-40aa48
```

命令通过已有 SSH 配置在服务器运行 `python -m rsi.review_live --latest`。
coach 输出中文诊断、证据、一个可测量改进目标和不确定性；结果、证据与模型请求/响应保存在
`data/live_reviews/<session>/<log-hash>/`。重复运行相同日志直接读取已保存结果。
复盘不会修改当前策略或跳过评测晋升；原生模拟器的代码学习循环是另一条入口。
日志的一次连接不一定等于完整一局（暂停会断开）；复盘会保留起止游戏时间、历史小推车状态和
缺失的胜负信息，不把中途接入的片段冒充完整对局。

The live control contract is `advisory-strategy-auto-suns-v4`. Planner output
contains an objective, ordered priorities, priority/blocked rows, sun reserve,
suggested spending allowance, adjustment policy, tactics, threat assessment and validity duration. The schema rejects placement lists,
seed slots and cell coordinates. Free-text guidance is never executed as a tool
call. The executor no longer has a planner placement queue.

`PVZ_DECIDER=onejev` enumerates placements admitted by the existing hard rules
for actual affordability, card usability and cell validity, then lets OneJev choose plant+row or wait.
Planner budget/reserve/row advice and tactical distance do not filter candidates. An independent code loop collects suns.
Choosing a plant+row requires a second column choice.
All admitted columns in the chosen group remain available; tactical code does not
preselect a single cell. Each stage includes wait. Both stages use the existing
text-only choice API (with legacy `noul` support), with at most 61 and 10 alternatives
respectively (10 cards / 6 rows), under one total `PVZ_DECIDER_TIMEOUT` deadline.
This ranking protocol needs real-model quality and latency measurement; the old
single-fact Jev measurements do not establish its performance on this task.

Jev errors, malformed scores and timeouts mean wait, with no hidden rule fallback.
`PVZ_DECIDER=rules` explicitly selects the old tactical baseline, subject to the
same execution checks. Its `jev` playbook settings are not consumed by
the model selector. The retired tough-zombie model adapter has been removed.

Before planting, the session rechecks guidance/scene freshness, changes during
the model call, pause state, occupancy, card use and actual affordability. Committed planting costs accumulate
across look updates and reset only when new planner guidance is accepted; the remaining suggested allowance is sent explicitly to Jev. A new
scene does not erase commands issued after its screenshot. Episode boundaries
invalidate pending results. Planner budget replacement cannot split the card/cell
click pair. Automatic sun collection confirms candidates in two recent frames, excludes UI regions,
deduplicates clicks and shares the planting lock. It runs without Planner/Jev readiness or budgets.
Planting clicks log their Jev decision id; collection clicks log `decision_source=sun_collector`.
Planner cannot pause or resume the game. Details: [docs/sun-collection.md](docs/sun-collection.md).
Operator controls remain direct; the obsolete pause-for-planner setting has been removed.

Live input requires both a fresh observed scene and a valid directive. Without
either, no clicks are sent. Qwen configurations keep the separate
looker active even when the planner consumes screenshots (`end_to_end`); that
mode no longer bypasses Jev. Other planner backends need a supplied looker for
planting. `/health` reports the contract, selector and whether a looker is wired;
it is not evidence that a fresh scene or successful model result exists.

Logs retain planner directives, Jev scores/choice/latency/errors, rejected actions
and budget usage. Local tests cover the model-response-to-click chain with fake
HTTP responses; live Qwen/OneJev performance must be verified on the server.
The native-emulator code-research loop below still evaluates generated Python
players, not this multi-model live pipeline. Its candidates are not automatically
promoted into this pipeline or the real client.

## Code research and native-emulator learning

The new `rsi.code_loop` separates **coach, engineer and judge**. The coach discovers
failures from completed development games and returns an evidence-backed task,
not code. An independent engineer investigates source and logs, edits player
modules, runs development tests and repairs failures. The fixed judge decides
whether to promote the resulting candidate. This is code evolution, not
model-weight training. The old parameter-only loop below remains available.

Coach and engineer use separate Qwen contexts and prompts on the existing server
model pool; no additional model deployment is needed. The engineer may challenge
the coach's diagnosis. Its tools are `list_files`, `read_file`, `write_file`,
`check`, `test_candidate` and `finish`. Each round permits one coach request,
up to twelve engineer requests and two engineer development evaluations, followed
by the independent promotion stages. Engineer tests use the current development
seeds; no confirmation/audit records are visible to either role.

The engineer works in a private copy of five modules: `player/perception.py`,
`player/state.py`, `player/actions.py`, `player/policy.py` and `player/tools.py`.
These are virtual candidate paths, not the live server's files. Modules share
a restricted function namespace without imports. The engineer can add and change
pixel-processing tools, observation/memory processing and action construction,
using the fixed pixel primitives. It can read those primitives' source and the
renderer contract, but cannot edit the host primitive implementations, evaluator,
model transport, simulator or real Windows client. This is a bounded player-tool
engineer, not an unrestricted repository-maintenance agent.

Every submission must have successfully completed a development test of its
exact final source; a low score is allowed to reach the judge and be rejected.
Edits after testing invalidate submission until retested. Validation/test errors
return to the engineer for repair within its budget. Exhaustion or failure leaves
the incumbent in place. Module boundaries are stored inside the immutable
candidate source, so subsequent rounds inherit the promoted tools as well as the
policy. Legacy single-file candidates are split mechanically without policy changes.

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

`start_learning.ps1` uses `/srv/operator/pvz-learning`, its own virtual environment,
and tmux session `pvz-learning`. It does not restart the live `pvz` service. Model
settings are read from the existing server `/srv/pvz-harness/.env`; secrets
are neither downloaded nor inherited by candidate workers. All Qwen requests
remain on the server; this loop does not send images to OneJev.

```bash
# From /srv/operator/pvz-learning:
.venv/bin/python -m rsi.code_loop --rounds 3 --settings-env /srv/pvz-harness/.env
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
`roles/<id>/` saves the coach task, separate role requests/responses (including
HTTP errors), engineer tool actions/results, before/work module copies, candidate
diff and manifest. Status distinguishes `coaching`, `engineering`,
`engineer_testing` and judge evaluations. Internally generated candidates have
origin `qwen-engineer`; recorded external proposals retain their supplied origin.
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
cd /srv/pvz-harness && uv run python -m rsi.loop --rounds 3
```

First run (2026-10-28): v001 -> v002 (hue upper bound 35 -> 31, drops decoys) -> v003
(min sun area 600 -> 300, catches small suns); held-out mean 0.153 -> 0.955.

## Client

```bash
ssh -N -L 8765:127.0.0.1:8765 -p 22 -i <key> operator@server.example.com   # keep open
python -m client.mock_client --token <PVZ_TOKEN> --seconds 30           # any machine
python -m client.windows_client --token <PVZ_TOKEN>                     # Windows + game
```

一键启动（2026-11-03）：双击 `scripts\start_client.cmd`，或在控制中心 `http://127.0.0.1:8766/client` 点「启动客户端」。
脚本先检查客户端是否已在运行；8765 端口没有隧道时，用 `scripts\connect_server.ps1 -Mode Tunnel` 在最小化窗口里开一条会自动重连的隧道；
令牌取自 `$env:PVZ_TOKEN` 或用户环境变量 `PVZ_TOKEN`，都没有时在窗口里提示输入（只留在该进程内存，不写文件）；然后启动客户端，窗口即客户端日志。

Windows 控制面板固定四个按钮：**开始 / 暂停 / 继续 / 结束**，只控制客户端。
“开始”启用识图和决策；“暂停”停止上传、拒绝操作并断开玩家连接；“继续”解除客户端暂停；
“结束”停止客户端任务并断开玩家连接，保留控制窗口、统计和复盘，可点“开始”重新运行；右上角关闭按钮才退出程序。已经送到模型服务的请求可能仍会完成。
客户端启动时未启用。启用后若游戏失焦或处于菜单，则等待你手动进入/恢复游戏；
连续两次确认前台画面可玩后建立新连接，无须再点客户端按钮。主动暂停客户端必须点继续。
游戏的开始、暂停、恢复、重打、领奖与选卡全部手动；客户端不代点菜单，不抢游戏焦点，
也不挂起或恢复游戏进程。游戏本身可能在点击控制台失去焦点时自动暂停，这不是客户端暂停命令。
终局仍自动复盘；手动进入下一局表示操作员确认继续，客户端不会替你重打或等待复盘结果来代点菜单。
菜单识别使用仓库的 `docs/screens/` 中的参考截图，请保留这些文件。更新后需重启客户端
才能看到新按钮；服务器和 SSH 隧道仍需正常运行。

面板不显示会话名，显示客户端 RSS 内存、每秒上/下行应用数据量（不含 SSH/TCP 开销）、
WebSocket Ping/Pong 往返延迟，以及成功点击的“截图 → 点击完成”端到端中位数/p95。
Qwen 识图、植物矩阵、规划及 Jev 决策分别显示最近一次模型调用耗时（含服务排队与响应传输，
不是 GPU 纯推理时间）；没有实际调用时显示等待，不把规则等待计为零延迟推理。
模型遥测由支持该功能的服务器回传，旧服务器会明确显示“服务器未提供”。暂停后延迟保留上次结果。

模型面板区分 **未触发 / 正在推理 / 完成 / 失败**，显示本次连接累计调用次数、
滚动近 60 秒启动次数、距上次触发的秒数及配置轮询间隔。Jev 以一次种植/等待决策为一轮
（内部可能包含两次评分请求）；无合法候选时不计推理次数。轮询间隔不是固定发起频率：
识图与规划按调用开始间隔调度，且需有新画面和有效规划等条件。正在推理包含服务排队。
未触发时显示等待条件，断开时未完成调用标为失败并注明未收到结果；重连重新计数。
客户端用 Windows 命名互斥量阻止重复打开，避免多实例争用鼠标和游戏焦点。
暂停时“距触发”和近 60 秒统计冻结；恢复建立新连接并重新计数。
结束后暂停/继续不可用；暂停后开始/暂停不可用，只能继续或结束。旧连接退出前暂不允许开始/继续，避免残留指令进入下一次运行。游戏保持由用户控制。

视觉拆成三路独立 Qwen 请求：资源与卡片（`PVZ_LOOK_INTERVAL`，默认 4 秒）、僵尸
（`PVZ_ZOMBIES_INTERVAL`，默认 4 秒）、植物布局（`PVZ_PLANTS_INTERVAL`，默认 4 秒）。
卡片可用性仍由代码读亮度，Qwen 读取阳光数。各路独立记录帧号、延迟和状态；僵尸更新不重置
用卡/花费账本，资源更新不覆盖僵尸结果。缺少/过期的僵尸结果会阻止决策；植物布局过期只阻止
种植。Planner 接收合并局势、布局和各路来源帧号；Jev 始终只接收结构化文本。

Planner 会根据指令有效期和最近推理时间提前刷新，配置间隔是上限。未触发原因区分首次识图、
首次规划、过期、阳光/冷却、预算和保留阳光；阻塞统计反馈给 Planner。
小推车新增游戏时间指标，早触发扣分更多。真实游戏只读采集、未知时间处理、原生模拟器限制
及固定评分公式见 [小推车评价和操作权限](docs/mower-evaluation.md)。
