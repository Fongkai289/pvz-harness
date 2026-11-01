# 小推车触发时间与操作权限（2026-10-30）

实时 OneJev 模式中，只有成功的 Jev 决策可以触发收阳光或种植。代码识图给出候选阳光，
硬规则筛选可种位置，Jev 选择收阳光、种植或等待；执行器只能编译并检查该决定。
每条点击日志的 `meta.decision_source=onejev` 和 `decision_id` 对应 `jev` 事件。
Planner 不发送点击、暂停或继续；自动暂停配置已移除。
客户端操作员的开始/暂停/继续/结束只控制客户端。`PVZ_DECIDER=rules` 是明确指定的离线规则基线，
不会冒充模型调用，也不作为模型失败时的回退。

Planner 的刷新间隔取配置间隔和“指令剩余有效期减去推理时间预留”的较小值。
预留为最近推理时长的 1.5 倍，至少 1 秒。推理延迟突增仍可能导致过期；过期后不操作，
不会延长旧指令或擅自补充预算。Jev 阻塞原因也进入下一次规划输入。

## 固定评价规则

`mower-time-v1`：每次小推车触发扣 `0.1 + 0.1 * exp(-t / 60)`，t 为从本关开始的游戏秒数。
例如 10/60/180 秒触发分别约扣 0.185/0.137/0.105；完全不触发不扣分。
基础扣分保留，时间项是新的固定权重，不由教练或工具工程师调整。
不除以最终对局时长，避免拖长对局稀释早期失误。暂停和连接耗时不增加游戏时间。

简化草坪新总分版本为 `lawn-mower-time-v2`：原击杀和胜利得分减去上述惩罚。
基线与候选必须重跑同一版本、相同关卡和种子，不拿旧分数直接比较。
真实游戏先单独输出 `mower_evaluation`；尚无完整的真实游戏胜负/击杀总分，`score` 仍为 null。
现有原生模拟器没有小推车，此项对它不适用，不能把模拟器未提供的数据当成零消耗。

`mower_events` 保存每行触发的游戏秒数和采样误差。Windows 以 20 Hz 只读采样，暂停/重连
不清空同一棋盘记录；新棋盘或游戏时钟回退清空。初次接入已经触发、或采样断档超过一秒，
时间标为 null。部分覆盖不能用于自动晋升。旧日志缺少时间字段仍标记 `lawn-v1`，不编造时间。

## 只读来源与验证范围

- [PvZ Toolkit 版本布局](https://github.com/lmintlcx/pvztoolkit/blob/master/src/data.cpp)：英文 GOTY 1.2.0.1096，
  LawnApp 指针 0x731c50，board +0x868，游戏时钟 +0x5580，小推车数组 +0x118、长度 +0x11c。
- [版本签名与读取逻辑](https://github.com/lmintlcx/pvztoolkit/blob/master/src/pvz.cpp)：PE timestamp 0x4d02b058。
- [小推车布局](https://github.com/Electr0Gunner/PvZ-Reconstruction-BugFix/blob/main/Lawn/LawnMower.h)及
  [枚举](https://github.com/Electr0Gunner/PvZ-Reconstruction-BugFix/blob/main/ConstEnums.h)：行 +0x14，状态 +0x2c，
  dead +0x30；状态 1 待命、2 已触发，步长 0x48。
- [游戏计数器](https://github.com/Electr0Gunner/PvZ-Reconstruction-BugFix/blob/main/Lawn/Board.cpp)：运行时递增，100 tick 为一游戏秒。

只申请 `PROCESS_VM_READ | PROCESS_QUERY_LIMITED_INFORMATION`；没有写内存、注入或加速。
不匹配版本或读数校验失败时停止提供有效评分数据。采集只经 StateMsg 送给 Judge，
不进入识图、Planner 或 Jev 输入。实机 1-7 暂停时已确认 4 辆待命、1 辆已触发，与布局一致。

## 胜负与赛后复盘

仅针对已核对布局的英文 GOTY Adventure：LawnApp +0x91c 的场景值 4 表示失败、5 表示奖励页。
游戏场景为 3 时，Board +0x5624 的 `mLevelAwardSpawned` 表示已生成通关奖励，可在领奖前标胜。
该字段的原版布局位于 [Board.h](https://github.com/Electr0Gunner/PvZ-Reconstruction-BugFix/blob/main/Lawn/Board.h)
的 +0x560c，GOTY 对应区间增加 0x18；
[Zombie.cpp](https://github.com/Electr0Gunner/PvZ-Reconstruction-BugFix/blob/main/Lawn/Zombie.cpp)
在最后一波且没有敌方僵尸时置位并设置 BOARDRESULT_WON。
暂停、退出、手动关闭连接均不是胜负。其他模式不猜测结果。当前实机 GAME OVER 已核对场景值 4；
胜利分支有固定状态样本测试，尚未在这次实机验证中赢下一局。

确定结果后，客户端停止玩家点击、发送最终裁判状态和结束事件。服务器封存日志后排队调用
coach，一局一个任务；任务与 WebSocket 生命周期分离，服务重启恢复已排队任务。
结果写入 `data/live_reviews`，只读接口需原有令牌认证。手动暂停不排队；完成复盘也不会自动重试。
重打、领奖和恢复游戏都由操作员手动点击；客户端启用时等待可玩画面后自动连接，绝不代点菜单。
