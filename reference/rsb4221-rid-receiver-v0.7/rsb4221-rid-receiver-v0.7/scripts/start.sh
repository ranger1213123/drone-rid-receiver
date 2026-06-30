#!/bin/sh
# RID Receiver Launcher (整合 WiFi 扫描 + Web 服务器)
cd /opt/rid-receiver
export PYTHONPATH=/opt/rid-receiver
export PATH=$PATH:/sbin:/usr/sbin

PORT=${1:-5000}
IFACE=${2:-wlan0}
CHANNEL=${3:-6}

# 设置 monitor mode
ip link set $IFACE down 2>/dev/null
iw dev $IFACE set type monitor 2>/dev/null
ip link set $IFACE up 2>/dev/null
iw dev $IFACE set channel $CHANNEL 2>/dev/null

echo "=== RSB-4221 RID Receiver ==="
echo "Port: $PORT  Iface: $IFACE  CH: $CHANNEL"
echo "Starting..."

python2 rid_launcher.py --port $PORT --iface $IFACE --channel $CHANNEL --no-driver
