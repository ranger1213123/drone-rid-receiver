"""
WiFi RID 接收器 - 扫描 WiFi Beacon 中的 Open Drone ID 广播

Open Drone ID WiFi 格式 (ASTM F3411 / ASD-STAN 4709-002):
1. **WiFi Beacon (完整帧)**: Vendor Specific IE, OUI 0xFA0B0C, 包含 ODID Message Pack
2. **WiFi Nanobeacon**: 简化的单帧广播, 用于低功耗场景

双平台支持:
- Windows:  使用 scapy + Npcap 捕获 WiFi 帧 (需要 Npcap 已安装)
- Linux:    使用 scapy + monitor mode (需要 aircrack-ng 设置监听模式)

此外提供 netsh 回退方案 (Windows 被动扫描, 无需 Npcap)。
"""

import asyncio
import struct
import subprocess
import re
from abc import ABC, abstractmethod
from typing import Optional, Callable, List, Dict
from collections import defaultdict

from rid_parser import parse_rid_pack, ParsedRID
from rid_receiver import RIDReceiver


# ODID WiFi Vendor OUI (little-endian 3 bytes)
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
            if oui == ODID_OUI:
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


class WiFiRIDReceiver(RIDReceiver):
    """
    WiFi RID 接收器基类

    子类实现平台特定的 WiFi 扫描逻辑
    """

    def __init__(self, callback: Callable[[ParsedRID], None]):
        super().__init__(callback)
        self._seen_ids: Dict[str, float] = {}  # 去重


class ScapyWiFiReceiver(WiFiRIDReceiver):
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
        print(f"[WiFi] 开始监听 WiFi Beacon 帧...")

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
            kwargs = {
                "prn": packet_handler,
                "timeout": self.timeout,
                "store": 0,
            }
            if self.interface:
                kwargs["iface"] = self.interface

            while self._running:
                try:
                    sniff(**kwargs)
                except Exception as e:
                    if self._running:
                        print(f"[WiFi] 嗅探错误: {e}")
                    break

        await loop.run_in_executor(None, sniff_thread)

    async def stop(self):
        self._running = False


class NetshWiFiReceiver(WiFiRIDReceiver):
    """
    基于 Windows netsh 的 WiFi 被动扫描接收器

    通过 `netsh wlan show networks mode=bssid` 获取附近 WiFi AP 信息,
    检查 BSSID 是否包含 ODID 标识。

    优点: 不需要 Npcap, 不需要管理员权限
    缺点: 只能看到广播 SSID 的 AP, 且数据有限
    """

    def __init__(self, callback: Callable[[ParsedRID], None],
                 scan_interval: float = 3.0):
        super().__init__(callback)
        self.scan_interval = scan_interval
        self._last_networks: Dict[str, dict] = {}

    async def start(self):
        self._running = True
        print(f"[WiFi/netsh] 开始被动扫描 (间隔: {self.scan_interval}s)...")

        loop = asyncio.get_event_loop()

        while self._running:
            try:
                networks = await loop.run_in_executor(
                    None, self._netsh_scan
                )
                for net_info in networks:
                    # 检查是否为 ODID 广播
                    # ODID WiFi Beacon 使用特定的 SSID 模式或 Vendor OUI
                    parsed = self._try_parse_netsh_result(net_info)
                    if parsed and parsed.has_location and parsed.drone_id:
                        self.callback(parsed)
            except Exception as e:
                if self._running:
                    print(f"[WiFi/netsh] 扫描错误: {e}")

            await asyncio.sleep(self.scan_interval)

    async def stop(self):
        self._running = False

    def _netsh_scan(self) -> List[dict]:
        """执行 netsh 扫描"""
        try:
            result = subprocess.run(
                ["netsh", "wlan", "show", "networks", "mode=bssid"],
                capture_output=True, text=True, timeout=10, encoding='gbk',
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
            )
            return self._parse_netsh_output(result.stdout)
        except FileNotFoundError:
            # 非 Windows 系统
            return []
        except Exception:
            return []

    def _parse_netsh_output(self, output: str) -> List[dict]:
        """解析 netsh 输出"""
        networks = []
        current_ssid = None
        current_bssid = None
        current = {}

        for line in output.splitlines():
            line = line.strip()

            m = re.match(r'^SSID \d+ : (.+)', line)
            if m:
                if current:
                    networks.append(current)
                current = {"ssid": m.group(1), "bssids": []}
                current_ssid = m.group(1)
                continue

            m = re.match(r'^\s*BSSID \d+\s*: ([0-9a-fA-F:]+)', line)
            if m:
                current_bssid = m.group(1)
                # 检查是否为 ODID MAC 前缀
                # 某些无人机使用固定 OUI 前缀
                continue

            m = re.match(r'^\s*信号\s*: (\d+)%', line)
            if m and current_bssid:
                signal_pct = int(m.group(1))
                # 估算 RSSI: 0% ≈ -100dBm, 100% ≈ -50dBm
                rssi = -100 + signal_pct * 0.5
                current["rssi"] = rssi

        if current:
            networks.append(current)

        return networks

    def _try_parse_netsh_result(self, net_info: dict) -> Optional[ParsedRID]:
        """尝试将 netsh 结果转换为 RID 数据"""
        # netsh 无法获取原始 ODID payload, 只能看到 SSID 和 BSSID
        # 某些无人机在 SSID 中编码信息, 但这不标准
        # 此方法主要用于标识存在性检测
        ssid = net_info.get("ssid", "")
        if "ODID" in ssid.upper() or "DRONE" in ssid.upper():
            # 构造基本信息
            return parse_rid_pack(
                b"\x00\x00",  # 最小消息包
                mac_address=net_info.get("bssid", "??:??:??:??:??:??"),
                rssi=int(net_info.get("rssi", -70))
            )
        return None


# ─────────────────── 自动检测最佳 WiFi 后端 ───────────────────

def create_wifi_receiver(callback: Callable[[ParsedRID], None],
                         interface: Optional[str] = None,
                         prefer_scapy: bool = True) -> WiFiRIDReceiver:
    """
    自动选择最佳 WiFi 接收器

    优先级: scapy (需要 Npcap/monitor mode) > netsh (Windows 回退)

    如果 scapy 不可用, 自动回退到 netsh (Windows) 或提示用户安装依赖
    """
    if prefer_scapy:
        try:
            import scapy  # noqa: F401
            return ScapyWiFiReceiver(callback, interface=interface)
        except ImportError:
            pass

    # 回退到 netsh (仅 Windows)
    import platform
    if platform.system() == "Windows":
        print("[WiFi] scapy 不可用, 回退到 netsh 被动扫描")
        return NetshWiFiReceiver(callback)
    else:
        raise RuntimeError(
            "WiFi RID 接收需要 scapy (pip install scapy)\n"
            "Linux 需将 WiFi 适配器设为 monitor mode:\n"
            "  sudo airmon-ng start wlan0"
        )


# Re-export RIDReceiver for compatibility
from rid_receiver import MockRIDReceiver, BLE_RIDReceiver  # noqa: E402, F401
