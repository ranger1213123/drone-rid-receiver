"""
数据库模块 - SQLite 初始化、建表、CRUD 操作

安全特性:
- WAL 模式 (并发读写)
- 外键约束强制启用
- 忙等待超时 + 自动重试
- Schema 版本号 (PRAGMA user_version) 用于未来迁移
"""

import sqlite3
import os
import time
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple

from logging_config import get_logger

logger = get_logger(__name__)

# 当前数据库 schema 版本
CURRENT_SCHEMA_VERSION = 2

# SQLITE_BUSY 重试配置
MAX_RETRIES = 3
RETRY_DELAY_MS = 100


class Database:
    """无人机 RID 系统数据库 — SQLite with WAL"""

    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._apply_pragmas()
        self._init_tables()
        self._check_schema_version()

    def _apply_pragmas(self):
        self._execute("PRAGMA journal_mode=WAL")
        self._execute("PRAGMA foreign_keys=ON")
        self._execute("PRAGMA busy_timeout=5000")
        self._execute("PRAGMA synchronous=NORMAL")

    def _execute(self, sql: str, params=()):
        """执行 SQL，遇 SQLITE_BUSY 自动重试"""
        for attempt in range(MAX_RETRIES):
            try:
                return self.conn.execute(sql, params)
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e).lower() and attempt < MAX_RETRIES - 1:
                    logger.warning("数据库锁定，重试 %d/%d", attempt + 1, MAX_RETRIES)
                    time.sleep(RETRY_DELAY_MS / 1000 * (attempt + 1))
                else:
                    raise

    def _executescript(self, sql: str):
        for attempt in range(MAX_RETRIES):
            try:
                self.conn.executescript(sql)
                return
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e).lower() and attempt < MAX_RETRIES - 1:
                    logger.warning("数据库锁定，重试 %d/%d", attempt + 1, MAX_RETRIES)
                    time.sleep(RETRY_DELAY_MS / 1000 * (attempt + 1))
                else:
                    raise

    def _commit(self):
        for attempt in range(MAX_RETRIES):
            try:
                self.conn.commit()
                return
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e).lower() and attempt < MAX_RETRIES - 1:
                    logger.warning("提交失败，重试 %d/%d", attempt + 1, MAX_RETRIES)
                    time.sleep(RETRY_DELAY_MS / 1000 * (attempt + 1))
                else:
                    raise

    def _check_schema_version(self):
        version = self._execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            self._migrate_v0_to_v2()
        elif version == 1:
            self._migrate_v1_to_v2()
        elif version != CURRENT_SCHEMA_VERSION:
            logger.warning("数据库 schema 版本不匹配: 当前 v%d, 期望 v%d",
                           version, CURRENT_SCHEMA_VERSION)

    def _migrate_v0_to_v2(self):
        logger.info("数据库 schema 初始化至 v%d", CURRENT_SCHEMA_VERSION)
        self._executescript("""
            CREATE TABLE IF NOT EXISTS raw_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                drone_id TEXT NOT NULL DEFAULT '',
                timestamp TEXT NOT NULL,
                protocol TEXT NOT NULL DEFAULT '',
                msg_type TEXT NOT NULL DEFAULT '',
                raw_data TEXT NOT NULL,
                mac TEXT NOT NULL DEFAULT '',
                rssi REAL DEFAULT 0,
                prev_hash TEXT NOT NULL DEFAULT '',
                current_hash TEXT NOT NULL,
                verified INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_raw_drone_time
                ON raw_messages(drone_id, timestamp);

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                operation TEXT NOT NULL,
                table_name TEXT NOT NULL,
                record_id INTEGER,
                operator TEXT NOT NULL DEFAULT 'system',
                details TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(timestamp);
        """)
        self._execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
        self._commit()

    def _migrate_v1_to_v2(self):
        logger.info("数据库 schema 迁移 v1 → v2")
        self._executescript("""
            CREATE TABLE IF NOT EXISTS raw_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                drone_id TEXT NOT NULL DEFAULT '',
                timestamp TEXT NOT NULL,
                protocol TEXT NOT NULL DEFAULT '',
                msg_type TEXT NOT NULL DEFAULT '',
                raw_data TEXT NOT NULL,
                mac TEXT NOT NULL DEFAULT '',
                rssi REAL DEFAULT 0,
                prev_hash TEXT NOT NULL DEFAULT '',
                current_hash TEXT NOT NULL,
                verified INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_raw_drone_time
                ON raw_messages(drone_id, timestamp);

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                operation TEXT NOT NULL,
                table_name TEXT NOT NULL,
                record_id INTEGER,
                operator TEXT NOT NULL DEFAULT 'system',
                details TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(timestamp);
        """)
        self._execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
        self._commit()
        logger.info("数据库 schema 迁移完成 v2")

    def _init_tables(self):
        """创建数据库表"""
        self._executescript("""
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
                message TEXT,
                FOREIGN KEY (drone_id) REFERENCES drones(id),
                FOREIGN KEY (line_id) REFERENCES power_lines(id)
            );

            CREATE INDEX IF NOT EXISTS idx_alerts_drone_time
                ON alerts(drone_id, timestamp);
        """)
        self._commit()

    # ──────────────────── 无人机操作 ────────────────────

    def upsert_drone(self, drone_id: str, lat: float, lon: float,
                     alt: float, speed: float = 0, heading: float = 0):
        """插入或更新无人机状态"""
        now = datetime.now(timezone.utc).isoformat()
        cur = self._execute(
            "SELECT id FROM drones WHERE id = ?", (drone_id,)
        )
        if cur.fetchone():
            self._execute("""
                UPDATE drones
                SET last_seen = ?, last_lat = ?, last_lon = ?, last_alt = ?,
                    last_speed = ?, last_heading = ?
                WHERE id = ?
            """, (now, lat, lon, alt, speed, heading, drone_id))
        else:
            self._execute("""
                INSERT INTO drones (id, first_seen, last_seen,
                    last_lat, last_lon, last_alt, last_speed, last_heading, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')
            """, (drone_id, now, now, lat, lon, alt, speed, heading))
        self._commit()

    def update_drone_distance(self, drone_id: str, distance: float,
                               line_id: int, status: str):
        """更新无人机最小距离和状态"""
        cur = self._execute(
            "SELECT min_distance FROM drones WHERE id = ?", (drone_id,)
        )
        row = cur.fetchone()
        if row:
            min_dist = (
                distance if row["min_distance"] is None
                else min(row["min_distance"], distance)
            )
            self._execute("""
                UPDATE drones
                SET min_distance = ?, nearest_line_id = ?, status = ?
                WHERE id = ?
            """, (min_dist, line_id, status, drone_id))
            self._commit()

    def get_active_drones(self) -> List[Dict]:
        """获取所有活跃无人机"""
        self.conn.row_factory = sqlite3.Row
        rows = self._execute(
            "SELECT * FROM drones WHERE status != 'gone' ORDER BY last_seen DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_gone(self, drone_id: str):
        """标记无人机已离开"""
        self._execute(
            "UPDATE drones SET status = 'gone' WHERE id = ?", (drone_id,)
        )
        self._commit()

    # ──────────────────── 电力线操作 ────────────────────

    def load_power_lines(self, lines: List[Dict]):
        """从配置加载电力线（先解除外键引用再清空插入）"""
        self._execute("UPDATE alerts SET line_id = NULL")
        self._execute("UPDATE trajectories SET line_id = NULL")
        self._execute("DELETE FROM power_lines")
        for line in lines:
            self._execute("""
                INSERT INTO power_lines (name, lat1, lon1, alt1, lat2, lon2, alt2)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (line["name"], line["lat1"], line["lon1"], line["alt1"],
                  line["lat2"], line["lon2"], line["alt2"]))
        self._commit()

    def get_power_lines(self) -> List[Dict]:
        """获取所有电力线"""
        self.conn.row_factory = sqlite3.Row
        rows = self._execute("SELECT * FROM power_lines").fetchall()
        return [dict(r) for r in rows]

    # ──────────────────── 轨迹操作 ────────────────────

    def add_trajectory_point(self, drone_id: str, lat: float, lon: float,
                              alt: float, distance: float, line_id: int):
        """添加轨迹点"""
        now = datetime.now(timezone.utc).isoformat()
        self._execute("""
            INSERT INTO trajectories (drone_id, timestamp, lat, lon, alt,
                                       distance_to_line, line_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (drone_id, now, lat, lon, alt, distance, line_id))
        self._commit()

    def get_trajectory(self, drone_id: str, limit: int = 500) -> List[Dict]:
        """获取指定无人机轨迹"""
        self.conn.row_factory = sqlite3.Row
        rows = self._execute("""
            SELECT * FROM trajectories
            WHERE drone_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
        """, (drone_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def get_last_trajectory_time(self, drone_id: str) -> Optional[str]:
        """获取某无人机最后轨迹点时间"""
        row = self._execute("""
            SELECT timestamp FROM trajectories
            WHERE drone_id = ?
            ORDER BY timestamp DESC LIMIT 1
        """, (drone_id,)).fetchone()
        return row["timestamp"] if row else None

    # ──────────────────── 告警操作 ────────────────────

    def add_alert(self, drone_id: str, level: str, distance: float,
                  line_id: int, message: str) -> int:
        """记录告警"""
        now = datetime.now(timezone.utc).isoformat()
        cur = self._execute("""
            INSERT INTO alerts (drone_id, timestamp, level, distance, line_id, message)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (drone_id, now, level, distance, line_id, message))
        self._commit()
        return cur.lastrowid

    def get_last_alert(self, drone_id: str, level: str) -> Optional[Dict]:
        """获取某无人机最后一条指定级别告警 (用于去重)"""
        self.conn.row_factory = sqlite3.Row
        row = self._execute("""
            SELECT * FROM alerts
            WHERE drone_id = ? AND level = ?
            ORDER BY timestamp DESC LIMIT 1
        """, (drone_id, level)).fetchone()
        return dict(row) if row else None

    # ──────────────────── 原始报文存档 ────────────────────

    def insert_raw_message(self, drone_id: str, timestamp: str,
                           protocol: str, msg_type: str, raw_data: str,
                           mac: str, rssi: float,
                           prev_hash: str, current_hash: str) -> int:
        cur = self._execute("""
            INSERT INTO raw_messages (drone_id, timestamp, protocol, msg_type,
                raw_data, mac, rssi, prev_hash, current_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (drone_id, timestamp, protocol, msg_type, raw_data, mac, rssi,
              prev_hash, current_hash))
        self._commit()
        self._add_audit_log_internal("INSERT", "raw_messages", cur.lastrowid)
        return cur.lastrowid

    def get_raw_message_chain(self, drone_id: str) -> List[Dict]:
        self.conn.row_factory = sqlite3.Row
        rows = self._execute("""
            SELECT * FROM raw_messages
            WHERE drone_id = ?
            ORDER BY timestamp ASC
        """, (drone_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_raw_messages(self, drone_id: str, limit: int = 100) -> List[Dict]:
        self.conn.row_factory = sqlite3.Row
        rows = self._execute("""
            SELECT * FROM raw_messages
            WHERE drone_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
        """, (drone_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def get_all_drones_with_raw_data(self) -> List[str]:
        rows = self._execute("""
            SELECT DISTINCT drone_id FROM raw_messages
        """).fetchall()
        return [r[0] for r in rows]

    def delete_raw_messages_before(self, cutoff: str):
        self._execute("""
            DELETE FROM raw_messages WHERE timestamp < ?
        """, (cutoff,))
        self._commit()
        self._add_audit_log_internal("DELETE", "raw_messages", None,
                                     f"cleanup before {cutoff}")

    # ──────────────────── 审计日志 ────────────────────

    def _add_audit_log_internal(self, operation: str, table_name: str,
                                record_id: int = None, details: str = ""):
        now = datetime.now(timezone.utc).isoformat()
        self._execute("""
            INSERT INTO audit_log (timestamp, operation, table_name, record_id, details)
            VALUES (?, ?, ?, ?, ?)
        """, (now, operation, table_name, record_id, details or ""))

    def add_audit_log(self, operation: str, table_name: str,
                      record_id: int = None, operator: str = "system",
                      details: str = "") -> int:
        now = datetime.now(timezone.utc).isoformat()
        cur = self._execute("""
            INSERT INTO audit_log (timestamp, operation, table_name, record_id,
                operator, details)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (now, operation, table_name, record_id, operator, details))
        self._commit()
        return cur.lastrowid

    def get_audit_logs(self, limit: int = 200) -> List[Dict]:
        self.conn.row_factory = sqlite3.Row
        rows = self._execute("""
            SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self.conn.close()
