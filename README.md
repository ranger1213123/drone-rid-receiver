# 无人机 RID 接收与电力线防碰撞监控系统

基于 ASTM F3411 / ASD-STAN 4709-002 Open Drone ID 标准的无人机 Remote ID 接收系统。

## 功能

- **多模式接收**: BLE (蓝牙)、WiFi Beacon、模拟数据
- **实时显示**: 终端 ANSI / GUI 图形界面
- **电力线防碰撞**: 三维垂直距离计算，支持录入电线杆/电力线段坐标
- **三级告警**: ≤200m 警告、≤100m 严重、≤50m 驱离
- **短信通知**: 支持 Twilio / 阿里云 / 模拟
- **轨迹记录**: SQLite 持续记录，支持回放查看
- **跨平台**: Windows / Linux / macOS

## 快速开始

### 安装

```bash
pip install -r requirements.txt
```

### 运行

**GUI 模式 (推荐):**
```bash
python app/gui_launcher.py
```

**CLI 模式:**
```bash
# 模拟模式
python app/cli.py --mode mock

# BLE 模式 (需要蓝牙硬件)
python app/cli.py --mode ble

# WiFi 模式 (需要 scapy + Npcap/Linux monitor mode)
python app/cli.py --mode wifi
```

### 测试

```bash
python -m pytest tests/test_system.py -v
```

## 配置

编辑 `config/config.yaml`:

- 电力线坐标: `config/power_lines.yaml`
- 告警阈值: `warning: 200`, `severe: 100`, `critical: 50`
- 短信后端: `mock` / `twilio` / `aliyun`
- 预设联系人: `alert_contacts`

## 项目结构

```
drone-rid-receiver/
├── app/                    # 应用入口
│   ├── cli.py              # CLI 入口
│   ├── gui_launcher.py     # GUI 入口
│   └── web.py              # Web 入口 (Flask)
├── core/                   # 核心业务逻辑
│   ├── pipeline.py         # 数据处理管道
│   ├── parser.py           # ASTM F3411 消息解析
│   ├── powerline.py        # 电力线管理 + 距离计算
│   ├── alert.py            # 告警系统 + 短信
│   └── trajectory.py       # 轨迹记录
├── receiver/               # 数据接收层
│   ├── ble.py              # BLE 接收器 + 模拟
│   └── wifi.py             # WiFi 接收器 (scapy + Npcap)
├── display/                # 展示层
│   ├── terminal.py         # CLI 终端显示
│   └── gui/
│       ├── window.py           # GUI 主窗口
│       └── powerline.py        # 电力线录入对话框
├── storage/                # 数据持久化
│   └── database.py         # SQLite 数据库
├── config/
│   ├── config.yaml
│   └── power_lines.yaml
├── tests/
│   └── test_system.py      # 27 项单元测试
├── data/                   # 数据库文件 (自动生成)
└── PLAN.md                 # 实现计划
```

## Windows 上使用 WiFi/BLE

1. **BLE**: 安装 `bleak`, 确保蓝牙适配器已开启
2. **WiFi (scapy)**: 安装 [Npcap](https://npcap.com), 然后 `pip install scapy`
