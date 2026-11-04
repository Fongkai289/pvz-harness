# 工作区目录与完整性

2026-11-04 整理后的 Windows 布局：

```text
Computer Use/
  <private-key-file>             # 现有 SSH 私钥，留在仓库外
  pvz-harness/
    README.md                # 本机工作区导航
    external/                # 外部源码、引擎、DLL、构建工具
      PvZ-Portable/
      pvz_rl/
      msys64/
      pvz-1065-setup/
    app/               # 主 Git 仓库；命令工作目录
      client/                # Windows 截图、输入、只读裁判与控制台
      server/                # 实时服务和模型调用
      rsi/                   # Coach、候选代码、评测与晋升
      rules/                 # 固定协议、边界与评测规则
      common/                # 共享协议
      knowledge/             # 图鉴与识别模板
      playbook_seed/         # 初始化策略
      sim/、eval/            # 模拟和评测辅助模块
      scripts/               # 启动、连接、部署、诊断
      tests/、docs/
      data/                  # 本机配置、对局、复盘、策略和研究状态
        native_rsi/config.json
        portable_rsi/config.json
        portable_benchmarks/ # 归档的 2026-10-31 引擎测试
        migration_check/    # 本次路径调整的备份和审计
      logs/                  # 运行日志
      .venv/                 # 本机 Python 环境
```

`data/`、`logs/`、`.venv/` 在 Git 中忽略，不代表可以删除。外部引擎的源码修改和独立 Git 仓库也必须保留。
历史报告、会话缓存及日志中的旧绝对路径属于当时的记录，不批量改写；当前配置、包安装路径和构建文件使用新地址。

## 路径边界

- Windows 主仓库：`<workspace>/app`。
- 外部依赖：`<workspace>/external`。
- 服务器项目由 `local.json` 的 `ssh.remote_dir` 指定；`/srv/pvz-harness` 仅为公开示例。
- 服务器目录的分层与启动入口见[服务器目录与维护](server-layout.md)。
- 个人游戏资源仍从训练配置中的 Steam 目录读取，没有移动个人游戏或存档。
- SSH 私钥保留原位置；不放入主仓库，也不写入文档或日志。
- `native_rsi` 的 `game_executable=null` 表示尚无兼容的完整 1065 游戏，不是本次迁移丢失文件。当前训练使用 Portable。

## 入口

先进入 `app`，使用 `.venv\Scripts\python.exe -m ...`，无需依赖系统 PATH。

| 操作 | 入口 |
|---|---|
| 客户端 | `scripts/start_client.cmd` |
| 控制中心 | `.venv\Scripts\python.exe scripts/training_dashboard.py` |
| Coach 状态/启动/停止 | `scripts/codex_coach.ps1 -Mode Status/Start/Stop`，每次选择一个模式 |
| 服务器连接 | `scripts/connect_server.ps1` |
| Portable 构建 | `../external/PvZ-Portable/tools/build-harness.ps1` |
| 离线测试 | `.venv\Scripts\python.exe -m pytest -q` |

1065 准备脚本和可选 Gym 契约测试均通过主仓库的父目录定位 `external`。
训练配置中的 `work_dir` 必须位于当前主仓库的 `data` 下，避免重新生成旧工作区。
构建工具通过 Portable 的兄弟目录寻找 `msys64`，不修改全局 PATH。

目录整理不启动任何训练、真实游戏客户端或远程服务。
