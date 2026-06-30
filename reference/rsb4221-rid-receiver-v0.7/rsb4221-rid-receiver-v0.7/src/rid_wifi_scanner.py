# -*- coding: utf-8 -*-
"""
RID WiFi 扫描器 v0.7 — 增强版
监听所有帧类型，从 Beacon 和 Vendor IE 中提取 RID 信息
包括位置数据
"""
import socket
import struct
import time
import threading
import math
import sys

_scanning = False
_scan_thread = None
_latest_drones = []
_lock = threading.Lock()

DJI_OUI = {
    "60:60:1f": "DJI",
    "34:d2:62": "DJI", 
    "04:d6:aa": "DJI",
}

# ASTM RID 消息类型
MSG_BASIC_ID = 0
MSG_LOCATION = 1
MSG_AUTH = 2
MSG_SELF_ID = 3
MSG_OPERATOR_ID = 4


def parse_astm_rid(frame_data):
    """
    解析 ASTM Remote ID 消息（Message Pack）
    参考: ASTM F3411-22a / ASD-STAN prEN 4709-002
    
    Message Pack 结构:
    - byte 0: protocol_version (top 4 bits) | message_count (bottom 4 bits)
    - byte 1..n: messages (each 25 bytes or less)
    
    每条消息:
    - byte 0: [header] top 4 bits = msg_type, bottom 4 bits = ?
    - byte 1+: payload
    """
    result = {}
    
    if len(frame_data) < 2:
        return result
    
    protocol = (ord(frame_data[0]) >> 4) & 0xF
    msg_count = ord(frame_data[0]) & 0xF
    
    pos = 1
    msgs = []
    
    while pos < len(frame_data) and len(msgs) < msg_count:
        msg_type = ord(frame_data[pos]) & 0x0F
        
        if msg_type == MSG_BASIC_ID and pos + 22 <= len(frame_data):
            # Basic ID: byte 1 = id_type|ua_type, byte 2-21 = UAS ID
            id_type = (ord(frame_data[pos]) >> 4) & 0xF
            uas_id = frame_data[pos+2:pos+22].split(b"\x00")[0]
            try:
                result["drone_id"] = uas_id.decode("ascii", errors="replace")
            except:
                result["drone_id"] = repr(uas_id)
            pos += 22
            
        elif msg_type == MSG_LOCATION and pos + 16 <= len(frame_data):
            # Location Message (16+ bytes)
            try:
                lat_raw = struct.unpack(">i", frame_data[pos+6:pos+10])[0]
                lon_raw = struct.unpack(">i", frame_data[pos+10:pos+14])[0]
                lat = lat_raw / 1e7
                lon = lon_raw / 1e7
                
                # Altitude (2 bytes, 0.5m resolution, signed)
                if pos + 16 <= len(frame_data):
                    alt_raw = struct.unpack(">h", frame_data[pos+14:pos+16])[0]
                    alt_geo = alt_raw * 0.5
                else:
                    alt_geo = None
                
                # Speed (2 bytes at offset 20)
                speed = None
                if pos + 22 <= len(frame_data):
                    spd_raw = struct.unpack(">H", frame_data[pos+20:pos+22])[0]
                    if spd_raw != 0xFFFF:
                        speed = spd_raw * 0.01
                
                # Heading (2 bytes at offset 22)
                heading = None
                if pos + 24 <= len(frame_data):
                    hdg_raw = struct.unpack(">H", frame_data[pos+22:pos+24])[0]
                    if hdg_raw != 0xFFFF:
                        heading = hdg_raw * 0.01
                
                result["location"] = {
                    "lat": lat,
                    "lon": lon,
                    "alt": alt_geo,
                    "speed": speed,
                    "heading": heading,
                }
            except:
                pass
            pos += 25
            
        elif msg_type == MSG_OPERATOR_ID and pos + 6 <= len(frame_data):
            # Operator ID (byte 1-5)
            op_id = frame_data[pos+1:pos+6].split(b"\x00")[0]
            try:
                result["operator_id"] = op_id.decode("ascii", errors="replace")
            except:
                result["operator_id"] = repr(op_id)
            pos += 6
            
        else:
            # Skip unknown message (usually 25 bytes)
            pos += min(25, len(frame_data) - pos)
    
    result["messages"] = msgs
    return result


def parse_beacon_vsie(pkt, radiotap_len):
    """
    从 Beacon 帧体的 Vendor Specific IE (tag=221) 提取 RID 数据
    DJI 特定 OUI: 0xFA, 0x0B, 0xBC
    ASTM RID OUI: 0xFA, 0x0B, 0xBC
    """
    hdr_end = radiotap_len + 24
    pos = hdr_end + 12  # skip fixed params (timestamp + interval + cap)
    
    if pos + 2 > len(pkt):
        return None
    
    results = []
    
    while pos + 2 < len(pkt):
        tag = ord(pkt[pos])
        tlen = ord(pkt[pos+1])
        if pos + 2 + tlen > len(pkt):
            break
        val = pkt[pos+2:pos+2+tlen]
        
        if tag == 221 and tlen >= 5:  # Vendor Specific IE
            oui = val[:3]
            # DJI/ASTM RID OUI
            if oui in (b"\xFA\x0B\xBC", b"\xFA\x0B\xBC"):
                # 前3字节是OUI，第4字节可能是类型
                if tlen > 4:
                    rid_data = val[3:]
                    parsed = parse_astm_rid(rid_data)
                    if parsed:
                        results.append(parsed)
            # WFA (Wi-Fi Alliance) OUI
            elif oui == b"\x50\x6F\x9A" and tlen > 3:
                # WSC, P2P等
                pass
            elif oui == b"\x00\x50\xF2" and tlen > 3:
                # Microsoft/WiFi
                pass
        
        pos += 2 + tlen
    
    return results if results else None


def parse_action_frame(pkt, radiotap_len):
    """
    解析 Public Action 帧中的 RID 数据
    Category 4 (Public) / Category 13 (NAN)
    """
    if len(pkt) < radiotap_len + 28:
        return None
    
    fc = struct.unpack("<H", pkt[radiotap_len:radiotap_len+2])[0]
    ftype = (fc >> 2) & 0x3
    fsubtype = (fc >> 4) & 0xF
    
    # Action frame = type 0, subtype 13
    if ftype != 0 or fsubtype != 13:
        return None
    
    body_start = radiotap_len + 24  # MAC header = 24 bytes
    if body_start + 2 > len(pkt):
        return None
    
    category = ord(pkt[body_start])
    action_code = ord(pkt[body_start + 1])
    
    # Public Action (category 4): OUI-based
    if category == 4 and body_start + 6 <= len(pkt):
        oui = pkt[body_start+2:body_start+5]
        oui_type = ord(pkt[body_start+5])
        
        # ASTM RID (OUI FA:0B:BC, type = 0x0F 或其他)
        if oui == b"\xFA\x0B\xBC" and body_start + 7 <= len(pkt):
            rid_data = pkt[body_start+6:]
            parsed = parse_astm_rid(rid_data)
            return parsed
    
    # NAN (category 13): Service Discovery Frame
    if category == 13 and body_start + 4 <= len(pkt):
        # NAN SDF
        pass
    
    return None


def extract_rssi(pkt, radiotap_len):
    if radiotap_len < 12:
        return 0
    try:
        rt_flags = struct.unpack("<H", pkt[8:10])[0]
        if rt_flags & 0x0020:
            val = ord(pkt[10])
            if val > 127:
                val -= 256
            return val
    except:
        pass
    return 0


def extract_mac(pkt, radiotap_len):
    try:
        mac = pkt[radiotap_len+10:radiotap_len+16]
        return ":".join("%02x" % ord(b) for b in mac)
    except:
        return ""


def extract_ssid_from_beacon(pkt, radiotap_len):
    hdr_end = radiotap_len + 24
    sid_off = hdr_end + 12
    if sid_off + 2 > len(pkt):
        return None
    tag_id = ord(pkt[sid_off])
    tag_len = ord(pkt[sid_off+1])
    if tag_id == 0 and tag_len > 0 and sid_off + 2 + tag_len <= len(pkt):
        return pkt[sid_off+2:sid_off+2+tag_len]
    return None


def scan_loop(iface="wlan0", channel=6, callback=None):
    global _scanning, _latest_drones
    import select
    
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
    s.bind((iface, 0))
    s.settimeout(1)
    
    seen_drones = {}
    total_packets = 0
    beacon_count = 0
    action_count = 0
    
    while _scanning:
        try:
            ready, _, _ = select.select([s], [], [], 1)
            if not ready:
                continue
            
            pkt = s.recv(4096)
            total_packets += 1
            
            if len(pkt) < 36:
                continue
            
            radiotap_len = struct.unpack("<H", pkt[2:4])[0]
            if radiotap_len < 8 or radiotap_len + 28 > len(pkt):
                continue
            
            fc = struct.unpack("<H", pkt[radiotap_len:radiotap_len+2])[0]
            ftype = (fc >> 2) & 0x3
            fsubtype = (fc >> 4) & 0xF
            
            mac = extract_mac(pkt, radiotap_len)
            rssi = extract_rssi(pkt, radiotap_len)
            now = time.time()
            
            drone_id = None
            location = None
            
            # === Beacon (MGMT, subtype 8) ===
            if ftype == 0 and fsubtype == 8:
                beacon_count += 1
                ssid = extract_ssid_from_beacon(pkt, radiotap_len)
                
                if ssid and ssid.startswith("RID-"):
                    drone_id = ssid
                    # 从 VSIE 尝试提取位置
                    vsie_data = parse_beacon_vsie(pkt, radiotap_len)
                    if vsie_data and len(vsie_data) > 0:
                        loc = vsie_data[0].get("location")
                        if loc and loc.get("lat"):
                            location = loc
                        if vsie_data[0].get("drone_id"):
                            drone_id = vsie_data[0]["drone_id"]
            
            # === Action frames (MGMT, subtype 13) ===
            elif ftype == 0 and fsubtype == 13:
                action_count += 1
                parsed = parse_action_frame(pkt, radiotap_len)
                if parsed:
                    if parsed.get("drone_id"):
                        drone_id = parsed["drone_id"]
                    if parsed.get("location"):
                        location = parsed["location"]
            
            if not drone_id:
                continue
            
            # === 更新无人机数据 ===
            drone_data = {
                "drone_id": drone_id.decode() if isinstance(drone_id, bytes) else drone_id,
                "mac": mac,
                "rssi": rssi,
                "source": "wifi_rid",
                "first_seen": now,
                "last_seen": now,
                "location": location,
            }
            
            if drone_id in seen_drones:
                seen_drones[drone_id]["last_seen"] = now
                seen_drones[drone_id]["count"] += 1
                if location:
                    seen_drones[drone_id]["location"] = location
                if rssi:
                    seen_drones[drone_id]["rssi"] = rssi
            else:
                seen_drones[drone_id] = {
                    "drone_id": drone_data["drone_id"],
                    "mac": mac,
                    "rssi": rssi,
                    "first_seen": now,
                    "last_seen": now,
                    "count": 1,
                    "location": location,
                }
            
            if callback:
                try:
                    callback(drone_data)
                except:
                    pass
            
            # 清理过期（>30秒）
            expired = [d for d, v in seen_drones.items() if now - v["last_seen"] > 30]
            for d in expired:
                del seen_drones[d]
            
            with _lock:
                _latest_drones = [
                    {
                        "drone_id": v["drone_id"],
                        "mac": v.get("mac", ""),
                        "rssi": v.get("rssi", 0),
                        "first_seen": v.get("first_seen", 0),
                        "last_seen": v.get("last_seen", 0),
                        "count": v.get("count", 0),
                        "location": v.get("location"),
                    }
                    for v in sorted(seen_drones.values(), 
                                   key=lambda x: x.get("first_seen", 0))
                ]
                
        except socket.timeout:
            continue
        except Exception as e:
            if str(e):
                pass
    
    s.close()


def start_scan(iface="wlan0", channel=6, callback=None):
    global _scanning, _scan_thread
    if _scanning:
        return False
    _scanning = True
    _scan_thread = threading.Thread(target=scan_loop, args=(iface, channel, callback))
    _scan_thread.daemon = True
    _scan_thread.start()
    return True


def stop_scan():
    global _scanning, _scan_thread
    _scanning = False
    if _scan_thread:
        _scan_thread.join(timeout=3)
        _scan_thread = None
    return True


def get_latest_drones():
    with _lock:
        return list(_latest_drones)


def is_scanning():
    return _scanning


if __name__ == "__main__":
    import sys, time
    iface = sys.argv[1] if len(sys.argv) > 1 else "wlan0"
    def cb(d):
        loc = d.get("location") or {}
        print "[RID] %s  RSSI:%d  loc:%s" % (
            d["drone_id"], d.get("rssi",0),
            "YES" if loc.get("lat") else "NO")
    print "Starting v0.7 scanner on %s..." % iface
    start_scan(iface=iface, callback=cb)
    try:
        while True:
            time.sleep(30)
            drones = get_latest_drones()
            print "--- Active: %d ---" % len(drones)
            for d in drones:
                loc = d.get("location") or {}
                print "  %s  RSSI:%d  pkt:%d  lat:%.4f  lon:%.4f" % (
                    d["drone_id"], d["rssi"], d["count"],
                    loc.get("lat", 0), loc.get("lon", 0))
    except KeyboardInterrupt:
        stop_scan()
