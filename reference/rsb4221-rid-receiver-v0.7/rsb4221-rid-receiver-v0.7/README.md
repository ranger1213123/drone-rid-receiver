# 无人机 Remote ID 接收装置 (RSB-4221)

## 概述

基于 **Avalue RSB-4221** (TI AM3358 Cortex-A8) 开发的无人机 RID (Remote ID) 接收装置。支持两种接收方式：

- **串口 RID 接收** — 通过 CH340 USB 转串口读取 ESP32 解码的 RID JSON 数据 (115200 baud)
- **WiFi 被动扫描** — 通过 RTL8812AU USB WiFi 网卡监听蓝牙/WiFi RID 广播包

搭配轻量级 Web 界面实现无人机实时监控、电力线距离告警。

---

## 硬件清单

| 组件 | 型号 | 说明 |
|---|---|---|
| 主控板 | Avalue RSB-4221 | TI AM3358, Cortex-A8, 512MB RAM |
| USB 串口 | CH340G | 连接 ESP32 RID 解码器，115200 baud |
| WiFi 网卡 | RTL8812AU | USB 双频 802.11ac，用于 monitor mode 扫描 |
| ESP32 | 任意型号 | 接收 BT/WiFi RID 广播，解码后通过串口输出 JSON |

---

## 软件架构

```
/opt/rid-receiver/
├── rid_launcher.py          # 主启动器 — 整合 WiFi/串口/Web 服务
├── rid_serial_receiver.py   # 串口 RID 接收器 — 解析 ESP32 JSON 数据
├── rid_wifi_scanner.py      # WiFi 扫描器 — monitor mode 嗅探 RID 包
├── server_web.py            # Web 服务器 — REST API + 监控界面 (端口 5000)
├── minimal_serial.py        # 纯 Python 串口库 (替代 pyserial，适用于嵌入式)
├── init_lines.py            # 电力线初始化脚本 (插入测试数据)
├── rsb_serial_receiver.py   # (备用) 基于 pyserial 的串口接收器
├── test.py                  # 数据库/处理逻辑测试脚本
├── scripts/
│   ├── start.sh             # 启动脚本 (整合版)
│   └── start_rid.sh         # 启动脚本 (简化版)
├── data/
│   └── rid.db               # SQLite 数据库 (无人机数据、轨迹、告警)
└── config/                  # 配置目录 (预留)
```

### 运行环境

- **操作系统**: Linux (内核 3.x+)
- **Python**: 2.7 (RSB-4221 原生)
- **依赖**: 仅标准库 + `termios` (Python 内置)

> ⚠️ RSB-4221 的 Arago Linux 无 pip/apt，所有代码均使用 Python 2.7 标准库，无第三方依赖。

---

## 快速开始

### 1. 部署到 RSB-4221

```bash
# 将本项目复制到 RSB-4221
scp -r rsb4221-rid-receiver-v0.7 root@192.168.8.106:/opt/rid-receiver

# 设置权限
chmod +x /opt/rid-receiver/scripts/*.sh
```

### 2. AP 热点模式启动（WiFi 扫描关闭）

当 wlan0 已配置为 AP 热点时，跳过 WiFi 扫描，仅启动串口 RID + Web 服务器：

```bash
cd /opt/rid-receiver
export PATH=$PATH:/sbin:/usr/sbin

python2 rid_launcher.py \
  --port 5000 \
  --serial /dev/ttyUSB0 \
  --serial-baud 115200 \
  --no-driver
```

### 3. WiFi 扫描模式启动

当 wlan0 可用作 monitor mode 时：

```bash
cd /opt/rid-receiver
python2 rid_launcher.py \
  --port 5000 \
  --iface wlan0 \
  --channel 6 \
  --serial /dev/ttyUSB0
```

### 4. 仅串口 RID 接收（独立运行）

```bash
python2 rid_serial_receiver.py
```

### 5. 仅 Web 服务器（独立运行）

```bash
python2 server_web.py --port 5000
```

---

## Web 界面

访问 `http://<RSB-4221_IP>:5000/`

### REST API

| 端点 | 方法 | 说明 |
|---|---|---|
| `/` | GET | HTML 管理界面 |
| `/api/status` | GET | 系统状态、无人机列表、告警日志、电力线 |
| `/api/drones` | GET | 全部无人机数据 |
| `/api/alerts` | GET | 告警历史 |
| `/api/powerlines` | GET | 电力线列表 |
| `/api/start` | POST | 启动模拟扫描 |
| `/api/stop` | POST | 停止模拟扫描 |
| `/api/powerlines` | POST | 添加电力线 |

### `/api/status` 响应示例

```json
{
  "running": false,
  "drone_count": 1,
  "alert_count": 12,
  "pl_count": 2,
  "drones": [
    {
      "id": "1581F8PJC245B0001KRC",
      "lat": 30.6150,
      "lon": 104.0672,
      "alt": 565,
      "min_distance": 150.3,
      "status": "warning"
    }
  ],
  "logs": [
    {
      "level": "warning",
      "message": "[注意] 无人机 1581F8... 距离 测试线1 150.3m (阈值: 200m)"
    }
  ],
  "power_lines": [
    {
      "id": 1,
      "name": "测试线1",
      "lat1": 39.9042,
      "lon1": 116.4074,
      "lat2": 39.9142,
      "lon2": 116.4274
    }
  ],
  "now": "14:30:25"
}
```

---

## 串口 RID 数据格式

ESP32 通过 CH340 以 **115200 baud** 发送 JSON 格式数据，每行一条：

### Format 1 — 心跳包
```json
{"devId":"EXD001","count":87}
```

### Format 2 — 完整数据包
```json
{
  "devId": "EXD001",
  "data": {
    "osid": "1581F8PJC245B0001KRC",
    "RSSI": -27,
    "Fre": 1,
    "UAType": 2,
    "Status": 2,
    "Heading": 361,
    "Speed": 0,
    "Uprate": 0,
    "Lon": 0.00000,
    "Lat": 0.00000,
    "AltGeo": -1000,
    "AltBaro": 565,
    "Height": 1,
    "Op_Lon": 0.00000,
    "Op_Lat": 0.00000,
    "Op_Alt": -1000,
    "UATime": 0
  },
  "count": 88
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `devId` | string | 设备标识符 (EXD001 = 转发器) |
| `data.osid` | string | 无人机 ID (ICAO 或序列号) |
| `data.Op_Lat/Op_Lon` | float | 操作员位置 (度) |
| `data.Lat/Lon` | float | 无人机位置 (度) |
| `data.AltBaro` | int | 气压计高度 (m) |
| `data.AltGeo` | int | 地理高度 (m) |
| `data.RSSI` | int | 信号强度 (dBm) |
| `data.Heading` | int | 航向 (度) |
| `data.Speed` | int | 速度 (m/s) |

---

## 电力线告警系统

支持配置电力线坐标，系统自动计算无人机到电力线的最短三维距离：

| 级别 | 距离阈值 | 颜色 | 说明 |
|---|---|---|---|
| 注意 (Warning) | ≤ 200m | 🟡 黄 | 巡视提醒 |
| 严重告警 (Severe) | ≤ 100m | 🟠 橙 | 加强注意 |
| 立即驱离 (Critical) | ≤ 50m | 🔴 红 | 危险接近 |

### 添加电力线

```bash
curl -X POST http://192.168.8.106:5000/api/powerlines \
  -H "Content-Type: application/json" \
  -d '{"name":"220kV 安龙线","lat1":30.6100,"lon1":104.0600,"alt1":50,"lat2":30.6200,"lon2":104.0750,"alt2":55}'
```

---

## AP 热点配置

RSB-4221 的 wlan0 可用作 AP 热点，此时 WiFi RID 扫描不可用。

```bash
# 配置文件位置
/etc/hostapd.conf          # hostapd 配置
/etc/udhcpd.conf           # DHCP 配置

# SSID: RSB4221-AP
# 密码: 12345678
# 网关: 192.168.4.1
# DHCP: 192.168.4.2 - 192.168.4.100
```

---

## 版本历史

| 版本 | 日期 | 变更 |
|---|---|---|
| v0.7 | 2026-05 | 当前版本。修复 Content-Length 编码 bug，支持 AP 模式跳过 WiFi 扫描，串口 JSON 解析优化 |

---

## 文件清单

```
rsb4221-rid-receiver-v0.7/
├── README.md                # 本说明书
├── src/
│   ├── rid_launcher.py      # 主启动器
│   ├── rid_serial_receiver.py  # 串口 RID 接收器
│   ├── rid_wifi_scanner.py  # WiFi RID 扫描器
│   ├── server_web.py        # Web 服务器
│   ├── minimal_serial.py    # 纯 Python 串口库
│   ├── init_lines.py        # 电力线初始化
│   ├── test.py              # 测试脚本
│   └── __init__.py          # 包初始化
├── scripts/
│   ├── start.sh             # 启动脚本 (整合版)
│   └── start_rid.sh         # 启动脚本 (简化版)
├── data/                    # 数据目录
│   └── .gitkeep
└── config/                  # 配置目录
    └── .gitkeep
```

---

## 常见问题

### Q: Web 页面显示 "Content-Length mismatch"
已修复。旧版代码用 `len(body)`（字符长度）而非 `len(body_bytes)`（字节长度）计算 Content-Length，含中文时会导致截断。

### Q: 串口接收不到数据？
1. 确认 CH340 已插入：`ls /dev/ttyUSB*`
2. 确认 ESP32 已在发送数据
3. 确认波特率匹配 (115200)
4. 确认没有其他进程占用串口

### Q: WiFi 扫描和 AP 模式冲突？
RTL8812AU 不能同时处于 monitor mode 和 AP mode。需在两种模式间选择。

### Q: RSB-4221 没有 pip，如何安装依赖？
本项目全部使用 Python 2.7 标准库，零外部依赖。`minimal_serial.py` 替代了 pyserial。
