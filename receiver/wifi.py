"""
WiFi RID 接收器 - 扫描 WiFi Beacon 中的 Open Drone ID 广播

Open Drone ID WiFi 格式 (ASTM F3411 / ASD-STAN 4709-002):
1. WiFi Beacon (完整帧): Vendor Specific IE, OUI 0xFA0B0C, 包含 ODID Message Packo
2. WiFi Nanobeacon: 简化的单帧广播, 用于低功耗场景

平台要求:
- Windows:  scapy + Npcap (https://npcap.com)
- Linux:    scapy + WiFi monitor mode
"""

import asyncio
import struct
from typing import Optional, Callable

from logging_config import get_logger
from core.parser import parse_rid_pack, ParsedRID, get_active_protocol
from receiver.ble import RIDReceiver

logger = get_logger(__name__)


# ODID WiFi Vendor OUI (ASTM: 0xFA0B0C, GB: 0xFA0BBC)
# NOTE: 旧常量保留向后兼容，新代码应使用 get_active_protocol().wifi_oui
ODID_OUI = bytes([0x0C, 0x0B, 0xFA])


def find_odid_in_beacon(frame_data: bytes) -> Optional[bytes]:
    """
    在 802.11 Beacon 帧中查找 ODID Vendor Specific IE

    802.11 Beacon 帧结构:
    - Frame Control (2B)
    - Duration (2B)
    - DA (6B)
    - SA (6B)     ← 无人机 MAC 地址
    - BSSID (6B)
    - Seq Ctrl (2B)
    - Timestamp (8B)
    - Beacon Interval (2B)
    - Capability (2B)
    - Tagged Parameters (IEs) ...

    IE 格式: [Tag(1B)][Length(1B)][Data(Length bytes)]
    Tag 221 = Vendor Specific
    """
    if len(frame_data) < 36:
        return None

    # 跳过到 Tagged Parameters 部分
    offset = 36
    sa_addr = frame_data[10:16]
    sa_mac = ":".join(f"{b:02X}" for b in sa_addr)

    while offset < len(frame_data) - 1:
        tag = frame_data[offset]
        length = frame_data[offset + 1]

        if offset + 2 + length > len(frame_data):
            break

        if tag == 221 and length >= 3:  # Vendor Specific IE
            oui = frame_data[offset + 2:offset + 5]
            proto_oui = get_active_protocol().wifi_oui
            # 同时检查 ASTM 和 GB 的 OUI
            if oui == ODID_OUI or oui == proto_oui:
                odid_data = frame_data[offset + 5:offset + 2 + length]
                return odid_data  # 返回 ODID Message Pack 原始数据

        offset += 2 + length

    return None


def parse_nanobeacon(data: bytes, rssi: int = 0) -> Optional[ParsedRID]:
    """
    解析 WiFi Nanobeacon 广播

    Nanobeacon 格式 (18-32 字节):
    Bytes 0-5:  发射器 MAC 地址
    Byte 6:     Message Counter (低 4 位)
    Byte 7:     Length (消息包总字节数) + 其他标志
    Bytes 8+:   ODID Message Pack
    """
    if len(data) < 10:
        return None

    mac = ":".join(f"{b:02X}" for b in data[0:6])
    # 直接将整个数据交给解析器处理
    return parse_rid_pack(data, mac_address=mac, rssi=rssi)


class ScapyWiFiReceiver(RIDReceiver):
    """
    基于 scapy 的 WiFi RID 接收器

    需要:
    - 安装 scapy: pip install scapy
    - Windows: 安装 Npcap (https://npcap.com)
    - Linux:   WiFi 适配器设为 monitor mode

    用法:
        receiver = ScapyWiFiReceiver(callback, interface="Wi-Fi")
        await receiver.start()
    """

    def __init__(self, callback: Callable[[ParsedRID], None],
                 interface: Optional[str] = None, timeout: float = 1.0):
        super().__init__(callback)
        self.interface = interface
        self.timeout = timeout

    async def start(self):
        """使用 scapy 嗅探 WiFi 帧"""
        try:
            from scapy.all import sniff, Dot11, Dot11Beacon
        except ImportError:
            raise RuntimeError(
                "请安装 scapy: pip install scapy\n"
                "Windows 用户还需安装 Npcap: https://npcap.com"
            )

        self._running = True
        iface_info = f" ({self.interface})" if self.interface else " (所有网卡)"
        logger.info("WiFi 监听启动%s", iface_info)

        def packet_handler(pkt):
            if not self._running:
                return True  # 停止嗅探

            try:
                if not pkt.haslayer(Dot11Beacon):
                    return

                # 获取原始帧数据
                raw = bytes(pkt)

                # 提取 ODID 数据
                odid_data = find_odid_in_beacon(raw)
                if odid_data is None:
                    return

                # 提取 MAC 地址
                if pkt.haslayer(Dot11):
                    mac = pkt[Dot11].addr2 or "00:00:00:00:00:00"
                else:
                    mac = "00:00:00:00:00:00"

                # RSSI (如果可用)
                rssi = getattr(pkt, 'dBm_AntSignal', 0)

                

                parsed = parse_rid_pack(odid_data, mac_address=mac, rssi=rssi)
                if parsed.has_location and parsed.drone_id:
                    self.callback(parsed)

            except Exception as e:
                pass  # 静默跳过解析失败的数据包

        # 在异步线程中运行 sniff
        loop = asyncio.get_event_loop()

        def sniff_thread():
            import time
            kwargs = {
                "prn": packet_handler,
                "timeout": self.timeout,
                "store": 0,
            }
            if self.interface:
                kwargs["iface"] = self.interface

            retries = 0
            max_delay = 30

            while self._running:
                try:
                    sniff(**kwargs)
                    retries = 0  # 成功运行后重置
                except Exception as e:
                    if not self._running:
                        break
                    retries += 1
                    delay = min(1 << min(retries - 1, 5), max_delay)
                    if retries <= 3:
                        logger.warning("WiFi 嗅探错误, %ds 后重试 (%d/3): %s", delay, retries, e)
                    elif retries <= 10:
                        logger.error("WiFi 嗅探持续失败 (%d 次), %ds 后重试", retries, delay)
                    else:
                        logger.critical("WiFi 嗅探已失败 %d 次, 仍在尝试 (间隔 %ds)", retries, delay)
                    time.sleep(delay)

        await loop.run_in_executor(None, sniff_thread)

    async def stop(self):
        self._running = False


def _detect_best_interface() -> Optional[str]:
    """
    自动选择最佳 WiFi 抓包网卡，优先选择支持 monitor mode 的网卡。

    选择策略:
    1. 过滤出无线网卡 (WIRELESS flag)
    2. 排除 Intel 网卡 (Windows 下不支持 monitor mode)
    3. 优先选择未连接 WiFi 的网卡 (DISCONNECTED，可自由切换信道)
    4. 优先选择已知支持 monitor mode 的芯片: RTL8812AU, MediaTek 等
    """
    try:
        from scapy.all import IFACES
    except ImportError:
        return None

    wireless = []
    for name, iface in IFACES.items():
        flags = str(getattr(iface, 'flags', ''))
        if 'WIRELESS' not in flags:
            continue
        desc = (iface.description or name).lower()
        wireless.append((iface, desc))

    if not wireless:
        return None

    def score(item):
        iface, desc = item
        s = 0
        # 排除 Intel (不支持 monitor mode)
        if 'intel' in desc:
            return -1
        # 未连接 = 自由切换信道
        flags = str(getattr(iface, 'flags', ''))
        if 'DISCONNECTED' in flags:
            s += 100
        # 已知支持 monitor mode 的芯片
        known_good = ['rtl8812', 'rtl8814', 'mt7612', 'mt7610', 'ar9271',
                      'rt2870', 'rt3070', 'rt3572', 'rt5572', 'ath9k']
        for chip in known_good:
            if chip in desc:
                s += 200
                break
        # USB 网卡通常比内置网卡更适合抓包
        if 'usb' in desc:
            s += 50
        return s

    wireless.sort(key=score, reverse=True)
    best = wireless[0]
    best_iface = best[0]
    best_desc = best[1]

    if score(best) < 0:
        logger.warning("未找到支持 monitor mode 的 WiFi 网卡，将使用默认网卡")
        return None

    logger.info("自动选择 WiFi 网卡: %s (%s)", best_iface.name or best_iface.description, best_desc)
    return best_iface


def create_wifi_receiver(callback: Callable[[ParsedRID], None],
                         interface: Optional[str] = None) -> ScapyWiFiReceiver:
    """
    创建 WiFi RID 接收器 (scapy + Npcap/monitor mode)

    interface: 指定网卡名称 (如 "WLAN 2" 或完整的 NPF GUID)，为 None 时自动检测
    """
    try:
        import scapy  # noqa: F401
    except ImportError:
        raise RuntimeError(
            "WiFi RID 接收需要 scapy (pip install scapy)\n"
            "Windows 用户还需安装 Npcap: https://npcap.com\n"
            "Linux 需将 WiFi 适配器设为 monitor mode:\n"
            "  sudo airmon-ng start wlan0"
        )
    if interface is None:
        best = _detect_best_interface()
        if best is not None:
            interface = best
    return ScapyWiFiReceiver(callback, interface=interface)


# Re-export RIDReceiver for compatibility
from receiver.ble import BLE_RIDReceiver  # noqa: E402, F401
