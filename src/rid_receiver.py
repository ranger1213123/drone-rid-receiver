"""
BLE RID 接收器 - 扫描蓝牙低功耗广播，识别 Open Drone ID 无人机

支持两种模式:
  - 真实 BLE 模式: 使用 bleak 扫描真实的 RID 广播
  - 模拟模式: 生成模拟的 RID 数据用于测试 (在无 BLE 硬件环境下使用)
"""

import asyncio
import struct
import random
import math
import time
from abc import ABC, abstractmethod
from typing import Optional, Callable, List

from rid_parser import parse_rid_pack, ParsedRID, ODID_SERVICE_UUID


class RIDReceiver(ABC):
    """RID 接收器抽象基类"""

    def __init__(self, callback: Callable[[ParsedRID], None]):
        """
        callback: 收到 RID 数据后的回调函数
        """
        self.callback = callback
        self._running = False

    @abstractmethod
    async def start(self):
        """启动接收"""
        ...

    @abstractmethod
    async def stop(self):
        """停止接收"""
        ...


class BLE_RIDReceiver(RIDReceiver):
    """真实 BLE RID 接收器 (使用 bleak)"""

    def __init__(self, callback: Callable[[ParsedRID], None],
                 scan_duration: float = 5.0, device_filter: Optional[List[str]] = None):
        super().__init__(callback)
        self.scan_duration = scan_duration
        self.device_filter = set(device_filter) if device_filter else None
        self._scanner = None

    def _detection_callback(self, device, advertisement_data):
        """bleak 扫描回调"""
        # 检查 MAC 过滤
        if self.device_filter and device.address not in self.device_filter:
            return

        # 在 service_data 中查找 ODID Service UUID
        odid_data = None
        try:
            # bleak 0.21+ 使用 service_uuids 和 service_data
            if hasattr(advertisement_data, 'service_data'):
                odid_uuid_str = f"0000{ODID_SERVICE_UUID:04x}-0000-1000-8000-00805f9b34fb"
                odid_data = advertisement_data.service_data.get(odid_uuid_str)
        except Exception:
            pass

        if odid_data is None:
            return  # 不是 ODID 广播

        # 解析 RID 消息
        rssi = advertisement_data.rssi if hasattr(advertisement_data, 'rssi') else 0
        parsed = parse_rid_pack(odid_data, mac_address=device.address, rssi=rssi)

        if parsed.has_location and parsed.drone_id:
            self.callback(parsed)

    async def start(self):
        """启动 BLE 持续扫描"""
        try:
            from bleak import BleakScanner
        except ImportError:
            raise RuntimeError(
                "请安装 bleak: pip install bleak\n"
                "在 WSL 下 BLE 不可用，请使用 'mock' 模式运行。"
            )

        self._running = True
        self._scanner = BleakScanner(
            detection_callback=self._detection_callback,
            service_uuids=[f"0000{ODID_SERVICE_UUID:04x}-0000-1000-8000-00805f9b34fb"]
        )

        print(f"[BLE] 开始扫描 ODID 广播 (Service UUID: 0x{ODID_SERVICE_UUID:04X})")
        loop = asyncio.get_event_loop()

        while self._running:
            try:
                await self._scanner.start()
                await asyncio.sleep(self.scan_duration)
                await self._scanner.stop()
                await asyncio.sleep(0.1)
            except Exception as e:
                print(f"[BLE] 扫描错误: {e}")
                await asyncio.sleep(1.0)

    async def stop(self):
        """停止扫描"""
        self._running = False
        if self._scanner:
            try:
                await self._scanner.stop()
            except Exception:
                pass


class MockRIDReceiver(RIDReceiver):
    """
    模拟 RID 接收器 - 生成模拟的无人机 RID 数据

    用于在没有 BLE 硬件或 WSL 环境下测试系统。
    模拟 3 架无人机在不同轨迹上飞行，其中部分靠近电力线。
    """

    def __init__(self, callback: Callable[[ParsedRID], None],
                 interval: float = 1.0, num_drones: int = 3):
        super().__init__(callback)
        self.interval = interval
        self._drones = []

        # 初始化模拟无人机 (部分会靠近电力线)
        base_lat, base_lon = 30.2900, 120.1550  # 电力线区域中心
        drone_configs = [
            # (drone_id, start_lat, start_lon, start_alt, speed_ns, speed_ew, v_speed_threshold)
            ("DRONE-A001", base_lat + 0.002, base_lon - 0.002, 200.0, 0.0002, 0.0001, 0.5),
            ("DRONE-B002", base_lat - 0.001, base_lon + 0.001, 80.0, 0.0001, -0.0002, 1.0),
            ("DRONE-C003", base_lat + 0.001, base_lon + 0.002, 150.0, -0.0001, 0.0001, 2.0),
        ]

        for drone_id, lat, lon, alt, ns, ew, v_spd in drone_configs[:num_drones]:
            self._drones.append({
                "id": drone_id,
                "lat": lat,
                "lon": lon,
                "alt": alt,
                "speed_ns": ns,
                "speed_ew": ew,
                "vert_speed": v_spd,
                "heading": random.uniform(0, 360),
                "phase": random.uniform(0, 2 * math.pi),
            })

    async def start(self):
        """启动模拟数据生成"""
        self._running = True
        tick = 0
        print(f"[模拟] 启动 {len(self._drones)} 架模拟无人机")

        while self._running:
            for d in self._drones:
                tick += 1
                # 模拟飞行轨迹 (螺旋 + 线性漂移)
                d["phase"] += 0.05
                d["lat"] += d["speed_ns"] * math.sin(d["phase"]) * 0.5
                d["lon"] += d["speed_ew"] * math.cos(d["phase"]) * 0.5
                d["alt"] += d["vert_speed"] * math.sin(d["phase"] * 0.5)
                d["alt"] = max(10, min(300, d["alt"]))  # 限制在 10-300m
                d["heading"] = (d["heading"] + random.uniform(-5, 5)) % 360

                # 构造模拟的 ParsedRID
                from rid_parser import BasicIDMessage, LocationMessage, ParsedRID

                parsed = ParsedRID(
                    raw_data=b"",
                    mac_address=f"AA:BB:CC:DD:{d['id'][-2:]}:{tick%100:02x}",
                    rssi=-50,
                    basic_id=BasicIDMessage(
                        id_type=1,  # Serial Number
                        ua_type=2,  # 多旋翼
                        uas_id=d["id"],
                    ),
                    location=LocationMessage(
                        latitude=d["lat"],
                        longitude=d["lon"],
                        altitude_geodetic=d["alt"],
                        altitude_pressure=d["alt"] - 1.0,
                        height_agl=d["alt"],
                        speed_horizontal=abs(d["speed_ns"]) * 111320 + random.uniform(0, 2),
                        speed_vertical=d["vert_speed"],
                    ),
                )

                self.callback(parsed)

            await asyncio.sleep(self.interval)

    async def stop(self):
        """停止模拟"""
        self._running = False
