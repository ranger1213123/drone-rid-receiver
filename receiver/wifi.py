"""
WiFi RID 接收器 — Linux raw socket 抓取 802.11 帧

使用 AF_PACKET raw socket 直接捕获 Beacon / Action 帧中的
Open Drone ID 广播 (ASTM F3411 / DJI Remote ID)。

平台: Linux only (需 monitor mode + RTL8812AU 等芯片)
参考: rid_wifi_scanner.py v0.7 (RSB-4221 实测通过)
"""

import asyncio
import select
import socket
import struct
import subprocess
import threading
import time
from typing import Optional, Callable, List

from logging_config import get_logger
from core.parser import parse_rid_pack, ParsedRID
from receiver.ble import RIDReceiver

logger = get_logger(__name__)

# ASTM F3411 / DJI Remote ID OUI
# Reference 实测: FA-0B-BC 同时用于 DJI 专用 RID 和 ASTM RID
ODID_OUI = bytes([0xFA, 0x0B, 0xBC])
ODID_OUI_ASTM = bytes([0xFA, 0x0B, 0x0C])  # 备用，部分实现使用

# DJI 已知 MAC 前缀
DJI_MAC_PREFIXES = {
    "60:60:1f": "DJI",
    "34:d2:62": "DJI",
    "04:d6:aa": "DJI",
}


def _extract_rssi(pkt: bytes, radiotap_len: int) -> int:
    """从 radiotap header 提取 RSSI (dBm)

    Radiotap flags 在 byte 8-9 (little-endian u16).
    Bit 5 (0x0020) 表示 antenna signal 字段存在，
    位于 radiotap 数据区的第一个非标准字段 (byte 10).
    """
    if radiotap_len < 12:
        return 0
    try:
        rt_flags = struct.unpack("<H", pkt[8:10])[0]
        if rt_flags & 0x0020:
            val = pkt[10]
            if val > 127:
                val -= 256
            return val
    except (struct.error, IndexError):
        pass
    return 0


def _extract_mac(pkt: bytes, radiotap_len: int) -> str:
    """从 802.11 帧头提取源 MAC 地址 (SA, offset 10-15)"""
    try:
        mac = pkt[radiotap_len + 10:radiotap_len + 16]
        return ":".join(f"{b:02x}" for b in mac)
    except (IndexError, TypeError):
        return ""


def _extract_ssid_from_beacon(pkt: bytes, radiotap_len: int) -> Optional[bytes]:
    """从 Beacon 帧的 tagged parameters 中提取 SSID (tag=0)"""
    hdr_end = radiotap_len + 24
    ssid_off = hdr_end + 12  # 跳过 fixed params (timestamp 8B + interval 2B + capability 2B)
    if ssid_off + 2 > len(pkt):
        return None
    tag_id = pkt[ssid_off]
    tag_len = pkt[ssid_off + 1]
    if tag_id == 0 and tag_len > 0 and ssid_off + 2 + tag_len <= len(pkt):
        return pkt[ssid_off + 2:ssid_off + 2 + tag_len]
    return None


def _find_odid_vsie(pkt: bytes, radiotap_len: int) -> Optional[bytes]:
    """在 Beacon 帧的 tagged parameters 中查找 ODID Vendor Specific IE (tag=221)

    802.11 Beacon tagged parameters 从 radiotap_len + 24 + 12 开始.
    每个 IE: [Tag(1B)][Length(1B)][Data(Length bytes)]

    Returns: RID Message Pack 数据 (OUI 之后的 bytes) 或 None
    """
    pos = radiotap_len + 24 + 12  # 802.11 hdr + fixed beacon params

    if pos + 2 > len(pkt):
        return None

    while pos + 2 <= len(pkt):
        tag = pkt[pos]
        tlen = pkt[pos + 1]
        if pos + 2 + tlen > len(pkt):
            break
        val = pkt[pos + 2:pos + 2 + tlen]

        if tag == 221 and tlen >= 5:  # Vendor Specific IE, min 3B OUI + 2B type+data
            oui = val[:3]
            if oui in (ODID_OUI, ODID_OUI_ASTM):
                # val[3:] = OUI Type (1B) + RID Message Pack
                return val[3:]

        pos += 2 + tlen

    return None


def _parse_action_frame(pkt: bytes, radiotap_len: int) -> Optional[bytes]:
    """解析 Public Action 帧 (category 4) 中的 RID 数据

    Action frame: type=0, subtype=13
    MAC header = 24B, body 从 radiotap_len + 24 开始.
    Category 4 (Public Action): OUI 在 body+2, RID 数据在 body+6.
    """
    if len(pkt) < radiotap_len + 28:
        return None

    fc = struct.unpack("<H", pkt[radiotap_len:radiotap_len + 2])[0]
    ftype = (fc >> 2) & 0x3
    fsubtype = (fc >> 4) & 0xF

    if ftype != 0 or fsubtype != 13:
        return None

    body_start = radiotap_len + 24
    if body_start + 2 > len(pkt):
        return None

    category = pkt[body_start]
    if category == 4 and body_start + 6 <= len(pkt):
        oui = pkt[body_start + 2:body_start + 5]
        if oui in (ODID_OUI, ODID_OUI_ASTM):
            return pkt[body_start + 6:]

    return None


class RawWiFiReceiver(RIDReceiver):
    """Linux raw socket WiFi RID 接收器

    使用 AF_PACKET raw socket 直接抓取 802.11 帧，
    解析 Beacon / Action 帧中的 ASTM F3411 RID 数据。

    需要:
    - Linux + WiFi monitor mode
    - 推荐芯片: RTL8812AU, RTL8814, MT7612, AR9271

    用法:
        receiver = RawWiFiReceiver(callback, interface="wlan0", channel=6)
        await receiver.start()
    """

    def __init__(self, callback: Callable[[ParsedRID], None],
                 interface: str = "wlan0", channel: int = 6,
                 timeout: float = 1.0):
        super().__init__(callback)
        self.interface = interface
        self.channel = channel
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._seen_drones: dict = {}
        self._total_packets: int = 0
        self._beacon_count: int = 0
        self._action_count: int = 0

    def _set_channel(self, channel: int) -> bool:
        """通过 iw 命令设置 monitor 接口信道"""
        try:
            subprocess.run(
                ["iw", "dev", self.interface, "set", "channel", str(channel)],
                capture_output=True, timeout=3, check=False,
            )
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _scan_loop(self):
        """阻塞式抓包线程 — 解析 Beacon 和 Action 帧中的 RID"""
        try:
            self._sock = socket.socket(
                socket.AF_PACKET, socket.SOCK_RAW,
                socket.htons(0x0003)  # ETH_P_ALL
            )
            self._sock.bind((self.interface, 0))
            self._sock.settimeout(self.timeout)
        except AttributeError:
            logger.error("AF_PACKET raw socket 仅支持 Linux，当前平台不可用")
            self._running = False
            return
        except PermissionError:
            logger.error("需要 root 权限运行 raw socket, 请使用 sudo")
            self._running = False
            if self._sock:
                self._sock.close()
                self._sock = None
            return
        except OSError as e:
            logger.error("无法绑定网卡 %s: %s", self.interface, e)
            self._running = False
            if self._sock:
                self._sock.close()
                self._sock = None
            return

        if self.channel:
            self._set_channel(self.channel)

        logger.info("WiFi 监听启动: %s CH%d (raw socket)", self.interface, self.channel)

        loop = asyncio.new_event_loop()

        while self._running:
            try:
                ready, _, _ = select.select([self._sock], [], [], 1.0)
                if not ready:
                    continue

                pkt = self._sock.recv(4096)
                self._total_packets += 1

                if len(pkt) < 36:
                    continue

                # 解析 radiotap header 长度
                radiotap_len = struct.unpack("<H", pkt[2:4])[0]
                if radiotap_len < 8 or radiotap_len + 28 > len(pkt):
                    continue

                # 解析 Frame Control
                fc = struct.unpack("<H", pkt[radiotap_len:radiotap_len + 2])[0]
                ftype = (fc >> 2) & 0x3
                fsubtype = (fc >> 4) & 0xF

                mac = _extract_mac(pkt, radiotap_len)
                rssi = _extract_rssi(pkt, radiotap_len)
                now = time.time()

                rid_data: Optional[bytes] = None

                # Beacon 帧 (type=0, subtype=8)
                if ftype == 0 and fsubtype == 8:
                    self._beacon_count += 1

                    # 路径 1: SSID 以 "RID-" 开头 — 表明是 RID 广播
                    ssid = _extract_ssid_from_beacon(pkt, radiotap_len)
                    if ssid:
                        try:
                            ssid_str = ssid.decode("ascii", errors="replace")
                        except UnicodeDecodeError:
                            ssid_str = ""
                        if ssid_str.startswith("RID-"):
                            logger.debug("检测到 RID Beacon SSID: %s", ssid_str)

                    # 路径 2: Vendor Specific IE (tag 221) 包含 ODID OUI
                    rid_data = _find_odid_vsie(pkt, radiotap_len)

                # Action 帧 (type=0, subtype=13)
                elif ftype == 0 and fsubtype == 13:
                    self._action_count += 1
                    rid_data = _parse_action_frame(pkt, radiotap_len)

                if rid_data is None:
                    continue

                # 使用现有协议引擎解析 Message Pack
                parsed = parse_rid_pack(rid_data, mac_address=mac, rssi=rssi,
                                        protocol="astm_f3411")
                if not parsed.has_location or not parsed.drone_id:
                    continue

                # 去重 & 回调
                drone_id = parsed.drone_id
                if drone_id in self._seen_drones:
                    if now - self._seen_drones[drone_id]["last_seen"] < 1.0:
                        continue  # 同一秒内去重
                    self._seen_drones[drone_id]["last_seen"] = now
                    self._seen_drones[drone_id]["count"] += 1
                else:
                    self._seen_drones[drone_id] = {
                        "last_seen": now,
                        "count": 1,
                    }

                try:
                    self.callback(parsed)
                except Exception:
                    pass

                # 清理过期 (>30s)
                expired = [did for did, v in self._seen_drones.items()
                           if now - v["last_seen"] > 30]
                for did in expired:
                    del self._seen_drones[did]

            except socket.timeout:
                continue
            except Exception as e:
                if str(e):
                    logger.debug("WiFi 抓包异常: %s", e)

        loop.close()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        logger.info("WiFi 监听停止 (共 %d 包, %d Beacon, %d Action)",
                    self._total_packets, self._beacon_count, self._action_count)

    async def start(self):
        """启动 WiFi 抓包"""
        self._running = True
        loop = asyncio.get_event_loop()
        self._thread = threading.Thread(target=self._scan_loop, daemon=True)
        self._thread.start()
        logger.info("WiFi RID 接收器已启动")

    async def stop(self):
        """停止 WiFi 抓包"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None


def create_wifi_receiver(callback: Callable[[ParsedRID], None],
                         interface: Optional[str] = None,
                         channels: Optional[List[int]] = None,
                         hop_interval: float = 0.3) -> RawWiFiReceiver:
    """创建 WiFi RID 接收器 (Linux raw socket)

    interface: 网卡名称 (默认 "wlan0")
    channels:  信道列表 (仅用第一个, 杆塔设备固定信道)
    hop_interval: 保留参数, raw socket 不做信道跳频
    """
    if interface is None:
        interface = "wlan0"

    channel = 6
    if channels and len(channels) > 0:
        channel = channels[0]

    return RawWiFiReceiver(callback, interface=interface, channel=channel)
