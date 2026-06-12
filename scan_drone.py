#!/usr/bin/env python3
"""
无人机 RID 扫描器 — 独立脚本
扫描附近广播 ODID (Open Drone ID) 的无人机并显示详细信息

支持两种传输方式:
  - BLE (蓝牙): Service UUID 0xFFFA
  - WiFi Beacon: Vendor Specific IE, OUI 0xFA0B0C

用法:
  python scan_drone.py                    # BLE 持续扫描 (默认)
  python scan_drone.py --mode wifi        # WiFi Beacon 扫描 (需要 Npcap + scapy)
  python scan_drone.py --mode wifi -i Wi-Fi  # 指定 WiFi 网卡
  python scan_drone.py --once             # 扫描一次后退出
  python scan_drone.py --timeout 30       # 扫描 30 秒
"""

import asyncio
import struct
import sys
import argparse
from datetime import datetime

ODID_SERVICE_UUID = 0xFFFA
ODID_UUID_STR = f"0000{ODID_SERVICE_UUID:04x}-0000-1000-8000-00805f9b34fb"

# ODID WiFi Vendor OUI (little-endian bytes: 0xFA, 0x0B, 0x0C → [0x0C, 0x0B, 0xFA])
ODID_WIFI_OUI = bytes([0x0C, 0x0B, 0xFA])

# ── 无人机类型 ──
UA_TYPES = {
    0: "未声明", 1: "固定翼", 2: "多旋翼", 3: "旋翼机",
    4: "混合动力", 5: "扑翼机", 6: "滑翔机", 7: "风筝",
    8: "自由气球", 9: "系留气球", 10: "飞艇", 11: "自由落体",
    12: "火箭", 13: "系留", 14: "地面障碍物", 15: "其他",
}

# ── ID 类型 ──
ID_TYPES = {0: "无", 1: "序列号", 2: "CAA注册号", 3: "UTM分配", 4: "会话ID"}


def parse_odid_basic_id(data, offset=0):
    """解析 Basic ID 消息"""
    if len(data) < offset + 2:
        return None
    hdr = data[offset]
    id_type = hdr & 0x0F
    ua_type = (hdr >> 4) & 0x0F
    uas_id = data[offset + 2:offset + 22].split(b'\x00')[0].decode('ascii', errors='replace')
    return {"id_type": id_type, "ua_type": ua_type, "uas_id": uas_id}


def parse_odid_location(data, offset=0):
    """解析 Location/Vector 消息"""
    if len(data) < offset + 25:
        return None

    status = data[offset]
    direction = data[offset + 1]
    speed_mult = 0.25 if (direction & 0x01) else 1.0

    speed_h = struct.unpack_from('<H', data, offset + 2)[0] * 0.01 * speed_mult
    speed_v = struct.unpack_from('<h', data, offset + 4)[0] * 0.01 * speed_mult
    lat = struct.unpack_from('<i', data, offset + 6)[0] / 1e7
    lon = struct.unpack_from('<i', data, offset + 10)[0] / 1e7
    alt_p = struct.unpack_from('<h', data, offset + 14)[0] * 0.5
    alt_g = struct.unpack_from('<h', data, offset + 16)[0] * 0.5
    height_agl = struct.unpack_from('<H', data, offset + 18)[0] * 0.5

    return {
        "latitude": lat, "longitude": lon,
        "altitude_geodetic": alt_g, "altitude_pressure": alt_p,
        "height_agl": height_agl,
        "speed_horizontal": speed_h, "speed_vertical": speed_v,
    }


def parse_odid_pack(service_data: bytes):
    """解析 ODID Service Data / WiFi Beacon 消息包"""
    if len(service_data) < 2:
        return None

    result = {}
    offset = 2  # skip counter + version

    while offset < len(service_data) - 1:
        header = service_data[offset]
        msg_type = header & 0x0F

        if msg_type == 0x0:  # Basic ID
            basic = parse_odid_basic_id(service_data, offset + 1)
            if basic:
                result["basic"] = basic
            offset += 23
        elif msg_type == 0x1:  # Location
            loc = parse_odid_location(service_data, offset + 1)
            if loc:
                result["location"] = loc
            offset += 26
        elif msg_type == 0x3:  # Self-ID
            offset += 25
        elif msg_type == 0x5:  # Operator ID
            offset += 22
        else:
            break

    return result if result else None


def print_detection(parsed: dict, mac: str = "", rssi: int = 0, source: str = ""):
    """统一输出检测结果"""
    now = datetime.now().strftime("%H:%M:%S")

    print(f"\n{'='*60}")
    print(f"[{now}] 发现无人机 RID 广播! ({source})")
    if mac:
        print(f"  MAC 地址: {mac}")
    if rssi:
        print(f"  信号强度: {rssi} dBm")

    if "basic" in parsed:
        b = parsed["basic"]
        print(f"  无人机 ID: {b['uas_id']}")
        print(f"  ID 类型:   {ID_TYPES.get(b['id_type'], '?')}")
        print(f"  机型:      {UA_TYPES.get(b['ua_type'], '未知')}")

    if "location" in parsed:
        loc = parsed["location"]
        print(f"  纬度:      {loc['latitude']:.6f}")
        print(f"  经度:      {loc['longitude']:.6f}")
        print(f"  大地高度:  {loc['altitude_geodetic']:.1f}m")
        print(f"  气压高度:  {loc['altitude_pressure']:.1f}m")
        print(f"  离地高度:  {loc['height_agl']:.1f}m")
        print(f"  水平速度:  {loc['speed_horizontal']:.1f}m/s")
        print(f"  垂直速度:  {loc['speed_vertical']:.1f}m/s")

    if "location" in parsed:
        lat = parsed["location"]["latitude"]
        lon = parsed["location"]["longitude"]
        print(f"  地图:      https://www.google.com/maps?q={lat},{lon}")

    print(f"{'='*60}")


# ═══════════════════════════════════════════════════════════════
# BLE 扫描
# ═══════════════════════════════════════════════════════════════

def detection_callback(device, advertisement_data):
    """BLE 扫描回调 — 过滤 ODID 广播并解析"""
    odid_data = None
    try:
        odid_data = advertisement_data.service_data.get(ODID_UUID_STR)
    except Exception:
        pass

    if odid_data is None:
        return

    parsed = parse_odid_pack(odid_data)
    if parsed is None:
        return

    rssi = getattr(advertisement_data, 'rssi', 0)
    print_detection(parsed, mac=device.address, rssi=rssi, source="BLE")


async def scan_ble_continuous(timeout=None):
    """BLE 持续扫描"""
    try:
        from bleak import BleakScanner
    except ImportError:
        print("请安装 bleak: pip install bleak")
        return

    scanner = BleakScanner(
        detection_callback=detection_callback,
        service_uuids=[ODID_UUID_STR]
    )

    print("\U0001f50d 开始 BLE 扫描 Open Drone ID 广播...")
    print(f"   Service UUID: 0x{ODID_SERVICE_UUID:04X}")
    print(f"   按 Ctrl+C 停止\n")

    try:
        await scanner.start()
        if timeout:
            await asyncio.sleep(timeout)
        else:
            while True:
                await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        await scanner.stop()
        print("\n扫描已停止")


async def scan_ble_once():
    """BLE 单次扫描"""
    try:
        from bleak import BleakScanner
    except ImportError:
        print("请安装 bleak: pip install bleak")
        return

    scanner = BleakScanner(
        detection_callback=detection_callback,
        service_uuids=[ODID_UUID_STR]
    )

    print(f"🔍 BLE 扫描 Open Drone ID 广播 (单次, 10秒)...")
    await scanner.start()
    await asyncio.sleep(10)
    await scanner.stop()
    print("扫描完成")


# ═══════════════════════════════════════════════════════════════
# WiFi Beacon 扫描
# ═══════════════════════════════════════════════════════════════

def find_odid_in_beacon(frame_data: bytes) -> bytes:
    """
    在 802.11 Beacon 帧中查找 ODID Vendor Specific IE

    802.11 Beacon 帧头部 = 36 字节固定字段，然后是 Tagged Parameters (IEs)
    每个 IE: [Tag(1B)] [Length(1B)] [Data(Length bytes)]
    Tag 221 = Vendor Specific → OUI(3B) + Vendor Data

    返回 ODID Message Pack 数据 (OUI 之后的 payload)
    """
    if len(frame_data) < 36:
        return None

    offset = 36  # 跳过固定头部

    while offset < len(frame_data) - 1:
        tag = frame_data[offset]
        length = frame_data[offset + 1]

        if offset + 2 + length > len(frame_data):
            break

        if tag == 221 and length >= 3:  # Vendor Specific IE
            oui = frame_data[offset + 2:offset + 5]
            if oui == ODID_WIFI_OUI:
                # 返回 OUI 之后的数据 (ODID Message Pack)
                ie_data = frame_data[offset + 2:offset + 2 + length]
                return ie_data[3:]  # skip 3-byte OUI

        offset += 2 + length

    return None


async def scan_wifi_continuous(timeout=None, interface=None):
    """WiFi Beacon 持续扫描 (scapy)"""
    try:
        from scapy.all import sniff, Dot11Beacon, Dot11
    except ImportError:
        print("请安装 scapy: pip install scapy")
        print("Windows 用户还需安装 Npcap: https://npcap.com")
        print("安装时务必勾选 'Support raw 802.11 traffic'")
        return

    running = True
    loop = asyncio.get_event_loop()

    def packet_handler(pkt):
        nonlocal running
        if not running:
            return True

        try:
            if not pkt.haslayer(Dot11Beacon):
                return

            raw = bytes(pkt)
            odid_data = find_odid_in_beacon(raw)
            if odid_data is None:
                return

            parsed = parse_odid_pack(odid_data)
            if parsed is None:
                return

            mac = pkt[Dot11].addr2 if pkt.haslayer(Dot11) else ""
            rssi = getattr(pkt, 'dBm_AntSignal', 0)
            print_detection(parsed, mac=mac, rssi=rssi, source="WiFi Beacon")

        except Exception:
            pass

    print("🔍 开始 WiFi Beacon 扫描 Open Drone ID 广播...")
    print(f"   OUI: 0xFA0B0C (Vendor Specific IE tag 221)")
    if interface:
        print(f"   网卡: {interface}")
    print(f"   按 Ctrl+C 停止\n")
    print(f"   提示: 如果收不到任何数据，请确认:")
    print(f"     1. Npcap 已安装 (勾选 raw 802.11)")
    print(f"     2. 无人机已开机且在 WiFi 范围内\n")

    def sniff_thread():
        kwargs = {"prn": packet_handler, "store": 0}
        if interface:
            kwargs["iface"] = interface

        while running:
            try:
                sniff(timeout=1.0, **kwargs)
            except Exception as e:
                if running:
                    print(f"  [WiFi] 嗅探错误: {e}")
                break

    try:
        if timeout:
            await asyncio.wait_for(
                loop.run_in_executor(None, sniff_thread),
                timeout=timeout
            )
        else:
            await loop.run_in_executor(None, sniff_thread)
    except asyncio.TimeoutError:
        pass
    except KeyboardInterrupt:
        pass
    finally:
        running = False
        print("\n扫描已停止")


async def scan_wifi_once(interface=None):
    """WiFi Beacon 单次扫描"""
    await scan_wifi_continuous(timeout=10, interface=interface)


# ═══════════════════════════════════════════════════════════════
# 主程序
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="无人机 RID 扫描器 (BLE + WiFi Beacon)")
    parser.add_argument(
        "--mode", "-m",
        choices=["ble", "wifi"],
        default="ble",
        help="扫描模式: ble (蓝牙) 或 wifi (WiFi Beacon)"
    )
    parser.add_argument(
        "--interface", "-i",
        default=None,
        help="WiFi 网卡接口名称 (如 Wi-Fi, wlan0)"
    )
    parser.add_argument(
        "--once", action="store_true",
        help="扫描一次后退出"
    )
    parser.add_argument(
        "--timeout", type=int, default=None,
        help="扫描时长(秒)"
    )
    args = parser.parse_args()

    if args.mode == "wifi":
        if args.once:
            asyncio.run(scan_wifi_once(interface=args.interface))
        else:
            asyncio.run(scan_wifi_continuous(
                timeout=args.timeout, interface=args.interface
            ))
    else:
        if args.once:
            asyncio.run(scan_ble_once())
        else:
            asyncio.run(scan_ble_continuous(timeout=args.timeout))


if __name__ == "__main__":
    main()
