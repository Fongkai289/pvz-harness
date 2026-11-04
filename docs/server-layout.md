# 服务器目录与维护

项目部署根目录由不提交的 `local.json` 中 `ssh.remote_dir` 指定。以下是示例，不是真实服务器地址：

`/srv/pvz-harness`

这是部署根目录，直接包含 Python 模块；不再套一层 `app`。
本机则是 `pvz-harness/app` 主仓库与 `pvz-harness/external` 外部依赖并列，见[本机目录](workspace-layout.md)。

```text
fengkai/
  pvz-harness/
    README.md、README.zh-CN.md  # 项目导航
    server/、rsi/              # 实时服务、研究协调与评测
    common/、rules/            # 协议、边界与固定规则
    client/、sim/、eval/        # 同步的客户端源码和评测辅助模块
    knowledge/、playbook_seed/ # 图鉴、模板与初始化策略
    scripts/、tests/、docs/
    .env                      # 当前私有配置，不提交或输出内容
    .venv/                    # Linux Python 环境
    data/
      playbooks/、CURRENT     # 实战策略版本和当前指针
      native_player_code/    # 版本化玩家代码
      live_reviews/          # 实战复盘
      portable_study/        # Portable 研究策略、任务与结果
      native_rsi/            # 原生后端研究任务与结果
      codex_coach/           # Coach 请求与响应缓存
      codex_coach_cancelled/ # 取消记录
      deploy_backups/        # 历次部署备份
        config/              # 归档的 .env.bak-*，权限 600
      migrations/            # 路径迁移备份与审计
    runs/                    # 实战会话事件与截图
    logs/                    # 服务日志
    DEPLOYED                 # 既有部署标记，仅供历史参考
```

其他 `data` 子目录中的识图标注、选卡、策略重放、锁文件和部署清单保留原位置。
旧日志和审计中的旧绝对路径保留历史原文，不把它们当作当前运行配置。
`data/`、`runs/` 和 `logs/` 都是实验资产，不因清理代码而删除。

## 启动和连接

本次目录整理保持应用停止。用户要求启动时，在服务器执行：

```bash
cd /srv/pvz-harness
tmux new-session -d -s pvz -c "$PWD" 'bash scripts/run_server.sh'
```

先检查是否已有 `pvz` 会话，避免重复启动。服务仍通过 `127.0.0.1:8765` 和 SSH 隧道访问。
`scripts/run_server.sh` 会在应用退出后重启；停止应用时也必须停止它的监督循环。
本机连接、复盘、部署脚本和研究 SSH 桥使用上面的新目录。

共享模型服务、相邻项目和 SSH 地址不属于本项目目录，整理时不改动。
旧目录不保留兼容软链接；虚拟环境入口随迁移更新，避免依赖旧路径。
