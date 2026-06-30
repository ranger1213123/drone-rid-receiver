#!/bin/sh
# RSB-4221 RID Receiver 启动脚本
# 用法: ./start_rid.sh [port]
# 默认端口 5000
PORT=${1:-5000}
IFACE=${2:-wlan0}
CHANNEL=${3:-6}

cd /opt/rid-receiver
export PYTHONPATH=/opt/rid-receiver

echo "=== RSB-4221 RID Receiver ==="
echo "Port:    $PORT"
echo "Iface:   $IFACE"
echo "Channel: $CHANNEL"
echo "============================="

# 如果需要自动加载驱动，去掉下面注释
# echo "Loading driver..."
# /sbin/modprobe cfg80211 2>/dev/null
# /tmp/fload /home/root/8812au_drv/8812au_mod.ko 2>/dev/null
# echo "0bda 8812" > /sys/bus/usb/drivers/rtl8812au/new_id 2>/dev/null
# sleep 2

# 设置 monitor mode
export PATH=$PATH:/sbin:/usr/sbin
ip link set $IFACE down 2>/dev/null
iw dev $IFACE set type monitor 2>/dev/null
ip link set $IFACE up 2>/dev/null
iw dev $IFACE set channel $CHANNEL 2>/dev/null

# 启动 launcher
python2 rid_launcher.py --port $PORT --iface $IFACE --channel $CHANNEL --no-driver
