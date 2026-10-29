# 交接：Windows 端（游戏机 / 瘦客户端）

写于 2026-10-28，由 Mac 上的 Claude Code 会话交给 Windows 上的新会话。

## 1. 项目一句话

用"看截图 + 点鼠标"玩《植物大战僵尸》的 computer-use 智能体，带 RSI 循环：教练改策略本（playbook），用固定测试局打分，变好就升级，变差就回滚。
研究问题是**看画面操作**和**直接读游戏状态**到底差多少。

设计文档（claude.ai 登录同一账号可看）：https://claude.ai/artifact/LoFF6d77152w5JRaBon1HC

## 2. 架构：反向云游戏

```
Windows 本机（这个会话负责）                 远程服务器（Mac 会话负责）
┌───────────────────────────┐   WebSocket   ┌──────────────────────────────────┐
│ client/windows_client.py  │ ── 截图 JPEG ─→│ 识图(代码+Qwen) → Jev(每秒) → 点击 │
│  截图 mss / 点击 pydirect │ ←── 点击命令 ──│ planner(Qwen, ~20s) / 裁判 / 日志  │
│  暂停 psutil / 读内存     │ ── 真实状态 ──→│ 真实状态只给裁判和教练，玩家看不到  │
└───────────────────────────┘ (SSH 隧道)    └──────────────────────────────────┘
```

- Windows 端只做四件事：截图、执行点击、暂停/恢复游戏进程、只读内存（给裁判打分用）。
- 所有模型调用都在服务器上，Windows 不调任何模型。
- 协议在 `common/protocol.py`，客户端通用部分在 `client/link.py`。

## 3. 分工（重要：避免两边改同一个文件）

| 这个 Windows 会话负责 | Mac 会话负责 |
|---|---|
| `client/windows_client.py`、新增 `scripts/verify_env.py`、`docs/env.md` | `server/`、`rules/`、`rsi/`、`tests/`、部署服务器 |

- `common/protocol.py` 和 `server/geometry.py` 两边都用。要改的话，先只记录要改什么，写进 `docs/env.md`，由用户带回 Mac 会话去改。
- **不要从 Windows 部署或重启服务器**。服务器的部署脚本、`.env` 和数据都由 Mac 会话管理。

## 4. 要做的事：环境验证（设计文档"第一步"）

| # | 项目 | 通过标准 | 备注 |
|---|---|---|---|
| 1 | 游戏版本 | 版本号已记录，PvZ Toolkit 能连上游戏 | 用 Steam 正版 GOTY 版，预期 **1.2.0.1096**。窗口模式，不要全屏 |
| 2 | 截图 | 客户区正好 **800×600**，无黑边，≥ 20 帧/秒 | 先调 `SetProcessDpiAwareness(2)`；Windows 缩放最好 100% |
| 3 | 点击 | 白天 1-1 里种 20 次，全部落在预期格子 | 测 5×9 草坪四个角；顺便测窗口不在前台时点击是否生效 |
| 4 | 读状态（只读） | 阳光、植物、僵尸的数量和位置跟画面一致 | pymem 附加进程；偏移参考 PvZ Toolkit 源码里 1.2.0.1096 的那套。**只读不写** |
| 5 | 暂停 | 挂起 10 秒僵尸不动，恢复后不跳变 | `psutil.Process(pid).suspend()/resume()` |
| 6 | 变速 / AvZ | 实际倍数误差 < 10% | 确认 AvZ 支不支持 1.2.0.1096；不支持就记下来，不要自己硬改游戏 |
| 7 | 大模型延迟 | —— | **已在服务器上做完，Windows 跳过** |
| 8 | 连服务器 | 截图到点击 p95 < 500 ms；真游戏里阳光能被自动收 | 见第 6 节 |

### 坐标校准（第 2、3 项顺便做）

服务器上 `server/geometry.py` 的坐标是从 Steam 官方截图量出来的（800×600 坐标系）：

- 卡片栏：第 0 张卡左边 x=96，间距 59.1，宽 48，y 8–78
- 阳光数字：(22, 58) – (78, 81)
- 草坪（5 行）：左边 x=40，格宽 80，第 0 行上边 y=80，行高 100（**这几个是粗估，需要校准**）
- 草坪（6 行，泳池）：第 0 行上边 y=76，行高 86

在真游戏里截几张图（白天、泳池各一张，带卡片栏），量一下实际位置。如果有偏差，把正确数值写进 `docs/env.md`，**不要直接改 geometry.py**。

### 交付物

- `scripts/verify_env.py`：一次跑完第 1–6 项，每项输出通过或失败
- `docs/env.md`：游戏版本号、截图尺寸、坐标校准结果、内存偏移和来源、暂停结果、变速结论、第 8 项的延迟数字（中位数和 p95）
- 几张真游戏截图（白天 1-1、泳池关各一张），放 `docs/screens/`，以后做识图评测用
- `read_state()`（在 `windows_client.py` 里，现在是空的）：返回给裁判的真实状态

## 5. 安装

```powershell
# 装 uv（或用 Python 3.11+ 自带的 pip）
winget install --id=astral-sh.uv -e
cd pvz-agent
uv sync --extra client
uv run pytest      # 应该全部通过（41 个）；只是确认代码完整
```

## 6. 连服务器（第 8 项）

1. **SSH 密钥**：用户自己把 `id_ed25519_llm2` 拷到 Windows，例如 `C:\Users\<你>\.ssh\id_ed25519_llm2`。Windows 上如果报 "UNPROTECTED PRIVATE KEY FILE / too open"，要收紧权限，只让自己能读：
   ```powershell
   icacls $env:USERPROFILE\.ssh\id_ed25519_llm2 /inheritance:r /grant:r "$($env:USERNAME):R"
   ```
2. **先测能不能连上**（2026-10-28 傍晚服务器 SSH 端口一度连不上，可能还没恢复）：
   ```powershell
   ssh -i $env:USERPROFILE\.ssh\id_ed25519_llm2 -p 32420 root@117.186.102.78 "echo ok"
   ```
   连不上的话，先做第 1–6 项（都不需要服务器），再告诉用户。
3. **建隧道**，窗口一直开着：
   ```powershell
   ssh -N -L 8765:127.0.0.1:8765 -p 32420 -i $env:USERPROFILE\.ssh\id_ed25519_llm2 root@117.186.102.78
   ```
4. **令牌**：在服务器 `/root/fkchang/pvz-agent/.env` 的 `PVZ_TOKEN`。由用户提供，不要写进任何文件，也不要贴进聊天之外的地方。
5. 先跑假游戏，再跑真游戏：
   ```powershell
   uv run python -m client.mock_client --token <令牌> --seconds 30 --game lawn
   uv run python -m client.windows_client --token <令牌> --seconds 60
   ```

## 7. 规矩（必须遵守）

- **服务器是多人共用的容器**。只碰 `/root/fkchang/`，不要动别人的目录、进程和服务。
- 服务器上的 Qwen（:18201）和 OneJev（:18200 / :18202）都是共享模型池，**不要从 Windows 直接调用它们**；需要测模型就交给 Mac 会话。**永远不要给 OneJev 发图片**（之前发过一次，把别人的服务弄挂了）。
- **读内存只读不写**，不往游戏里注入代码。
- **信息隔离**：真实游戏状态只能给裁判、教练和识图标注用，玩家这一侧（识图、Jev、planner）不能读到。`tests/test_isolation.py` 会检查这一点。
- 密码、API 密钥、令牌都由用户自己填，Claude 不代填。

## 8. 当前进度（截至交接时）

- 服务器端已经跑通：识图（阳光 8/8，卡片 48/48，僵尸类型约 90%）、Jev 三层决策（模型判断 → 策略本战术 → 硬规则）、planner、RSI 骨架，以及假游戏（收阳光 / 简化草坪）。41 个测试全部通过。
- **还没碰过真游戏。** 卡片栏坐标是从官方截图量的，草坪坐标是估的，`windows_client.py` 也一次都没跑过。所以这一步的环境验证就是第一次接真游戏。

## 9. 做完后带回 Mac 会话的东西

把 `docs/env.md` 和 `docs/screens/`，以及改过的 `client/`、`scripts/` 文件带回 Mac（压缩包、网盘都行）。Mac 会话会据此校准 `geometry.py`、把截图加进识图评测，并把裁判接上真实状态。
