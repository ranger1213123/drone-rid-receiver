"""
共享类型定义 — 所有 RID 协议共用

包含:
  - 消息类型常量 (MSG_*)
  - ID 类型 / UA 类型枚举
  - 所有 dataclass (ParsedRID, BasicIDMessage, LocationMessage, ...)
  - 两协议线格式相同的解码器: parse_basic_id, parse_self_id
"""

import struct
from dataclasses import dataclass, field
from typing import Optional, List

# ── 协议无关常量 ──

ODID_SERVICE_UUID = 0xFFFA  # ASTM 原始值, 保持向后兼容

MSG_BASIC_ID = 0x0
MSG_LOCATION = 0x1
MSG_AUTH = 0x2
MSG_SELF_ID = 0x3
MSG_SYSTEM = 0x4
MSG_OPERATOR_ID = 0x5
MSG_PACK = 0xF          # GB 46750-2025 打包报文

ID_TYPE_NONE = 0
ID_TYPE_SERIAL = 1
ID_TYPE_CAA = 2
ID_TYPE_UTM = 3
ID_TYPE_SESSION = 4

UA_TYPE_NONE = 0
UA_TYPE_AEROPLANE = 1
UA_TYPE_HELICOPTER = 2
UA_TYPE_GYROPLANE = 3
UA_TYPE_HYBRID = 4
UA_TYPE_ORNITHOPTER = 5
UA_TYPE_GLIDER = 6
UA_TYPE_KITE = 7
UA_TYPE_FREE_BALLOON = 8
UA_TYPE_CAPTIVE_BALLOON = 9
UA_TYPE_AIRSHIP = 10
UA_TYPE_FREE_FALL = 11
UA_TYPE_ROCKET = 12
UA_TYPE_TETHERED = 13
UA_TYPE_GROUND = 14
UA_TYPE_OTHER = 15

UA_TYPE_NAMES = {
    UA_TYPE_NONE: "未声明",
    UA_TYPE_AEROPLANE: "固定翼",
    UA_TYPE_HELICOPTER: "直升机/多旋翼",
    UA_TYPE_GYROPLANE: "旋翼机",
    UA_TYPE_HYBRID: "混合动力",
    UA_TYPE_ORNITHOPTER: "扑翼机",
    UA_TYPE_GLIDER: "滑翔机",
    UA_TYPE_KITE: "风筝",
    UA_TYPE_FREE_BALLOON: "自由气球",
    UA_TYPE_CAPTIVE_BALLOON: "系留气球",
    UA_TYPE_AIRSHIP: "飞艇",
    UA_TYPE_FREE_FALL: "自由落体",
    UA_TYPE_ROCKET: "火箭",
    UA_TYPE_TETHERED: "系留",
    UA_TYPE_GROUND: "地面障碍物",
    UA_TYPE_OTHER: "其他",
}

LOC_STATUS_TIMESTAMP_VALID = 0x10


# ── Dataclasses ──

@dataclass
class BasicIDMessage:
    """Basic ID 消息 - 无人机标识 (两协议线格式相同)"""
    id_type: int = 0
    ua_type: int = 0
    uas_id: str = ""


@dataclass
class LocationMessage:
    """Location/Vector 消息 - 无人机位置和速度"""
    status: int = 0
    speed_multiplier: float = 1.0
    speed_horizontal: float = 0.0
    speed_vertical: float = 0.0
    latitude: float = 0.0
    longitude: float = 0.0
    altitude_pressure: float = 0.0
    altitude_geodetic: float = 0.0
    height_agl: float = 0.0
    height_type: int = 0
    horizontal_accuracy: int = 0
    vertical_accuracy: int = 0
    baro_accuracy: int = 0
    speed_accuracy: int = 0
    timestamp: float = 0.0
    # GB 46750 独有字段 (ASTM 解析时保持默认值)
    track_angle: float = 0.0   # 航迹角 (0-359°)


@dataclass
class SelfIDMessage:
    """Self-ID / 运行描述消息 (两协议线格式相同)"""
    text: str = ""
    description_type: int = 0


@dataclass
class SystemMessage:
    """System 消息 - 操作员位置和区域"""
    operator_lat: float = 0.0
    operator_lon: float = 0.0
    area_count: int = 0
    area_radius: float = 0.0
    area_ceiling: float = 0.0
    area_floor: float = 0.0
    category_eu: int = 0
    class_eu: int = 0
    operator_alt_geo: float = 0.0
    # GB 46750 独有字段
    coordinate_system: int = 0   # 0=WGS-84, 1=CGCS2000
    timestamp_unix: int = 0      # Unix 时间戳 (从 2019-01-01 起算)


@dataclass
class OperatorIDMessage:
    """Operator ID 消息 (仅 ASTM F3411 使用)"""
    operator_id: str = ""
    operator_id_type: int = 0


@dataclass
class ParsedRID:
    """一次 RID 广播的完整解析结果"""
    raw_data: bytes = b""
    mac_address: str = ""
    rssi: int = 0
    basic_id: Optional[BasicIDMessage] = None
    location: Optional[LocationMessage] = None
    self_id: Optional[SelfIDMessage] = None
    system: Optional[SystemMessage] = None
    operator_id: Optional[OperatorIDMessage] = None
    messages: List = field(default_factory=list)

    @property
    def drone_id(self) -> Optional[str]:
        if self.basic_id:
            return self.basic_id.uas_id
        return None

    @property
    def has_location(self) -> bool:
        return self.location is not None


# ── 共享解码器 (两协议线格式相同) ──

def parse_basic_id(data: bytes) -> BasicIDMessage:
    """解析 Basic ID 消息 (msg_type=0x0) — ASTM / GB 46750 通用"""
    if len(data) < 2:
        return BasicIDMessage()
    id_type = data[0] & 0x0F
    ua_type = (data[0] >> 4) & 0x0F
    uas_id = data[2:22].split(b'\x00')[0].decode('ascii', errors='replace')
    return BasicIDMessage(id_type=id_type, ua_type=ua_type, uas_id=uas_id)


def parse_self_id(data: bytes) -> SelfIDMessage:
    """解析 Self-ID / 运行描述消息 (msg_type=0x3) — ASTM / GB 46750 通用"""
    if len(data) < 2:
        return SelfIDMessage()
    desc_type = data[0] & 0x0F
    text = data[1:24].split(b'\x00')[0].decode('utf-8', errors='replace')
    return SelfIDMessage(text=text, description_type=desc_type)
