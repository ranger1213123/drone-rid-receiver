"""
模拟 RID 接收器 — 生成虚拟无人机数据用于单机演示和测试

无人机行为模式:
  - 绕飞: 围绕电力线中点做圆周运动, 在不同高度层触发不同告警级别
  - 穿越: 从远处飞近电力线再远离, 模拟无人机穿越场景
  - 徘徊: 在电力线附近随机游走

用法:
  python app/cli.py --mode simulated
  python app/web.py  (在 Web UI 中选择 simulated 模式)
"""

import asyncio
import math
import random
import time
from dataclasses import dataclass, field
from typing import Optional, Callable, List

from logging_config import get_logger
from core.parser import (
    ParsedRID, BasicIDMessage, LocationMessage, SystemMessage,
    ID_TYPE_SERIAL, UA_TYPE_HELICOPTER,
)
from core.powerline import PowerLineManager, PowerLineSegment
from receiver.ble import RIDReceiver

logger = get_logger(__name__)


@dataclass
class SimDrone:
    """模拟无人机状态"""
    drone_id: str
    mac: str
    behavior: str           # "orbit" | "cross" | "wander"
    center_lat: float       # 活动中心纬度
    center_lon: float       # 活动中心经度
    orbit_radius: float     # 绕飞半径 (度)
    base_alt: float         # 基准高度 (m)
    alt_range: float        # 高度变化范围 (m)
    speed: float            # 移动速度 (度/秒)
    angle: float = 0.0      # 当前角度 (绕飞模式)
    phase: float = 0.0      # 穿越模式相位 (0→1)
    phase_dir: float = 1.0  # 穿越方向
    wander_lat: float = 0.0
    wander_lon: float = 0.0
    last_update: float = 0.0
    takeoff_lat: float = 0.0  # 起飞位置 (首次设置后不变)
    takeoff_lon: float = 0.0
    takeoff_alt: float = 0.0


# 预定义无人机 — SN 使用已知前缀以触发型号推断
_DRONE_POOL = [
    # (SN模板, MAC模板, 产品型号)
    ("1581F{id:06X}",      "A4:B1:C2:{b1:02X}:{b2:02X}:{b3:02X}",   "DJI Mini 4 Pro"),
    ("3FMFK{id:06X}",      "D0:E1:F2:{b1:02X}:{b2:02X}:{b3:02X}",   "DJI Mini 4K"),
    ("1SFOJ{id:06X}",      "00:11:22:{b1:02X}:{b2:02X}:{b3:02X}",   "DJI Mavic 3"),
    ("1TBLG{id:06X}",      "AA:BB:{b1:02X}:{b2:02X}:{b3:02X}:{b4:02X}", "DJI Air 3S"),
    ("3FMFK{id:06X}A",     "CC:DD:{b1:02X}:{b2:02X}:{b3:02X}:{b4:02X}", "DJI Mini 4K"),
    ("6FFFL{id:06X}",      "DE:AD:{b1:02X}:{b2:02X}:{b3:02X}:{b4:02X}", "DJI Mini 3"),
]


class SimulatedReceiver(RIDReceiver):
    """模拟 RID 接收器 — 生成虚拟无人机数据"""

    def __init__(self, callback: Callable[[ParsedRID], None],
                 pl_manager: PowerLineManager,
                 drone_count: int = 6,
                 update_interval: float = 1.0):
        super().__init__(callback)
        self.pl_manager = pl_manager
        self.drone_count = drone_count
        self.update_interval = update_interval
        self._drones: List[SimDrone] = []
        self._init_drones()

    def _init_drones(self):
        """根据电力线数据初始化模拟无人机"""
        behaviors = ["orbit", "cross", "wander"]

        for i in range(self.drone_count):
            template = _DRONE_POOL[i % len(_DRONE_POOL)]
            drone_id = template[0].format(id=i + 1)
            b = [random.randint(0, 255) for _ in range(6)]
            mac = template[1].format(
                b1=b[0], b2=b[1], b3=b[2], b4=b[3], b5=b[4], b6=b[5]
            )

            # 选一条电力线作为活动中心
            if self.pl_manager.lines:
                line = random.choice(self.pl_manager.lines)
                mid_lat = (line.lat1 + line.lat2) / 2
                mid_lon = (line.lon1 + line.lon2) / 2
                avg_alt = (line.alt1 + line.alt2) / 2
            else:
                mid_lat, mid_lon, avg_alt = 30.0, 120.0, 100.0

            behavior = behaviors[i % 3]

            # 不同行为对应的参数
            if behavior == "orbit":
                base_alt = avg_alt + random.choice([30, 80, 130, 200])
                alt_range = random.uniform(5, 15)
                orbit_radius = random.uniform(0.001, 0.004)  # ~100-400m
                speed = random.uniform(0.3, 0.8)  # 弧度/秒
            elif behavior == "cross":
                base_alt = avg_alt + random.choice([-20, 20, 60, 100])
                alt_range = random.uniform(3, 10)
                orbit_radius = random.uniform(0.003, 0.008)
                speed = random.uniform(0.05, 0.15)
            else:  # wander
                base_alt = avg_alt + random.uniform(-30, 250)
                alt_range = random.uniform(10, 40)
                orbit_radius = random.uniform(0.002, 0.006)
                speed = random.uniform(0.02, 0.08)

            # 起飞位置 (出发点的地面位置)
            t_lat = mid_lat + random.uniform(-0.008, -0.003)
            t_lon = mid_lon + random.uniform(-0.008, -0.003)
            t_alt = avg_alt - random.uniform(30, 80)  # 地面高度

            drone = SimDrone(
                drone_id=drone_id,
                mac=mac,
                behavior=behavior,
                center_lat=mid_lat + random.uniform(-0.005, 0.005),
                center_lon=mid_lon + random.uniform(-0.005, 0.005),
                orbit_radius=orbit_radius,
                base_alt=base_alt,
                alt_range=alt_range,
                speed=speed,
                angle=random.uniform(0, 2 * math.pi),
                phase=random.random(),
                phase_dir=1.0,
                wander_lat=mid_lat + random.uniform(-0.003, 0.003),
                wander_lon=mid_lon + random.uniform(-0.003, 0.003),
                takeoff_lat=t_lat,
                takeoff_lon=t_lon,
                takeoff_alt=t_alt,
            )
            self._drones.append(drone)

        logger.info("模拟接收器已初始化: %d 架无人机 (%d 条电力线)",
                     self.drone_count, len(self.pl_manager.lines))

    def _update_drone(self, d: SimDrone) -> tuple:
        """更新一架无人机的位置, 返回 (lat, lon, alt)"""
        now = time.time()
        if d.last_update == 0:
            d.last_update = now

        dt = now - d.last_update
        d.last_update = now

        if d.behavior == "orbit":
            d.angle += d.speed * dt
            lat = d.center_lat + d.orbit_radius * math.cos(d.angle)
            lon = d.center_lon + d.orbit_radius * math.sin(d.angle)
            alt = d.base_alt + d.alt_range * math.sin(d.angle * 3)

        elif d.behavior == "cross":
            d.phase += d.speed * dt * d.phase_dir
            if d.phase > 1.0:
                d.phase = 1.0
                d.phase_dir = -1.0
            elif d.phase < 0.0:
                d.phase = 0.0
                d.phase_dir = 1.0
            # 从远处飞向电力线再飞走
            offset = (d.phase - 0.5) * 2  # -1 → 0 → 1
            lat = d.center_lat + offset * d.orbit_radius
            lon = d.center_lon + offset * d.orbit_radius * 0.7
            alt = d.base_alt + d.alt_range * math.sin(d.phase * math.pi)

        else:  # wander
            d.wander_lat += random.uniform(-0.0001, 0.0001) * d.speed * 10
            d.wander_lon += random.uniform(-0.0001, 0.0001) * d.speed * 10
            # 限制游走范围
            d.wander_lat = max(d.center_lat - d.orbit_radius,
                              min(d.center_lat + d.orbit_radius, d.wander_lat))
            d.wander_lon = max(d.center_lon - d.orbit_radius,
                              min(d.center_lon + d.orbit_radius, d.wander_lon))
            lat = d.wander_lat
            lon = d.wander_lon
            alt = d.base_alt + random.uniform(-d.alt_range, d.alt_range)

        return lat, lon, alt

    def _make_parsed(self, d: SimDrone, lat: float, lon: float, alt: float) -> ParsedRID:
        """构造 ParsedRID 对象 — 包含 Basic ID + Location + System (起飞位)"""
        speed_h = random.uniform(5.0, 15.0)
        speed_v = random.uniform(-2.0, 2.0)
        rssi = -30 - random.uniform(10, 40)

        # 每 3 次广播中有 1 次附带 System 消息 (包含起飞位置)
        has_system = random.random() < 0.5
        system_msg = None
        if has_system:
            system_msg = SystemMessage(
                operator_lat=d.takeoff_lat,
                operator_lon=d.takeoff_lon,
                operator_alt_geo=d.takeoff_alt,
                area_count=1,
                area_radius=50.0,
                area_ceiling=120.0,
                area_floor=0.0,
                op_pos_type=0,  # 起飞位
                coordinate_system=0,
            )

        return ParsedRID(
            basic_id=BasicIDMessage(
                id_type=ID_TYPE_SERIAL,
                ua_type=UA_TYPE_HELICOPTER,
                uas_id=d.drone_id,
            ),
            location=LocationMessage(
                latitude=lat,
                longitude=lon,
                altitude_geodetic=alt,
                altitude_pressure=alt - random.uniform(-5, 5),
                speed_horizontal=speed_h,
                speed_vertical=speed_v,
                height_agl=alt - random.uniform(0, 30),
            ),
            system=system_msg,
            mac_address=d.mac,
            rssi=rssi,
            raw_data=b"SIM",
        )

    async def start(self):
        """启动模拟数据生成"""
        self._running = True
        logger.info("模拟接收器已启动 (%d 架无人机)", len(self._drones))

        while self._running:
            try:
                for d in self._drones:
                    lat, lon, alt = self._update_drone(d)
                    parsed = self._make_parsed(d, lat, lon, alt)
                    self.callback(parsed)
                await asyncio.sleep(self.update_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("模拟数据生成错误: %s", e)
                await asyncio.sleep(1)

        logger.info("模拟接收器已停止")

    async def stop(self):
        self._running = False


def create_simulated_receiver(
    callback: Callable[[ParsedRID], None],
    pl_manager: PowerLineManager,
    drone_count: int = 6,
    update_interval: float = 1.0,
) -> SimulatedReceiver:
    """创建模拟接收器的工厂函数"""
    return SimulatedReceiver(
        callback=callback,
        pl_manager=pl_manager,
        drone_count=drone_count,
        update_interval=update_interval,
    )
