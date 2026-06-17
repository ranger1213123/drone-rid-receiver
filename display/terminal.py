"""
实时显示模块 — 事件驱动日志行输出 (不刷屏)
"""

import sys
from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Dict

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


class LogDisplay(DisplayBackend):
    """日志行显示 — 只在有事件时输出一行，不刷屏"""

    _LEVEL_LABEL = {"warning": "警告", "severe": "严重", "critical": "驱离"}

    def __init__(self, thresholds: Dict[str, float]):
        super().__init__(thresholds)
        self._seen: Dict[str, str] = {}
        self._tick = 0
        self._startup = True

    def add_alert(self, drone_id: str, level: str, distance: float, line_name: str):
        pass

    def refresh(self, drones: List[Dict], alert_drones: Dict[str, str]):
        if self._startup:
            self._startup = False
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] 系统启动  "
                  f"≤{self.thresholds.get('warning','?')}m 警告  "
                  f"≤{self.thresholds.get('severe','?')}m 严重  "
                  f"≤{self.thresholds.get('critical','?')}m 驱离")
            return

        changed = False
        seen_now = set()

        for d in drones:
            did = d.get("id", "")
            seen_now.add(did)
            new_level = alert_drones.get(did, "")
            old_level = self._seen.get(did)
            dist = d.get("min_distance")
            dist_str = f"距电力线 {dist:.0f}m" if dist is not None else ""

            if did not in self._seen:
                ts = datetime.now().strftime("%H:%M:%S")
                tag = f" [{self._LEVEL_LABEL.get(new_level)}]" if new_level else ""
                print(f"[{ts}] 发现 {did}  {dist_str}{tag}")
                self._seen[did] = new_level
                changed = True
            elif new_level != old_level:
                ts = datetime.now().strftime("%H:%M:%S")
                if new_level:
                    label = self._LEVEL_LABEL.get(new_level, new_level)
                    print(f"[{ts}] {label}  {did}  {dist_str}")
                else:
                    print(f"[{ts}] 解除  {did}")
                self._seen[did] = new_level
                changed = True

        gone = set(self._seen) - seen_now
        for did in gone:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] 离线  {did}")
            del self._seen[did]
            changed = True

        if changed:
            self._tick = 0
        else:
            self._tick += 1
            if self._tick >= 30:
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"[{ts}] 监听中... 活跃: {len(drones)}")
                self._tick = 0


class SimpleDisplay(DisplayBackend):
    """简易显示 — 非 TTY 或管道模式下的纯文本输出"""

    def __init__(self, thresholds: Dict[str, float]):
        super().__init__(thresholds)

    def add_alert(self, drone_id: str, level: str, distance: float, line_name: str):
        pass

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
