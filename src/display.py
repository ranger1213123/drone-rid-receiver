"""
实时显示模块 - 终端表格展示所有活跃无人机

使用 ANSI 转义码实现原地刷新，避免刷屏。
"""

import os
import sys
from datetime import datetime
from typing import List, Dict


class Display:
    """终端实时显示"""

    # ANSI 颜色
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    RESET = "\033[0m"
    CLEAR = "\033[2J\033[H"

    LEVEL_COLORS = {
        "warning":  YELLOW,
        "severe":   RED,
        "critical": RED + BOLD,
    }

    STATUS_ICONS = {
        "active":   "●",
        "warning":  "⚠",
        "severe":   "▲",
        "critical": "■",
        "gone":     "○",
    }

    def __init__(self, thresholds: Dict[str, float]):
        self.thresholds = thresholds
        self.frame_count = 0

    def refresh(self, drones: List[Dict], alert_drones: Dict[str, str]):
        """
        刷新显示

        drones: 活跃无人机列表
        alert_drones: {drone_id: alert_level} 当前告警中的无人机
        """
        self.frame_count += 1
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 清屏并绘制
        output = [self.CLEAR]
        output.append(f"{self.BOLD}{self.CYAN}╔{'═'*70}╗{self.RESET}")
        output.append(f"{self.BOLD}{self.CYAN}║{'无人机 RID 接收与电力线防碰撞监控系统':^58}║{self.RESET}")
        output.append(f"{self.BOLD}{self.CYAN}╚{'═'*70}╝{self.RESET}")
        output.append(f"更新时间: {now}  |  帧: #{self.frame_count}  |  活跃无人机: {len(drones)}")
        output.append("")

        if not drones:
            output.append(f"  {self.GREEN}等待 RID 广播数据...{self.RESET}")
        else:
            # 表格头
            header = (
                f"  {'ID':<16} {'类型':<10} {'纬度':>10} {'经度':>11} "
                f"{'高度':>7} {'距离':>7} {'状态':^6}"
            )
            output.append(f"{self.BOLD}{header}{self.RESET}")
            output.append(f"  {'─'*16} {'─'*10} {'─'*10} {'─'*11} {'─'*7} {'─'*7} {'─'*6}")

            for drone in drones:
                did = drone.get("id", "?")[:16]
                ua_type = drone.get("ua_type", "多旋翼")
                lat = drone.get("last_lat", 0) or 0
                lon = drone.get("last_lon", 0) or 0
                alt = drone.get("last_alt", 0) or 0
                dist = drone.get("min_distance")
                status = drone.get("status", "active")

                # 确定颜色
                if did in alert_drones:
                    level = alert_drones[did]
                    color = self.LEVEL_COLORS.get(level, self.YELLOW)
                else:
                    color = self.GREEN

                icon = self.STATUS_ICONS.get(status, "?")

                dist_str = f"{dist:.1f}m" if dist is not None else "-"
                status_str = f"{icon} {status}" if status != "active" else f"{icon}"

                line = (
                    f"{color}  {did:<16} {ua_type:<10} "
                    f"{lat:>10.5f} {lon:>11.5f} "
                    f"{alt:>6.0f}m {dist_str:>7} {status_str:^6}{self.RESET}"
                )
                output.append(line)

        # 告警级别图例
        output.append("")
        output.append(f"  阈值: {self.YELLOW}≤{self.thresholds.get('warning','?')}m{self.RESET} 记录轨迹  "
                      f"{self.RED}≤{self.thresholds.get('severe','?')}m{self.RESET} 严重警告  "
                      f"{self.RED}{self.BOLD}≤{self.thresholds.get('critical','?')}m{self.RESET} 驱离+通知")

        # 输出到终端
        sys.stdout.write("\n".join(output) + "\n")
        sys.stdout.flush()


class SimpleDisplay:
    """简易显示 - 不依赖 ANSI，纯文本输出"""

    def __init__(self, thresholds: Dict[str, float]):
        self.thresholds = thresholds

    def refresh(self, drones: List[Dict], alert_drones: Dict[str, str]):
        now = datetime.now().strftime("%H:%M:%S")
        print(f"\n=== 无人机 RID 监控 [{now}] 活跃: {len(drones)} ===")

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
            status = drone.get("status", "active")
            alert_level = alert_drones.get(did, "")

            dist_str = f"{dist:.1f}m" if dist is not None else "-"
            if alert_level:
                tag = f"⚠ {alert_level}"
            else:
                tag = "● OK" if status == "active" else status

            print(f"  {did:<16} {lat:>10.5f} {lon:>11.5f} {alt:>6.0f}m {dist_str:>7} {tag:>8}")
