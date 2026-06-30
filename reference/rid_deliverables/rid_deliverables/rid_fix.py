#!/usr/bin/env python2
# -*- coding: utf-8 -*-
"""
RID Scanner v0.4 (RSB-4221)
正确解析 radiotap header + 802.11 frames
"""
import socket, struct, sys, time

IFACE = sys.argv[1] if len(sys.argv) > 1 else "wlan0"

s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
s.bind((IFACE, 0))
s.settimeout(30)

print("=" * 60)
print("  RID Scanner v0.4 (RSB-4221)")
print("  接口: %s" % IFACE)
print("  等待 RID beacon...")
print("=" * 60)

start = time.time()
count = 0
rid_found = False

while True:
    try:
        pkt = s.recv(4096)
        count += 1
        
        # 检查最小长度: radiotap header (至少8字节) + 802.11 header (24字节)
        if len(pkt) < 32:
            continue
            
        # 解析 radiotap header 长度 (字节2-3)
        radiotap_len = struct.unpack("<H", pkt[2:4])[0]
        
        if radiotap_len < 8 or radiotap_len + 24 > len(pkt):
            continue
            
        # 跳过 radiotap header，到达 802.11 帧
        llc_start = radiotap_len
        
        # 802.11 Frame Control (2字节)
        fc = struct.unpack("<H", pkt[llc_start:llc_start+2])[0]
        ftype = (fc >> 2) & 0x3
        fsubtype = (fc >> 4) & 0xF
        
        # Beacon (type=0, subtype=8) 或 Probe Response (type=0, subtype=5)
        if ftype == 0 and fsubtype == 8:
            # 解析 tagged parameters
            # 802.11 Beacon: 24 bytes MAC header + 12 bytes fixed params
            hdr_end = llc_start + 24
            ssid_tag_offset = hdr_end + 12  # 跳过 fixed params
            
            if ssid_tag_offset + 2 > len(pkt):
                continue
                
            tag_id = ord(pkt[ssid_tag_offset])
            tag_len = ord(pkt[ssid_tag_offset + 1])
            
            if tag_id == 0 and tag_len > 0 and ssid_tag_offset + 2 + tag_len <= len(pkt):
                ssid = pkt[ssid_tag_offset+2:ssid_tag_offset+2+tag_len]
                
                if ssid.startswith("RID-"):
                    rid_found = True
                    elapsed = int(time.time() - start)
                    mac_addr = pkt[llc_start+10:llc_start+16]
                    mac_str = ":".join("%02x" % ord(b) for b in mac_addr)
                    
                    # 从 radiotap 提取 RSSI
                    rssi = "?"
                    if radiotap_len >= 10:
                        # Radiotap flags at byte 8
                        rt_flags = struct.unpack("<H", pkt[8:10])[0]
                        # Check antenna signal present
                        if rt_flags & 0x0020 and radiotap_len >= 12:
                            rssi_val = ord(pkt[10]) - 256 if ord(pkt[10]) > 127 else ord(pkt[10])
                            rssi = "%ddBm" % rssi_val
                    
                    print("\n" + "=" * 60)
                    print("  [RID] 无人机检测到！")
                    print("=" * 60)
                    print("  SSID:    %s" % ssid)
                    print("  MAC:     %s" % mac_str)
                    print("  RSSI:    %s" % rssi)
                    print("  包计数:  %d" % count)
                    print("  运行:    %ds" % (time.time() - start))
                    print("=" * 60)
                    
        if count % 50 == 0:
            sys.stdout.write(".")
            sys.stdout.flush()
            
        if count > 5000:
            break
            
    except socket.timeout:
        print("\n[超时] 30秒无数据")
        break
    except KeyboardInterrupt:
        break

elapsed = int(time.time() - start)
print("\n=== 完成: %ds / %d pkt / RID:%d ===" % (elapsed, count, 1 if rid_found else 0))
s.close()
