# 无人机 RID 接收与电力线防碰撞系统 — 实现计划

> **For Hermes:** 逐步实现，每个模块完成后立即验证。

**目标:** 构建一个完整的无人机 Remote ID 接收系统，实时显示无人机位置，计算与电力线的垂直距离，在低于阈值时发送短信告警并记录轨迹。

**架构:** 模块化 Python 应用，BLE 扫描接收 RID 广播，解析 ASTM F3411 消息，SQLite 存储轨迹，可插拔短信后端（Twilio / 阿里云短信 / 模拟）。

**技术栈:** Python 3.10+, bleak (BLE), PyYAML, SQLite, asyncio

---

## 系统架构

```
┌─────────────────────────────────────────────────────────┐
│                    main.py (控制器)                       │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌─────────┐ │
│  │rid_      │  │display   │  │alert     │  │trajectory│ │
│  │receiver  │  │(实时显示) │  │(短信告警) │  │(轨迹记录) │ │
│  └────┬─────┘  └──────────┘  └────┬─────┘  └────┬─────┘ │
│       │                           │              │       │
│  ┌────┴─────┐              ┌──────┴──────┐ ┌────┴─────┐ │
│  │rid_parser│              │distance     │ │db        │ │
│  │(F3411)   │              │(垂直距离)    │ │(SQLite)  │ │
│  └──────────┘              └──────┬──────┘ └──────────┘ │
│                                   │                      │
│                            ┌──────┴──────┐              │
│                            │powerline    │              │
│                            │(电力线数据)  │              │
│                            └─────────────┘              │
└─────────────────────────────────────────────────────────┘
```

## 数据流

1. BLE 扫描 → 发现 RID 广播 → rid_parser 解包
2. 解析出 (drone_id, lat, lon, alt) → 更新显示
3. 与所有电力线段计算垂直距离 → 取最小值
4. 判断阈值:
   - ≤200m: 开始记录轨迹 + 给飞手发"警告"
   - ≤100m: 给飞手发"严重警告"
   - ≤50m:  给飞手发"立即驱离" + 给预设人员发短信
5. 轨迹记录: drone_id + timestamp + lat/lon/alt → SQLite

## 数据库设计

```sql
-- 无人机表（当前活跃）
CREATE TABLE drones (
    id TEXT PRIMARY KEY,       -- drone ID / serial
    first_seen TIMESTAMP,
    last_seen TIMESTAMP,
    last_lat REAL,
    last_lon REAL,
    last_alt REAL,
    last_speed REAL,
    last_heading REAL,
    min_distance REAL,         -- 距离最近电力线的最小垂直距离
    status TEXT                -- 'active', 'warning', 'critical', 'gone'
);

-- 电力线表
CREATE TABLE power_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    lat1 REAL, lon1 REAL, alt1 REAL,
    lat2 REAL, lon2 REAL, alt2 REAL
);

-- 轨迹表（仅记录 <200m 的无人机）
CREATE TABLE trajectories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    drone_id TEXT,
    timestamp TIMESTAMP,
    lat REAL, lon REAL, alt REAL,
    distance_to_line REAL,
    line_id INTEGER,
    FOREIGN KEY (drone_id) REFERENCES drones(id),
    FOREIGN KEY (line_id) REFERENCES power_lines(id)
);

-- 告警记录表
CREATE TABLE alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    drone_id TEXT,
    timestamp TIMESTAMP,
    level TEXT,                -- 'warning'(<200m), 'severe'(<100m), 'critical'(<50m)
    distance REAL,
    line_id INTEGER,
    sms_sent_pilot BOOLEAN,
    sms_sent_staff BOOLEAN,
    message TEXT
);
```

## 任务列表

### Task 1: 项目骨架 — 目录结构、依赖、配置

创建项目结构、requirements.txt、config.yaml

### Task 2: 数据库模块 db.py

SQLite 初始化、建表、CRUD 操作

### Task 3: RID 消息解析器 rid_parser.py

解析 ASTM F3411 / ASD-STAN 4709-002 Open Drone ID 消息

### Task 4: BLE RID 接收器 rid_receiver.py

使用 bleak 扫描 BLE，过滤 ODID 广播，回调解析

### Task 5: 电力线模块 powerline.py + distance.py

电力线数据管理 + 3D 点到线段垂直距离计算

### Task 6: 告警系统 alert.py

阈值判断逻辑 + 短信发送（支持 Twilio / 阿里云 / 模拟）

### Task 7: 轨迹记录 trajectory.py

当无人机距离 <200m 时，持续记录轨迹点

### Task 8: 实时显示 display.py

终端表格显示所有活跃无人机

### Task 9: 主程序 main.py

异步事件循环，串联所有模块

### Task 10: 集成测试

端到端测试（模拟 BLE 数据 + 模拟电力线）
