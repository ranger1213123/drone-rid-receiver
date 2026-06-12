"""
实时显示模块 - ANSI 终端 UI，表格 + 告警日志 + 状态栏
"""

import shutil
import sys
from abc import ABC, abstractmethod
from collections import deque
from datetime import datetime
from typing import List, Dict

# 强制 UTF-8 输出，避免 Windows GBK 终端下 Unicode 编码错误
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass


class DisplayBackend(ABC):
    """显示后端抽象基类"""

    def __init__(self, thresholds: Dict[str, float]):
        self.thresholds = thresholds

    @abstractmethod
    def refresh(self, drones: List[Dict], alert_drones: Dict[str, str]):
        ...

    @abstractmethod
    def add_alert(self, drone_id: str, level: str, distance: float, line_name: str):
        ...


class Display(DisplayBackend):
    """终端实时显示 — 自适应宽度，分区域布局"""

    # ANSI
    R = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    _COLORS = {
        "critical": "\033[91m",
        "severe":   "\033[38;5;215m",
        "warning":  "\033[93m",
        "info":     "\033[96m",
        "ok":       "\033[92m",
        "muted":    "\033[90m",
    }

    _ICONS = {"active": "*", "warning": "W", "severe": "S", "critical": "X", "gone": "o"}
    _STATUS_TEXT = {"warning": "警告", "severe": "严重", "critical": "驱离"}

    def __init__(self, thresholds: Dict[str, float]):
        super().__init__(thresholds)
        self.frame = 0
        self._alerts: deque = deque(maxlen=50)
        self._w = 80

    def add_alert(self, drone_id: str, level: str, distance: float, line_name: str):
        self._alerts.appendleft((
            datetime.now().strftime("%H:%M:%S"),
            drone_id, level, distance, line_name,
        ))

    def refresh(self, drones: List[Dict], alert_drones: Dict[str, str]):
        self.frame += 1
        self._w = shutil.get_terminal_size().columns
        w = self._w
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        out = []
        c = self._c  # shorthand

        # ── 光标隐藏 + 上移 ──
        out.append("\033[?25l")

        # ═══════ 标题栏 ═══════
        title = f"Drone RID 监控  |  {now}  |  帧 #{self.frame}"
        out.append(c("bold", "cyan"))
        out.append(f"┌─ {title} " + "─" * max(1, w - len(f"┌─ {title} ") - 1) + "┐")
        out.append(c("r"))

        # ═══════ 统计行 ═══════
        count_critical = sum(1 for v in alert_drones.values() if v == "critical")
        count_severe = sum(1 for v in alert_drones.values() if v == "severe")
        count_warning = sum(1 for v in alert_drones.values() if v == "warning")
        pl_count = len({d.get("nearest_line_id") for d in drones if d.get("nearest_line_id")})

        stats = f"活跃: {len(drones)}"
        if count_critical:
            stats += f"  │  {c('critical')}驱离: {count_critical}{c('r')}"
        if count_severe:
            stats += f"  │  {c('severe')}严重: {count_severe}{c('r')}"
        if count_warning:
            stats += f"  │  {c('warning')}警告: {count_warning}{c('r')}"
        stats += f"  │  电力线段: {pl_count}"
        out.append(f"│ {stats}" + " " * max(1, w - len(f"│ {stats}") - 1) + "│")

        # ═══════ 分隔 ═══════
        out.append(c("muted") + "├" + "─" * (w - 2) + "┤" + c("r"))

        # ═══════ 无人机表格 ═══════
        if not drones:
            out.append("│" + c("muted") + "  等待 RID 广播数据..." .ljust(w - 2) + c("r") + "│")
        else:
            # 列宽计算
            id_w = min(18, max(8, max((len(d.get("id", "")[:18]) for d in drones), default=8)))
            lat_w = 10
            lon_w = 11
            alt_w = 6
            dist_w = 8
            line_w = max(4, min(16, w - id_w - lat_w - lon_w - alt_w - dist_w - 14))

            header = (
                f"│ {c('bold')}"
                f"{'ID':<{id_w}} {'纬度':>{lat_w}} {'经度':>{lon_w}} "
                f"{'高度':>{alt_w}} {'最近电力线':<{line_w}} {'距离':>{dist_w}}"
                f"{c('r')} │"
            )
            out.append(header)

            for drone in drones[:20]:  # max 20 rows
                did = drone.get("id", "?")[:id_w]
                lat = drone.get("last_lat", 0) or 0
                lon = drone.get("last_lon", 0) or 0
                alt = drone.get("last_alt", 0) or 0
                dist = drone.get("min_distance")
                status = drone.get("status", "active")

                # 告警颜色
                level = alert_drones.get(did, "")
                color = self._COLORS.get(level, self._COLORS["ok"])
                icon = self._ICONS.get(level or status, self._ICONS["active"])

                dist_str = f"{dist:.0f}m" if dist is not None else "-"
                alt_str = f"{alt:.0f}m"

                # 最近电力线名称
                line_name = drone.get("line_name", "-")[:line_w]

                row = (
                    f"│ {color}{icon} {did:<{id_w - 2}} "
                    f"{lat:>{lat_w}.5f} {lon:>{lon_w}.5f} "
                    f"{alt_str:>{alt_w}} {line_name:<{line_w}} "
                    f"{dist_str:>{dist_w}}{self.R} │"
                )
                out.append(row)

        # ═══════ 告警日志 ═══════
        if self._alerts:
            out.append(c("muted") + "├" + "─" * (w - 2) + "┤" + c("r"))
            log_lines = min(4, len(self._alerts))
            for i in range(log_lines):
                ts, did, level, dist, line = list(self._alerts)[i]
                color = self._COLORS.get(level, self._COLORS["info"])
                icon = self._ICONS.get(level, "!")
                msg = f"{icon} {ts}  {did}  距 {line}  {dist:.1f}m  [{self._STATUS_TEXT.get(level, level)}]"
                out.append(f"│ {color}{msg}{self.R}" + " " * max(1, w - len(f"│ {msg}") - 1) + "│")

        # ═══════ 底部状态 ═══════
        out.append(c("muted") + "├" + "─" * (w - 2) + "┤" + c("r"))
        t = self.thresholds
        footer = (
            f"{self._ICONS['warning']}<= {t.get('warning','?')}m 轨迹  "
            f"{self._ICONS['severe']}<= {t.get('severe','?')}m 严重  "
            f"{self._ICONS['critical']}<= {t.get('critical','?')}m 驱离"
        )
        out.append(f"│ {c('muted')}{footer}{c('r')}" + " " * max(1, w - len(f"│ {footer}") - 1) + "│")
        out.append(c("muted") + "└" + "─" * (w - 2) + "┘" + c("r"))

        # 输出
        sys.stdout.write("\n".join(out) + "\n")
        sys.stdout.flush()
        # 光标回到顶部
        total_lines = len(out)
        sys.stdout.write(f"\033[{total_lines}A")

    def _c(self, *args: str) -> str:
        """快捷颜色: _c('bold','cyan'), _c('critical'), _c('r')"""
        result = ""
        for a in args:
            if a == "bold":
                result += self.BOLD
            elif a == "r":
                result += self.R
            elif a in self._COLORS:
                result += self._COLORS[a]
        return result


class SimpleDisplay(DisplayBackend):
    """简易显示 — 非 TTY 或管道模式下的纯文本输出"""

    def __init__(self, thresholds: Dict[str, float]):
        super().__init__(thresholds)

    def add_alert(self, drone_id: str, level: str, distance: float, line_name: str):
        pass  # 非交互模式不缓存

    def refresh(self, drones: List[Dict], alert_drones: Dict[str, str]):
        now = datetime.now().strftime("%H:%M:%S")
        print(f"\n=== Drone RID [{now}] 活跃: {len(drones)} ===")
        if not drones:
            print("  等待 RID 广播...")
            return
        print(f"  {'ID':<16} {'纬度':>10} {'经度':>11} {'高度':>7} {'距离':>7} {'状态':>8}")
        print(f"  {'-'*16} {'-'*10} {'-'*11} {'-'*7} {'-'*7} {'-'*8}")
        for drone in drones:
            did = drone.get("id", "?")[:16]
            lat = drone.get("last_lat", 0) or 0
            lon = drone.get("last_lon", 0) or 0
            alt = drone.get("last_alt", 0) or 0
            dist = drone.get("min_distance")
            level = alert_drones.get(did, "")
            dist_str = f"{dist:.0f}m" if dist is not None else "-"
            tag = f"! {level}" if level else "OK"
            print(f"  {did:<16} {lat:>10.5f} {lon:>11.5f} {alt:>6.0f}m {dist_str:>7} {tag:>8}")
