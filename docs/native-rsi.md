# pvz_rl 自动代码与策略闭环

2026-10-31。Emulator 路线已停止。本入口使用 yych00/pvz_rl 的原版游戏接口；不运行它的 DQN。

## 自动链路

真实对局确认胜负并关闭记录 → LiveReview 复盘完成 → （2026-11-03 起只在输了或用了小推车时）服务端持久队列 → Windows pvz_rl 基线对局 → Coach 根据开发集发现问题和提出目标 → 独立 Engineer 读源码、修改、检查、要求 Windows 测试、根据反馈修复 → 提交实际测过的代码候选 → 固定多局评测 → 合格后原子晋升 CURRENT。无需操作员替 Coach 总结打法或手写候选。

两个条件使用同一套 Qwen Planner / OneJev：state 条件读取当前阳光、卡片、植物、僵尸和合法动作；pixels 条件只给 Qwen 截图与公开选卡，再给 Planner/Jev 识别结果。Jev 始终不接收图片。未来出怪表、随机种子、实时血量和裁判分数不会进入玩家消息。

默认 `PVZ_NATIVE_EVOLUTION=code`，Engineer 可以编辑可变的玩家模块目录，并接受检查和精确版本测试；`strategy` 可选择旧的纯 planner.knowledge 候选模式。两种模式均不启动 Emulator。

Engineer 可在 `player/` 下新增、拆分、合并、重命名和删除 Python 模块，也可建立子目录；原有 perception、state、actions、policy、tools 五个文件只是初始布局，数量与职责都可改变。文件名使用小写标识符，路径不能越界。`write_file` 新增或替换文件，`delete_file` 删除候选文件，`edit_modules` 一次提交 writes/deletes 完成多文件重构。整个批次验证后才修改工作副本，不会删除历史候选与原始快照。

各模块共享受限命名空间，不使用 Python import；合并编译后必须恰好定义一次 `perceive(image)` 和 `decide(observation, memory)`，入口可放在任意模块。总源码仍限制24000字符，不再固定模块数。隔离进程真实执行这份代码，可计算观察融合、图像特征、跨帧状态、预算/威胁/距离以及模型上下文，并请求 Planner 刷新。输入是当前玩家观察、旧规划、已下发预算和截图的16×12 RGB摘要（state 条件没有像素）；输出是 Planner/Jev 的派生上下文和刷新请求。不是任意编辑宿主源码，也没有直接鼠标权限：只有 Jev 决定种植，原始观察、合法性、暂停、评分和晋升仍由宿主负责。具体函数协议见 rsi/native_player.py 的 CONTRACT。

隔离进程没有模型凭据，禁止导入、文件/网络访问，限制执行时间、内存和输出大小。候选用内容摘要保存，playbook.player_code 绑定代码版本；晋升原子切换整个 playbook，因此策略与代码不会分别切换。真实客户端下次连接加载同一份代码，并把计算结果送入 Planner/Jev；失败时不发种植命令。个人游戏仍不使用 native game state。

Engineer 每轮最多12个工具动作、2次开发集测试。test_candidate 不在服务器运行模拟器，而是落盘 Windows pvz_rl 任务；结果回传给 Engineer 后才允许继续修复或 finish。测试失败不能授权提交；修改文件内容或目录结构后都必须重测。可变目录、状态与模型对话可断线恢复；候选无法写测试结果、裁判、凭据或保留集。旧五文件 bundle 继续兼容，新目录布局通过 v2 bundle 保存，晋升清单记录新增和删除的模块。

## 固定评测

初始范围为普通白天 Adventure 1-2、1-3、1-4、1-6，不代表已支持或打通全部50关。

|阶段|输入|局数|
|---|---|---|
|开发基线|state / pixels，同一组种子|8 + 8|
|开发候选|state，与基线配对|8|
|新种子确认|state，基线 / 候选|12 + 12|
|独立截图审计|pixels，基线 / 候选|12 + 12|

固定晋升评测（门槛 `paired-v1`）最多72局；Portable 训练触发的针对关卡改进轮用 `targeted-v1`，6/6/6 共 42 局（见 portable-rsi.md），Engineer 另有最多2×8局开发测试，总计最多88局；前阶段不合格即停止。开发集和确认集要求平均分至少提升0.02；开发集至少60%配对改善，确认集单侧精确符号检验 p≤0.05；各阶段完整胜局不能减少，截图审计平均分不能下降。Coach/Engineer 只能看开发集和真实复盘，不看确认/审计原始成绩或种子。

评分：胜利基础1，失败基础0.2×波次进度，再减去每次小推车触发的 `0.1 + 0.1*exp(-t/60)`，最低0。越早触发扣分越多。服务端根据原始评分字段重新计算；超时、截断、模型失败、非法动作、随机种子未应用或小推车事件不完整均不能晋升。

每步推进25游戏tick（约0.25游戏秒），推理期间冻结游戏；两组都启用原生自动收阳光。这是受控决策质量实验，**不能据此判断真实客户端的推理延迟、实时操作能力或图像拾取效果**。截图由私有实例窗口采集；兼容游戏到位后仍须验证首帧和实际运行。

## 游戏时间调度与吞吐（2026-10-31，本地实现）

推进游戏与调用模型已分开：执行器每0.25游戏秒读取结果，Jev 默认每2游戏秒决策；state 条件下新增威胁、僵尸跨列前进、植物损失、卡片冷却/阳光使新卡可用，能在最短0.5游戏秒间隔后提前触发。无事件仍有周期心跳，不会无限等待。pixels 条件不使用隐藏 game state 触发，只按周期和已有规划有效期请求识图/决策。

Planner 默认目标间隔20游戏秒，playbook.timing.planner_seconds 的明确设置优先；规划自己的有效期到期会提前刷新。威胁/植物损失事件只唤醒 Jev 决策，不再直接触发重规划；提前重规划只由玩家代码 decide() 的 request_replan 发起（按方针的 replan_when 检查），桥接端保留待处理的请求，直到满足固定间隔（至少 8 游戏秒，danger_min_seconds 更大时取它）；方针有效期按不短于 15 秒处理。Jev 决策返回的具体种植只执行一次，之后的推进步使用空动作；代码不选择或重复种植，仍只有 Jev 授权操作。本阶段没有加入动作包。

本机 config.json 已设 workers=2，支持1–4。并行评测使用每局独立的 Python/DLL 游戏进程和私有存档；一条带请求ID的 SSH 通道承载并发决策，结果按ID匹配，Planner/预算/代码记忆按局隔离。队列变更与裁判操作等待在途决策完成，仍只有一个研究队列控制者。完成局单独保存，断线后保留已完成证据。初始调度参数或并发数改变后，不能混用同一任务的旧证据。

config.json 的 decision_schedule 支持 seconds、planner_seconds、event_min_seconds、events、selection。selection=flat 表示一次 HTTP 请求从全部合法“植物+行+列”和 wait 中选择；hierarchical 保留原来的两阶段选择作对照。更多选项可能增加模型服务内部工作量，所以一次HTTP不代表一定更快，需要测实际延迟。真实截图客户端暂时保持原选择模式。

Engineer 可通过 jev_context.runtime_schedule 返回同样的受限配置，调整后续原生评测调用周期、事件集合与选择模式。输入输出入口不变；参数越界导致本次评测失败，不能关闭周期心跳。结构/源码和调度改动均要重新测试精确代码版本。候选 Python 现在每局保持一个隔离进程，首次编译后复用；仍无凭据/文件/网络权限，保留每次调用超时、内存、输出和Linux CPU限制，重置/关闭会终止旧进程。

单局 throughput 保存决策次数、Jev HTTP请求数、触发原因，以及 RPC、Planner、Jev、System0、环境推进/重置的次数、总时长、p50/p95。阶段 report.json 另有实际本次新跑局数、恢复局数、batch_wall_s 与 measured_new_episodes_per_hour；恢复阶段的吞吐只计算本次新完成的局。开发集指标给 Coach/Engineer 查阅，速度统计不改变胜率/小推车等固定晋升门槛，也不会凭速度单独晋升。

离线测试证明调度、乱序RPC、并发记忆隔离、停止拦截和版本恢复的接线；2026-10-31 SSH已恢复，新RPC及代码自改版本已部署并重启，服务器166项测试通过、1项跳过，健康检查正常。仍缺1065游戏，未跑真实并发游戏，也没有实测每小时局数。

## Windows 入口

本机配置：`data/native_rsi/config.json`（忽略入Git），其中 `game_executable` 当前为 null。填入兼容的完整零售版1.2.0.1065目录中的 EXE，不能填 Steam/GOTY版。上游源码与 DLL 锁定 `d6ec92cba925218b655de90c56e850089ae349dc` / 0.1.1；已下载官方 DLL，核对 SHA256 和源版本清单。

从仓库运行：

```powershell
.\scripts\native_rsi.ps1 -Mode Check
.\scripts\native_rsi.ps1 -Mode Watch
```

Watch 消费自动队列，Run 消费已有任务直到本轮结束。`-Mode Run -Session <已关闭的Windows记录名>` 可手动排队一次；同一记录重复排队不会重复创建任务。旧任务因代码迁移过期后，可加 `-Retry` 为同一真实记录建立新种子任务，旧证据保留。缺游戏时写入 waiting_runtime 并退出，不启动模型或伪造对局。

`-Mode Pause / Resume / Stop` 控制研究执行器；暂停等待期间不推进游戏，推理中暂停会在返回后拦住下一次动作。Stop 终止本机研究执行器及其私有游戏，保留断点，STOP 标记不会被自动修复。明确要重新启动时，删除配置 work_dir 下的 STOP 文件再运行 Run/Watch。真实客户端原有四个按钮仍只控制真实客户端任务。

网络中断或失败会保留每局检查点，重新运行同一配置可从未完成的局继续。代码、模型配置、策略或原生运行库变化时不能混用旧证据；任务标为 stale 时需用新的真实复盘建立新任务。使用独立游戏副本和训练存档，仅控制自己创建的PID，关闭后清理自身进程，不接管个人游戏/存档。

## 服务端与查看结果

本次增量部署入口：`.\.venv\Scripts\python.exe scripts/deploy_native_rsi.py`，自动选择当前 Wi-Fi、先备份、跑服务器测试，仅在没有活动对局时重启 pvz；`--package-only` 只生成部署包。不会修改共享模型服务。网络恢复后先部署，再使用兼容游戏执行 Check/Run/Watch。

`PVZ_AUTO_NATIVE_RSI=true`（默认）使新完成的真实复盘自动排队。Windows 执行器通过现有 SSH 密钥及 Wi-Fi 优先连接到研究RPC；模型与凭据始终在服务器。没有后台 Windows 执行器时任务等待，不会自动改用 Emulator。

控制台“复盘”页显示 `/learning/latest` 的任务状态、基线、候选和阻塞原因。服务端 `data/native_rsi/<session>/` 保存任务、候选提案和各阶段报告；Windows 配置的 work_dir 保存进度和单局检查点。晋升只在下一次真实客户端连接生效，正在进行的对局固定原策略。

离线自动测试覆盖排队、Coach/Engineer 分工、Windows 测试回传、精确代码版本提交、子进程真实执行、权限/资源限制、真实会话加载、全阶段晋升、截图退化拒绝、错误证据拒绝、暂停、断点恢复与上游Gym接口。测试夹具明确不能作为线上成绩。当前缺兼容游戏，尚无本闭环真实对局成绩，也未证明 state 优于截图或 RSI 带来提升。2026-10-31 新代码已部署到 /srv/pvz-harness，native_evolution=code、automatic_native_rsi=true；已核对41个文件与更新包一致。备份：/srv/operator/pvz-backups/before-native-rsi-20261031-144626.tgz。

## 1.2.0.1065 获取线索

未找到官方直接提供这一精确旧构建的完整下载。当前 Steam 商店是 GOTY。速通社区提供 [Steam 1.2.0.1096 → 1.2.0.1065 的 xdelta 差分补丁](https://www.speedrun.com/pvz/resources/qwrly) 和 [1.2.0.1065 en 配套文件](https://www.speedrun.com/pvz/resources/5lam4)。这是社区维护渠道，不是官方重新发行；应使用自己已有游戏的独立副本，按说明核对源版本，保留个人游戏和存档。补丁结果能否满足 pvz_rl 的 PE 布局与签名仍须实际校验，不能只看显示的版本号。当前没有下载或应用这些游戏补丁。

2026-10-31 已确认本机 Steam EXE 的 SHA256 与记录一致，已下载官方 xdelta 3.2.2 并核对 GitHub 发布资产 SHA256。准备脚本为 `scripts/prepare_pvz1065.py`，从仓库执行 `.\.venv\Scripts\python.exe -X utf8 scripts/prepare_pvz1065.py`。它先下载补丁和资源、检查压缩包路径，在 external/ 下建立独立游戏副本，通过 pvz_rl preflight 后才更新本地 game_executable，不启动游戏或修改 Steam 安装。

本次仍卡在下载：Speedrun 的 TLS 连接被重置，命令行和浏览器都失败；已有服务器 SSH 也不可用。补丁与资源未下载成功，未生成1065游戏，训练配置仍为 null。恢复网络后重跑脚本即可；也可手动把上述两个下载分别保存为 `external/pvz-1065-setup/steam-to-1065.xdelta`、`accompanying-1065.zip`，脚本会复用它们。安装和运行兼容性仍待真实文件验证。

