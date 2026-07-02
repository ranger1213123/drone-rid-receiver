"""
中心数据库 ORM 模型 — SQLAlchemy

支持 SQLite (开发) 和 PostgreSQL (生产) 双驱动
"""

from datetime import datetime, timezone, timedelta
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, ForeignKey, Index,
    Text, Boolean, create_engine, event, CheckConstraint,
)
from sqlalchemy.orm import (
    declarative_base, relationship, scoped_session, sessionmaker,
)

from logging_config import get_logger

logger = get_logger(__name__)

Base = declarative_base()

# ── 时区转换: UTC → 北京时间 (UTC+8) ──
_BEIJING_TZ = timezone(timedelta(hours=8))

def _bj(ts):
    """将 UTC datetime 转为北京时间 ISO 字符串"""
    if ts is None:
        return ""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(_BEIJING_TZ).isoformat()


class Tenant(Base):
    """租户/客户 — 拥有 license_key, 控制用户注册上限"""

    __tablename__ = "tenants"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    license_key = Column(String, unique=True, nullable=False, index=True)
    max_users = Column(Integer, default=3)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime)
    created_by = Column(String, default="")
    contact = Column(String, default="")

    __table_args__ = (
        Index("idx_tenant_license", "license_key"),
        CheckConstraint("length(license_key) = 19", name="ck_tenant_license_key"),
    )


class Device(Base):
    __tablename__ = "devices"

    name = Column(String, primary_key=True)
    location = Column(String, default="")
    lat = Column(Float)
    lon = Column(Float)
    alt = Column(Float)
    first_seen = Column(DateTime)
    last_seen = Column(DateTime)
    status = Column(String, default="online")
    drone_count = Column(Integer, default=0)
    alert_count = Column(Integer, default=0)
    station_name = Column(String, default="")
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)

    drones = relationship("Drone", back_populates="device", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("length(name) >= 1", name="ck_device_name"),
    )


class Drone(Base):
    __tablename__ = "drones"

    id = Column(String, primary_key=True)
    device_name = Column(String, ForeignKey("devices.name"), primary_key=True)
    last_seen = Column(DateTime)
    last_lat = Column(Float)
    last_lon = Column(Float)
    last_alt = Column(Float)
    last_speed = Column(Float, default=0)
    last_heading = Column(Float, default=0)
    min_distance = Column(Float)
    nearest_line = Column(String)
    status = Column(String, default="active")
    rssi = Column(Integer, default=0)
    status_code = Column(Integer, default=0)   # ESP32 Status: 0=未知,1=地面,2=空中
    height_agl = Column(Float, nullable=True)   # ESP32 Height (AGL)
    model = Column(String, default="")           # 产品型号 (如 DJI Mini 4K)
    max_alt_agl = Column(Float, nullable=True)   # 观测最高对地高度 (m)
    max_alt_asl = Column(Float, nullable=True)   # 观测最高海拔 (m)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)

    device = relationship("Device", back_populates="drones")

    __table_args__ = (
        Index("idx_drones_device", "device_name"),
        CheckConstraint("length(id) >= 1", name="ck_drone_id"),
    )


class DronePosition(Base):
    """无人机位置历史 — 用于轨迹回放"""
    __tablename__ = "drone_positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    drone_id = Column(String, nullable=False, index=True)
    device_name = Column(String)
    lat = Column(Float)
    lon = Column(Float)
    alt = Column(Float)
    distance_to_line = Column(Float, nullable=True)
    nearest_line = Column(String, default="")
    nearby_lines = Column(Text, default="")   # JSON: [{"line":"...","dist":45.2}, ...]
    timestamp = Column(DateTime, nullable=False, index=True)

    __table_args__ = (
        Index("idx_dp_drone_time", "drone_id", "timestamp"),
    )


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    device_name = Column(String)
    drone_id = Column(String)
    timestamp = Column(DateTime)
    level = Column(String)
    distance = Column(Float)
    line_name = Column(String)
    message = Column(String)
    acknowledged = Column(Integer, default=0)
    ack_by = Column(String, default="")
    ack_time = Column(DateTime, nullable=True)
    ack_note = Column(String, default="")

    __table_args__ = (
        Index("idx_alerts_time", "timestamp"),
        Index("idx_alerts_level_device", "level", "device_name"),
        Index("idx_alerts_drone", "drone_id"),
        Index("idx_alerts_device", "device_name"),
    )


class PowerLine(Base):
    __tablename__ = "power_lines"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    lat1 = Column(Float, default=0)
    lon1 = Column(Float, default=0)
    alt1 = Column(Float, default=0)
    lat2 = Column(Float, default=0)
    lon2 = Column(Float, default=0)
    alt2 = Column(Float, default=0)
    tower_height1 = Column(Float, nullable=True)
    tower_height2 = Column(Float, nullable=True)
    voltage_level = Column(String, default="")
    device_name = Column(String, ForeignKey("devices.name"), nullable=True, index=True)
    created_at = Column(DateTime)
    updated_at = Column(DateTime)

    __table_args__ = (
        Index("idx_pl_device", "device_name"),
    )


class WebUser(Base):
    __tablename__ = "web_users"

    username = Column(String, primary_key=True)
    password_hash = Column(String, nullable=False)
    role = Column(String, default="user")
    station = Column(String, default="")
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    scope = Column(String, default="station")        # "tenant" | "station"
    assigned_station = Column(String, default="")     # scope=station 时的绑定站点

    __table_args__ = (
        CheckConstraint("length(username) >= 2 AND length(username) <= 32",
                       name="ck_webuser_username"),
    )


class Station(Base):
    __tablename__ = "stations"

    name = Column(String, primary_key=True)
    location = Column(String, default="")
    province = Column(String, default="")
    city = Column(String, default="")
    county = Column(String, default="")
    lat = Column(Float, default=0)
    lon = Column(Float, default=0)
    alt = Column(Float, default=0)
    device_name = Column(String, nullable=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True, index=True)
    webhook_url = Column(String, default="")


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key = Column(String, primary_key=True)
    value = Column(String, default="")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, index=True)
    username = Column(String)
    operation = Column(String)
    table_name = Column(String, default="")
    record_id = Column(Integer, nullable=True)
    detail = Column(String, default="")


class DeviceSecret(Base):
    __tablename__ = "device_secrets"

    device_name = Column(String, primary_key=True)
    device_secret = Column(String, nullable=False)
    client_cert = Column(Text, nullable=True)
    cert_serial = Column(String, nullable=True)
    cert_issued_at = Column(DateTime, nullable=True)
    revoked = Column(Boolean, default=False)
    revoked_at = Column(DateTime, nullable=True)
    station = Column(String, default="")
    tenant_id = Column(Integer, nullable=True)  # 租户归属
    created_at = Column(DateTime)


class StationPersonnel(Base):
    """站点负责人及其联系电话 (SMS通知用)"""

    __tablename__ = "station_personnel"

    id = Column(Integer, primary_key=True, autoincrement=True)
    station_name = Column(String, nullable=False)
    name = Column(String, default="")
    phone = Column(String, nullable=False)

    __table_args__ = (
        Index("idx_personnel_station", "station_name"),
        CheckConstraint("length(phone) = 11", name="ck_personnel_phone"),
        CheckConstraint("length(name) >= 1", name="ck_personnel_name"),
    )


class DroneWhitelist(Base):
    """无人机白名单 — 匹配的 SN 不触发告警"""

    __tablename__ = "drone_whitelist"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sn = Column(String, nullable=False, index=True)
    match_mode = Column(String, default="exact")  # "exact" | "prefix"
    note = Column(String, default="")
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    created_at = Column(DateTime)
    created_by = Column(String, default="")

    __table_args__ = (
        CheckConstraint("length(sn) >= 1", name="ck_whitelist_sn"),
    )


# ── 引擎与会话 ──

_engine = None
_Session = None


def _on_sqlite_connect(dbapi_conn, connection_record):
    """SQLite 特殊 pragmas"""
    dbapi_conn.execute("PRAGMA journal_mode=WAL")
    dbapi_conn.execute("PRAGMA foreign_keys=ON")
    dbapi_conn.execute("PRAGMA busy_timeout=5000")
    dbapi_conn.execute("PRAGMA synchronous=NORMAL")


def init_db(database_url: str = "sqlite:///data/center.db",
            pool_size: int = 5, pool_overflow: int = 10, pool_timeout: int = 30):
    """初始化数据库引擎和会话工厂"""
    global _engine, _Session

    engine_kwargs = {}
    if "sqlite" in database_url:
        engine_kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_engine(database_url, **engine_kwargs)
        event.listen(_engine, "connect", _on_sqlite_connect)
    else:
        _engine = create_engine(
            database_url,
            pool_size=pool_size,
            max_overflow=pool_overflow,
            pool_timeout=pool_timeout,
            pool_pre_ping=True,
            isolation_level="REPEATABLE READ",
        )

    _Session = scoped_session(sessionmaker(bind=_engine))
    Base.metadata.create_all(_engine)
    _migrate_schema(_engine)
    logger.info("中心数据库已初始化: %s", database_url.split("://")[0])
    return _engine


def _migrate_schema(engine):
    """Auto-add missing columns to existing tables (safe ALTER TABLE ADD COLUMN).

    SQLAlchemy's create_all only creates new tables; it never adds columns
    to existing tables. This scans every mapped table and adds any column
    present in the model but missing from the live database.
    """
    import sqlalchemy as sa

    for table in Base.metadata.sorted_tables:
        table_name = table.name
        with engine.connect() as conn:
            # Get existing columns from the live database
            if engine.dialect.name == "sqlite":
                rows = conn.execute(
                    sa.text(f"PRAGMA table_info('{table_name}')")
                ).fetchall()
                existing = {row[1] for row in rows}  # column name is index 1
            else:
                # PostgreSQL / others
                insp = sa.inspect(engine)
                cols = insp.get_columns(table_name)
                existing = {c["name"] for c in cols}

            for col in table.columns:
                if col.name not in existing:
                    col_type_sql = _render_column_type(engine.dialect.name, col)
                    # NOT NULL columns need a DEFAULT for existing rows in PostgreSQL
                    default_clause = ""
                    if not col.nullable and col.default is None and not col.primary_key:
                        default_clause = _default_for_type(col)
                    sql = (
                        f"ALTER TABLE {table_name} ADD COLUMN "
                        f"{col.name} {col_type_sql}{default_clause}"
                    )
                    try:
                        conn.execute(sa.text(sql))
                        logger.warning(
                            "迁移: %s.%s (%s) 已添加", table_name, col.name, col_type_sql
                        )
                    except Exception as e:
                        # 竞态条件: 多个进程同时启动时可能重复添加同一列
                        err_msg = str(e).lower()
                        if "duplicate" in err_msg or "already exists" in err_msg:
                            continue
                        raise
            conn.commit()


def _render_column_type(dialect, col):
    """Render a Column's type as DDL-compatible SQL for ALTER TABLE ADD COLUMN."""
    from sqlalchemy import Integer, String, Float, DateTime, Text, Boolean

    t = col.type
    if isinstance(t, Integer):
        return "INTEGER"
    if isinstance(t, String):
        return f"VARCHAR({t.length or 255})" if dialect != "sqlite" else "TEXT"
    if isinstance(t, Float):
        return "REAL" if dialect == "sqlite" else "DOUBLE PRECISION"
    if isinstance(t, DateTime):
        return "DATETIME" if dialect == "sqlite" else "TIMESTAMP"
    if isinstance(t, Text):
        return "TEXT"
    if isinstance(t, Boolean):
        return "INTEGER" if dialect == "sqlite" else "BOOLEAN"
    # Fallback: use SQLAlchemy's type compiler
    from sqlalchemy import create_engine as _ce
    return str(t.compile(dialect=_ce("sqlite:///").dialect if dialect == "sqlite" else None))


def _default_for_type(col):
    """Return a DEFAULT clause for a non-nullable column with no explicit default."""
    from sqlalchemy import Integer, String, Float, DateTime, Text, Boolean
    from datetime import datetime, timezone
    t = col.type
    if isinstance(t, (Integer, Float)):
        return " DEFAULT 0"
    if isinstance(t, Boolean):
        return " DEFAULT 0"  # SQLite uses 0/1 for boolean
    if isinstance(t, DateTime):
        return " DEFAULT '1970-01-01T00:00:00'"
    if isinstance(t, (String, Text)):
        return " DEFAULT ''"
    return ""


def get_session():
    """获取当前线程的数据库会话"""
    if _Session is None:
        raise RuntimeError("数据库未初始化，请先调用 init_db()")
    return _Session()


def close_db():
    global _Session
    if _Session:
        _Session.remove()


# ── CRUD helpers (可替换原有 CenterDB 调用) ──

def upsert_device(name: str, location: str = "",
                  lat: float = None, lon: float = None, alt: float = None):
    sess = get_session()
    now = datetime.now(timezone.utc)
    dev = sess.get(Device, name)
    if dev:
        dev.last_seen = now
        if lat is not None:
            dev.lat = lat
        if lon is not None:
            dev.lon = lon
        if alt is not None:
            dev.alt = alt
        if location:
            dev.location = location
        dev.status = "online"
    else:
        dev = Device(
            name=name, location=location,
            lat=lat if lat is not None else 0,
            lon=lon if lon is not None else 0,
            alt=alt if alt is not None else 0,
            first_seen=now, last_seen=now, status="online",
        )
        sess.add(dev)
    # 填充 tenant_id（来自设备密钥表）
    if dev.tenant_id is None:
        ds = sess.get(DeviceSecret, name)
        if ds and ds.tenant_id is not None:
            dev.tenant_id = ds.tenant_id
    sess.commit()


def upsert_drone(device_name: str, drone_id: str,
                 lat: float, lon: float, alt: float,
                 speed: float = 0, heading: float = 0,
                 model: str = "", height_agl: float = None):
    # 基础数据校验 — 过滤明显脏数据
    if not drone_id or len(drone_id) < 4 or len(drone_id) > 64:
        return
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return
    if alt < -500 or alt > 20000:
        return
    sess = get_session()
    now = datetime.now(timezone.utc)
    drone = sess.get(Drone, (drone_id, device_name))
    if drone:
        drone.last_seen = now
        drone.last_lat = lat
        drone.last_lon = lon
        drone.last_alt = alt
        drone.last_speed = speed
        drone.last_heading = heading
        drone.status = "active"
        if model and not drone.model:
            drone.model = model
        if height_agl is not None:
            drone.height_agl = height_agl
            if drone.max_alt_agl is None or height_agl > drone.max_alt_agl:
                drone.max_alt_agl = height_agl
        if drone.max_alt_asl is None or alt > drone.max_alt_asl:
            drone.max_alt_asl = alt
    else:
        drone = Drone(
            id=drone_id, device_name=device_name,
            last_seen=now, last_lat=lat, last_lon=lon, last_alt=alt,
            last_speed=speed, last_heading=heading, status="active",
            model=model, max_alt_agl=height_agl, max_alt_asl=alt,
        )
        sess.add(drone)
    # 填充 tenant_id（来自设备密钥表）
    if drone.tenant_id is None:
        ds = sess.get(DeviceSecret, device_name)
        if ds and ds.tenant_id is not None:
            drone.tenant_id = ds.tenant_id
    sess.commit()


def update_drone_status(device_name: str, drone_id: str,
                        distance: float, line_name: str, status: str):
    sess = get_session()
    drone = sess.get(Drone, (drone_id, device_name))
    if drone:
        if drone.min_distance is None or distance < drone.min_distance:
            drone.min_distance = distance
        drone.nearest_line = line_name
        drone.status = status
        sess.commit()


def add_drone_position(drone_id: str, device_name: str,
                       lat: float, lon: float, alt: float,
                       distance: float = None, line_name: str = ""):
    """记录无人机位置历史 — 供轨迹回放使用"""
    sess = get_session()
    try:
        dp = DronePosition(
            drone_id=drone_id, device_name=device_name,
            lat=lat, lon=lon, alt=alt,
            distance_to_line=distance, nearest_line=line_name,
            timestamp=datetime.now(timezone.utc),
        )
        sess.add(dp)
        sess.commit()
    finally:
        sess.close()


def add_alert(device_name: str, drone_id: str, level: str,
              distance: float, line_name: str, message: str):
    sess = get_session()
    now = datetime.now(timezone.utc)
    a = Alert(device_name=device_name, drone_id=drone_id,
              timestamp=now, level=level, distance=distance,
              line_name=line_name, message=message)
    sess.add(a)
    sess.commit()


def get_devices() -> list:
    sess = get_session()
    devices = sess.query(Device).order_by(Device.last_seen.desc()).all()
    # 动态计算每个设备下的活跃无人机数
    from sqlalchemy import func as _func
    counts = dict(sess.query(
        Drone.device_name, _func.count()
    ).filter(Drone.status != "offline").group_by(Drone.device_name).all() or [])
    return [
        {
            "name": d.name, "location": d.location or "",
            "lat": d.lat or 0, "lon": d.lon or 0, "alt": d.alt or 0,
            "first_seen": _bj(d.first_seen),
            "last_seen": _bj(d.last_seen),
            "status": d.status or "offline",
            "drone_count": counts.get(d.name, 0),
            "alert_count": d.alert_count or 0,
            "station_name": d.station_name or "",
            "tenant_id": d.tenant_id,
        }
        for d in devices
    ]


def get_all_drones() -> list:
    sess = get_session()
    drones = sess.query(Drone).filter(Drone.status != "gone").order_by(
        Drone.last_seen.desc()
    ).all()
    return [
        {
            "id": d.id, "device_name": d.device_name,
            "last_seen": _bj(d.last_seen),
            "last_lat": d.last_lat or 0, "last_lon": d.last_lon or 0,
            "last_alt": d.last_alt or 0,
            "last_speed": d.last_speed or 0, "last_heading": d.last_heading or 0,
            "min_distance": d.min_distance, "nearest_line": d.nearest_line or "",
            "status": d.status or "active",
            "rssi": d.rssi or 0,
            "status_code": d.status_code or 0, "height_agl": d.height_agl,
            "model": d.model or "",
            "max_alt_agl": d.max_alt_agl,
            "max_alt_asl": d.max_alt_asl,
            "tenant_id": d.tenant_id,
        }
        for d in drones
    ]


def get_trajectory_summaries(drone_id: str = None,
                            date_from: str = None, date_to: str = None) -> dict:
    """返回轨迹摘要 — 单次 GROUP BY 查询，避免 N+1"""
    from sqlalchemy import func
    sess = get_session()
    q = sess.query(
        DronePosition.drone_id,
        func.count().label("count"),
        func.min(DronePosition.timestamp).label("first_ts"),
        func.max(DronePosition.timestamp).label("last_ts"),
        func.min(DronePosition.distance_to_line).label("min_dist"),
        func.max(DronePosition.device_name).label("device_name"),
    )
    if drone_id:
        q = q.filter(DronePosition.drone_id.ilike(f"%{drone_id}%"))
    if date_from:
        q = q.filter(DronePosition.timestamp >= date_from)
    if date_to:
        q = q.filter(DronePosition.timestamp <= date_to)
    rows = q.group_by(DronePosition.drone_id).all()
    result = {}
    for row in rows:
        result[row.drone_id] = {
            "count": row.count,
            "min_dist": row.min_dist or 0,
            "first": _bj(row.first_ts)[:19] if row.first_ts else "",
            "last": _bj(row.last_ts)[:19] if row.last_ts else "",
            "device_name": row.device_name or "",
        }
    return result


def get_trajectory_points(drone_id: str, limit: int = 500) -> list:
    """返回指定无人机轨迹坐标点"""
    sess = get_session()
    points = sess.query(DronePosition).filter(
        DronePosition.drone_id == drone_id
    ).order_by(DronePosition.timestamp.desc()).limit(limit).all()
    return [{
        'lat': p.lat,
        'lon': p.lon,
        'alt': p.alt,
        'distance': p.distance_to_line,
        'nearest_line': p.nearest_line or '',
        'nearby_lines': p.nearby_lines or '',
        'time': _bj(p.timestamp)[:19] if p.timestamp else '',
    } for p in points]


def get_recent_alerts(limit: int = 100, level: str = None, since: str = None,
                     to_date: str = None, drone_id: str = None,
                     device_name: str = None, acknowledged: int = None) -> list:
    sess = get_session()
    q = sess.query(Alert)
    if level:
        q = q.filter(Alert.level == level)
    if since:
        q = q.filter(Alert.timestamp >= since)
    if to_date:
        q = q.filter(Alert.timestamp <= to_date)
    if drone_id:
        q = q.filter(Alert.drone_id == drone_id)
    if device_name:
        q = q.filter(Alert.device_name == device_name)
    if acknowledged is not None:
        q = q.filter(Alert.acknowledged == acknowledged)
    alerts = q.order_by(Alert.timestamp.desc()).limit(limit).all()
    return [
        {
            "id": a.id,
            "device_name": a.device_name, "drone_id": a.drone_id,
            "timestamp": _bj(a.timestamp),
            "level": a.level, "distance": a.distance,
            "line_name": a.line_name or "", "message": a.message or "",
            "acknowledged": bool(a.acknowledged),
            "ack_by": a.ack_by or "",
            "ack_time": _bj(a.ack_time),
            "ack_note": a.ack_note or "",
        }
        for a in alerts
    ]


def acknowledge_alert(alert_id: int, username: str, note: str = "") -> bool:
    sess = get_session()
    a = sess.get(Alert, alert_id)
    if a:
        a.acknowledged = 1
        a.ack_by = username
        a.ack_time = datetime.now(timezone.utc)
        a.ack_note = note
        sess.commit()
        return True
    return False


def get_hourly_alert_counts(hours: int = 24) -> list:
    """近N小时每小时的告警数量 (用于24h趋势图) — 返回北京时间小时键"""
    sess = get_session()
    cutoff_utc = datetime.now(timezone.utc) - timedelta(hours=hours)
    alerts = sess.query(Alert.timestamp, Alert.level).filter(
        Alert.timestamp >= cutoff_utc
    ).order_by(Alert.timestamp).all()
    buckets: dict = {}  # key: "HH:00_level" → count
    for ts, level in alerts:
        if not ts or not level:
            continue
        # 转北京时间
        if ts.tzinfo is None:
            from datetime import timezone as _tz
            ts = ts.replace(tzinfo=_tz.utc)
        bj_ts = ts.astimezone(_BEIJING_TZ)
        hour_key = bj_ts.strftime("%Y-%m-%dT%H:00")
        bucket_key = f"{hour_key}|{level}"
        buckets[bucket_key] = buckets.get(bucket_key, 0) + 1
    result = []
    for key, count in sorted(buckets.items()):
        hour, level = key.rsplit("|", 1)
        result.append({"hour": hour, "level": level, "count": count})
    return result


def get_alert_stats() -> dict:
    """告警统计: 按等级计数"""
    from sqlalchemy import func
    sess = get_session()
    rows = sess.query(
        Alert.level, func.count()
    ).filter(
        Alert.acknowledged == 0
    ).group_by(Alert.level).all()
    result = {"critical": 0, "severe": 0, "warning": 0}
    for level, cnt in rows:
        if level in result:
            result[level] = cnt
    return result


def mark_stale_devices(timeout_seconds: int = 60):
    sess = get_session()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
        sess.query(Device).filter(Device.last_seen < cutoff).update(
            {"status": "offline"}, synchronize_session=False
        )
        sess.query(Drone).filter(Drone.last_seen < cutoff).update(
            {"status": "offline"}, synchronize_session=False
        )
        sess.commit()
    finally:
        sess.close()


# ── Power Line CRUD ──

def get_power_lines(device_name: str = None, lat: float = None, lon: float = None,
                   radius_km: float = None) -> list:
    """获取电力线列表。device_name=None返回全局电力线，否则返回全局+该设备的。
    可选 lat/lon/radius_km 空间过滤，只返回至少一端在半径内的线路。"""
    sess = get_session()
    q = sess.query(PowerLine)
    if device_name:
        from sqlalchemy import or_
        q = q.filter(or_(PowerLine.device_name == None, PowerLine.device_name == device_name))
    lines = q.order_by(PowerLine.id).all()

    if lat is not None and lon is not None and radius_km:
        import math
        deg_per_km_lat = 1.0 / 111.32
        deg_per_km_lon = 1.0 / (111.32 * math.cos(math.radians(lat)))
        dlat = radius_km * deg_per_km_lat
        dlon = radius_km * deg_per_km_lon
        filtered = []
        for l in lines:
            if ((lat - dlat <= l.lat1 <= lat + dlat and lon - dlon <= l.lon1 <= lon + dlon) or
                (lat - dlat <= l.lat2 <= lat + dlat and lon - dlon <= l.lon2 <= lon + dlon)):
                filtered.append(l)
        lines = filtered

    return [{
        'id': l.id, 'name': l.name,
        'lat1': l.lat1, 'lon1': l.lon1, 'alt1': l.alt1,
        'lat2': l.lat2, 'lon2': l.lon2, 'alt2': l.alt2,
        'tower_height1': l.tower_height1,
        'tower_height2': l.tower_height2,
        'voltage_level': l.voltage_level or '',
        'device_name': l.device_name or '',
        'updated_at': _bj(l.updated_at),
    } for l in lines]


# ── 塔杆高度参考 (GB 50545 / DL/T 5092 典型值) ──
TYPICAL_TOWER_HEIGHTS = {
    '10kV': 15, '35kV': 18, '66kV': 22,
    '110kV': 25, '220kV': 35, '330kV': 40,
    '500kV': 50, '750kV': 60, '±800kV': 65, '1000kV': 80,
}


def estimate_tower_height(voltage_level: str) -> float:
    """根据电压等级返回典型塔杆高度 (m), 未匹配返回 25m"""
    if not voltage_level:
        return 25.0
    for k, v in TYPICAL_TOWER_HEIGHTS.items():
        if k in voltage_level:
            return float(v)
    return 25.0


def upsert_power_line(data: dict) -> int:
    """新增或更新电力线，返回id"""
    sess = get_session()
    now = datetime.now(timezone.utc)
    pl_id = data.get('id')
    if pl_id:
        pl = sess.get(PowerLine, pl_id)
        if pl:
            for k in ('name', 'lat1', 'lon1', 'alt1', 'lat2', 'lon2', 'alt2',
                      'tower_height1', 'tower_height2', 'voltage_level', 'device_name'):
                if k in data:
                    setattr(pl, k, data[k])
            pl.updated_at = now
            sess.commit()
            return pl_id
    pl = PowerLine(
        name=data.get('name', ''),
        lat1=data.get('lat1', 0), lon1=data.get('lon1', 0), alt1=data.get('alt1', 0),
        lat2=data.get('lat2', 0), lon2=data.get('lon2', 0), alt2=data.get('alt2', 0),
        tower_height1=data.get('tower_height1'),
        tower_height2=data.get('tower_height2'),
        voltage_level=data.get('voltage_level', ''),
        device_name=data.get('device_name') or None,
        created_at=now, updated_at=now,
    )
    sess.add(pl)
    sess.commit()
    return pl.id


def delete_power_line(pl_id: int) -> bool:
    sess = get_session()
    pl = sess.get(PowerLine, pl_id)
    if pl:
        sess.delete(pl)
        sess.commit()
        return True
    return False


def load_power_lines_from_list(lines: list):
    """从列表批量替换电力线（用于边缘同步）— 事务保护"""
    sess = get_session()
    try:
        sess.query(PowerLine).delete()
        now = datetime.now(timezone.utc)
        for i, l in enumerate(lines, 1):
            pl = PowerLine(
                id=i,
                name=l.get('name', ''),
                lat1=l.get('lat1', 0), lon1=l.get('lon1', 0), alt1=l.get('alt1', 0),
                lat2=l.get('lat2', 0), lon2=l.get('lon2', 0), alt2=l.get('alt2', 0),
                tower_height1=l.get('tower_height1'),
                tower_height2=l.get('tower_height2'),
                voltage_level=l.get('voltage_level', ''),
                device_name=l.get('device_name') or None,
                created_at=now, updated_at=now,
            )
            sess.add(pl)
        sess.commit()
    except Exception:
        sess.rollback()
        raise


# ── Web User CRUD ──

from werkzeug.security import generate_password_hash, check_password_hash


def get_web_users() -> list:
    sess = get_session()
    return [{
        'username': u.username, 'role': u.role, 'station': u.station or '',
        'tenant_id': u.tenant_id,
        'scope': u.scope or 'station',
        'assigned_station': u.assigned_station or '',
    } for u in sess.query(WebUser).all()]


def verify_web_user(username: str, password: str) -> dict:
    """验证web用户，成功返回用户dict，失败返回None"""
    sess = get_session()
    u = sess.get(WebUser, username)
    if u and check_password_hash(u.password_hash, password):
        return {
            'username': u.username, 'role': u.role, 'station': u.station or '',
            'tenant_id': u.tenant_id,
            'scope': u.scope or 'station',
            'assigned_station': u.assigned_station or '',
        }
    return None


def upsert_web_user(username: str, password: str = None, role: str = 'user',
                    station: str = '', tenant_id: int = None, scope: str = 'station',
                    assigned_station: str = '') -> bool:
    sess = get_session()
    u = sess.get(WebUser, username)
    if u:
        if password:
            u.password_hash = generate_password_hash(password)
        u.role = role
        u.station = station
        if tenant_id is not None:
            u.tenant_id = tenant_id
        u.scope = scope
        u.assigned_station = assigned_station
    else:
        if not password:
            return False
        u = WebUser(
            username=username,
            password_hash=generate_password_hash(password),
            role=role, station=station,
            tenant_id=tenant_id, scope=scope,
            assigned_station=assigned_station,
        )
        sess.add(u)
    sess.commit()
    return True


def delete_web_user(username: str) -> bool:
    sess = get_session()
    u = sess.get(WebUser, username)
    if u:
        sess.delete(u)
        sess.commit()
        return True
    return False


def count_admin_users() -> int:
    sess = get_session()
    return sess.query(WebUser).filter(WebUser.role == 'admin').count()


# ── Station CRUD ──

def get_stations() -> list:
    sess = get_session()
    devices = {d.name: d for d in sess.query(Device).all()}
    return [{
        'name': s.name, 'location': s.location or '',
        'province': s.province or '', 'city': s.city or '', 'county': s.county or '',
        'lat': s.lat or 0, 'lon': s.lon or 0, 'alt': s.alt or 0,
        'device_name': s.device_name or '',
        'webhook_url': s.webhook_url or '',
        'mqtt_online': devices[s.device_name].status == 'online' if s.device_name and s.device_name in devices else False,
        'tenant_id': s.tenant_id,
    } for s in sess.query(Station).all()]


def upsert_station(name: str, location: str = '', lat: float = 0, lon: float = 0,
                   alt: float = 0, device_name: str = None, tenant_id: int = None,
                   province: str = '', city: str = '', county: str = '',
                   webhook_url: str = None):
    sess = get_session()
    s = sess.get(Station, name)
    if s:
        s.location = location
        s.province = province
        s.city = city
        s.county = county
        if lat or lon:  # 只有传入非零坐标时才更新，避免地理编码覆盖已有 GPS
            s.lat = lat; s.lon = lon
        if alt:
            s.alt = alt
        if device_name is not None:
            s.device_name = device_name
        if tenant_id is not None:
            s.tenant_id = tenant_id
        if webhook_url is not None:
            s.webhook_url = webhook_url
    else:
        s = Station(name=name, location=location, province=province, city=city,
                    county=county, lat=lat, lon=lon, alt=alt,
                    device_name=device_name, tenant_id=tenant_id,
                    webhook_url=webhook_url or '')
        sess.add(s)
    sess.commit()


def delete_station(name: str) -> bool:
    sess = get_session()
    s = sess.get(Station, name)
    if s:
        # 清理关联设备的站点绑定
        if s.device_name:
            ds = sess.get(DeviceSecret, s.device_name)
            if ds and ds.station == name:
                ds.station = ""
        sess.delete(s)
        sess.commit()
        return True
    return False


# ── System Settings CRUD ──

def get_settings() -> dict:
    sess = get_session()
    return {s.key: s.value for s in sess.query(SystemSetting).all()}


def get_setting(key: str, default: str = '') -> str:
    sess = get_session()
    s = sess.get(SystemSetting, key)
    return s.value if s else default


def set_setting(key: str, value: str):
    sess = get_session()
    s = sess.get(SystemSetting, key)
    if s:
        s.value = value
    else:
        sess.add(SystemSetting(key=key, value=value))
    sess.commit()


# ── Audit Log ──

def add_audit_log(username: str, operation: str, table_name: str = '',
                  record_id: int = None, detail: str = ''):
    sess = get_session()
    a = AuditLog(
        timestamp=datetime.now(timezone.utc),
        username=username, operation=operation,
        table_name=table_name, record_id=record_id, detail=detail,
    )
    sess.add(a)
    sess.commit()


def get_audit_logs(limit: int = 100) -> list:
    sess = get_session()
    logs = sess.query(AuditLog).order_by(AuditLog.timestamp.desc()).limit(limit).all()
    return [{
        'id': a.id,
        'timestamp': _bj(a.timestamp),
        'username': a.username, 'operation': a.operation,
        'table_name': a.table_name, 'record_id': a.record_id, 'detail': a.detail,
    } for a in logs]


# ── Device Secrets ──

def get_device_secrets(tenant_id: int = None) -> list:
    sess = get_session()
    q = sess.query(DeviceSecret)
    if tenant_id is not None:
        q = q.filter(DeviceSecret.tenant_id == tenant_id)
    return [{
        'device_name': d.device_name,
        'device_secret': d.device_secret,
        'station': d.station or '',
        'client_cert': d.client_cert,
        'cert_serial': d.cert_serial,
        'cert_issued_at': _bj(d.cert_issued_at),
        'revoked': bool(d.revoked),
        'revoked_at': _bj(d.revoked_at),
        'tenant_id': d.tenant_id,
        'created_at': _bj(d.created_at),
    } for d in q.all()]


def upsert_device_secret(device_name: str, device_secret: str, station: str = '',
                        client_cert: str = None, cert_serial: str = None,
                        cert_issued_at = None, tenant_id: int = None) -> bool:
    sess = get_session()
    d = sess.get(DeviceSecret, device_name)
    if d:
        d.device_secret = device_secret
        d.station = station
        if tenant_id is not None:
            d.tenant_id = tenant_id
        if client_cert is not None:
            d.client_cert = client_cert
        if cert_serial is not None:
            d.cert_serial = cert_serial
        if cert_issued_at is not None:
            d.cert_issued_at = cert_issued_at
    else:
        d = DeviceSecret(device_name=device_name, device_secret=device_secret,
                         station=station,
                         client_cert=client_cert,
                         cert_serial=cert_serial,
                         cert_issued_at=cert_issued_at,
                         tenant_id=tenant_id,
                         created_at=datetime.now(timezone.utc))
        sess.add(d)
    sess.commit()
    return True


def delete_device_secret(device_name: str) -> bool:
    sess = get_session()
    d = sess.get(DeviceSecret, device_name)
    if d:
        # 清理关联站点的 device_name 引用
        st = sess.query(Station).filter(Station.device_name == device_name).first()
        if st:
            st.device_name = ""
        sess.delete(d)
        sess.commit()
        return True
    return False


# ── Station Personnel (SMS) ──

def get_personnel_by_station(station_name: str) -> list:
    sess = get_session()
    rows = sess.query(StationPersonnel).filter(
        StationPersonnel.station_name == station_name
    ).all()
    return [{'name': r.name, 'phone': r.phone} for r in rows]


def get_all_alert_phones() -> list:
    sess = get_session()
    return list(set(r.phone for r in sess.query(StationPersonnel).all()))


def upsert_personnel(station_name: str, name: str, phone: str) -> int:
    sess = get_session()
    p = sess.query(StationPersonnel).filter(
        StationPersonnel.station_name == station_name,
        StationPersonnel.phone == phone,
    ).first()
    if p:
        p.name = name
    else:
        p = StationPersonnel(station_name=station_name, name=name, phone=phone)
        sess.add(p)
        sess.flush()
    sess.commit()
    return p.id


def get_all_personnel(station_name: str = None) -> list:
    sess = get_session()
    q = sess.query(StationPersonnel)
    if station_name:
        q = q.filter(StationPersonnel.station_name == station_name)
    return [{"id": r.id, "station_name": r.station_name,
             "name": r.name, "phone": r.phone} for r in q.all()]


def delete_personnel(personnel_id: int) -> bool:
    sess = get_session()
    p = sess.get(StationPersonnel, personnel_id)
    if p:
        sess.delete(p)
        sess.commit()
        return True
    return False


# ── Azimuth / Distance ──

def compute_azimuth_distance(station_lat: float, station_lon: float,
                             drone_lat: float, drone_lon: float):
    """计算站点到无人机的方位角(°)和水平距离(m)
    返回 (bearing_deg, horizontal_distance_m)
    """
    import math
    R = 6371000.0  # 地球半径 (m)

    lat1, lon1 = math.radians(station_lat), math.radians(station_lon)
    lat2, lon2 = math.radians(drone_lat), math.radians(drone_lon)

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    dist = R * c

    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    bearing = (math.degrees(math.atan2(y, x)) + 360) % 360

    return round(bearing, 1), round(dist, 1)


# ── Drone Model Distribution ──

def get_drone_model_distribution(device_names: set = None) -> list:
    """按 model 字段统计无人机型号分布"""
    from sqlalchemy import func
    sess = get_session()
    q = sess.query(Drone.model, func.count()).filter(
        Drone.status != "gone", Drone.model != "", Drone.model.isnot(None)
    )
    if device_names is not None:
        q = q.filter(Drone.device_name.in_(device_names))
    rows = q.group_by(Drone.model).order_by(func.count().desc()).all()
    return [{"model": m, "count": cnt} for m, cnt in rows]


# ── Drone Whitelist CRUD ──

def get_whitelist(tenant_id: int = None) -> list:
    sess = get_session()
    q = sess.query(DroneWhitelist)
    if tenant_id is not None:
        q = q.filter(DroneWhitelist.tenant_id == tenant_id)
    return [{
        "id": w.id, "sn": w.sn, "match_mode": w.match_mode,
        "note": w.note or "", "tenant_id": w.tenant_id,
        "created_at": _bj(w.created_at),
        "created_by": w.created_by or "",
    } for w in q.order_by(DroneWhitelist.created_at.desc()).all()]


def add_to_whitelist(sn: str, match_mode: str = "exact", note: str = "",
                     tenant_id: int = None, created_by: str = "") -> int:
    sess = get_session()
    w = DroneWhitelist(
        sn=sn.strip(), match_mode=match_mode, note=note,
        tenant_id=tenant_id, created_by=created_by,
        created_at=datetime.now(timezone.utc),
    )
    sess.add(w)
    sess.commit()
    return w.id


def remove_from_whitelist(whitelist_id: int) -> bool:
    sess = get_session()
    w = sess.get(DroneWhitelist, whitelist_id)
    if w:
        sess.delete(w)
        sess.commit()
        return True
    return False


def is_drone_whitelisted(drone_id: str, tenant_id: int = None) -> bool:
    """检查无人机是否在白名单中 (支持精确匹配和前缀匹配)"""
    if not drone_id:
        return False
    sess = get_session()
    # 精确匹配
    q = sess.query(DroneWhitelist).filter(
        DroneWhitelist.sn == drone_id,
        DroneWhitelist.match_mode == "exact",
    )
    if tenant_id is not None:
        q = q.filter(DroneWhitelist.tenant_id == tenant_id)
    if q.first():
        return True
    # 前缀匹配: drone_id LIKE sn || '%'
    from sqlalchemy import and_, literal
    q2 = sess.query(DroneWhitelist).filter(
        DroneWhitelist.match_mode == "prefix",
    )
    if tenant_id is not None:
        q2 = q2.filter(DroneWhitelist.tenant_id == tenant_id)
    for w in q2.all():
        if drone_id.startswith(w.sn):
            return True
    return False


# ── Tenant CRUD ──

import secrets
import string


def _generate_license_key() -> str:
    """生成 16 位随机密钥: XXXX-XXXX-XXXX-XXXX"""
    chars = string.ascii_uppercase + string.digits
    parts = [''.join(secrets.choice(chars) for _ in range(4)) for _ in range(4)]
    return '-'.join(parts)


def create_tenant(name: str, max_users: int = 3, contact: str = "",
                  created_by: str = "") -> dict:
    """创建租户并生成密钥, 返回 dict"""
    sess = get_session()
    # 确保唯一密钥
    for _ in range(10):
        key = _generate_license_key()
        existing = sess.get(Tenant, key) if False else sess.query(Tenant).filter(
            Tenant.license_key == key).first()
        if not existing:
            break
    now = datetime.now(timezone.utc)
    t = Tenant(
        name=name, license_key=key, max_users=max_users,
        is_active=True, created_at=now, created_by=created_by,
        contact=contact,
    )
    sess.add(t)
    sess.commit()
    return {
        'id': t.id, 'name': t.name, 'license_key': t.license_key,
        'max_users': t.max_users, 'is_active': t.is_active,
        'created_at': _bj(now), 'created_by': created_by,
        'contact': contact,
    }


def get_tenants() -> list:
    sess = get_session()
    return [{
        'id': t.id, 'name': t.name, 'license_key': t.license_key,
        'max_users': t.max_users, 'is_active': bool(t.is_active),
        'created_at': _bj(t.created_at),
        'created_by': t.created_by, 'contact': t.contact or '',
        'user_count': count_users_in_tenant(t.id),
    } for t in sess.query(Tenant).order_by(Tenant.id).all()]


def get_tenant_by_key(license_key: str):
    """按密钥查找租户, 返回 Tenant ORM 对象或 None"""
    sess = get_session()
    return sess.query(Tenant).filter(
        Tenant.license_key == license_key.strip().upper()
    ).first()


def get_tenant_by_id(tenant_id: int):
    sess = get_session()
    return sess.get(Tenant, tenant_id)


def update_tenant(tenant_id: int, **kwargs) -> bool:
    """更新租户: name, max_users, is_active, contact"""
    sess = get_session()
    t = sess.get(Tenant, tenant_id)
    if not t:
        return False
    for k in ('name', 'max_users', 'is_active', 'contact'):
        if k in kwargs and kwargs[k] is not None:
            setattr(t, k, kwargs[k])
    sess.commit()
    return True


def delete_tenant(tenant_id: int) -> bool:
    """软删除租户 (is_active=False)"""
    return update_tenant(tenant_id, is_active=False)


def count_users_in_tenant(tenant_id: int) -> int:
    """当前租户已注册用户数"""
    sess = get_session()
    return sess.query(WebUser).filter(WebUser.tenant_id == tenant_id).count()


def get_tenant_stations(tenant_id: int) -> list:
    """返回某个租户拥有的站点列表"""
    sess = get_session()
    stations = sess.query(Station).filter(
        Station.tenant_id == tenant_id
    ).all()
    return [{
        'name': s.name, 'location': s.location or '',
        'lat': s.lat or 0, 'lon': s.lon or 0, 'alt': s.alt or 0,
        'device_name': s.device_name or '',
        'tenant_id': s.tenant_id,
    } for s in stations]


def get_user_stations(username: str) -> list:
    """返回某用户有权查看的站点名列表
    admin → None (全部)
    tenant_admin (scope=tenant) → 租户下所有站点
    user (scope=station) → [assigned_station]
    未关联租户 → []
    """
    sess = get_session()
    u = sess.get(WebUser, username)
    if not u:
        return []
    if u.role == "admin":
        return None  # sentinel: 无限制
    if u.tenant_id is None:
        return []
    if u.scope == "tenant":
        stations = sess.query(Station).filter(
            Station.tenant_id == u.tenant_id
        ).all()
        return [s.name for s in stations] or []
    # scope == "station"
    if u.assigned_station:
        # 验证站点确实属于该租户
        st = sess.query(Station).filter(
            Station.name == u.assigned_station,
            Station.tenant_id == u.tenant_id,
        ).first()
        if st:
            return [u.assigned_station]
    return []


# ── Data Retention Cleanup ──

def cleanup_old_data():
    """定期清理过期数据: DronePosition (可配置天数), Alert (90天), AuditLog (90天).
    由 __init__.py 的后台守护线程调用.
    """
    archive_enabled = get_setting("raw_archive_enabled", "true")
    if archive_enabled != "true":
        return {"skipped": True, "reason": "raw_archive_enabled is not true"}

    retention_days = int(get_setting("raw_archive_retention_days", "30"))
    alert_retention_days = 90
    audit_retention_days = 90

    from datetime import datetime, timezone, timedelta

    sess = get_session()
    result = {"drone_positions": 0, "alerts": 0, "audit_logs": 0}
    try:
        # DronePosition (原始报文轨迹，可配置保留天数)
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        deleted = sess.query(DronePosition).filter(
            DronePosition.timestamp < cutoff
        ).delete(synchronize_session=False)
        result["drone_positions"] = deleted

        # Alert (告警记录，固定 90 天)
        alert_cutoff = datetime.now(timezone.utc) - timedelta(days=alert_retention_days)
        deleted = sess.query(Alert).filter(
            Alert.timestamp < alert_cutoff
        ).delete(synchronize_session=False)
        result["alerts"] = deleted

        # AuditLog (操作审计，固定 90 天)
        audit_cutoff = datetime.now(timezone.utc) - timedelta(days=audit_retention_days)
        deleted = sess.query(AuditLog).filter(
            AuditLog.timestamp < audit_cutoff
        ).delete(synchronize_session=False)
        result["audit_logs"] = deleted

        sess.commit()
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.close()

    return result
