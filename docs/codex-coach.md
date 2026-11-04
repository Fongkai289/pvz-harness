# Codex Astra Coach

Coach 可通过 Windows 上已登录的 Codex app-server 调用 `gpt-6-astra`。
实现使用官方 app-server JSON-RPC（SDK 的底层接口），不依赖聊天窗口，也不复制登录凭据到服务器。

Linux 的 `request_role` 把 `coach` / `live_coach` 请求写入私有持久队列。
Windows 的 `rsi.codex_coach` 通过 SSH 领取，按研究轮次创建独立 Codex 会话；后续调用只补充工具反馈。
模型输出一个既有 harness 工具动作，仍由服务器 `NativeCoachWorkspace` 验证和执行。
因此读证据、查对局、改 player 模块和 playbook、申请 Windows 开发集测试、再修复和提交的循环保持不变。
Jev 权限、评分标准、独立确认/截图审计与精确候选版本晋升仍由原有代码控制。
本机禁用 Coach 的原生 shell、应用、浏览器等工具，使用只读沙箱；不授予模型直接发布或控制游戏的入口。

## 启动

在项目根目录运行：

```powershell
.\scripts\codex_coach.ps1 -Mode Start
.\scripts\codex_coach.ps1 -Mode Open
```

Start/Resume 启动或恢复后台进程，不打开游戏；Pause 停止领取新任务并取消当前调用；Stop 结束 worker。
启动时检查 PID 和命令行，进程内另有系统文件锁防止重复 worker。Windows 需保持运行和 Codex 登录有效。
没有设置开机自启。`-Mode Status` 读取最近一次用量快照，`-Mode Open` 打开每 5 秒刷新的用量页。

后台进程优先使用 `PVZ_CODEX_EXECUTABLE` 显式路径，其次 PATH 中的 Codex，最后查找
`%LOCALAPPDATA%/OpenAI/Codex/bin/*/codex.exe` 中最新的可用程序，兼容启动后未更新的 PATH。
启动/初始化失败会立即把具体错误回报给队列；日志中的 `Coach app-server ready` 表示初始化成功。
每轮的会话 ID 和回复缓存保存在 `data/codex_coach_worker/sessions/<轮次哈希>/`；
`session.json` 的 `thread_id` 与 token 明细中的 `thread_id` 指向同一个独立 Codex 会话。
每次调用在回报队列前关闭自己的 app-server 进程，释放会话写入锁；下一次调用可由另一并行
worker 恢复同一会话，沿用历史和累计 token，不会争用前一个进程持有的写入权限。

服务器 `.env`：

```dotenv
PVZ_COACH_PROVIDER=codex
PVZ_COACH_MODEL=gpt-6-astra
PVZ_COACH_TIMEOUT=240
```

队列默认位于项目 `data/codex_coach`；Portable 的独立 data_dir 仍共用这个通信队列，但按 controls 路径分开轮次。
Planner、Qwen 识图和 Jev 仍在原服务器调用。没有静默回退：Codex 不可用会明确失败，不能晋升。
调用方超时、PAUSE/STOP 或取消会使队列请求取消；迟到回复只保留用量，不执行候选。
相同证据/消息/轮次的请求有固定 ID，已完成回复和失败均不自动重试收费；修复后以新的研究轮次显式重试。
已领取的任务不会被其他 worker 重复领取。worker 崩溃且没有持久回复时，任务最终超时；不会冒险重复调用。

## 每轮 token

- Windows：`data/codex_coach_worker/tokens.html` 为总览，`tokens.json` 含每次调用明细。
- 服务器：每个研究/复盘 controls 目录的 `coach_tokens.json`，以及队列请求中的 metadata。
- 控制台：复盘区域显示最近六轮用量，认证接口 `/coach/usage` 返回各轮明细。
- 统计使用 `thread/tokenUsage/updated` 的累计量之差，同一轮多次 turn 和重复通知不重复累计。
- 输入、缓存输入、输出、推理输出、总量分别记录。缓存是输入子集，推理是输出子集。
- 缺失字段为 null/“未知”，同时保留已知小计；这不是账户剩余额度或费用估算。
- 范围为 Coach 调用，不包含 Planner/Qwen/Jev。失败与取消的已上报 token 也保留。

接口依据：[Codex SDK](https://learn.chatgpt.com/docs/codex-sdk)、
[Codex app-server](https://learn.chatgpt.com/docs/app-server)。

## 2026-11-02 验证记录

服务器已启用 codex / gpt-6-astra，Windows worker 已启动。服务器回归 498 项通过、27 项跳过。
真实跨机验证同一 Astra 会话连续完成 `read_file → check`，单次延迟 3.766 / 2.468 秒。
两次合计输入 16,230（其中缓存 7,168）、输出 92、总计 16,322 token，与 Codex 累计计数一致。
证据：服务器 `data/codex_integration_checks/20261102-104218/result.json`。
这是接口与工具调用检查，不是对局或 RSI 优化成绩；CURRENT 保持 v009。
