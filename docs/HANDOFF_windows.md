# Windows 开发与运行交接

更新：2026-11-04。当前说明以 README、PROJECT.md、reproduce.md 为准。

## 入口与私有配置

- 主仓库位于工作区 `app/`，外部依赖位于并列的 `external/`；目录本身可由使用者选择。
- 真实 SSH 地址、端口、密钥路径与远程目录仅写在忽略的 `local.json`。示例为 `local.example.json`。
- `scripts/connect_server.ps1` 负责 SSH，默认优先可用 Wi-Fi；不修改系统网络路由。
- `scripts/start_client.cmd` 启动真实客户端；控制中心为本机 8766，客户端桥为 8767，SSH 隧道为 8765。
- `scripts/deploy.ps1 -Test` 是部署入口，操作员明确要求部署时才调用；脚本会重启项目服务。
- Codex Coach 可选，入口 `scripts/codex_coach.ps1`。Portable 配置在 `data/portable_rsi/config.json`。
- 旧 Emulator 启动器及一次性服务器迁移脚本已退役；安装器仅保留离线基准用途。

## 当前分工和结果

Qwen 识图与 Planner 给观察和规划；Jev 做文字动作选择；System0 可在经过闸门且配置允许时提供反射/后备动作；Coach 修改受限策略和玩家代码；固定裁判不由候选修改。
Portable 已记录 1-2 至 1-7 无小推车通关及一次 1-5 的 Coach 自改晋升；真实游戏已记录 1-7/1-8 胜利。
这些结果不证明全冒险通关、跨种子泛化或实战因果提升。详见 PROJECT.md。

## 操作边界

- 开始、暂停、继续、结束控制客户端；真实游戏菜单和开局由操作员控制。结束不直接退出控制台。
- 个人游戏只读内存，裁判信息不泄漏给截图玩家；Portable/pvz_rl 私有研究实例是独立链路。
- 不向 Jev 发图片。模型服务独立维护，不随 harness 部署重启。
- 只操作配置明确指向的项目目录；不操作相邻项目和共享模型进程。
- `data`、`runs`、日志和历史策略是实验资产，不能因整理目录删除。
- 私钥、令牌、实际服务器入口不提交 Git。截图/模板只在本机和私有部署保存。

真实客户端校验目标为 GOTY 1.2.0.1096、800×600 客户区；未知版本不能沿用内存偏移。旧测量见 env.md（历史记录）。
公开用户先按 reproduce.md 配置模型、资源、私有模板，再启动；Codex 与 1065 游戏均不是 Portable 离线测试的前置条件。
