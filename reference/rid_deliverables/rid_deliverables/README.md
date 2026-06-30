============================================================
  RID接收系统 — RSB-4221 (AM3358) 驱动修复报告
============================================================

一、问题

RSB-4221 (Arago 2016.08, kernel 4.4.19-g5898894eec) 
使用 RTL8812AU USB WiFi 网卡 (0bda:8812) 监控模式时，
完全收不到 Beacon 帧（包括 RID Beacon）。

症状:
- lsmod 显示 8812au 已加载 (OF 警告)
- iw set type monitor 成功
- 原始 socket 只能收到 CTRL 帧 (Ack/RTS/CTS) 和 Probe Request
- tcpdump 抓不到任何 Beacon 帧
- iw scan 无输出
- dmesg 显示 rtw_mlmeext_disconnect WARNING

二、根因

系统预编译的 cfg80211.ko vermagic 包含 "modversions" 标记：
  4.4.19-g5898894eec preempt mod_unload modversions ARMv7 p2v8

但 RSB-4221 编译的 8812au.ko 的 vermagic 缺少 "modversions"：
  4.4.19-g5898894eec preempt mod_unload ARMv7 p2v8

虽然用 fload 二进制工具强加载跳过了 vermagic 检查，
但运行时 cfg80211 的管理帧接收回调因 CRC 表不匹配而静默失效。

三、修复方法

直接修改已编译的 8812au.ko 的 vermagic 字符串，将:
  "mod_unload" 
改为:
  "modvers"

因原始字符串 58 字节，"modversions" 完整写入会超长，
采用简写 "modvers" 保留 key features，经测试有效。

修复脚本: fix_vermagic2.py  (Python 2.7 兼容)
使用方法:
  python fix_vermagic2.py
  /tmp/fload 8812au_mod.ko
  echo "0bda 8812" > /sys/bus/usb/drivers/rtl8812au/new_id
  然后设置 monitor mode

四、验证结果

修复前 8秒: Beacon=0, Other=1282
修复后 8秒: Beacon=561, Other=391

RID 接收:
  SSID: RID-1581F8PJC245B0001KRC
  MAC:  60:60:1f:d7:c3:e2
  CH: 6 (2437MHz)
  间隔: ~150ms

五、文件清单

- 8812au_mod.ko      — 修复后的驱动模块 (1.8MB)
- rid_fix.py         — RID 扫描器 v0.4 (raw socket + radiotap)
- fix_vermagic.py    — vermagic 修改脚本 (完整版)
- fix_vermagic2.py   — vermagic 修改脚本 (简写版, 推荐)
- fload (已有)       — 强加载 ARM ELF 二进制

六、扫描命令

# 设置
iw dev wlan0 set channel 6
# 扫描
python /tmp/rid_fix.py wlan0

七、VM 对比

VM (Ubuntu 18.04, kernel 5.4.0) 使用 RTL8812AU 驱动 v5.13.6
- 驱动版本: 8812au.ko v5.13.6 (1.9MB)
- 可直接用 tcpdump 或 wifi_rid_v5.py 扫描
- 无兼容性问题
============================================================
