"""WiFi 扫描 — 列出所有附近 WiFi 网络"""
import subprocess, re

print("Scanning all nearby WiFi networks...\n")

result = subprocess.run(
    ['netsh', 'wlan', 'show', 'networks', 'mode=bssid'],
    capture_output=True, text=True, encoding='gbk', errors='replace'
)

networks = []
current = None

for line in result.stdout.splitlines():
    line = line.strip()
    if not line:
        continue

    m = re.match(r'^SSID \d+ : (.+)', line)
    if m:
        current = {"ssid": m.group(1), "bssids": []}
        networks.append(current)
        continue

    m = re.match(r'BSSID \d+\s*: ([0-9a-fA-F:]+)', line)
    if m and current is not None:
        current["bssids"].append(m.group(1))
        continue

    m = re.match(r'信号\s*: (\d+)%', line)
    if m and current is not None:
        current["signal"] = m.group(1) + "%"
        continue

    m = re.match(r'频道\s*: (\d+)', line)
    if m and current is not None:
        current["channel"] = m.group(1)
        continue

print(f"{'SSID':<30} {'BSSID':<20} {'信号':>6} {'频道':>6}")
print("-" * 66)

for net in networks:
    ssid = net.get("ssid", "(hidden)")[:29]
    signal = net.get("signal", "?")
    channel = net.get("channel", "?")
    for bssid in net.get("bssids", ["?"]):
        # 标记可能的无人机
        tag = ""
        upper = ssid.upper()
        if any(k in upper for k in ['DJI', 'RID', 'DRONE', 'ODID', 'RC', 'MAVIC', 'MINI', 'AIR', 'UAV']):
            tag = " <-- DRONE?"
        print(f"{ssid:<30} {bssid:<20} {signal:>6} {channel:>6}{tag}")

print(f"\nTotal: {len(networks)} networks")
