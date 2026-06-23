# 无人机 RID 接收与电力线防碰撞监控系统

基于 ASTM F3411 / GB 46750-2025 双协议的无人机 Remote ID 接收系统，部署于电力线杆塔物联网设备上，实时监测无人机接近电力线行为并分级告警。

## 系统架构

```
┌──────────────────── 边缘设备 (ARM Linux 杆塔盒子) ────────────────────┐
│                                                                        │
│  drone-receiver.service    drone-pipeline.service   drone-backhaul.service │
│  ┌──────────┐  raw_packets  ┌───────────┐  outbox   ┌────────────┐   │
│  │ BLE/WiFi │ ────────────→ │ 协议解析   │ ────────→ │ MQTT mTLS  │   │
│  │ ESP32串口│   SQLite DB   │ 3D距离计算 │  SQLite   │ 心跳+回传   │──┼───→
│  │ 信号接收  │              │ 告警判定   │           │ 配置同步    │   │
│  └──────────┘              └───────────┘          └────────────┘   │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
                                    │
                              MQTT (mTLS)
                                    │
                                    ▼
┌──────────────────── 云端服务器 (K8s 部署) ──────────────────────────────┐
│                                                                          │
│  mqtt-consumer.service    Flask Web 服务           PostgreSQL           │
│  ┌──────────────┐       ┌─────────────┐      ┌──────────────┐          │
│  │ MQTT 订阅    │       │ REST API    │      │ 设备状态      │          │
│  │ 批量写入DB   │       │ 聚合仪表盘   │      │ 告警记录      │          │
│  │ 配置下发     │       │ 电力线管理   │      │ 轨迹数据      │          │
│  └──────────────┘       └─────────────┘      └──────────────┘          │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

**三层微服务（边缘侧）**：
- **Receiver** — BLE/WiFi/ESP32 信号捕获，原始报文写入 SQLite
- **Pipeline** — 协议解析 → 3D 距离计算 → 告警判定 → 轨迹记录 → 结果入 outbox
- **Backhaul** — MQTT mTLS 上行回传 + 下行配置同步，断网时 outbox 持久化积压

## 功能

### 信号接收
- **BLE 扫描**: 蓝牙 Bleak 库，ASTM F3411 UUID `0xFFFA` / GB 46750 UUID `0xFFFF`
- **WiFi Beacon**: Linux raw socket (`AF_PACKET`)，手动解析 Radiotap + 802.11 帧，OUI `FA:0B:BC`，移除 scapy 依赖
- **ESP32 串口**: `/dev/ttyUSB0` 115200 baud，JSON 行协议（心跳 + 完整 RID 数据）
- **模拟数据**: 开发测试用，无需硬件

### 双协议解析
- **ASTM F3411** (国际标准): Open Drone ID Message Pack，WiFi Beacon/Nanobeacon + BLE
- **GB 46750-2025** (国标): `0x0F` 包装 + CRC16-IBM，WiFi + BLE Service Data

### 电力线防碰撞
- **3D 欧氏距离**: 水平面 + 垂直方向，比纯垂直距离更准确
- **导线垂度建模**: 抛物线悬链线，Golden Section Search (15 迭代)，电压等级 × 档距自动估算
- **保守安全系数**: 垂度 ×1.5，补偿温度/覆冰/施工偏差
- **坐标系转换**: GCJ-02 → WGS-84 自动转换，消除 100–700m 系统性偏差

### 三级告警
| 级别 | 距离 | 冷却 | 处置建议 |
|------|------|------|----------|
| warning | ≤200m | 120s | 注意飞行路径 |
| severe | ≤100m | 60s | 立即调整航向 |
| critical | ≤50m | 30s | 立即降落或返航 |

- **防抖引擎**: OUTSIDE→ENTERING(3s)→INSIDE→LEAVING(10s)→OUTSIDE 状态机，抑制边界抖动
- **级别升级**: 告警级别升高时绕过冷却立即触发
- **数据库记录**: SQLite 本地存储，MQTT 回传云端

### 数据回传
- **MQTT mTLS**: 证书双向认证，QoS 0/1/2 分级
- **Outbox 模式**: MQTT 中断时消息入 SQLite outbox，重连后自动补传
- **无降级链路**: 杆塔设备有稳定供电和 4G/5G，不做 SMS/北斗降级

### 其他
- 轨迹记录（≤200m 警告区内自动记录）
- 原始报文存档 + 哈希链防篡改
- 飞手推送通知（UOM / 控制台）
- 空域管理（电子围栏、多边形判定）
- Web 仪表盘（Flask，电力线 CRUD，实时监控）
- GUI 桌面版（tkinter）

## 快速开始

### 安装

```bash
pip install -r requirements.txt
```

### 开发模式（Windows / Linux）

```bash
# Web 仪表盘 (推荐)
python app/web.py

# CLI 终端
python app/cli.py --mode simulated

# GUI 桌面版
python app/gui_launcher.py
```

### 边缘设备部署（Linux ARM 杆塔盒子）

```bash
# 三服务独立运行
python -m app.edge_receiver  --config /etc/drone-rid/config.yaml --mode ble
python -m app.edge_pipeline   --config /etc/drone-rid/config.yaml
python -m app.edge_backhaul   --config /etc/drone-rid/config.yaml

# 或一体化无头模式
python -m app.headless --config /etc/drone-rid/config.yaml --mode ble
```

### 云端服务

```bash
# Flask 聚合服务
python app/server.py --port 8080

# MQTT Consumer (K8s 多副本)
python -m app.mqtt_consumer --config /etc/drone-rid/cloud.yaml
```

### 测试

```bash
python -m pytest tests/ -v    # 97 项测试
```

## 配置

编辑 `config/config.yaml`：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `protocol` | 主解析协议 `gb46750` / `astm_f3411` | `gb46750` |
| `coordinate_system` | 电力线坐标系 `wgs84` / `gcj02` | `wgs84` |
| `thresholds.warning` | 警告距离 (m) | 200 |
| `thresholds.severe` | 严重距离 (m) | 100 |
| `thresholds.critical` | 危险距离 (m) | 50 |
| `anti_flapping.enabled` | 防抖开关 | false |
| `anti_flapping.debounce_in` | 进入确认时间 (s) | 3 |
| `anti_flapping.debounce_out` | 离开确认时间 (s) | 10 |
| `mqtt.enabled` | MQTT 通信开关 | false |
| `mqtt.broker.host` | MQTT Broker 地址 | localhost |
| `mqtt.broker.port` | MQTT 端口 | 8883 |
| `mqtt.tls.enabled` | mTLS 双向认证 | true |
| `trajectory.min_interval` | 轨迹记录最小间隔 (s) | 2.0 |
| `trajectory.max_points_per_drone` | 单机最大轨迹点数 | 1000 |
| `raw_archive.retention_days` | 原始报文保留天数 | 30 |

电力线配置：`config/power_lines.yaml`

```yaml
coordinate_system: wgs84          # 坐标系, gcj02 自动转换
auto_estimate_sag: true           # 按电压等级+档距自动估算垂度
power_lines:
  - name: "杭富高压线A-北段"
    lat1: 30.0000, lon1: 119.9980, alt1: 110.0
    lat2: 30.0050, lon2: 120.0030, alt2: 115.0
    voltage_level: "220kV"
    sag: 12.5                     # 手动指定, -1 则自动估算
```

## 项目结构

```
drone-rid-receiver/
├── app/                          # 应用入口 (9 个可独立运行的服务)
│   ├── cli.py                    # CLI 终端
│   ├── web.py                    # Flask Web 仪表盘
│   ├── gui_launcher.py           # tkinter 桌面版
│   ├── headless.py               # 边缘无头一体化模式
│   ├── edge_receiver.py          # 边缘服务 A: 信号接收
│   ├── edge_pipeline.py          # 边缘服务 B: 数据处理
│   ├── edge_backhaul.py          # 边缘服务 C: 数据回传
│   ├── mqtt_consumer.py          # 云端 MQTT Consumer
│   ├── server.py                 # 云端聚合服务
│   └── server/                   # 云端子模块 (API/认证/仪表盘/ORM)
│       ├── api_heartbeat.py
│       ├── api_report.py
│       ├── api_status.py
│       ├── api_web.py
│       ├── auth.py
│       ├── cert_manager.py
│       ├── dashboard.py
│       └── models.py
├── core/                         # 核心业务逻辑
│   ├── pipeline.py               # 数据处理管道
│   ├── powerline.py              # 电力线管理 + 3D 距离 + 垂度估算
│   ├── alert.py                  # 告警系统 (阈值/去重/升级)
│   ├── anti_flapping.py          # 告警防抖状态机
│   ├── trajectory.py             # 轨迹记录器
│   ├── backhaul.py               # MQTT 回传 + Outbox 管理
│   ├── mqtt_client.py            # MQTT mTLS 客户端
│   ├── coords.py                 # GCJ-02 ↔ WGS-84 坐标转换
│   ├── airspace.py               # 空域管理 (电子围栏)
│   ├── pilot_notify.py           # 飞手推送通知
│   ├── sms_gateway.py            # 短信网关 (阿里云/模拟)
│   ├── beidou.py                 # 北斗定位 + 短报文 (保留)
│   ├── raw_archive.py            # 原始报文存档 + 哈希链
│   ├── bootstrap.py              # 核心组件工厂
│   ├── config.py                 # 配置加载
│   ├── service_common.py         # 边缘服务公共函数
│   └── parser/                   # 协议解析
│       ├── base.py               # 解析器基类 + ParsedRID 类型
│       ├── astm.py               # ASTM F3411 协议
│       ├── gb46750.py            # GB 46750-2025 协议
│       └── types.py              # 共享类型定义
├── receiver/                     # 信号接收层
│   ├── ble.py                    # BLE 蓝牙接收器
│   ├── wifi.py                   # WiFi 接收器 (Linux raw socket)
│   ├── serial.py                 # ESP32 串口接收器
│   └── simulated.py             # 模拟数据生成器
├── display/                      # 展示层
│   ├── terminal.py               # CLI ANSI 终端渲染
│   └── gui/
│       ├── window.py             # tkinter 主窗口
│       └── powerline.py          # 电力线录入对话框
├── storage/                      # 数据持久化
│   └── database.py               # SQLite (边缘) + 迁移 + Outbox
├── config/
│   ├── config.yaml               # 主配置文件
│   ├── power_lines.yaml          # 电力线坐标
│   ├── web_users.yaml            # Web 用户
│   └── stations.yaml             # 站点配置
├── tests/
│   ├── test_system.py            # 系统测试 (77 项)
│   ├── test_protocols.py         # 协议解析测试 (20 项)
│   └── test_powerline.py         # 电力线/垂度/距离测试
├── data/                         # 数据库 + 日志 (运行生成)
└── reference/                    # 参考实现 (WiFi/ESP32)
```

## 技术要点

### 测距精度
- 水平距离: WGS-84 简化公式，纬度方向补偿椭球扁率
- 垂直距离: 考虑导线抛物线垂度 (`4sag·t·(1-t)`) + Golden Section Search 寻最近点
- 垂度保守: 查表值 ×1.5，补偿高温/覆冰/施工偏差
- 坐标系: GCJ-02 电力线坐标加载时自动转 WGS-84，确保与无人机 GPS 基准一致

### 告警可靠性
- 防抖状态机抑制边界抖动（3s ENTERING / 10s LEAVING 窗口）
- 冷却机制控制同级告警频率（30s/60s/120s）
- 级别升级绕过冷却立即触发
- 防抖仅管理进出转换，持续 INSIDE 交由冷却接管

### 数据不丢失
- MQTT 中断 → 消息写入 SQLite outbox → 重连后 FIFO 补传
- 原始报文存档 + SHA-256 哈希链，支持事后审计与篡改检测
- 所有边缘状态（告警/轨迹/outbox）均在 SQLite 中持久化
