# Portable 接入现有模型与 RSI

2026-10-31。使用现有 Steam GOTY 资源和隔离的 PvZ-Portable 实例。
上游固定为 `98b659380014c89c3553fbb2839f5e5a84d6df3d`；补丁在
`external/PvZ-Portable/src/Harness.cpp` 等文件，构建入口为该目录的
`tools/build-harness.ps1`。不运行已停用的 Emulator，不修改个人游戏。

## 运行入口

在 pvz-agent 仓库中运行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -u -m rsi.portable_loop --name trial-新的唯一名称 --mode both
```

默认本机配置 `data/portable_rsi/config.json`。`campaign` 只顺序闯关；`rsi`
只跑一次完整闭环；`both` 先闯关，后闭环，合格晋升后再按相同关卡种子复测。
PAUSE/STOP 文件位于配置的 work_dir，控制方式与 native_loop 相同。
启动后每路独立游戏进程，默认两路评测；模型和凭据只在现有服务器执行。

## 游戏与策略边界

- Planner/Jev 使用 `rsi.native_bridge.Bridge.decide` 的实际生产实现。
- Jev 独占种植；宿主按游戏返回的合法动作约束执行，代码自动收阳光。
- 传送带卡片费用为实际游戏返回的零，空槽不可用；普通卡片费用保持原值。
- 当前仅接第一大关 1-1 至 1-10 的初始化；顺序闯关在首败停止。到达接口边界
  只表示支持范围用尽，不能称为模型最大能力。1-5/1-10 为传送带特殊关。
- 选卡关使用固定公开初始卡组；这次尚未评测自主选卡。
- 每局重建独立 C++ 进程和私有存档，避免跨局缓存/档案污染随机轨迹。
  这与此前复用进程的 20 局 WAIT 引擎测速不同，不能沿用它的吞吐数字。
- 截图由真实游戏绘制到 800×600 离屏图像。两种条件不推进现实时间；图像
  条件不向玩家暴露动态 game state。渲染前后隔离游戏 RNG。

## RSI 与证据

现有 Coach 自行读取开发局发现问题，Engineer 编辑隔离的 player 模块、要求
Windows 测试并修复，最后进入原来的固定晋升门槛。没有手写战术候选，也没有
减小样本数、取消独立确认或跳过截图审计。计数仍为开发8、确认12、审计12。

Portable 的环境标识是 `pvz_portable`，绑定上游、实际 EXE、补丁、资源和执行器
摘要，不能接受 `pvz_rl` 或旧构建的证据。研究策略从真实 CURRENT v009 复制，
保存于服务器 `data/portable_study/`；晋升只改该研究目录的 CURRENT。真实客户端
的 CURRENT 保持独立，研究成绩不自动宣称已验证真实客户端迁移。

本机 `work_dir/<job>/step-*` 保存每局结果和终局截图，`live/` 保存最近实际决策。
`<name>-summary.json` 区分正式闯关、RSI结论和基础设施失败；服务器
`data/portable_study/native_rsi/<job>/` 保存 Coach/Engineer 记录与评测。

## 当前验证状态

- 1-1 至 1-10 均通过初始化和截图检查，**不是模型通关成绩**。
- 更强的整局复现检查暴露了复用进程的跨局差异；每局独立进程后，种子99222
  的两次无渲染和一次有渲染 WAIT 完整轨迹一致，终态tick为24225。
- `trial-20261031-1726` 正式从1-1开始，Planner返回后第一次Jev调用遇到
  `ConnectError`。没有种植、没有完整胜负局、没有生成或晋升RSI候选。
- 服务器实际配置为 `http://127.0.0.1:18220/v1/decide`；用户指定的18220和
  18221均在服务器本机立即拒绝连接。需服务恢复后用新的实验名重跑。
- 本地相关回归最初131项通过、1项旧断言失败；旧断言假定Planner只有一个文本
  片段，与现有静态知识片段冲突。更新为检查全部输入均为文本、包含场景及知识后，
  该文件12项通过。新增后端边界4项通过，服务器相关124项通过。
- 最终同步后服务器含上述文本隔离检查共136项通过（17:34）；两个Jev端口复测仍拒绝连接。

部署入口 `scripts/deploy_portable.py` 会备份限定文件、上传并跑测试，不操作共享
模型或重启真实游戏服务。最近一次初始备份为
`/srv/operator/pvz-backups/before-portable-20261031-172455.tgz`，最终增量备份为
`/srv/operator/pvz-backups/before-portable-20261031-173425.tgz`。
