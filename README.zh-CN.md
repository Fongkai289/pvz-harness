# pvz-harness

[English](README.md) | 简体中文

[从零复现（English）](docs/reproduce.md) · [自备游戏素材](docs/assets.md) · [MIT 许可证](LICENSE) · [第三方许可](THIRD_PARTY_NOTICES.md)

用通用大模型看截图玩《植物大战僵尸》（PC 版 GOTY 1.2.0.1096），不训练专用的游戏 AI；
每局之后通过修改自己的策略和玩家代码变强，不改模型权重。

Windows 客户端上传截图、执行点击；识图、规划、动作选择与裁判在配置的 Linux 服务器运行。
可选的 Codex Coach 在 Windows 调用，研究评测协调两端。

**现状（2026-11-04）。** 实时链路能完整打真实对局，包括选卡和收阳光；铲子已接入玩家代码，但还没被用上。
Portable 训练环境里已不用小推车通过 1-2 到 1-7；实战赢过 1-7 和 1-8。
还不能说已经能打通冒险模式。结果和局限见[技术报告](docs/REPORT.zh-CN.md)。

## 工作方式

```
 Windows 游戏机                            GPU 服务器（FastAPI，127.0.0.1:8765）
┌──────────────────────────┐   SSH 隧道   ┌──────────────────────────────────────────────┐
│ client/windows_client     │  WebSocket  │ server/session.py：每局一组异步循环          │
│  截图（JPEG）     ─────────┼────────────▶│  识图 ×3（Qwen）─▶ Planner（Qwen）            │
│  点击         ◀───────────┼─────────────│  System0 玩家代码 ─▶ Jev（OneJev）─▶ 点击    │
│  只读游戏内存：            │             │  收阳光（代码）                               │
│  胜负、小推车、时钟        │             │  裁判（唯一能看真实状态的模块）               │
│ 控制中心（Edge 应用窗口）  │             │ rsi/：赛后 Coach、训练、晋升                  │
└──────────────────────────┘             └──────────────────────────────────────────────┘
```

| 角色 | 做什么 | 模型 / 代码 |
|---|---|---|
| 识图 | 每帧三路独立识别：阳光与卡片、僵尸、植物布局 | Qwen3.8-27B；卡片、草坪类型、铲子和僵尸位置由代码识别 |
| Planner | 给方针：目标、重点行、预算建议、威胁判断；约每 25 秒一次，有情况时提前 | Qwen3.8-27B |
| System0 | 有版本的玩家代码：整理局面、给 Jev 列出可选决策、把选中的决策落成动作（种植、铲除）、反射动作 | Python，由 Coach 修改 |
| Jev | 从决策菜单里选一项或等待；只看文字 | AutoTrust-JEV-27B（OneJev） |
| 收阳光 | 两帧都确认的阳光才点 | 代码 |
| 选卡 | 按预告的僵尸挑卡组 | Qwen |
| Coach | 复盘用了小推车的对局；在训练里改 playbook 和玩家代码 | Codex（gpt-6-astra）或 Qwen |
| 裁判 | 按只读的游戏状态给胜负和小推车时间打分 | 代码 |

## 设计约束

以下几条都由代码和测试强制，不靠约定。

- **信息隔离。** 真实游戏状态（胜负、小推车、时钟）只给裁判、Coach 和识图标注；
  玩家侧模块不能导入裁判（`tests/test_isolation.py`）。
- **操作权限。** 只有被接受的 Jev 决策（或经过闸门的 System0 代码）能种植或铲除；
  Planner 只给建议、不能点击。每次点击都记录决策编号。
- **固定规则。** 硬规则（`rules/hard_rules.py`）、playbook 取值范围和晋升门槛，Coach 都改不了。
- **版本可追溯。** playbook 和玩家代码的每个版本都不可变，每次改动都有记录。
- **共享模型池。** 实时对局的模型调用统一经过服务器；可选的 Windows Codex Coach 使用独立工作进程。Jev 只接收文本。

## 目录

本机工作区由 `app/` 主仓库和并列的 `external/` 依赖目录组成，详见[工作区目录与入口](docs/workspace-layout.md)。

| 路径 | 内容 |
|---|---|
| `client/` | Windows 客户端：截图、点击、只读内存、菜单识别、控制中心窗口 |
| `server/` | 实时会话、识图、Planner、Jev、动作闸门、选卡、铲子、复盘、连接测试 |
| `rules/` | 固定的硬规则、playbook 结构、重规划上下限、晋升门槛 |
| `rsi/` | 学习：赛后复盘、Coach、玩家代码运行时、Portable / pvz_rl 训练执行器 |
| `common/` | 客户端和服务器共用的通信协议 |
| `knowledge/` | 策略笔记、静态图鉴、卡片和铲子模板 |
| `scripts/` | 启动、部署、控制中心服务、训练工具 |
| `tests/` | 自动化测试；CI 跑 Linux 离线测试，缺少 Windows/引擎/私有素材的测试明确跳过 |
| `docs/` | 架构、项目现状、运维（见下） |

## 上手

### 服务器

部署目录与数据归档位置见[服务器目录与维护](docs/server-layout.md)。

1. 把 `.env.example` 复制成 `.env` 并填好（`PVZ_TOKEN`、模型地址）。`.env` 不会被同步或提交。
2. 在 tmux 里用自动重启脚本运行服务器：`scripts/run_server.sh`（监听 `127.0.0.1:8765`，经 SSH 隧道访问）。
3. 在 Windows 上用 `scripts/deploy.ps1 -Test` 部署：同步、跑测试、重启。

### 游戏机（Windows）

1. 游戏可以先开（窗口模式），也可以后开：客户端不开游戏也能启动，游戏打开后会自动接上。
2. 双击 `scripts/start_client.cmd`，或在控制中心点 **启动客户端**。启动脚本会打开 SSH 隧道、
   从服务器读取令牌（只留在内存里），再启动客户端。
3. 在控制中心窗口操作（`http://127.0.0.1:8766/client`）：
   - **开局前准备：** *测试连接* 测隧道往返、上传带宽、Qwen 和 OneJev 的耗时。
   - **客户端控制：** *开始 / 暂停 / 继续 / 结束* 只控制客户端，游戏菜单由你手动操作。
   - **游戏进度：** *选植物* 在选卡界面把卡放进卡槽，“一起摇滚吧”由你来点。
   - **赛后复盘：** 最近几局的复盘和每局的胜负。
4. 使用可选的 Codex Coach 时，先启动其后台程序：`scripts/codex_coach.ps1 -Mode Start`。

### 训练

在控制中心的训练概览页（`http://127.0.0.1:8766/`）开始或停止训练。训练在 Portable 引擎上打
1-2 到 1-10；模型思考时游戏暂停，所以模型延迟不影响对局。环境准备见
[docs/portable-rsi.md](docs/portable-rsi.md)。

### 测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests
```

## 学习闭环

1. 实战打完、用了小推车的局（输了一定用过）由 Coach 复盘：诊断、证据、一个可度量的改进目标。
   胜利且没用小推车的局不复盘。
2. 训练轮次里 Coach 修改 playbook（策略笔记、条件提示、调用周期、各项设置）和 System0 玩家代码，
   候选版本在固定种子上和当前版本对比评测。
3. 只有通过固定门槛的候选才会晋升。实战和训练共用晋升后的版本。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/REPORT.zh-CN.md](docs/REPORT.zh-CN.md) | 技术报告（初稿）：结果、发现与局限 |
| [docs/PROJECT.md](docs/PROJECT.md) | 项目总览、现状、与绝悟的对比 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 架构、数据流、权限 |
| [docs/HANDOFF_windows.md](docs/HANDOFF_windows.md) | Windows 端开发与运维 |
| [docs/portable-rsi.md](docs/portable-rsi.md) | Portable 训练环境 |
| [docs/native-rsi.md](docs/native-rsi.md) | pvz_rl 训练环境（旧） |
| [docs/codex-coach.md](docs/codex-coach.md) | Codex Coach 后台程序 |
| [docs/sun-collection.md](docs/sun-collection.md) | 自动收阳光 |
| [docs/game-mechanics.md](docs/game-mechanics.md) | 静态游戏图鉴 |
| [docs/mower-evaluation.md](docs/mower-evaluation.md) | 小推车评分与只读状态 |
| [docs/IMPROVEMENTS.md](docs/IMPROVEMENTS.md) | 改进记录 |
