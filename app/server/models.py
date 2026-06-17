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

    __table_args__ = (
        Index("idx_alerts_time", "timestamp"),
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


def get_recent_alerts(limit: int = 100) -> list:
    sess = get_session()
    alerts = sess.query(Alert).order_by(
        Alert.timestamp.desc()
    ).limit(limit).all()
    return [
        {
            "device_name": a.device_name, "drone_id": a.drone_id,
            "timestamp": a.timestamp.isoformat() if a.timestamp else "",
            "level": a.level, "distance": a.distance,
            "line_name": a.line_name or "", "message": a.message or "",
        }
        for a in alerts
    ]


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
