"""Read-only operator data, separate from the screenshot/player stream."""
import asyncio
import json
import httpx

REVIEW_LIST = 8  # recent games in the 赛后复盘 list

REVIEW_STATES = {"not_triggered": "尚无赛后复盘", "queued": "等待复盘", "reviewing": "Coach 正在复盘",
                 "completed": "复盘完成 · 待你确认下一局", "failed": "复盘失败",
                 "skipped": "无需复盘 · 胜利且没用小推车"}
LEARNING_STATES = {"not_triggered": "等待真实对局复盘", "waiting_runtime": "等待 Windows 评测环境",
                   "evaluating": "多局评测中", "coaching": "Coach 正在生成候选", "promoted": "候选通过，下一次连接生效",
                   "rejected": "候选未通过，保留旧版本", "failed": "本轮失败，保留旧版本",
                   "stale": "当前版本已改变，停止旧候选晋升", "stopped": "本轮已停止", "blocked": "评测受阻，保留旧版本",
                   "engineering": "Engineer 检查与修改代码", "engineer_testing": "Engineer 代码正在 pvz_rl 开发集测试"}


class OperatorView:
    def __init__(self, url, token):
        self.base = url.replace("ws://", "http://", 1).replace("wss://", "https://", 1).rsplit("/ws", 1)[0]
        self.token = token
        self.review = {"status": "not_triggered"}
        self.playbook = None
        self.error = None
        self.decisions = {"status": "not_started"}
        self.mechanics = None
        self.mechanics_error = None
        self.learning = {"status": "not_triggered"}
        self.coach_usage = {"rounds": []}
        self.reviews = []  # recent games' reviews, newest first (a server without /reviews leaves this empty)

    async def run(self):
        async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
            rounds = 0
            while True:
                if rounds % 5 == 0:  # every ~10 s: the list changes only when a game ends
                    try:
                        listed = await client.get(self.base + "/reviews", params={"limit": REVIEW_LIST},
                                                  headers={"Authorization": f"Bearer {self.token}"}, timeout=10)
                        listed.raise_for_status()
                        self.reviews = listed.json()
                    except httpx.HTTPError:
                        pass  # the latest review below still shows
                rounds += 1
                try:
                    review, book, decisions, learning, usage = await asyncio.gather(*[
                        client.get(self.base + path, headers={"Authorization": f"Bearer {self.token}"})
                        for path in ("/reviews/latest", "/playbook", "/decisions/latest", "/learning/latest", "/coach/usage")])
                    review.raise_for_status()
                    book.raise_for_status()
                    decisions.raise_for_status()
                    learning.raise_for_status()
                    self.review, self.playbook = review.json(), book.json()
                    self.decisions = decisions.json()
                    self.learning = learning.json()
                    usage.raise_for_status()
                    self.coach_usage = usage.json()
                    self.error = None
                except httpx.HTTPError as exc:
                    self.error = type(exc).__name__
                if self.mechanics is None:
                    try:
                        result = await client.get(self.base + "/mechanics", headers={"Authorization": f"Bearer {self.token}"})
                        result.raise_for_status()
                        self.mechanics = result.json()
                        self.mechanics_error = None
                    except httpx.HTTPError as exc:
                        self.mechanics_error = type(exc).__name__
                await asyncio.sleep(2)

    def mechanics_text(self):
        if self.mechanics is None:
            return "正在读取服务器游戏图鉴…" + (f"\n暂未读取成功：{self.mechanics_error}" if self.mechanics_error else "")
        data = self.mechanics
        lines = [f"PC 原版参考 · {data['version']}",
                 "基础参数用于安排预算、火力和种植间距；没有实时血量计算。",
                 "速度单位为秒/格：数字越小越快；阶段未识别时不能假定已经加速或减速。",
                 "数值来自社区资料，尚未逐项在当前 GOTY 版本校准；空值代表未知或不适用。",
                 "\n植物"]
        fields = {"sun_cost": "阳光", "full_hp": "满血 HP", "full_hp_unarmed": "未就绪时 HP",
                  "damage_per_projectile": "每发伤害", "damage_per_explosion": "爆炸伤害",
                  "attack_interval_s": "平均攻击间隔/秒", "packet_recharge_s": "卡片冷却/秒",
                  "packet_recharge_s_approx": "卡片冷却约/秒", "arming_s_reference_range": "准备时间参考/秒",
                  "arming_s_planning_margin": "规划采用准备时间/秒", "digestion_s": "消化时间/秒",
                  "activation_s_approx": "触发延时约/秒", "sun_per_cycle": "每次产阳光",
                  "production_interval_s_approx": "产阳光间隔约/秒", "first_sun_s_range": "首次产阳光/秒",
                  "single_normal_zombie_chew_s_approx": "满血被单只普通僵尸持续啃食约/秒",
                  "movement_multiplier_when_chilled": "适用冰冻减速后的移动倍率",
                  "damage_per_bite_inedible_target": "无法吞食目标时每口伤害",
                  "damage_direct": "直接伤害", "damage_splash": "溅射伤害",
                  "damage_kernel": "玉米伤害", "damage_butter": "黄油伤害", "butter_stun_s": "黄油眩晕/秒"}
        for name, info in data["plants"].items():
            lines.append("\n" + info.get("name_zh", name) + (f"  ({name})" if info.get("name_zh") else ""))
            lines.append(" · ".join(f"{label}：{info[key]}" for key, label in fields.items() if info.get(key) is not None))
            if info.get("detail_status"):
                lines.append("其余参数暂未核实")
            if info.get("uncertainty"):
                lines.append("准备时间资料有14秒与15秒两种表述；15秒仅作规划参考，不表示场上已经就绪。")
        lines.extend(["\n僵尸", "血量含列出的护具；掉头后的濒死阶段使血量不能直接换算成精确击杀弹数。"])
        phases = {"walking":"步行", "running":"奔跑", "moving":"移动", "before_vault":"跳跃前", "after_vault":"跳跃后",
                  "paper_intact":"报纸完整", "paper_destroyed":"报纸破损", "sliding":"滑行", "dancing":"舞步",
                  "swimming":"游泳", "sled":"雪橇滑行", "before_jump":"跳跃前", "after_jump":"跳跃后",
                  "flying":"飞行", "digging":"地下", "bouncing":"跳跃", "without_pogo":"失去跳杆",
                  "carrying_ladder":"持梯", "after_placing":"放梯后", "adventure_walking":"冒险模式步行"}
        for name, info in data["zombies"].items():
            hp = info.get("full_hp")
            lines.append(f"\n{info.get('name_zh', name)} · 满血 HP：{hp if hp is not None else '未知、随变体变化或不适用'}")
            if "equipment_hp" in info:
                lines.append(f"本体 {info['body_hp']} + 护具 {info['equipment_hp']}")
            speeds = info.get("seconds_per_tile")
            lines.append("速度：" + (" / ".join(f"{phases.get(p,p)} {v}秒/格" for p,v in speeds.items()) if speeds else "暂未核实或不适用"))
            if info.get("chew_dps"):
                lines.append(f"普通啃食：{info['chew_dps']} HP/秒")
        lines.extend(["\n使用边界", "坚果的约40秒是满血、单只普通僵尸、无减速且持续啃食的基准；多只或特殊攻击不能照搬。",
                      "并非所有植物都有固定寿命；冷却、攻击间隔与准备时间分别记录。", "\n资料来源（核对日期："+data["checked_on"]+"）"])
        lines.extend(data["sources"].values())
        return "\n".join(lines)

    def collection_text(self):
        sample = self.decisions.get("events", {}).get("sun_collection")
        if not sample:
            return "阳光 · 代码自动拾取，等待新对局数据"
        prefix = "阳光 · " if self.decisions.get("status") == "live" else "阳光 · 上次连接："
        return (prefix + sample.get("reason", "") + f" · 已下发 {sample.get('total_clicks', 0)} 次点击"
                + "（不等于确认拾取）")

    def decisions_text(self):
        d = self.decisions
        if d.get("status") == "not_started":
            return "尚无真实对局指令。开始或继续后自动刷新。"
        lines = ["实时指令" if d.get("status") == "live" else "上次连接记录（已暂停或断开，非正在执行）",
                 "中文行列从1计数；原始英文说明/点击日志的行列从0计数。这里只显示模型输出。"]
        if self.error:
            lines.append(f"刷新失败：{self.error}，保留上次记录")
        lines.extend(["\n自动拾取阳光", self.collection_text()])
        events = d.get("events", {})
        p = events.get("plan", {})
        plan = p.get("plan")
        lines.append("\nPlanner 最新规划")
        if plan:
            labels = {"defense": "防守", "economy": "经济", "reinforce": "加固", "save_sun": "存阳光"}
            lines.extend([f"目标：{plan['objective']}",
                "优先级：" + " → ".join(labels.get(x, x) for x in plan.get("priorities", [])),
                "重点行：" + str([r + 1 for r in plan.get("priority_rows", [])]),
                "建议避开行：" + str([r + 1 for r in plan.get("blocked_rows", [])]),
                f"建议额度 {plan.get('spend_limit') if plan.get('spend_limit') is not None else '未指定'} · 建议保留 {plan.get('sun_reserve') if plan.get('sun_reserve') is not None else '未指定'} · 有效期 {plan.get('valid_for_seconds')}s",
                f"预算调整条件：{plan.get('budget_policy') or '未提供'}",
                f"战术说明：{plan.get('tactics') or '未提供'}",
                f"说明：{plan.get('note', '')}",
                "本次规划触发：" + "；".join(p.get("trigger_reasons") or ["定时／有效期刷新"])])
        else:
            lines.append(p.get("error") or "尚未生成规划")
        j = events.get("jev", {})
        lines.extend(["\nJev 最新决策", self._jev_line(j)])
        advice = (j.get("decision_context") or {}).get("budget_advice")
        if advice and advice.get('initial_allowance') is None:
            lines.append(f"本次决策未指定额度建议 · 已下发预计费用 {advice.get('committed_spend')}（不是实际阳光）")
        elif advice:
            lines.append(f"本次决策的初始建议 {advice.get('initial_allowance')} − 已下发预计费用 {advice.get('committed_spend')}"
                         f" = 剩余建议 {advice.get('remaining_advice')}（不是实际阳光，也不是硬上限）")
        if j.get("blocked_reasons"):
            lines.append("受阻原因：" + json.dumps(j["blocked_reasons"], ensure_ascii=False))
        lines.append("\n最近实际下发的点击（不同于模型提出的动作）")
        for command in d.get("commands", []):
            source = (command.get("meta") or {}).get("decision_source")
            source = {"sun_collector": "代码拾取", "onejev": "Jev", "rules": "规则基线"}.get(source, "历史记录")
            lines.append(f"#{command['cmd_id']} [{source}] {command['reason']} · ({command['x']}, {command['y']})")
        last_exec = events.get("exec")
        if last_exec:
            lines.append(f"最近回执 #{last_exec['cmd_id']}：" + ("输入已执行（不等于种植成功）" if last_exec['ok'] else str(last_exec['error'])))
        lines.append("\nPlanner 危险判断（模型意见）")
        assessments = (plan or {}).get("threat_assessment", [])
        urgency = {"low": "低", "medium": "中", "high": "高", "critical": "紧急"}
        lines.extend(f"第{a['row']+1}行：{urgency.get(a['urgency'], a['urgency'])} · {a['reason']}" for a in assessments)
        if not assessments:
            lines.append("该规划尚未提供判断")
        lines.append("\n逐行观察（不由代码划分危险等级）")
        rows = events.get("threats", {}).get("rows")
        if rows:
            for row in rows:
                col = row.get("nearest_col")
                lines.append(f"第{row['row'] + 1}行：{'观察到僵尸' if row['types'] else '未见僵尸'} · "
                    f"最近列 {col + 1 if col is not None else '未知'} · "
                    f"攻击植物 {row.get('attackers')} · 阻挡 {row.get('walls')} · {','.join(row['types'])}")
        else:
            z = events.get("zombies", {}).get("scene") or {}
            for row, col in z.get("nearest_col", {}).items():
                lines.append(f"第{int(row) + 1}行：最近僵尸在第{col + 1}列（旧日志无危险评级）")
        trigger = events.get("replan_trigger", {})
        if trigger:
            lines.append("最近提前规划请求：" + "；".join(trigger.get("reasons") or []))
        lines.append("\n最近 Jev 决策（含未执行）")
        lines.extend(self._jev_line(item) for item in d.get("history", []))
        return "\n".join(lines)

    @staticmethod
    def _jev_line(item):
        if not item:
            return "尚未决策"
        place = item.get("placement")
        choice = (f"卡槽{place['slot'] + 1} → 第{place['row'] + 1}行、第{place['col'] + 1}列" if place else
                  {"collect_suns": "收阳光", "wait": "等待"}.get(item.get("action"), "未触发"))
        blocked = item.get("rejected") or item.get("selector_error")
        return (f"#{item.get('decision_id') or '未调用'} {choice} · {item.get('reason', '')}"
                + (f" · 未执行：{blocked}" if blocked else ""))

    def review_views(self):
        """Recent games for the 赛后复盘 list, newest first; the latest one from its faster 2 s refresh."""
        rows = [{**r, **self.review} if r.get("session") == self.review.get("session") else r for r in self.reviews]
        if self.review.get("session") and all(r.get("session") != self.review.get("session") for r in rows):
            rows.insert(0, self.review)
        return [self.review_view(r) for r in rows or [self.review]]

    def review_view(self, r=None):
        """赛后复盘 as fields for the control center's cards; review_text() keeps the plain form."""
        r = self.review if r is None else r
        record = r.get("review") or {}
        coach = record.get("coach") or {}
        def listed(value, limit):
            return [str(x) for x in value][:limit] if isinstance(value, list) else ([str(value)] if value else [])
        return {"status": r.get("status", "not_triggered"), "label": REVIEW_STATES.get(r.get("status"), "等待状态"),
                "session": r.get("session"), "result": r.get("result"), "model": (record.get("model_call") or {}).get("model"),
                "diagnosis": coach.get("diagnosis"), "objective": coach.get("objective"),
                "evidence": listed(coach.get("evidence"), 8), "uncertainties": listed(coach.get("uncertainties"), 6),
                "error": r.get("error"), "refresh_error": self.error, "skipped": r.get("native_rsi_skipped"),
                "usage": [{"round": row["round_id"].rsplit("/", 1)[-1], "calls": row["tokens"].get("calls"),
                           **{k: row["tokens"].get(k) for k in ("input_tokens", "cached_input_tokens",
                                                                "output_tokens", "total_tokens")}}
                          for row in self.coach_usage.get("rounds", [])[-6:]]}

    def review_text(self):
        r = self.review
        return (REVIEW_STATES.get(r.get("status"), "等待状态") + "\n"
                + (f"状态刷新失败：{self.error}（保留上次结果）\n" if self.error else "")
                + r.get("text", r.get("error", "胜利或失败后会自动触发；暂停不会触发。"))
                + "\n\n自动 RSI · pvz_rl\n" + self.learning_text()
                + "\n\n" + self.coach_usage_text())

    def coach_usage_text(self):
        lines = ['Coach Token（不含 Planner / Qwen / Jev；缓存包含在输入中）']
        for row in self.coach_usage.get('rounds', [])[-6:]:
            tokens = row['tokens']
            show = lambda key: '未知' if tokens.get(key) is None else f"{tokens[key]:,}"
            lines.append(f"{row['round_id']} · {tokens['calls']} 次调用\n"
                         f"输入 {show('input_tokens')}（缓存 {show('cached_input_tokens')}） · "
                         f"输出 {show('output_tokens')} · 总计 {show('total_tokens')}")
        return '\n'.join(lines) if len(lines)>1 else lines[0]+'\n尚无调用记录'

    def learning_text(self):
        data = self.learning
        return (LEARNING_STATES.get(data.get("status"), data.get("status", "未知"))
                + (" · 已暂停" if data.get("paused") else "")
                + f"\n原策略 {data.get('parent', '—')} · 候选 {data.get('candidate') or '尚未生成'}"
                + "\n" + str(data.get("detail") or data.get("reason") or "state 对比、独立种子确认、截图审计全部合格后才晋升。")
                + "\n" + str(data.get("scope", "")))

    def playbook_text(self):
        if self.playbook is None:
            return "正在读取当前生效策略…" + (f"\n{self.error}" if self.error else "")
        timing = self.playbook.get("effective_timing", {})
        periods = " · ".join(f"{label} {timing[key]:g}s" for key, label in (
            ("resources_seconds", "资源"), ("zombies_seconds", "僵尸"),
            ("plants_seconds", "植物"), ("planner_seconds", "规划"),
            ("frame_log_seconds", "截图保存")) if key in timing)
        book = self.playbook["playbook"]
        strategy = (book.get("planner") or {}).get("knowledge", "暂无策略文本")
        parameters = {k:v for k,v in book.items() if k != "planner"}
        parameters["planner"] = {k:v for k,v in (book.get("planner") or {}).items() if k != "knowledge"}
        parameters["effective_timing"] = timing
        return (f"当前生效版本：{self.playbook['current']}\nCoach 建议未通过评测前，不会替换此版本。\n"
                + (f"调用周期：{periods}\n" if periods else "")
                + "\n策略正文\n" + strategy
                + "\n\n版本与其他参数\n" + json.dumps(parameters, ensure_ascii=False, indent=2))
