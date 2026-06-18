"""
中心数据库 ORM 模型 — SQLAlchemy

支持 SQLite (开发) 和 PostgreSQL (生产) 双驱动
"""

from datetime import datetime, timezone, timedelta
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, ForeignKey, Index,
    create_engine, event,
)
from sqlalchemy.orm import (
    declarative_base, relationship, scoped_session, sessionmaker,
)

from logging_config import get_logger

logger = get_logger(__name__)

Base = declarative_base()


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

    drones = relationship("Drone", back_populates="device", cascade="all, delete-orphan")


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

    device = relationship("Device", back_populates="drones")

    __table_args__ = (
        Index("idx_drones_device", "device_name"),
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
    voltage_level = Column(String, default="")
    device_name = Column(String, ForeignKey("devices.name"), nullable=True)
    created_at = Column(DateTime)
    updated_at = Column(DateTime)


class WebUser(Base):
    __tablename__ = "web_users"

    username = Column(String, primary_key=True)
    password_hash = Column(String, nullable=False)
    role = Column(String, default="user")
    station = Column(String, default="")


class Station(Base):
    __tablename__ = "stations"

    name = Column(String, primary_key=True)
    location = Column(String, default="")
    lat = Column(Float, default=0)
    lon = Column(Float, default=0)
    alt = Column(Float, default=0)
    device_name = Column(String, nullable=True)


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key = Column(String, primary_key=True)
    value = Column(String, default="")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime)
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
    station = Column(String, default="")
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
    logger.info("中心数据库已初始化: %s", database_url.split("://")[0])
    return _engine


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
                  lat: float = 0, lon: float = 0, alt: float = 0):
    sess = get_session()
    now = datetime.now(timezone.utc)
    dev = sess.get(Device, name)
    if dev:
        dev.last_seen = now
        dev.lat = lat
        dev.lon = lon
        dev.alt = alt
        dev.location = location
        dev.status = "online"
    else:
        dev = Device(
            name=name, location=location, lat=lat, lon=lon, alt=alt,
            first_seen=now, last_seen=now, status="online",
        )
        sess.add(dev)
    sess.commit()


def upsert_drone(device_name: str, drone_id: str,
                 lat: float, lon: float, alt: float,
                 speed: float = 0, heading: float = 0):
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
    else:
        drone = Drone(
            id=drone_id, device_name=device_name,
            last_seen=now, last_lat=lat, last_lon=lon, last_alt=alt,
            last_speed=speed, last_heading=heading, status="active",
        )
        sess.add(drone)
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
    return [
        {
            "name": d.name, "location": d.location or "",
            "lat": d.lat or 0, "lon": d.lon or 0, "alt": d.alt or 0,
            "first_seen": d.first_seen.isoformat() if d.first_seen else "",
            "last_seen": d.last_seen.isoformat() if d.last_seen else "",
            "status": d.status or "offline",
            "drone_count": d.drone_count or 0, "alert_count": d.alert_count or 0,
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
            "last_seen": d.last_seen.isoformat() if d.last_seen else "",
            "last_lat": d.last_lat or 0, "last_lon": d.last_lon or 0,
            "last_alt": d.last_alt or 0,
            "last_speed": d.last_speed or 0, "last_heading": d.last_heading or 0,
            "min_distance": d.min_distance, "nearest_line": d.nearest_line or "",
            "status": d.status or "active",
        }
        for d in drones
    ]


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
            "timestamp": a.timestamp.isoformat() if a.timestamp else "",
            "level": a.level, "distance": a.distance,
            "line_name": a.line_name or "", "message": a.message or "",
            "acknowledged": bool(a.acknowledged),
            "ack_by": a.ack_by or "",
            "ack_time": a.ack_time.isoformat() if a.ack_time else "",
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
    """近N小时每小时的告警数量 (用于24h趋势图)"""
    from sqlalchemy import func
    sess = get_session()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = sess.query(
        func.strftime("%Y-%m-%dT%H:00", Alert.timestamp).label("hour"),
        func.count().label("cnt"),
    ).filter(
        Alert.timestamp >= cutoff
    ).group_by("hour").order_by("hour").all()
    return [{"hour": r.hour, "count": r.cnt} for r in rows]


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
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
    sess.query(Device).filter(Device.last_seen < cutoff).update(
        {"status": "offline"}, synchronize_session=False
    )
    sess.query(Drone).filter(Drone.last_seen < cutoff).update(
        {"status": "gone"}, synchronize_session=False
    )
    sess.commit()


# ── Power Line CRUD ──

def get_power_lines(device_name: str = None) -> list:
    """获取电力线列表。device_name=None返回全局电力线，否则返回全局+该设备的"""
    sess = get_session()
    q = sess.query(PowerLine)
    if device_name:
        from sqlalchemy import or_
        q = q.filter(or_(PowerLine.device_name == None, PowerLine.device_name == device_name))
    lines = q.order_by(PowerLine.id).all()
    return [{
        'id': l.id, 'name': l.name,
        'lat1': l.lat1, 'lon1': l.lon1, 'alt1': l.alt1,
        'lat2': l.lat2, 'lon2': l.lon2, 'alt2': l.alt2,
        'voltage_level': l.voltage_level or '',
        'device_name': l.device_name or '',
        'updated_at': l.updated_at.isoformat() if l.updated_at else '',
    } for l in lines]


def upsert_power_line(data: dict) -> int:
    """新增或更新电力线，返回id"""
    sess = get_session()
    now = datetime.now(timezone.utc)
    pl_id = data.get('id')
    if pl_id:
        pl = sess.get(PowerLine, pl_id)
        if pl:
            for k in ('name', 'lat1', 'lon1', 'alt1', 'lat2', 'lon2', 'alt2', 'voltage_level', 'device_name'):
                if k in data:
                    setattr(pl, k, data[k])
            pl.updated_at = now
            sess.commit()
            return pl_id
    pl = PowerLine(
        name=data.get('name', ''),
        lat1=data.get('lat1', 0), lon1=data.get('lon1', 0), alt1=data.get('alt1', 0),
        lat2=data.get('lat2', 0), lon2=data.get('lon2', 0), alt2=data.get('alt2', 0),
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
    """从列表批量替换电力线（用于边缘同步）"""
    sess = get_session()
    sess.query(PowerLine).delete()
    now = datetime.now(timezone.utc)
    for i, l in enumerate(lines, 1):
        pl = PowerLine(
            id=i,
            name=l.get('name', ''),
            lat1=l.get('lat1', 0), lon1=l.get('lon1', 0), alt1=l.get('alt1', 0),
            lat2=l.get('lat2', 0), lon2=l.get('lon2', 0), alt2=l.get('alt2', 0),
            voltage_level=l.get('voltage_level', ''),
            device_name=l.get('device_name') or None,
            created_at=now, updated_at=now,
        )
        sess.add(pl)
    sess.commit()


# ── Web User CRUD ──

from werkzeug.security import generate_password_hash, check_password_hash


def get_web_users() -> list:
    sess = get_session()
    return [{
        'username': u.username, 'role': u.role, 'station': u.station or '',
    } for u in sess.query(WebUser).all()]


def verify_web_user(username: str, password: str) -> dict:
    """验证web用户，成功返回用户dict，失败返回None"""
    sess = get_session()
    u = sess.get(WebUser, username)
    if u and check_password_hash(u.password_hash, password):
        return {'username': u.username, 'role': u.role, 'station': u.station or ''}
    return None


def upsert_web_user(username: str, password: str = None, role: str = 'user', station: str = '') -> bool:
    sess = get_session()
    u = sess.get(WebUser, username)
    if u:
        if password:
            u.password_hash = generate_password_hash(password)
        u.role = role
        u.station = station
    else:
        if not password:
            return False
        u = WebUser(
            username=username,
            password_hash=generate_password_hash(password),
            role=role, station=station,
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
    return [{
        'name': s.name, 'location': s.location or '',
        'lat': s.lat or 0, 'lon': s.lon or 0, 'alt': s.alt or 0,
        'device_name': s.device_name or '',
    } for s in sess.query(Station).all()]


def upsert_station(name: str, location: str = '', lat: float = 0, lon: float = 0,
                   alt: float = 0, device_name: str = None):
    sess = get_session()
    s = sess.get(Station, name)
    if s:
        s.location = location
        s.lat = lat; s.lon = lon; s.alt = alt
        s.device_name = device_name
    else:
        s = Station(name=name, location=location, lat=lat, lon=lon, alt=alt,
                    device_name=device_name)
        sess.add(s)
    sess.commit()


def delete_station(name: str) -> bool:
    sess = get_session()
    s = sess.get(Station, name)
    if s:
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
        'timestamp': a.timestamp.isoformat() if a.timestamp else '',
        'username': a.username, 'operation': a.operation,
        'table_name': a.table_name, 'record_id': a.record_id, 'detail': a.detail,
    } for a in logs]


# ── Device Secrets ──

def get_device_secrets() -> dict:
    sess = get_session()
    return {d.device_name: d.device_secret for d in sess.query(DeviceSecret).all()}


def upsert_device_secret(device_name: str, device_secret: str, station: str = '',
                        client_cert: str = None, cert_serial: str = None,
                        cert_issued_at = None) -> bool:
    sess = get_session()
    d = sess.get(DeviceSecret, device_name)
    if d:
        d.device_secret = device_secret
        d.station = station
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
                         created_at=datetime.now(timezone.utc))
        sess.add(d)
    sess.commit()
    return True


def delete_device_secret(device_name: str) -> bool:
    sess = get_session()
    d = sess.get(DeviceSecret, device_name)
    if d:
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


def upsert_personnel(station_name: str, name: str, phone: str):
    sess = get_session()
    p = sess.query(StationPersonnel).filter(
        StationPersonnel.station_name == station_name,
        StationPersonnel.phone == phone,
    ).first()
    if p:
        p.name = name
    else:
        sess.add(StationPersonnel(station_name=station_name, name=name, phone=phone))
    sess.commit()


def delete_personnel(personnel_id: int) -> bool:
    sess = get_session()
    p = sess.get(StationPersonnel, personnel_id)
    if p:
        sess.delete(p)
        sess.commit()
        return True
    return False
