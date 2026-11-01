# pvz-agent 项目文档

按 2026-11-01 工作区代码整理（提交 54b61f7、81f1730 及之后）。
第二部分与腾讯「绝悟」（王者荣耀 AI）对比，列出本项目的不足。

## 0. 一句话与现状

用通用大模型玩《植物大战僵尸》（PvZ GOTY 1.2.0.1096，Steam）：Windows 游戏机只负责截图和点击，
远程服务器上的 Qwen 看图、Planner 定方针、OneJev（Jev）选具体种什么种在哪；
赛后由 Coach / Engineer 两个模型角色改策略文本或「System0」代码，经固定多局评测通过才晋升。

截至本文：

- 实时链路能完整运行（截图 → 识图 → 规划 → Jev → 点击），但没有记录到模型赢下一局真实关卡。
- 四条训练/研究环境都没有跑完一轮自主学习：伪阳光游戏只调过两个阈值；Emulator 路线已停；
  pvz_rl 缺 1.2.0.1065 游戏；Portable 在第一次 Jev 调用就因端口拒绝连接中断。
- 唯一一次晋升（Emulator，0/12 → 1/12 胜）来自外部 Codex 写的候选，不是 Qwen 自主学到的。

## 1. 总体架构

```
 Windows 游戏机                         远程服务器 /srv/pvz-harness（tmux: pvz）
 ┌─────────────────────────┐   SSH 隧道   ┌────────────────────────────────────────────┐
 │ client/windows_client   │ 127.0.0.1:  │ server/app.py  FastAPI  /ws  /health ...   │
 │  mss 截图 5fps → JPEG    │ ───8765───▶ │ server/session.py  每个连接一组异步循环     │
 │  执行点击（SetCursorPos） │ ◀────────── │   识图 ×3 / Planner / Jev / 收阳光          │
 │  只读内存：小推车/时钟/胜负 │  WebSocket  │ server/judge.py  只有它看真实状态          │
 │  Tk 控制台 4 按钮         │             │ rsi/  复盘、Coach、Engineer、评测、晋升     │
 └─────────────────────────┘             └───────────────┬────────────────────────────┘
                                                         │ 本机 HTTP（共享模型池，别的团队维护）
                                     Qwen3.8-27B :18201（看图/规划/教练）  OneJev-27B :18220（选动作）
```

设计原则（代码里有测试守着）：

- **信息隔离**：内存读到的真实状态只走 `StateMsg → Judge/Coach`，玩家侧（perception / scene / planner / Jev）
  不能 import judge（`tests/test_isolation.py`）。
- **操作权限**：只有一次成功的 Jev 决策能授权种植点击；收阳光由代码独立完成；Planner 只给建议，不能点击、暂停。
- **固定规则不可改**：`rules/` 下的硬规则、playbook 字段范围、晋升门槛，Coach 都改不了。
- **一切可追溯**：playbook 版本不可变，`data/ledger.jsonl` 记录谁改了什么；每次点击日志带 decision_id。

## 2. 目录

| 路径 | 作用 |
|---|---|
| `common/protocol.py` | 线协议：二进制帧（4 字节头长 + JSON 头 + JPEG）与文本消息（Hello / StateMsg / ExecReport / EpisodeEvent / Command / ModelTiming） |
| `client/windows_client.py` | 真实客户端：窗口查找、截图、点击、只读状态、控制台生命周期 |
| `client/menu.py` | 用 `docs/screens/` 参考图识别菜单/暂停/结束画面，只读，不点菜单 |
| `client/mower_state.py` | 只读内存：小推车状态、游戏时钟、胜负（仅英文 GOTY 1.2.0.1096） |
| `client/control_view.py`、`operator_view.py`、`model_activity.py` | Tk 控制台：指标、五路模型状态、规划/复盘/策略/图鉴页签 |
| `client/mock_client.py`、`fake_lawn.py` | 无需 Windows 的假游戏：伪阳光、简化草坪（6 种僵尸、小推车、胜负） |
| `server/session.py` | 一个连接的全部循环与执行检查（核心，655 行） |
| `server/scene.py` | 识图：代码读卡片亮度/草坪类型/僵尸位置，Qwen 读阳光、僵尸种类、植物矩阵（格子拼图） |
| `server/perception.py`、`policy.py`、`sun_collector.py` | 阳光 HSV 检测、点击编译、两帧确认的自动收阳光 |
| `server/planner.py` | Planner（Qwen / Claude / mock），输出 `Plan` 方针，禁止坐标和卡槽 |
| `server/action_selector.py` | Jev：枚举合法动作，让 OneJev 选「植物+行 → 列」或一次选全动作，或等待 |
| `server/decider.py` | 旧规则战术，只作 `PVZ_DECIDER=rules` 对照基线 |
| `server/threats.py`、`mechanics.py` | 逐行威胁事实（不打分）、静态图鉴参数 |
| `server/judge.py` | 评分：小推车时间惩罚、胜负 |
| `server/runlog.py`、`live_reviews.py`、`decision_view.py` | 日志、赛后复盘队列、决策查看 |
| `rules/hard_rules.py` | 每次种植的硬检查 |
| `rules/playbook_rules.py` | playbook 可含哪些字段、取值范围 |
| `rules/code_gate.py`、`rsi/native_gate.py` | 固定晋升门槛与评分公式 |
| `rsi/` | 学习与评测：参数 RSI、复盘、Coach/Engineer、受限代码运行时、pvz_rl / Portable 执行器 |
| `sim/` | PvZ-Emulator 宿主与程序化渲染（已停用） |
| `knowledge/` | 人工整理的策略文字、图鉴 JSON |
| `scripts/` | 部署、连接、环境验证、安装模拟器、启动学习 |
| `tests/` | 37 个测试文件 |

## 3. 实时对局链路

### 3.1 客户端（Windows）

- 找 `MainWindow` 类窗口，要求窗口化、客户区 800×600（175% DPI 下需「高 DPI 由应用程序处理」）。
- 默认 5 帧/秒上传 JPEG；只在游戏前台、画面可玩时连接，失焦/菜单时等待，不代点菜单，不挂起进程。
- 执行点击前检查坐标在窗口内且该点属于游戏窗口（防点到别的窗口）；`SetCursorPos` + 30 ms 再按下。
- 以 20 Hz 只读内存采集小推车、时钟、胜负，作为 `StateMsg` 只给裁判。
- 控制台四个按钮 开始 / 暂停 / 继续 / 结束 只控制客户端；命名互斥量防多开。

### 3.2 服务器会话的循环

`Session` 为每个连接起这些并发循环（周期可由 playbook.timing 覆盖，当前 v009）：

| 循环 | 周期 | 做什么 |
|---|---|---|
| 资源识图 look | 4 s | 代码读卡片可用性和草坪类型；Qwen 读阳光数 |
| 僵尸识图 zombies | 4 s | 白天草坪代码量僵尸行列，Qwen 读种类，再合并 |
| 植物识图 plants | 4 s | 81 格拼图交给 Qwen，返回 行×9 的植物编号矩阵 |
| Planner plan | 6 s 上限，按有效期提前刷新；危险事件可提前（间隔 ≥4 s） | 输出 `Plan` |
| Jev fast | 1 s | 枚举合法动作 → OneJev 选择 → 执行 |
| 收阳光 sun | 0.25 s | 两帧确认的黄色斑点，每批最多 2 次点击 |

同一路调用串行，不排队补发。识图过期（>2 个周期或 5 s）、Planner 指令过期、暂停时 Jev 不会种植。

### 3.3 识图（`server/scene.py`）

能精确测量的交给代码，语义交给 Qwen：

- 代码：卡片亮度判断是否可用；草坪类型（决定 5/6 行）；白天草坪上用灰度掩码 + 连通域量出僵尸所在行和最前列。
- Qwen：阳光数字、僵尸种类（按行配对到代码量出的位置）、植物布局。
- 植物布局用「格子拼图」：每格放大 1.5 倍并标注「行,列」，要求返回编号矩阵。12 帧 107 株的离线评测：
  拼图矩阵 107/107 找到、0 虚报、约 2.5 s；整图列表只有 85 株、19 个虚报。

### 3.4 Planner（`server/planner.py`）

输入：合并后的局面文字（可选附截图）、植物布局、上一条方针（标注为历史）、已下发花费、各路帧号、
Jev 被阻原因、逐行威胁事实、重规划原因、System0 派生上下文、playbook 的 `planner.knowledge`。

输出 `Plan`（pydantic 严格校验）：`objective`、`priorities ⊂ {defense, economy, reinforce, save_sun}`、
`priority_rows / blocked_rows`、`sun_reserve`、`spend_limit`、`budget_policy`、`tactics`、`threat_assessment`、
`valid_for_seconds`（5–120）。模式拒绝任何卡槽和坐标，也就是 Planner 不能直接下种植指令。

### 3.5 Jev（`server/action_selector.py`）

1. `candidates()` 枚举所有 卡槽 × 行 × 列，经 `DecisionContext.reject()` 过滤（硬规则 + 布局新鲜 + 阳光/费用已知）。
2. 分层模式：先在「wait + 每个（植物, 行）」里选，再在该组的列里选（含 wait）；flat 模式一次从全部动作里选。
3. 给 Jev 的 state 是一段 JSON 文字：方针、预算建议、策略知识、System0 上下文、当前可用阳光、逐行威胁事实、局面、图鉴参数。
   Jev 永远不收图片。
4. 两次请求共用一个总超时 `PVZ_DECIDER_TIMEOUT`（默认 1 s）。出错、超时、格式不对一律等待，不偷偷换规则兜底。
5. 返回后会话再查一遍：上下文没变、方针没换、动作仍合法，才在操作锁里连续发「点卡 + 点格」。

### 3.6 硬规则与执行检查

`rules/hard_rules.check`：格子在界内、卡槽存在、格子空（睡莲/花盆可叠）、卡片亮、上次识图后没用过这张卡、
阳光够。2026-10-30 起土豆雷提前量、一次性植物射程等战术限制已从硬规则中移除，交给模型判断。

### 3.7 System0（playbook.player_code）

Engineer 写的受限 Python（`rsi/code_runtime.py` 自定义子集：无 import、无属性访问、无 while、24000 字符、
64 KiB 记忆、16 KiB 输出，隔离进程 + 超时）。入口 `perceive(image)`、`decide(observation, memory)`，
返回 `planner_context`、`jev_context`、`request_replan`。它只能给模型补充计算出的上下文，不能直接点击。
真实客户端在连接时加载 playbook 绑定的代码版本；当前真实 CURRENT 没有绑定代码。

### 3.8 裁判与评分

- 真实游戏：`mower-time-v1`，每次小推车触发扣 `0.1 + 0.1·exp(-t/60)`（t 为游戏秒）；胜负来自内存场景值。
  真实游戏还没有完整总分（`score` 为 null）。
- 研究环境：`native-win-progress-mowers-v1`，胜 = 1，负 = 0.2 × 波次进度，再减小推车惩罚，最低 0。

### 3.9 日志与控制台

`runs/<session>/events.jsonl` 记录每次规划、Jev 打分/选择/延迟/拒绝原因、收阳光、模型状态；
普通截图每 4 s 存一张，危险和终局另存，总量有上限（服务器容器写满 160 GiB 会被驱逐）。
`/playbook`、`/reviews/*`、`/learning/latest`、`/decisions/latest`、`/mechanics` 均需 Bearer 令牌。

## 4. 学习与改进

项目里的「学习」都不改模型权重，而是改 **playbook**（参数、策略文字、调用周期）或 **System0 代码**，
再用固定门槛判断是否采用。先后有五条环境/路线：

| 路线 | 环境 | 能改什么 | 状态 |
|---|---|---|---|
| 参数 RSI `rsi/loop.py` | 伪阳光游戏（mock_client） | perception/fast 等字段，一次一个改动 | 跑过：v001→v003，留出集 0.153→0.955（只是收阳光） |
| 赛后复盘 `rsi/review_live.py` | 真实对局日志 | 只写诊断和改进目标，不改策略 | 可用，胜负确认后自动触发 |
| 代码进化 `rsi/code_loop.py` | PvZ-Emulator + 程序化渲染 | 玩家模块（感知+决策）代码 | 已停用；唯一晋升来自外部 Codex 候选 |
| pvz_rl `rsi/native_*` | yych00/pvz_rl 原版游戏接口 | System0 代码目录（可增删模块） | 接线完成并部署；缺 1.2.0.1065 游戏，未跑过 |
| Portable `rsi/portable_*` | PvZ-Portable（用 Steam 资源编译的开源重写），1-1～1-10 | 同上 | 1-1 首次 Jev 调用 ConnectError，未完成任何一局 |

### 4.1 自动闭环（pvz_rl / Portable 共用）

```
真实对局结束 → LiveReview 复盘 → 服务器持久队列 → Windows 执行器跑基线
 → Coach 读开发集，提出一个有证据的任务（不写代码）
 → Engineer 用工具读源码/改 player/ 模块/检查/请求 Windows 测试/修复（≤12 个动作、≤2 次开发测试）
 → 只能提交「最后一次测过」的精确版本
 → 固定评测：开发 8 对（≥60% 场景变好，均分 +0.02）
            → 新种子确认 12 对（均分 +0.02，单侧符号检验 p≤0.05）
            → 截图条件审计 12 对（不退步）
            → 任一阶段胜局数不能减少；全过才原子切换 CURRENT
```

一轮最多 88 局（72 局评测 + 16 局开发测试）。研究执行器冻结游戏时钟：每 0.25 游戏秒推进一步，
Jev 默认每 2 游戏秒、Planner 每 20 游戏秒，事件可提前触发；workers 默认 2（1–4）。

### 4.2 Playbook

`data/playbooks/vNNN.json` 不可变，`data/CURRENT` 指当前版本（真实客户端 v009），研究环境各有独立 CURRENT。
字段：`perception`、`fast`、`jev`（仅规则基线用）、`planner.knowledge`、`timing`、`notes`、`player_code`。
人工修改走 `python -m rsi.human_edit --set section.field=JSON --note ...`，同样校验并记入账本。

## 5. 部署与运维

- 部署：`powershell -File scripts\deploy.ps1 -Test`（打包 → scp → 服务器替换代码保留 .env/.venv/runs/logs/data → uv sync → pytest → 重启 tmux pvz → 健康检查）。
  另有 `deploy_native_rsi.py`、`deploy_portable.py` 做增量部署并先备份到 `/srv/operator/pvz-backups/`。
- 连接：`scripts/connect_server.ps1`（自动重连隧道，优先 Wi-Fi 网卡）。
- 规矩：只动 `/srv/operator/`；保留 `flide-patch`；模型只在服务器上调用；不给 OneJev 发图；内存只读；令牌不进文件。

## 6. 测试

37 个测试文件，覆盖协议、识图夹具、硬规则、Jev 请求解析、会话时序、隔离、控制台生命周期、
受限运行时、晋升门槛、pvz_rl/Portable 接线等。2026-10-31 记录：本地 321 项通过 1 项跳过；服务器 166 项通过。
测试证明接线正确，不证明打得好。

## 7. 已知问题（代码层面）

- 2026-10-29 到 11-01 的约 100 个文件改动曾长期未提交，11-01 才补成三个提交，且还没推到 GitHub；中间状态无法按提交回溯。
- `Settings` 数据类默认值与 `load_settings` 不一致：`auto_native_rsi` False/True、`native_evolution` "strategy"/"code"；
  `decider` 默认是 "rules"，线上靠 .env 设 onejev。
- `_execute_placement` 用 `PLANT_COSTS[card]` 记花费，而合法性检查用 `card_cost(card)`（传送带关卡费用为 0 时两者不一致，未知植物会 KeyError）。
- `_load_dotenv` 读 `.env` 未指定编码（Windows 上 GBK 问题曾出现过）。
- 依赖共享基础设施：单一 SSH 端口、别人维护的模型池；10-29 SSH 新连接超时、10-31 Jev 端口拒绝，都直接让工作停摆。

---

# 第二部分：与「绝悟」对比

## A. 绝悟怎么做（公开论文要点）

腾讯 AI Lab 的王者荣耀 AI。1v1：*Mastering Complex Control in MOBA Games with Deep Reinforcement Learning*（AAAI 2020）；
5v5：*Towards Playing Full MOBA Games with Deep Reinforcement Learning*（NeurIPS 2020）。

- **输入**：直接取游戏引擎的结构化状态（单位属性向量、类图像的小地图特征、全局信息），不看屏幕像素。
- **网络**：专门为这个游戏设计和训练的策略-价值网络，LSTM 记时序，目标注意力选攻击对象。
- **动作**：分层多头（先选按钮，再选方向/目标），用游戏知识做动作掩码剪掉非法动作；1v1 约 133 ms 决策一次。
- **算法**：双裁剪 PPO；采样（大量 CPU 容器里跑加速的游戏核心自对弈）与训练（GPU）解耦，论文规模为数十万 CPU 核、上千块 GPU。
- **奖励**：每一步都有的稠密多项奖励（经济、经验、血量、塔、击杀/死亡等）加最终胜负。
- **课程与蒸馏**：5v5 先训练固定阵容的老师，再蒸馏到一个学生；用 MCTS + 价值网络学选英雄（BP）。
- **评测**：与职业选手和大量公开对局对战。早期版本还用人类对局录像做监督学习。

## B. 对照表

| 维度 | 绝悟 | 本项目 |
|---|---|---|
| 状态从哪来 | 引擎结构化状态，每帧精确 | 实战：Qwen 每 4 s 读截图，单次约 2.5 s；研究：pvz_rl/Portable 给状态或截图 |
| 决策者 | 为该游戏训练的专用网络 | 通用 27B 模型（OneJev）在文字选项里挑，未针对 PvZ 训练 |
| 决策频率 | 约 133 ms | 实战 Jev 1 s、Planner 6 s；研究 2 游戏秒 / 20 游戏秒 |
| 改进方式 | 梯度更新，每个 batch 都在变 | Coach/Engineer 一轮提一个改动，88 局判一次 |
| 反馈信号 | 每步稠密奖励 + 价值函数 | 每局一个分数，靠 Coach 读日志归因 |
| 数据规模 | 每天相当于数百年游戏时长 | 一轮 88 局，且至今一轮未跑完 |
| 训练与实战环境 | 同一个游戏核心、同一份特征 | 研究冻结时钟+状态输入，实战实时+截图识图，文档自己承认不能直接迁移 |
| 动作空间 | 完整操作，含选英雄 | 只有「种一株」或等待；不选卡（卡组写死在配置）、不用铲子、菜单人工点 |
| 课程 | 固定阵容 → 全阵容，蒸馏 | 关卡顺序闯关，首败即停；无蒸馏 |
| 示范数据 | 早期用人类录像监督学习 | 有对局日志，但没有用来训练任何东西 |
| 基础设施 | 自有集群 | 共享服务器 + 别人的模型池 + 单一 SSH 端口 |

## C. 不好的地方（按影响排序）

1. **没有真正会学习的部分。** 模型权重从不更新。所谓学习，是让一个大模型读日志后改策略文字、参数或一段受限代码，
   每轮只验证一个假设。绝悟每个训练步都在改进策略，本项目一轮几十局才得到一个「采用 / 不采用」。

2. **反馈太稀疏。** 评分只在一局结束时给出（胜 1，负 0.2×进度，小推车扣分），没有每步奖励，也没有价值函数。
   哪一步种错了只能靠 Coach 读日志去猜。绝悟每一步都有奖励信号。

3. **吞吐被模型调用卡死。** 引擎本身很快：Portable 一局 189 游戏秒的空跑只用 0.063 s（约 3000 倍速）。
   但研究里每 2 游戏秒要调一次 Jev（实测 0.2–0.9 s），每 20 游戏秒调一次 Planner（实测 2–12 s），
   一局 200 游戏秒约 100 次 Jev + 10 次 Planner，估计每路 1–3.5 分钟。两路并行，一轮 88 局约要 0.7–2.6 小时，
   还没算 Coach/Engineer。而且模型池是共享的，代码里明确要求「keep traffic low」，无法像绝悟那样铺开。

4. **闭环从来没跑通过。** 五条路线没有一条完成过自主学习轮次（见第 4 节表）。时间主要花在搭新环境和接线上，
   没有积累出可比较的学习曲线。

5. **训练环境与实战环境不一致。** 研究用冻结时钟、可选给状态、原生自动收阳光；实战是实时、截图识图、识图有 2–4 s 延迟。
   研究里晋升的东西不能直接说明实战会变好，文档里也这样写了。绝悟训练和上线用的是同一套引擎接口。

6. **感知是实战的主要瓶颈和误差源。** 局面全靠 Qwen 每 4 s 读一次截图，读一次约 2.5 s；
   识图一过期 Jev 就停手。绝悟拿到的是每帧精确状态。代码颜色直方图 kNN 读植物已测到 100/107、约 6 ms，但还没用上。

7. **决策模型不是为这个游戏训练的。** Jev 是通用模型在最多几十个文字选项里打分，1 s 超时，失败就等待；
   没有时序记忆（System0 的记忆是唯一补充），没有价值估计。绝悟的网络就是为这个任务训练的，带动作掩码和 LSTM。

8. **动作空间不完整。** 不选卡（`PVZ_LOADOUT` 写死 6 张）、不用铲子、菜单和下一关要人工点、一次决策只种一株。
   绝悟连选英雄都是学出来的。

9. **评测门槛统计功效低、覆盖窄。** 确认阶段 12 对样本、单侧符号检验 p≤0.05：没有平局时至少 10 对要变好才通过，
   小幅但真实的改进大多会被拒。关卡只覆盖白天前几关。绝悟有对人类和大量对局的评测。

10. **没有利用示范和历史数据。** 每局日志里有完整的观察和决策，但没有用来做模仿学习、离线强化学习或蒸馏。

11. **太依赖别人的基础设施。** 单一 SSH 端口、共享模型池；10-29 SSH 新连接超时、10-31 Jev 端口拒绝、
    1065 游戏补丁下载被重置，都直接让进度停住。

12. **工程管理分散。** 五条路线并存，约 19000 行代码，改动曾三天不提交，文档有几处状态互相矛盾（见第 7 节）。
    绝悟是一套统一的训练基础设施。

公平地说，有些差异是有意的取舍：项目目标是测「通用大模型 + 截图」能不能玩，并且强调可审计、可回滚、信息隔离，
算力只有共享的推理服务；PvZ 是单人对固定出怪，不需要自对弈。但如果目标是「打得好、学得快」，上面 1–7 条是主要差距。

## D. 可借鉴的改进方向

1. **先让一个环境真正跑完一轮。** Portable 已能初始化 1-1～1-10 并截图，修好 Jev 端口就能跑；先拿到第一条学习曲线。
2. **加一个可训练的小策略网络。** pvz_rl / Portable 已有 Gym 式接口和状态。用 PPO/DQN 在状态上训练专用小网络，
   动作掩码直接用现有 `candidates()`，奖励用稠密项（僵尸推进、植物损失、击杀、阳光）。大模型改做老师和教练：
   用 Jev 的选择做模仿学习预热，再像绝悟一样蒸馏。
3. **稠密奖励和逐步归因。** 用研究环境的状态算每步奖励，Coach 读到的不只是一局总分。
4. **减少模型调用。** 研究里事件触发为主、心跳拉长；实战把植物/僵尸识图换成代码分类器（6 ms），Qwen 只读难的部分。
5. **让训练和实战用同一条感知链。** 研究的截图条件走和实战一样的代码识图，迁移才有依据。
6. **补全动作**：选卡、铲子；先在研究环境里加，再接实战。
7. **调整门槛的统计设计**：增加确认样本，或用均值差的置信区间，而不是只看符号。
8. **工程**：及时提交和推送；收敛到一条主路线，停掉的路线归档。
