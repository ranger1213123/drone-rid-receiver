"""
RID 消息解析器 - ASTM F3411 / ASD-STAN 4709-002 Open Drone ID

解析 BLE 广播和 WiFi Beacon 中的 Open Drone ID 消息。
支持 Message Pack 格式（多个消息打包在一次广播中）。
"""

import struct
from dataclasses import dataclass, field
from typing import Optional, List


# Open Drone ID BLE 16-bit Service UUID
ODID_SERVICE_UUID = 0xFFFA

# 消息类型
MSG_BASIC_ID = 0x0
MSG_LOCATION = 0x1
MSG_AUTH = 0x2
MSG_SELF_ID = 0x3
MSG_SYSTEM = 0x4
MSG_OPERATOR_ID = 0x5

# Basic ID 类型
ID_TYPE_NONE = 0
ID_TYPE_SERIAL = 1
ID_TYPE_CAA = 2
ID_TYPE_UTM = 3
ID_TYPE_SESSION = 4

# UA (无人机) 类型
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

# 位置状态标志
LOC_STATUS_TIMESTAMP_VALID = 0x10


@dataclass
class BasicIDMessage:
    """Basic ID 消息 - 无人机标识"""
    id_type: int = 0
    ua_type: int = 0
    uas_id: str = ""


@dataclass
class LocationMessage:
    """Location/Vector 消息 - 无人机位置和速度"""
    status: int = 0
    speed_multiplier: float = 1.0
    speed_horizontal: float = 0.0    # m/s
    speed_vertical: float = 0.0       # m/s
    latitude: float = 0.0             # 度
    longitude: float = 0.0            # 度
    altitude_pressure: float = 0.0    # 气压高度 (m)
    altitude_geodetic: float = 0.0    # 大地高度 (m, WGS84)
    height_agl: float = 0.0           # 离地高度 (m)
    height_type: int = 0
    horizontal_accuracy: int = 0
    vertical_accuracy: int = 0
    baro_accuracy: int = 0
    speed_accuracy: int = 0
    timestamp: float = 0.0            # 0.1秒精度，从当前小时开始


@dataclass
class SelfIDMessage:
    """Self-ID 消息 - 用户自定义文本"""
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


@dataclass
class OperatorIDMessage:
    """Operator ID 消息 - 操作员标识"""
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
        """获取无人机标识 (优先 Serial Number)"""
        if self.basic_id:
            return self.basic_id.uas_id
        return None

    @property
    def has_location(self) -> bool:
        """是否有位置数据"""
        return self.location is not None


def parse_basic_id(data: bytes) -> BasicIDMessage:
    """解析 Basic ID 消息 (msg_type=0x0)"""
    if len(data) < 2:
        return BasicIDMessage()
    id_type = data[0] & 0x0F
    ua_type = (data[0] >> 4) & 0x0F
    # ID 是 ASCII 字符串，最多 20 字节，去除尾部 null
    uas_id = data[2:22].split(b'\x00')[0].decode('ascii', errors='replace')
    return BasicIDMessage(id_type=id_type, ua_type=ua_type, uas_id=uas_id)


def parse_location(data: bytes) -> LocationMessage:
    """解析 Location/Vector 消息 (msg_type=0x1)"""
    if len(data) < 2:
        return LocationMessage()

    status = data[0]
    direction_byte = data[1]
    speed_mult = 0.25 if (direction_byte & 0x01) else 1.0

    # 水平速度 (cm/s → m/s), unsigned
    speed_h = struct.unpack_from('<H', data, 2)[0] * 0.01 * speed_mult

    # 垂直速度 (cm/s → m/s), signed
    speed_v_raw = struct.unpack_from('<h', data, 4)[0]
    speed_v = speed_v_raw * 0.01 * speed_mult

    # 经纬度 (degrees * 1e7 → degrees)
    lat = struct.unpack_from('<i', data, 6)[0] / 1e7
    lon = struct.unpack_from('<i', data, 10)[0] / 1e7

    # 气压高度 (m * 2 → m), int16
    alt_p = struct.unpack_from('<h', data, 14)[0] * 0.5

    # 大地高度 (m * 2 → m), int16
    alt_g = struct.unpack_from('<h', data, 16)[0] * 0.5

    # AGL 高度 (m * 2 → m), uint16
    height_agl = struct.unpack_from('<H', data, 18)[0] * 0.5

    height_type = data[20] & 0x0F
    h_acc = (data[21] >> 4) & 0x0F
    v_acc = data[21] & 0x0F
    baro_acc = (data[22] >> 4) & 0x0F
    spd_acc = data[22] & 0x0F
    ts = struct.unpack_from('<H', data, 23)[0] * 0.1

    return LocationMessage(
        status=status,
        speed_multiplier=speed_mult,
        speed_horizontal=speed_h,
        speed_vertical=speed_v,
        latitude=lat,
        longitude=lon,
        altitude_pressure=alt_p,
        altitude_geodetic=alt_g,
        height_agl=height_agl,
        height_type=height_type,
        horizontal_accuracy=h_acc,
        vertical_accuracy=v_acc,
        baro_accuracy=baro_acc,
        speed_accuracy=spd_acc,
        timestamp=ts,
    )


def parse_self_id(data: bytes) -> SelfIDMessage:
    """解析 Self-ID 消息 (msg_type=0x3)"""
    if len(data) < 2:
        return SelfIDMessage()
    desc_type = data[0] & 0x0F
    text = data[1:24].split(b'\x00')[0].decode('utf-8', errors='replace')
    return SelfIDMessage(text=text, description_type=desc_type)


def parse_system(data: bytes) -> SystemMessage:
    """解析 System 消息 (msg_type=0x4)"""
    if len(data) < 2:
        return SystemMessage()
    flags = data[0]
    op_lat = struct.unpack_from('<i', data, 1)[0] / 1e7 if flags & 0x01 else 0.0
    op_lon = struct.unpack_from('<i', data, 5)[0] / 1e7 if flags & 0x01 else 0.0
    area_count = struct.unpack_from('<H', data, 9)[0]
    area_radius = data[11]
    area_ceiling = struct.unpack_from('<h', data, 12)[0] * 0.5
    area_floor = struct.unpack_from('<h', data, 14)[0] * 0.5
    cat_eu = (data[16] >> 4) & 0x0F if len(data) > 16 else 0
    cls_eu = data[16] & 0x0F if len(data) > 16 else 0
    op_alt = struct.unpack_from('<h', data, 17)[0] * 0.5 if len(data) > 18 else 0.0
    return SystemMessage(
        operator_lat=op_lat,
        operator_lon=op_lon,
        area_count=area_count,
        area_radius=area_radius,
        area_ceiling=area_ceiling,
        area_floor=area_floor,
        category_eu=cat_eu,
        class_eu=cls_eu,
        operator_alt_geo=op_alt,
    )


def parse_operator_id(data: bytes) -> OperatorIDMessage:
    """解析 Operator ID 消息 (msg_type=0x5)"""
    if len(data) < 2:
        return OperatorIDMessage()
    op_type = data[0]
    op_id = data[1:21].split(b'\x00')[0].decode('ascii', errors='replace')
    return OperatorIDMessage(operator_id=op_id, operator_id_type=op_type)


# 消息解析器映射
_MESSAGE_PARSERS = {
    MSG_BASIC_ID:    parse_basic_id,
    MSG_LOCATION:    parse_location,
    MSG_SELF_ID:     parse_self_id,
    MSG_SYSTEM:      parse_system,
    MSG_OPERATOR_ID: parse_operator_id,
}


def parse_rid_pack(data: bytes, mac_address: str = "", rssi: int = 0) -> ParsedRID:
    """
    解析一个完整的 RID 消息包 (BLE Service Data / WiFi Beacon)

    Open Drone ID 消息包格式 (BLE Service Data):
      Byte 0:    Message Counter (0-7, each tx increments)
      Byte 1:    (reserved) | (4 bits) Version (4 bits)
      Byte 2+:   一条或多条消息

    每条消息格式:
      Byte 0:    Message Type (低4位) | Proto Version (高4位)
      Byte 1:    Message-specific data...

    WiFi Beacon (Nanobeacon) format:
      Byte 0-5:  MAC address
      Byte 6:    Message Counter
      Byte 7+:   同上消息格式
    """
    result = ParsedRID(raw_data=data, mac_address=mac_address, rssi=rssi)

    if len(data) < 2:
        return result

    offset = 0

    # 判断是否为 WiFi Nanobeacon (6-byte MAC prefix)
    if len(data) >= 8 and data[6] <= 7:
        offset = 7  # skip MAC + counter
    elif len(data) >= 2:
        offset = 2  # skip counter + version

    while offset < len(data) - 1:
        header = data[offset]
        msg_type = header & 0x0F
        # proto_version = (header >> 4) & 0x0F

        # 根据消息类型确定长度
        msg_lengths = {
            MSG_BASIC_ID:    22,
            MSG_LOCATION:    25,
            MSG_AUTH:        0,   # 跳过认证消息
            MSG_SELF_ID:     24,
            MSG_SYSTEM:      19,
            MSG_OPERATOR_ID: 21,
        }

        msg_len = msg_lengths.get(msg_type, 0)
        if msg_len == 0:
            break  # 未知消息类型或认证消息，停止解析

        payload = data[offset + 1:offset + 1 + msg_len]
        parser = _MESSAGE_PARSERS.get(msg_type)

        if parser and len(payload) >= 1:
            parsed = parser(payload)
            if msg_type == MSG_BASIC_ID:
                result.basic_id = parsed
            elif msg_type == MSG_LOCATION:
                result.location = parsed
            elif msg_type == MSG_SELF_ID:
                result.self_id = parsed
            elif msg_type == MSG_SYSTEM:
                result.system = parsed
            elif msg_type == MSG_OPERATOR_ID:
                result.operator_id = parsed

        offset += 1 + msg_len

    return result
