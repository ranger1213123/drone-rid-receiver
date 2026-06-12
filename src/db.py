"""
数据库模块 - SQLite 初始化、建表、CRUD 操作
"""

import sqlite3
import os
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple


class Database:
    """无人机 RID 系统数据库"""

    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        # 外键约束可选 - 实际场景中无人机可能先于数据库记录被接收到
        # 在测试中可关闭以避免需要预填充引用数据
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self._init_tables()

    def _init_tables(self):
        """创建数据库表"""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS drones (
                id TEXT PRIMARY KEY,
                first_seen TEXT,
                last_seen TEXT,
                last_lat REAL,
                last_lon REAL,
                last_alt REAL,
                last_speed REAL DEFAULT 0,
                last_heading REAL DEFAULT 0,
                min_distance REAL,
                nearest_line_id INTEGER,
                status TEXT DEFAULT 'active'
            );

            CREATE TABLE IF NOT EXISTS power_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                lat1 REAL, lon1 REAL, alt1 REAL,
                lat2 REAL, lon2 REAL, alt2 REAL
            );

            CREATE TABLE IF NOT EXISTS trajectories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                drone_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                lat REAL, lon REAL, alt REAL,
                distance_to_line REAL,
                line_id INTEGER,
                FOREIGN KEY (drone_id) REFERENCES drones(id),
                FOREIGN KEY (line_id) REFERENCES power_lines(id)
            );

            CREATE INDEX IF NOT EXISTS idx_traj_drone_time
                ON trajectories(drone_id, timestamp);

            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                drone_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                level TEXT NOT NULL,
                distance REAL,
                line_id INTEGER,
                sms_sent_pilot INTEGER DEFAULT 0,
                sms_sent_staff INTEGER DEFAULT 0,
                message TEXT,
                FOREIGN KEY (drone_id) REFERENCES drones(id),
                FOREIGN KEY (line_id) REFERENCES power_lines(id)
            );

            CREATE INDEX IF NOT EXISTS idx_alerts_drone_time
                ON alerts(drone_id, timestamp);
        """)
        self.conn.commit()

    # ──────────────────── 无人机操作 ────────────────────

    def upsert_drone(self, drone_id: str, lat: float, lon: float,
                     alt: float, speed: float = 0, heading: float = 0):
        """插入或更新无人机状态"""
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            "SELECT id FROM drones WHERE id = ?", (drone_id,)
        )
        if cur.fetchone():
            self.conn.execute("""
                UPDATE drones
                SET last_seen = ?, last_lat = ?, last_lon = ?, last_alt = ?,
                    last_speed = ?, last_heading = ?
                WHERE id = ?
            """, (now, lat, lon, alt, speed, heading, drone_id))
        else:
            self.conn.execute("""
                INSERT INTO drones (id, first_seen, last_seen,
                    last_lat, last_lon, last_alt, last_speed, last_heading, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')
            """, (drone_id, now, now, lat, lon, alt, speed, heading))
        self.conn.commit()

    def update_drone_distance(self, drone_id: str, distance: float,
                               line_id: int, status: str):
        """更新无人机最小距离和状态"""
        cur = self.conn.execute(
            "SELECT min_distance FROM drones WHERE id = ?", (drone_id,)
        )
        row = cur.fetchone()
        if row:
            min_dist = (
                distance if row["min_distance"] is None
                else min(row["min_distance"], distance)
            )
            self.conn.execute("""
                UPDATE drones
                SET min_distance = ?, nearest_line_id = ?, status = ?
                WHERE id = ?
            """, (min_dist, line_id, status, drone_id))
            self.conn.commit()

    def get_active_drones(self) -> List[Dict]:
        """获取所有活跃无人机"""
        self.conn.row_factory = sqlite3.Row
        rows = self.conn.execute(
            "SELECT * FROM drones WHERE status != 'gone' ORDER BY last_seen DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_gone(self, drone_id: str):
        """标记无人机已离开"""
        self.conn.execute(
            "UPDATE drones SET status = 'gone' WHERE id = ?", (drone_id,)
        )
        self.conn.commit()

    # ──────────────────── 电力线操作 ────────────────────

    def load_power_lines(self, lines: List[Dict]):
        """从配置加载电力线（先清空再插入）"""
        self.conn.execute("DELETE FROM power_lines")
        for line in lines:
            self.conn.execute("""
                INSERT INTO power_lines (name, lat1, lon1, alt1, lat2, lon2, alt2)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (line["name"], line["lat1"], line["lon1"], line["alt1"],
                  line["lat2"], line["lon2"], line["alt2"]))
        self.conn.commit()

    def get_power_lines(self) -> List[Dict]:
        """获取所有电力线"""
        self.conn.row_factory = sqlite3.Row
        rows = self.conn.execute("SELECT * FROM power_lines").fetchall()
        return [dict(r) for r in rows]

    # ──────────────────── 轨迹操作 ────────────────────

    def add_trajectory_point(self, drone_id: str, lat: float, lon: float,
                              alt: float, distance: float, line_id: int):
        """添加轨迹点"""
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute("""
            INSERT INTO trajectories (drone_id, timestamp, lat, lon, alt,
                                       distance_to_line, line_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (drone_id, now, lat, lon, alt, distance, line_id))
        self.conn.commit()

    def get_trajectory(self, drone_id: str, limit: int = 500) -> List[Dict]:
        """获取指定无人机轨迹"""
        self.conn.row_factory = sqlite3.Row
        rows = self.conn.execute("""
            SELECT * FROM trajectories
            WHERE drone_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
        """, (drone_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def get_last_trajectory_time(self, drone_id: str) -> Optional[str]:
        """获取某无人机最后轨迹点时间"""
        row = self.conn.execute("""
            SELECT timestamp FROM trajectories
            WHERE drone_id = ?
            ORDER BY timestamp DESC LIMIT 1
        """, (drone_id,)).fetchone()
        return row["timestamp"] if row else None

    # ──────────────────── 告警操作 ────────────────────

    def add_alert(self, drone_id: str, level: str, distance: float,
                  line_id: int, message: str,
                  sms_pilot: bool = False, sms_staff: bool = False) -> int:
        """记录告警"""
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute("""
            INSERT INTO alerts (drone_id, timestamp, level, distance, line_id,
                                 sms_sent_pilot, sms_sent_staff, message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (drone_id, now, level, distance, line_id,
              int(sms_pilot), int(sms_staff), message))
        self.conn.commit()
        return cur.lastrowid

    def get_last_alert(self, drone_id: str, level: str) -> Optional[Dict]:
        """获取某无人机最后一条指定级别告警 (用于去重)"""
        self.conn.row_factory = sqlite3.Row
        row = self.conn.execute("""
            SELECT * FROM alerts
            WHERE drone_id = ? AND level = ?
            ORDER BY timestamp DESC LIMIT 1
        """, (drone_id, level)).fetchone()
        return dict(row) if row else None

    def close(self):
        self.conn.close()
