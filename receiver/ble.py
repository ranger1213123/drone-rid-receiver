"""
BLE RID 接收器 - 扫描蓝牙低功耗广播，识别 Open Drone ID 无人机
"""

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Optional, Callable, List, Set

from logging_config import get_logger
from core.parser import parse_rid_pack, ParsedRID, get_active_protocol

logger = get_logger(__name__)


def _get_odid_uuid_128() -> str:
    """获取当前协议的 128-bit BLE UUID"""
    uuid16 = get_active_protocol().ble_service_uuid
    return f"0000{uuid16:04x}-0000-1000-8000-00805f9b34fb"


class RIDReceiver(ABC):
    """RID 接收器抽象基类"""

    def __init__(self, callback: Callable[[ParsedRID], None]):
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
    """BLE RID 接收器 — 持续扫描 Open Drone ID 广播"""

    def __init__(self, callback: Callable[[ParsedRID], None],
                 scan_duration: float = 0, device_filter: Optional[List[str]] = None):
        super().__init__(callback)
        # scan_duration=0 表示持续扫描；>0 表示定期重建扫描器的时间窗口
        self.scan_window = scan_duration if scan_duration > 0 else 60.0
        self.device_filter = set(device_filter) if device_filter else None
        self._scanner = None
        self._scan_count: int = 0
        self._seen_drones: Set[str] = set()
        self._last_status_time: float = 0

    def _detection_callback(self, device, advertisement_data):
        """bleak 扫描回调 — 在所有广播中查找 Open Drone ID"""
        if not self._running:
            return

        self._scan_count += 1

        # MAC 地址过滤 (可选)
        if self.device_filter and device.address not in self.device_filter:
            return

        # 在 service_data 中查找 ODID Service UUID (128-bit)
        odid_data = None
        service_data = getattr(advertisement_data, 'service_data', None) or {}
        odid_data = service_data.get(_get_odid_uuid_128())

        # 如果 128-bit 没找到，遍历所有 key 按 16-bit 后缀匹配
        if odid_data is None:
            proto_uuid = get_active_protocol().ble_service_uuid
            for uuid_key, data in service_data.items():
                if str(uuid_key).lower().endswith(f'{proto_uuid:04x}'):
                    odid_data = data
                    break

        if odid_data is None:
            return

        # 解析 RID 消息
        rssi = getattr(advertisement_data, 'rssi', 0) or 0
        parsed = parse_rid_pack(odid_data, mac_address=device.address, rssi=rssi)

        if parsed.has_location and parsed.drone_id:
            if parsed.drone_id not in self._seen_drones:
                self._seen_drones.add(parsed.drone_id)
                logger.info("发现无人机: %s (MAC: %s, RSSI: %d)",
                            parsed.drone_id, device.address[:17], rssi)
            self.callback(parsed)

    async def start(self):
        """启动 BLE 持续扫描"""
        try:
            from bleak import BleakScanner
        except ImportError:
            raise RuntimeError(
                "请安装 bleak: pip install bleak\n"
                "在 WSL 下 BLE 不可用，请使用 'wifi' 模式运行。"
            )

        self._running = True
        self._scan_count = 0
        self._last_status_time = time.time()

        proto = get_active_protocol()
        logger.info("BLE 扫描启动 (协议: %s, UUID: 0x%04X)",
                    proto.name, proto.ble_service_uuid)
        logger.info("持续扫描模式，每 %ds 重建扫描器以防止 Windows BLE 栈卡死", self.scan_window)

        retries = 0

        while self._running:
            try:
                # 创建扫描器 — 不传 service_uuids，在回调中自行过滤
                # Windows BLE 栈不支持硬件级 service UUID 过滤
                self._scanner = BleakScanner(
                    detection_callback=self._detection_callback,
                )
                await self._scanner.start()
                logger.debug("BLE 扫描器已启动")

                # 持续扫描直到窗口时间到
                await asyncio.sleep(self.scan_window)

                # 定期输出状态
                elapsed = time.time() - self._last_status_time
                if elapsed >= 60:
                    logger.info("BLE 扫描中: 已处理 %d 次广播, 发现 %d 架无人机",
                                self._scan_count, len(self._seen_drones))
                    self._scan_count = 0
                    self._last_status_time = time.time()

                await self._scanner.stop()
                self._scanner = None
                await asyncio.sleep(0.2)
                retries = 0

            except asyncio.CancelledError:
                break
            except Exception as e:
                retries += 1
                delay = min(2 ** min(retries - 1, 5), 60)
                logger.warning("BLE 扫描错误, %ds 后重试 (第 %d 次): %s",
                               delay, retries, e)
                if self._scanner:
                    try:
                        await self._scanner.stop()
                    except Exception:
                        pass
                    self._scanner = None
                await asyncio.sleep(delay)

    async def stop(self):
        """停止扫描"""
        self._running = False
        if self._scanner:
            try:
                await self._scanner.stop()
            except Exception:
                pass
            self._scanner = None
        logger.info("BLE 扫描已停止 (共发现 %d 架无人机)", len(self._seen_drones))



