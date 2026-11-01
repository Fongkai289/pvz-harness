"""Tk presentation only; lifecycle and game permissions stay in ControlPanel."""
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

BG, CARD, INK, MUTED, BORDER = "#f3f6f9", "#ffffff", "#192b3b", "#627486", "#dde5ec"
ACCENT = "#167d75"
FONT = "Microsoft YaHei UI"
STAGES = (("scene", "Qwen · 资源与卡片"), ("zombies", "Qwen · 僵尸"),
          ("plants", "Qwen · 植物布局"), ("plan", "Planner · 规划"), ("jev", "Jev · 决策"))


def model_row(sample):
    status = {"idle": "未触发", "running": "正在推理", "completed": "完成", "failed": "失败"}[sample["status"]]
    elapsed = sample.get("age_s")
    latency = sample.get("latency_ms")
    duration = (f"已等待 {elapsed:.1f}s" if sample["status"] == "running" and elapsed is not None
                else f"{latency:.0f} ms" if latency is not None else "—")
    period = sample.get("interval_s")
    frequency = (f"{period:g}s 周期" if period is not None else "按条件触发") + f" / {sample['recent_count']}次/分"
    age = "—" if elapsed is None else f"{elapsed:.0f}s"
    return status, duration, frequency, str(sample["count"]), age


class ControlView:
    def __init__(self, root, callbacks):
        root.configure(bg=BG)
        root.resizable(True, True)
        # Tk fonts follow Windows DPI, geometry sizes do not. Keep the minimum
        # client area large enough for every model row at 125-200% display scale.
        scale = max(1.0, float(root.tk.call("tk", "scaling")) / (96 / 72))
        root.minsize(round(820 * scale), round(650 * scale))
        root.geometry(f"{round(880 * scale)}x{round(700 * scale)}")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("PvZ.TNotebook", background=BG, borderwidth=0)
        style.configure("PvZ.TNotebook.Tab", font=(FONT, 10), padding=(17, 10), background=BG, foreground=MUTED)
        style.map("PvZ.TNotebook.Tab", background=[("selected", CARD)], foreground=[("selected", ACCENT)])
        style.configure("PvZ.Treeview", font=(FONT, 10), rowheight=34, background=CARD,
                        fieldbackground=CARD, foreground=INK, borderwidth=0)
        style.configure("PvZ.Treeview.Heading", font=(FONT, 9), background="#edf2f6", foreground=MUTED,
                        padding=(7, 9), relief="flat")
        header = tk.Frame(root, bg=BG)
        header.pack(fill="x", padx=24, pady=(20, 14))
        tk.Label(header, text="PvZ 控制台", bg=BG, fg=INK, font=(FONT, 22, "bold")).pack(side="left")
        self.badge = tk.Label(header, text="未开始", bg="#e4ebf1", fg=MUTED, font=(FONT, 11, "bold"), padx=16, pady=7)
        self.badge.pack(side="right")
        tk.Label(root, text="客户端自动识图与操作 · 游戏菜单由你手动控制", bg=BG, fg=MUTED,
                 font=(FONT, 10)).pack(anchor="w", padx=26, pady=(0, 14))

        footer = tk.Frame(root, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        footer.pack(side="bottom", fill="x")
        tk.Label(footer, text="结束保留窗口\n右上角 × 退出程序", bg=CARD, fg=MUTED,
                 font=(FONT, 9), justify="left").pack(side="left", padx=22, pady=15)
        controls = tk.Frame(footer, bg=CARD)
        controls.pack(side="right", padx=20, pady=14)
        self.buttons = {}
        self.button_colors = {}
        for label, color in (("开始", ACCENT), ("暂停", "#a36c16"), ("继续", "#386cb0"), ("结束", "#a84c54")):
            button = tk.Button(controls, text=label, command=callbacks[label], font=(FONT, 11, "bold"),
                               width=8, pady=8, relief="flat", bd=0, bg=color, fg="white",
                               activebackground=color, activeforeground="white", disabledforeground="#c2cbd3",
                               cursor="hand2", takefocus=True)
            button.pack(side="left", padx=4)
            self.buttons[label] = button
            self.button_colors[label] = color

        tabs = ttk.Notebook(root, style="PvZ.TNotebook")
        tabs.pack(fill="both", expand=True, padx=20, pady=(0, 15))
        overview = tk.Frame(tabs, bg=CARD)
        tabs.add(overview, text="运行概览")
        self.metric_values, self.metric_notes = {}, {}
        metrics = tk.Frame(overview, bg=CARD)
        metrics.pack(fill="x", padx=18, pady=(18, 14))
        for index, (key, label) in enumerate((("rtt", "本机 ↔ 服务器"), ("latency", "截图 → 点击完成"),
                                             ("memory", "客户端内存"), ("traffic", "上传 / 下载"))):
            metrics.columnconfigure(index, weight=1, uniform="metrics")
            box = tk.Frame(metrics, bg=BG, padx=12, pady=12)
            box.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 5, 0))
            tk.Label(box, text=label, bg=BG, fg=MUTED, font=(FONT, 9)).pack(anchor="w")
            value = tk.Label(box, text="—", bg=BG, fg=INK, font=(FONT, 17, "bold"))
            value.pack(anchor="w", pady=(8, 4))
            note = tk.Label(box, text="等待数据", bg=BG, fg=MUTED, font=(FONT, 9))
            note.pack(anchor="w")
            self.metric_values[key], self.metric_notes[key] = value, note
        title = tk.Frame(overview, bg=CARD)
        title.pack(fill="x", padx=20)
        tk.Label(title, text="模型调用", bg=CARD, fg=INK, font=(FONT, 12, "bold")).pack(side="left")
        self.scope = tk.Label(title, text="包含排队、推理及返回", bg=CARD, fg=MUTED, font=(FONT, 9))
        self.scope.pack(side="right")
        columns = ("name", "status", "latency", "frequency", "count", "age")
        self.table = ttk.Treeview(overview, columns=columns, show="headings", height=5, style="PvZ.Treeview", selectmode="browse")
        for key, label, width in zip(columns, ("模型 / 任务", "状态", "最近调用耗时", "周期 / 近60秒频率", "累计", "距触发"),
                                     (170, 90, 115, 190, 58, 65)):
            self.table.heading(key, text=label)
            self.table.column(key, width=width, minwidth=width-15, anchor="w" if key == "name" else "center", stretch=True)
        self.table.tag_configure("idle", foreground=MUTED)
        self.table.tag_configure("running", foreground="#386cb0")
        self.table.tag_configure("completed", foreground=ACCENT)
        self.table.tag_configure("failed", foreground="#b0444b")
        for stage, label in STAGES:
            self.table.insert("", "end", iid=stage, values=(label, "未触发", "—", "—", "0", "—"), tags=("idle",))
        self.table.pack(fill="x", padx=18, pady=(10, 8))
        self.details = tk.Label(overview, text="", bg=CARD, fg=MUTED, font=(FONT, 9), anchor="w", justify="left", wraplength=round(760 * scale))
        self.details.pack(fill="x", padx=20)
        self.notice = tk.Label(overview, text="", bg="#edf6f4", fg="#235e58", font=(FONT, 10), anchor="w",
                               justify="left", wraplength=round(760 * scale), padx=12, pady=11)
        self.notice.pack(fill="x", padx=18, pady=(10, 6))
        self.coach = tk.Label(overview, text="", bg=CARD, fg=MUTED, font=(FONT, 9), anchor="w", justify="left",
                              wraplength=round(760 * scale))
        self.coach.pack(fill="x", padx=20, pady=(0, 10))
        self.text_boxes, self.last_text = {}, {}
        for key, label, hint in (
                ("decisions", "规划与决策", "Planner 给目标与预算，Jev 决定种植，代码自动拾取阳光；下发与回执单独展示。"),
                ("review", "赛后复盘", "胜负后自动复盘；暂停或结束客户端不代表游戏失败。"),
                ("playbook", "当前策略", "查看当前生效策略与周期。Coach 建议不会直接替换现有策略。"),
                ("mechanics", "游戏图鉴", "基础血量、伤害、速度和时间，仅供规划参考；不计算实时剩余血量。")):
            page = tk.Frame(tabs, bg=CARD)
            tabs.add(page, text=label)
            tk.Label(page, text=hint, bg=CARD, fg=MUTED, font=(FONT, 10), anchor="w", wraplength=round(760 * scale),
                     justify="left").pack(fill="x", padx=18, pady=(16, 10))
            box = ScrolledText(page, font=(FONT, 10), bg=CARD, fg=INK, wrap="word", relief="flat", bd=0,
                               padx=8, pady=8, spacing1=3, spacing3=5, insertbackground=ACCENT)
            box.pack(fill="both", expand=True, padx=12, pady=(0, 12))
            box.configure(state="disabled")
            self.text_boxes[key] = box

    def set_buttons(self, states):
        for label, enabled in states.items():
            self.buttons[label].configure(state="normal" if enabled else "disabled",
                                         bg=self.button_colors[label] if enabled else "#edf1f5",
                                         disabledforeground="#8794a0")

    def update(self, state, metrics, samples, message, coach, details, retained=False, terminal=None):
        color = ("#e5eeeb", ACCENT) if state == "运行中" else ("#fff3de", "#95600d") if state == "已暂停" else ("#e4ebf1", MUTED)
        suffix = " · 游戏胜利" if terminal == "win" else " · 游戏失败" if terminal == "lose" else ""
        self.badge.configure(text=state+suffix, bg=color[0], fg=color[1])
        for key, (value, note) in metrics.items():
            self.metric_values[key].configure(text=value)
            self.metric_notes[key].configure(text=note)
        self.scope.configure(text="上次连接统计 · 已冻结" if retained else "本次连接 · 含排队、推理及返回")
        reasons = []
        for stage, label in STAGES:
            sample = samples[stage]
            self.table.item(stage, values=(label, *model_row(sample)), tags=(sample["status"],))
            if sample["status"] == "failed" and sample.get("reason"):
                reasons.append(label + "：" + sample["reason"])
        selected = self.table.selection()
        if selected:
            stage = selected[0]
            reason = dict(STAGES)[stage] + "：" + (samples[stage].get("reason") or "暂无额外说明")
        elif reasons:
            reason = "\n".join(reasons[:2])
        else:
            reason = "点击模型行查看触发原因；周期不代表每轮一定发起请求。"
            if samples["jev"]["status"] == "idle" and samples["jev"].get("reason") not in {None, "等待触发"}:
                reason = "Jev：" + samples["jev"]["reason"]
        self.details.configure(text=reason)
        self.notice.configure(text=message)
        self.coach.configure(text=coach)
        for key, text in details.items():
            if self.last_text.get(key) == text:
                continue
            box = self.text_boxes[key]
            scroll = box.yview()[0]
            box.configure(state="normal")
            box.delete("1.0", "end")
            box.insert("1.0", text)
            box.configure(state="disabled")
            box.yview_moveto(scroll)
            self.last_text[key] = text
