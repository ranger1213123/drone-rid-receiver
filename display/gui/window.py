"""
GUI 主窗口 - 无人机 RID 接收与电力线防碰撞监控系统

布局:
┌──────────────────────────────────────────────────────┐
│  标题栏: 系统状态 + 模式选择                           │
├────────┬─────────────────────────────────────────────┤
│ 控制   │  无人机实时列表 (Treeview)                   │
│ 面板   │  ┌──────────────────────────────────────┐  │
│        │  │ ID │类型│纬度│经度│高度│距离│状态│时间│  │
│ [开始] │  └──────────────────────────────────────┘  │
│ [停止] │                                            │
│        │  告警日志 (Text)                            │
│ 电力线 │  ┌──────────────────────────────────────┐  │
│ [管理] │  │ [11:30:02] [W] DRONE-001 距高压线A... │  │
│ [导入] │  └──────────────────────────────────────┘  │
│ [导出] │                                            │
│        │  轨迹查看 (下级窗口)                         │
│ 阈值   │  [查看轨迹]                                 │
│ 200m   │                                            │
│ 100m   │  状态栏: 活跃: 3 | 告警: 2 | 上次更新: ...  │
│ 50m    │                                            │
└────────┴─────────────────────────────────────────────┘
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading
import time
import os
import yaml
from datetime import datetime
from typing import Dict, Optional

# 添加项目路径
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from storage.database import Database
from core.powerline import PowerLineManager
from core.alert import AlertSystem
from core.trajectory import TrajectoryRecorder
from core.pipeline import RIDPipeline
from display.gui.powerline import PowerLineDialog


# ─────────────────── Catppuccin Mocha 主题 ───────────────────

THEME = {
    "bg":       "#1e1e2e",
    "bg2":      "#313244",
    "bg3":      "#45475a",
    "fg":       "#cdd6f4",
    "fg2":      "#a6adc8",
    "fg3":      "#6c7086",
    "red":      "#f38ba8",
    "green":    "#a6e3a1",
    "yellow":   "#f9e2af",
    "blue":     "#89b4fa",
    "mauve":    "#cba6f7",
    "teal":     "#94e2d5",
    "peach":    "#fab387",
    "surface0": "#313244",
    "surface1": "#45475a",
}


class MainWindow(tk.Tk):
    """主窗口"""

    def __init__(self, config: dict):
        super().__init__()

        self.config = config
        self.title("无人机 RID 接收与电力线防碰撞监控系统")
        self.geometry("1100x700")
        self.minsize(900, 550)
        self.configure(bg=THEME["bg"])

        # ─── 初始化后端组件 ───
        db_path = config.get("database", {}).get("path", "data/drone_rid.db")
        if not os.path.isabs(db_path):
            db_path = os.path.join(os.path.dirname(__file__), "..", "..", db_path)
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db = Database(db_path)

        self.pl_manager = PowerLineManager()
        pl_file = config.get("power_lines_file", "config/power_lines.yaml")
        if not os.path.isabs(pl_file):
            pl_file = os.path.join(os.path.dirname(__file__), "..", "..", pl_file)
        try:
            count = self.pl_manager.load_from_yaml(pl_file)
        except Exception:
            count = 0
        self.db.load_power_lines([
            {"name": l.name, "lat1": l.lat1, "lon1": l.lon1, "alt1": l.alt1,
             "lat2": l.lat2, "lon2": l.lon2, "alt2": l.alt2, "id": l.line_id}
            for l in self.pl_manager.lines
        ])

        thresholds = config.get("thresholds", {"warning": 200, "severe": 100, "critical": 50})
        self.thresholds = thresholds

        self.alert_system = AlertSystem(
            db=self.db,
            thresholds=thresholds,
        )

        traj_cfg = config.get("trajectory", {})
        self.trajectory_recorder = TrajectoryRecorder(
            db=self.db,
            min_interval=traj_cfg.get("min_interval", 2.0),
            max_points_per_drone=traj_cfg.get("max_points_per_drone", 1000),
        )

        self.pipeline = RIDPipeline(
            db=self.db,
            pl_manager=self.pl_manager,
            alert_system=self.alert_system,
            trajectory_recorder=self.trajectory_recorder,
            thresholds=thresholds,
        )

        # 运行状态
        self.is_running = False
        self.receiver_thread = None
        self.receiver = None
        self.current_mode = "ble"

        # ─── 构建 UI ───
        self._build_ui()

        # ─── 定时刷新 ───
        self._refresh_display()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ═══════════════════════════════════════════════════
    # UI 构建
    # ═══════════════════════════════════════════════════

    def _build_ui(self):
        """构建完整 UI"""
        # 顶部标题栏
        self._build_header()

        # 主内容区
        main = tk.Frame(self, bg=THEME["bg"])
        main.pack(fill=tk.BOTH, expand=True, padx=8, pady=(5, 0))

        # 左侧控制面板 (200px)
        self._build_control_panel(main)

        # 右侧内容区
        right = tk.Frame(main, bg=THEME["bg"])
        right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(5, 0))

        # 无人机列表
        self._build_drone_table(right)

        # 告警日志
        self._build_alert_log(right)

        # 底部状态栏
        self._build_status_bar()

    def _build_header(self):
        """顶部标题栏"""
        header = tk.Frame(self, bg=THEME["bg2"], height=48)
        header.pack(fill=tk.X, padx=8, pady=(8, 0))
        header.pack_propagate(False)

        # 标题
        title = tk.Label(
            header, text="无人机 RID 接收与电力线防碰撞监控系统",
            font=("Microsoft YaHei", 13, "bold"),
            bg=THEME["bg2"], fg=THEME["fg"]
        )
        title.pack(side=tk.LEFT, padx=15, pady=10)

        # 模式选择
        mode_frame = tk.Frame(header, bg=THEME["bg2"])
        mode_frame.pack(side=tk.RIGHT, padx=15)

        tk.Label(mode_frame, text="模式:", bg=THEME["bg2"], fg=THEME["fg2"],
                 font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)

        self.mode_var = tk.StringVar(value="ble")
        mode_combo = ttk.Combobox(
            mode_frame, textvariable=self.mode_var,
            values=["ble", "wifi"], state="readonly",
            width=8, font=("Microsoft YaHei", 9)
        )
        mode_combo.pack(side=tk.LEFT, padx=5)

        # 状态指示灯
        self.status_indicator = tk.Label(
            header, text="已停止", font=("Microsoft YaHei", 10, "bold"),
            bg=THEME["bg2"], fg=THEME["fg3"]
        )
        self.status_indicator.pack(side=tk.RIGHT, padx=15)

    def _build_control_panel(self, parent):
        """左侧控制面板"""
        panel = tk.Frame(parent, bg=THEME["bg2"], width=210)
        panel.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 5))
        panel.pack_propagate(False)

        pad = {"padx": 12, "pady": 3}

        # 扫描控制
        tk.Label(panel, text="━━ 扫描控制 ━━", bg=THEME["bg2"],
                 fg=THEME["fg"], font=("Microsoft YaHei", 10, "bold")
                 ).pack(pady=(15, 10))

        self.btn_start = tk.Button(
            panel, text="开始扫描", command=self._start_scanning,
            bg=THEME["green"], fg=THEME["bg"],
            font=("Microsoft YaHei", 10, "bold"),
            relief=tk.FLAT, padx=16, pady=8, activebackground=THEME["teal"]
        )
        self.btn_start.pack(fill=tk.X, **pad)

        self.btn_stop = tk.Button(
            panel, text="停止扫描", command=self._stop_scanning,
            bg=THEME["surface1"], fg=THEME["fg"],
            font=("Microsoft YaHei", 10),
            relief=tk.FLAT, padx=16, pady=8,
            state=tk.DISABLED
        )
        self.btn_stop.pack(fill=tk.X, **pad)

        # 告警阈值
        tk.Label(panel, text="━━ 告警阈值 ━━", bg=THEME["bg2"],
                 fg=THEME["fg"], font=("Microsoft YaHei", 10, "bold")
                 ).pack(pady=(20, 10))

        self._threshold_labels = {}
        for key, color, label in [
            ("warning", THEME["yellow"], "[W] 警告"),
            ("severe", THEME["peach"], "[S] 严重"),
            ("critical", THEME["red"], "[X] 危险"),
        ]:
            val = self.thresholds.get(key, "?")
            lbl = tk.Label(
                panel, text=f"{label}  ≤{val}m",
                bg=THEME["bg2"], fg=color,
                font=("Microsoft YaHei", 10, "bold")
            )
            lbl.pack(**pad)
            self._threshold_labels[key] = lbl

        # 电力线管理
        tk.Label(panel, text="━━ 电力线管理 ━━", bg=THEME["bg2"],
                 fg=THEME["fg"], font=("Microsoft YaHei", 10, "bold")
                 ).pack(pady=(20, 10))

        tk.Button(
            panel, text="管理电力线", command=self._open_powerline_dialog,
            bg=THEME["blue"], fg=THEME["bg"],
            font=("Microsoft YaHei", 10, "bold"),
            relief=tk.FLAT, padx=12, pady=7
        ).pack(fill=tk.X, **pad)

        tk.Button(
            panel, text="导入 YAML", command=self._import_powerlines,
            bg=THEME["surface0"], fg=THEME["fg"],
            font=("Microsoft YaHei", 9),
            relief=tk.FLAT, padx=12, pady=6
        ).pack(fill=tk.X, **pad)

        tk.Button(
            panel, text="导出 YAML", command=self._export_powerlines,
            bg=THEME["surface0"], fg=THEME["fg"],
            font=("Microsoft YaHei", 9),
            relief=tk.FLAT, padx=12, pady=6
        ).pack(fill=tk.X, **pad)

        # 轨迹
        tk.Label(panel, text="━━ 轨迹 ━━", bg=THEME["bg2"],
                 fg=THEME["fg"], font=("Microsoft YaHei", 10, "bold")
                 ).pack(pady=(20, 10))

        tk.Button(
            panel, text="查看轨迹", command=self._view_trajectory,
            bg=THEME["mauve"], fg=THEME["bg"],
            font=("Microsoft YaHei", 10),
            relief=tk.FLAT, padx=12, pady=7
        ).pack(fill=tk.X, **pad)

        # 线数显示
        self.pl_count_label = tk.Label(
            panel, text=f"已加载 {len(self.pl_manager.lines)} 条电力线",
            bg=THEME["bg2"], fg=THEME["fg3"],
            font=("Microsoft YaHei", 8)
        )
        self.pl_count_label.pack(pady=(20, 10))

    def _build_drone_table(self, parent):
        """无人机实时列表"""
        table_frame = tk.LabelFrame(
            parent, text=" 无人机实时列表 ",
            font=("Microsoft YaHei", 10, "bold"),
            bg=THEME["bg"], fg=THEME["fg"],
            foreground=THEME["fg"]
        )
        table_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 5))

        # Treeview
        columns = ("id", "type", "lat", "lon", "alt", "dist", "status", "time")
        self.drone_tree = ttk.Treeview(
            table_frame, columns=columns, show="headings", height=8
        )
        self.drone_tree.heading("id", text="无人机 ID")
        self.drone_tree.heading("type", text="类型")
        self.drone_tree.heading("lat", text="纬度")
        self.drone_tree.heading("lon", text="经度")
        self.drone_tree.heading("alt", text="高度")
        self.drone_tree.heading("dist", text="距离")
        self.drone_tree.heading("status", text="状态")
        self.drone_tree.heading("time", text="最后更新")

        self.drone_tree.column("id", width=140)
        self.drone_tree.column("type", width=80)
        self.drone_tree.column("lat", width=100)
        self.drone_tree.column("lon", width=100)
        self.drone_tree.column("alt", width=70)
        self.drone_tree.column("dist", width=70)
        self.drone_tree.column("status", width=70)
        self.drone_tree.column("time", width=120)

        # 滚动条
        vsb = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.drone_tree.yview)
        self.drone_tree.configure(yscrollcommand=vsb.set)

        # Tag 颜色配置
        self.drone_tree.tag_configure("critical", foreground=THEME["red"])
        self.drone_tree.tag_configure("severe", foreground=THEME["peach"])
        self.drone_tree.tag_configure("warning", foreground=THEME["yellow"])
        self.drone_tree.tag_configure("active", foreground=THEME["green"])
        self.drone_tree.tag_configure("gone", foreground=THEME["fg3"])

        self.drone_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

    def _build_alert_log(self, parent):
        """告警日志面板"""
        log_frame = tk.LabelFrame(
            parent, text=" 告警日志 ",
            font=("Microsoft YaHei", 10, "bold"),
            bg=THEME["bg"], fg=THEME["fg"],
            foreground=THEME["fg"]
        )
        log_frame.pack(fill=tk.BOTH, expand=False, pady=(0, 5))

        # 日志文本框
        self.alert_text = tk.Text(
            log_frame, height=8, wrap=tk.WORD,
            bg=THEME["bg2"], fg=THEME["fg"],
            font=("Consolas", 9),
            insertbackground=THEME["fg"],
            relief=tk.FLAT, padx=8, pady=5,
            state=tk.DISABLED
        )
        self.alert_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 滚动条
        log_vsb = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.alert_text.yview)
        self.alert_text.configure(yscrollcommand=log_vsb.set)
        log_vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # 颜色 tag
        self.alert_text.tag_configure("critical", foreground=THEME["red"])
        self.alert_text.tag_configure("severe", foreground=THEME["peach"])
        self.alert_text.tag_configure("warning", foreground=THEME["yellow"])
        self.alert_text.tag_configure("info", foreground=THEME["fg2"])
        self.alert_text.tag_configure("time", foreground=THEME["fg3"])

    def _build_status_bar(self):
        """底部状态栏"""
        status = tk.Frame(self, bg=THEME["bg2"], height=30)
        status.pack(fill=tk.X, padx=8, pady=(3, 8))
        status.pack_propagate(False)

        self.status_drones = tk.Label(
            status, text="活跃无人机: 0", bg=THEME["bg2"], fg=THEME["fg2"],
            font=("Microsoft YaHei", 9)
        )
        self.status_drones.pack(side=tk.LEFT, padx=15)

        self.status_alerts = tk.Label(
            status, text="告警中: 0", bg=THEME["bg2"], fg=THEME["fg2"],
            font=("Microsoft YaHei", 9)
        )
        self.status_alerts.pack(side=tk.LEFT, padx=15)

        self.status_update = tk.Label(
            status, text="上次更新: --", bg=THEME["bg2"], fg=THEME["fg3"],
            font=("Microsoft YaHei", 9)
        )
        self.status_update.pack(side=tk.RIGHT, padx=15)

    # ═══════════════════════════════════════════════════
    # 扫描控制
    # ═══════════════════════════════════════════════════

    def _start_scanning(self):
        """启动扫描"""
        mode = self.mode_var.get()
        self.current_mode = mode

        self.is_running = True
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.status_indicator.config(
            text=f"运行中 ({mode.upper()})", fg=THEME["green"]
        )

        self._log_alert("[系统] 扫描已启动", "info")

        # 在后台线程启动接收器
        self.receiver_thread = threading.Thread(
            target=self._run_receiver, args=(mode,), daemon=True
        )
        self.receiver_thread.start()

    def _stop_scanning(self):
        """停止扫描"""
        self.is_running = False
        self.btn_start.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)
        self.status_indicator.config(
            text="已停止", fg=THEME["fg3"]
        )
        self._log_alert("[系统] 扫描已停止", "info")

    def _run_receiver(self, mode: str):
        """在后台线程中运行接收器"""
        from receiver.ble import BLE_RIDReceiver

        if mode == "ble":
            self.receiver = BLE_RIDReceiver(
                callback=self._on_rid_data,
                scan_duration=5.0,
            )
        elif mode == "wifi":
            try:
                from receiver.wifi import create_wifi_receiver
                self.receiver = create_wifi_receiver(
                    callback=self._on_rid_data,
                )
            except Exception as e:
                self.after(0, lambda: self._log_alert(
                    f"[错误] WiFi 接收器初始化失败: {e}", "critical"
                ))
                self.after(0, self._stop_scanning)
                return

        # 在 asyncio 事件循环中运行
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def runner():
            await self.receiver.start()

        try:
            loop.run_until_complete(runner())
        except Exception as e:
            self.after(0, lambda: self._log_alert(
                f"[错误] {e}", "critical"
            ))
        finally:
            loop.close()

    # ═══════════════════════════════════════════════════
    # 数据处理
    # ═══════════════════════════════════════════════════

    def _on_rid_data(self, parsed):
        """收到 RID 数据 (在接收器线程中回调)"""
        if not self.is_running:
            return

        result = self.pipeline.process(parsed)
        if result is None:
            return

        if result.alert_level:
            tag = {"critical": "[X]", "severe": "[S]", "warning": "[W]"}.get(result.alert_level, "!")
            self.after(0, lambda l=result.alert_level, did=result.drone_id,
                       d=result.distance, n=result.nearest_line.name:
                       self._log_alert(
                           f"{tag} [{l}] {did} 距离 {n} {d:.0f}m", l
                       ))

    def _log_alert(self, message: str, level: str = "info"):
        """向告警日志添加条目"""
        self.alert_text.config(state=tk.NORMAL)
        now = datetime.now().strftime("%H:%M:%S")

        self.alert_text.insert(tk.END, f"[{now}] ", "time")
        self.alert_text.insert(tk.END, f"{message}\n", level)

        # 限制日志行数
        lines = int(self.alert_text.index('end-1c').split('.')[0])
        if lines > 500:
            self.alert_text.delete('1.0', f'{lines - 400}.0')

        self.alert_text.see(tk.END)
        self.alert_text.config(state=tk.DISABLED)

    # ═══════════════════════════════════════════════════
    # 定时刷新
    # ═══════════════════════════════════════════════════

    def _refresh_display(self):
        """定时刷新无人机列表和状态栏"""
        try:
            drones = self.db.get_active_drones()
            alert_drones = dict(self.alert_system._drone_level)

            # 更新树
            existing = set()
            for item in self.drone_tree.get_children():
                values = self.drone_tree.item(item, "values")
                if values:
                    existing.add(values[0])

            current_ids = set()

            for drone in drones:
                did = drone.get("id", "?")
                current_ids.add(did)
                status = drone.get("status", "active")
                alert_level = alert_drones.get(did, "")
                if alert_level:
                    display_status = f"{'[X]' if alert_level=='critical' else '[S]' if alert_level=='severe' else '[W]'} {alert_level}"
                    tag = alert_level
                else:
                    display_status = "正常"
                    tag = status

                dist = drone.get("min_distance")
                dist_str = f"{dist:.0f}m" if dist is not None else "-"

                last_seen = drone.get("last_seen", "")
                if last_seen:
                    try:
                        dt = datetime.fromisoformat(last_seen)
                        time_str = dt.strftime("%H:%M:%S")
                    except Exception:
                        time_str = last_seen[:19]
                else:
                    time_str = ""

                values = (
                    did,
                    "多旋翼",  # 可从 basic_id 获取
                    f"{drone.get('last_lat', 0) or 0:.5f}",
                    f"{drone.get('last_lon', 0) or 0:.5f}",
                    f"{drone.get('last_alt', 0) or 0:.0f}m",
                    dist_str,
                    display_status,
                    time_str,
                )

                if did in existing:
                    # 更新
                    for item in self.drone_tree.get_children():
                        if self.drone_tree.item(item, "values")[0] == did:
                            self.drone_tree.item(item, values=values, tags=(tag,))
                            break
                else:
                    # 新增
                    self.drone_tree.insert(
                        "", tk.END, values=values, tags=(tag,)
                    )

            # 删除不存在的
            for did in existing - current_ids:
                for item in self.drone_tree.get_children():
                    if self.drone_tree.item(item, "values")[0] == did:
                        self.drone_tree.delete(item)

            # 状态栏
            alert_count = len(alert_drones)
            self.status_drones.config(text=f"活跃无人机: {len(drones)}")
            self.status_alerts.config(
                text=f"告警中: {alert_count}",
                fg=THEME["red"] if alert_count > 0 else THEME["fg2"]
            )
            self.status_update.config(
                text=f"上次更新: {datetime.now().strftime('%H:%M:%S')}"
            )

        except Exception as e:
            pass

        self.after(1000, self._refresh_display)

    # ═══════════════════════════════════════════════════
    # 电力线管理
    # ═══════════════════════════════════════════════════

    def _open_powerline_dialog(self):
        """打开电力线管理对话框"""
        # 准备数据
        pl_data = [
            {
                "name": l.name, "lat1": l.lat1, "lon1": l.lon1, "alt1": l.alt1,
                "lat2": l.lat2, "lon2": l.lon2, "alt2": l.alt2
            }
            for l in self.pl_manager.lines
        ]

        def on_save(lines):
            self.pl_manager.load_from_list(lines)
            self.db.load_power_lines([
                {"name": l["name"], "lat1": l["lat1"], "lon1": l["lon1"],
                 "alt1": l["alt1"], "lat2": l["lat2"], "lon2": l["lon2"],
                 "alt2": l["alt2"], "id": i}
                for i, l in enumerate(lines, 1)
            ])
            self.pl_count_label.config(text=f"已加载 {len(self.pl_manager.lines)} 条电力线")
            self._log_alert(f"[系统] 电力线已更新: {len(lines)} 条", "info")

        PowerLineDialog(self, pl_data, on_save=on_save)

    def _import_powerlines(self):
        """从 YAML 文件导入电力线"""
        filename = filedialog.askopenfilename(
            title="导入电力线配置",
            filetypes=[("YAML 文件", "*.yaml *.yml"), ("所有文件", "*.*")]
        )
        if not filename:
            return

        try:
            count = self.pl_manager.load_from_yaml(filename)
            self.db.load_power_lines([
                {"name": l.name, "lat1": l.lat1, "lon1": l.lon1, "alt1": l.alt1,
                 "lat2": l.lat2, "lon2": l.lon2, "alt2": l.alt2, "id": l.line_id}
                for l in self.pl_manager.lines
            ])
            self.pl_count_label.config(text=f"已加载 {count} 条电力线")
            self._log_alert(f"[系统] 已从文件导入 {count} 条电力线", "info")
            messagebox.showinfo("导入成功", f"已导入 {count} 条电力线段")
        except Exception as e:
            messagebox.showerror("导入失败", str(e))

    def _export_powerlines(self):
        """导出电力线到 YAML 文件"""
        filename = filedialog.asksaveasfilename(
            title="导出电力线配置",
            defaultextension=".yaml",
            filetypes=[("YAML 文件", "*.yaml"), ("所有文件", "*.*")]
        )
        if not filename:
            return

        try:
            pl_data = {
                "power_lines": [
                    {
                        "name": l.name,
                        "lat1": l.lat1, "lon1": l.lon1, "alt1": l.alt1,
                        "lat2": l.lat2, "lon2": l.lon2, "alt2": l.alt2,
                    }
                    for l in self.pl_manager.lines
                ]
            }
            with open(filename, 'w', encoding='utf-8') as f:
                yaml.dump(pl_data, f, allow_unicode=True, default_flow_style=False)
            self._log_alert(f"[系统] 已导出 {len(self.pl_manager.lines)} 条电力线", "info")
            messagebox.showinfo("导出成功", f"已导出到 {filename}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    # ═══════════════════════════════════════════════════
    # 轨迹查看
    # ═══════════════════════════════════════════════════

    def _view_trajectory(self):
        """查看选中无人机的轨迹"""
        selection = self.drone_tree.selection()
        if not selection:
            # 尝试获取最后一架告警中的无人机
            alert_drones = dict(self.alert_system._drone_level)
            if alert_drones:
                drone_id = list(alert_drones.keys())[0]
            else:
                messagebox.showinfo("提示", "请先在列表中选择一架无人机")
                return
        else:
            drone_id = self.drone_tree.item(selection[0], "values")[0]

        # 获取轨迹数据
        points = self.db.get_trajectory(drone_id, limit=500)
        if not points:
            messagebox.showinfo("无轨迹", f"无人机 {drone_id} 没有轨迹数据")
            return

        # 在新窗口显示轨迹
        self._show_trajectory_window(drone_id, points)

    def _show_trajectory_window(self, drone_id: str, points: list):
        """显示轨迹窗口"""
        win = tk.Toplevel(self)
        win.title(f"轨迹回放 - {drone_id}")
        win.geometry("800x550")
        win.configure(bg=THEME["bg"])

        # 信息头
        header = tk.Frame(win, bg=THEME["bg2"])
        header.pack(fill=tk.X, padx=10, pady=(10, 5))

        min_dist = min(p["distance_to_line"] for p in points)
        max_dist = max(p["distance_to_line"] for p in points)

        info_text = (
            f"无人机: {drone_id}  |  轨迹点数: {len(points)}  |  "
            f"最近距离: {min_dist:.1f}m  |  最远距离: {max_dist:.1f}m  |  "
            f"时间: {points[-1]['timestamp'][:19]} → {points[0]['timestamp'][:19]}"
        )
        tk.Label(header, text=info_text, bg=THEME["bg2"], fg=THEME["fg"],
                 font=("Consolas", 10)).pack(pady=8, padx=10)

        # 轨迹表格
        table_frame = tk.Frame(win, bg=THEME["bg"])
        table_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        cols = ("time", "lat", "lon", "alt", "dist")
        traj_tree = ttk.Treeview(table_frame, columns=cols, show="headings", height=20)
        traj_tree.heading("time", text="时间")
        traj_tree.heading("lat", text="纬度")
        traj_tree.heading("lon", text="经度")
        traj_tree.heading("alt", text="高度(m)")
        traj_tree.heading("dist", text="距离(m)")

        traj_tree.column("time", width=200)
        traj_tree.column("lat", width=130)
        traj_tree.column("lon", width=130)
        traj_tree.column("alt", width=90)
        traj_tree.column("dist", width=90)

        traj_tree.tag_configure("critical", foreground=THEME["red"])
        traj_tree.tag_configure("warning", foreground=THEME["yellow"])

        for p in reversed(points):  # 时间升序
            dist = p["distance_to_line"]
            tag = "critical" if dist <= self.thresholds.get("critical", 50) else \
                  "warning" if dist <= self.thresholds.get("warning", 200) else ""
            traj_tree.insert("", tk.END, values=(
                p["timestamp"][:19],
                f"{p['lat']:.5f}",
                f"{p['lon']:.5f}",
                f"{p['alt']:.1f}",
                f"{dist:.1f}",
            ), tags=(tag,) if tag else ())

        vsb = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=traj_tree.yview)
        traj_tree.configure(yscrollcommand=vsb.set)
        traj_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # 关闭按钮
        tk.Button(
            win, text="关闭", command=win.destroy,
            bg=THEME["surface1"], fg=THEME["fg"],
            font=("Microsoft YaHei", 10),
            relief=tk.FLAT, padx=16, pady=6
        ).pack(pady=10)

    # ═══════════════════════════════════════════════════
    # 关闭
    # ═══════════════════════════════════════════════════

    def _on_close(self):
        """关闭窗口"""
        self.is_running = False
        self.db.close()
        self.destroy()
