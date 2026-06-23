"""
GB 46750-2025 《民用无人驾驶航空器系统运行识别规范》

物理层:
  BLE UUID: 0xFFFF, App Code: 0x0D
  WiFi OUI: 0xFA0BBC, Vendor Type: 0x0D

报文类型: 0x0 Basic ID (M), 0x1 Location (M), 0x2 预留,
          0x3 运行描述 (O), 0x4 System (M), 0x5 预留,
          0xF Message Pack (M)

与 ASTM F3411 线格式差异:
  - Location (0x1): 字段偏移完全不同, 航迹角替代方向, 速度乘数 0.25/0.75
  - System (0x4): 含坐标系类型 + 32-bit Unix 时间戳
  - 0x0F 结构化打包 (含 CRC16-IBM)
"""

import struct

from .base import RIDProtocol
from .types import (
    BasicIDMessage, LocationMessage, SelfIDMessage,
    SystemMessage, ParsedRID,
    MSG_BASIC_ID, MSG_LOCATION, MSG_SELF_ID, MSG_SYSTEM,
    parse_basic_id, parse_self_id,
)

BLE_SERVICE_UUID = 0xFFFF
WIFI_OUI = bytes([0xFA, 0x0B, 0xBC])  # FA-0B-BC on wire
APP_CODE = 0x0D

# GB 46750: 0x02 (Auth) 和 0x05 (Operator ID) 为预留
VALID_MSG_TYPES = {0x0, 0x1, 0x3, 0x4}
VALID_ID_TYPES = {0, 1, 2, 3}  # 无 Session ID (4)
MAX_MESSAGES = 10
MAX_PACK_SIZE = 250

# BLE Advertisement 相关
BLE_AD_SERVICE_DATA = 0x16
BLE_VENDOR_TYPE = 0x0D


def _crc16_ibm(data: bytes) -> int:
    """CRC16-IBM (poly 0x8005, init 0x0000)"""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = (crc << 1) ^ 0x8005 if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


def parse_location_gb(data: bytes) -> LocationMessage:
    """GB 46750-2025 Location 消息 (msg_type=0x1)

    线格式 (与 ASTM 完全不同):
      Byte 1:  [RunStatus(4b)][Reserved(1b)][HeightType(1b)]
               [TrackEW(1b)][SpeedMult(1b)]
      Byte 2:  TrackAngle (uint8, 0-179, +180 if TrackEW=1)
      Byte 3:  GroundSpeed (uint8, m/s)
      Byte 4:  VerticalSpeed (int8, m/s, +上升)
      Byte 5-8:   Latitude (int32, LE, /1e7)
      Byte 9-12:  Longitude (int32, LE, /1e7)
      Byte 13-14: BaroAlt (int16, LE, ×0.5m)
      Byte 15-16: GeoAlt (int16, LE, ×0.5m)
      Byte 17-18: HeightAGL (uint16, LE, ×0.5m)
      Byte 19:  [GVA(4b)][NACp(4b)]  垂直/水平精度
      Byte 20:  [BaroAcc(4b)][NACv(4b)]
      Byte 21-22: Timestamp (uint16, LE, ×0.1s)
      Byte 23:  TimestampAccuracy (×0.1s 倍数)
      Byte 24:  Reserved
    """
    if len(data) < 24:
        return LocationMessage()

    flags = data[0]

    # 速度乘数: bit0=0 → ×0.25, bit0=1 → ×0.75
    speed_mult = 0.75 if (flags & 0x01) else 0.25

    # 航迹角
    track_ew = (flags >> 1) & 0x01
    track_raw = data[1]
    track_angle = track_raw + (180 if track_ew else 0)

    # 速度 (注: 单位需对照标准原文确认)
    ground_speed = data[2] * speed_mult
    vertical_speed = struct.unpack_from('<b', data, 3)[0] * speed_mult

    lat = struct.unpack_from('<i', data, 4)[0] / 1e7
    lon = struct.unpack_from('<i', data, 8)[0] / 1e7

    alt_p = struct.unpack_from('<h', data, 12)[0] * 0.5
    alt_g = struct.unpack_from('<h', data, 14)[0] * 0.5
    height_agl = struct.unpack_from('<H', data, 16)[0] * 0.5

    height_type = (flags >> 2) & 0x01
    v_acc = (data[18] >> 4) & 0x0F
    h_acc = data[18] & 0x0F
    baro_acc = (data[19] >> 4) & 0x0F
    spd_acc = data[19] & 0x0F
    ts = struct.unpack_from('<H', data, 20)[0] * 0.1

    return LocationMessage(
        status=flags & 0x10,  # timestamp valid flag
        speed_multiplier=speed_mult,
        speed_horizontal=ground_speed,
        speed_vertical=vertical_speed,
        latitude=lat, longitude=lon,
        altitude_pressure=alt_p, altitude_geodetic=alt_g,
        height_agl=height_agl, height_type=height_type,
        horizontal_accuracy=h_acc, vertical_accuracy=v_acc,
        baro_accuracy=baro_acc, speed_accuracy=spd_acc,
        timestamp=ts, track_angle=track_angle,
    )


def parse_system_gb(data: bytes) -> SystemMessage:
    """GB 46750-2025 System 消息 (msg_type=0x4)

    线格式 (与 ASTM 不同):
      Byte 1:  [XX(2b)][CoordSys(2b)][AreaClass(3b)][OpPosType(2b)]
               CoordSys: 0=WGS-84, 1=CGCS2000, 2=GLONASS90
               OpPosType: 0=起飞位, 1=动态位, 2=固定位
      Byte 2-5:   OperatorLat (int32, LE, /1e7)
      Byte 6-9:   OperatorLon (int32, LE, /1e7)
      Byte 10-11: AreaCount (uint16, LE)
      Byte 12:    AreaRadius (×10m)
      Byte 13-14: AreaCeiling (int16, LE, ×0.5m)
      Byte 15-16: AreaFloor (int16, LE, ×0.5m)
      Byte 17:    [UACategory(4b)][UAClass(4b)]
      Byte 18-19: OperatorAlt (int16, LE, ×0.5m)
      Byte 20-23: Timestamp (uint32, LE, Unix seconds since 2019-01-01)
      Byte 24:    Reserved
    """
    if len(data) < 24:
        return SystemMessage()

    flags = data[0]
    coord_sys = (flags >> 4) & 0x03
    # area_class = (flags >> 1) & 0x07
    op_pos_type = flags & 0x03

    # Operator position present if op_pos_type != 0
    has_op = op_pos_type != 0

    op_lat = struct.unpack_from('<i', data, 1)[0] / 1e7 if has_op else 0.0
    op_lon = struct.unpack_from('<i', data, 5)[0] / 1e7 if has_op else 0.0

    area_count = struct.unpack_from('<H', data, 9)[0]
    area_radius = data[11] * 10
    area_ceiling = struct.unpack_from('<h', data, 12)[0] * 0.5
    area_floor = struct.unpack_from('<h', data, 14)[0] * 0.5

    cat = (data[16] >> 4) & 0x0F
    cls = data[16] & 0x0F

    op_alt = struct.unpack_from('<h', data, 17)[0] * 0.5 if has_op else 0.0

    # Unix timestamp from 2019-01-01 00:00:00 UTC
    ts_unix = struct.unpack_from('<I', data, 19)[0] if len(data) >= 23 else 0

    return SystemMessage(
        operator_lat=op_lat, operator_lon=op_lon,
        area_count=area_count, area_radius=area_radius,
        area_ceiling=area_ceiling, area_floor=area_floor,
        category_eu=cat, class_eu=cls,
        operator_alt_geo=op_alt,
        coordinate_system=coord_sys,
        timestamp_unix=ts_unix,
        op_pos_type=op_pos_type,
    )


_GB_DECODERS = {
    MSG_BASIC_ID: parse_basic_id,
    MSG_LOCATION: parse_location_gb,
    MSG_SELF_ID: parse_self_id,
    MSG_SYSTEM: parse_system_gb,
}

_MSG_LENGTHS = {
    # GB 46750 每条报文 25 字节 (1 header + 24 payload)
    MSG_BASIC_ID: 24,
    MSG_LOCATION: 24,
    MSG_SELF_ID: 24,
    MSG_SYSTEM: 24,
}


def _looks_like_mac(b: bytes) -> bool:
    """检查 6 字节是否像有效 MAC (非全零/全FF, unicast, OUI 合理)

    避免将 BLE Service Data 头 (counter=0, version=0, MSG_BASIC_ID=0, ...)
    误判为 WiFi Nanobeacon 的 MAC 前缀。
    """
    if len(b) < 6:
        return False
    if b[0:6] in (b'\x00\x00\x00\x00\x00\x00', b'\xff\xff\xff\xff\xff\xff'):
        return False
    # IEEE 不分配 OUI 00:00:00, 前 3 字节全零必然是解析数据而非 MAC
    if b[0:3] == b'\x00\x00\x00':
        return False
    # bit 0 of byte 0 = multicast flag, must be 0 for unicast device
    if b[0] & 0x01:
        return False
    return True


def _parse_gb46750_pack(data: bytes, mac_address: str = "",
                        rssi: int = 0) -> ParsedRID:
    """GB 46750-2025 消息包解析

    支持多种输入格式 (根据接收器剥离程度):
      1. BLE Service Data: [AppCode 0x0D][Counter][0x0F|Msg...]
      2. WiFi Nanobeacon:  [MAC(6)][Counter(0-7)][0x0F|Msg...]
      3. WiFi Beacon IE:   [0x0F|Msg...]
      4. 简单拼接 (兼容):  [Msg1][Msg2]...
    """
    result = ParsedRID(raw_data=data, mac_address=mac_address, rssi=rssi)

    if len(data) < 2:
        return result

    actual = data

    # 1. WiFi Nanobeacon 外层剥离 (MAC 6 + counter 1)
    #    验证前 6 字节像有效 MAC，避免误裁 WiFi Beacon IE 数据
    if len(actual) >= 10 and _looks_like_mac(actual[0:6]) and actual[6] <= 7:
        actual = actual[7:]

    # 2. BLE AppCode / WiFi Vendor Type 剥离 (0x0D)
    #    BLE: [0x0D][Counter(0-7)][Message Pack]
    #    WiFi Beacon IE: [0x0D][Message Pack] (无 counter)
    if len(actual) >= 3 and actual[0] == APP_CODE:
        if actual[1] <= 7:
            actual = actual[2:]  # BLE: 有 counter
        else:
            actual = actual[1:]  # WiFi: 无 counter, 仅剥离 vendor type

    # 3. 消息包解析
    if len(actual) >= 4 and actual[0] == 0x0F:
        _parse_0x0f_pack(actual, result)
    else:
        _parse_simple_pack(actual, result)

    return result


def _parse_0x0f_pack(data: bytes, result: ParsedRID):
    """解析 0x0F 结构化 Message Pack"""
    if len(data) < 5:
        return

    msg_len = data[1]
    msg_count = data[2]

    if msg_len != 25 or msg_count < 1 or msg_count > MAX_MESSAGES:
        return

    # CRC16 校验 (覆盖 header + count + messages)
    expected_body = 3 + msg_count * 25  # header(3) + N×25
    if len(data) >= expected_body + 2:
        crc_stored = struct.unpack_from('<H', data, expected_body)[0]
        crc_computed = _crc16_ibm(data[:expected_body])
        if crc_stored != crc_computed:
            pass  # 宽容模式: CRC 不匹配仍尝试解析

    offset = 3
    parsed_count = 0
    while offset + 1 < len(data) and parsed_count < msg_count:
        header = data[offset]
        msg_type = header & 0x0F
        msg_payload_len = _MSG_LENGTHS.get(msg_type, 0)

        if msg_payload_len == 0:
            # 未知/预留报文类型, 跳过整条 25 字节报文
            offset += msg_len
            continue

        if offset + 1 + msg_payload_len > len(data):
            break

        payload = data[offset + 1: offset + 1 + msg_payload_len]
        decoder = _GB_DECODERS.get(msg_type)
        if decoder and len(payload) >= 1:
            parsed = decoder(payload)
            if msg_type == MSG_BASIC_ID:
                result.basic_id = parsed
            elif msg_type == MSG_LOCATION:
                result.location = parsed
            elif msg_type == MSG_SELF_ID:
                result.self_id = parsed
            elif msg_type == MSG_SYSTEM:
                result.system = parsed

        offset += 1 + msg_payload_len
        parsed_count += 1


def _parse_simple_pack(data: bytes, result: ParsedRID):
    """简单拼接格式 (非标准兜底)"""
    offset = 0
    while offset < len(data) - 1:
        header = data[offset]
        msg_type = header & 0x0F
        msg_len = _MSG_LENGTHS.get(msg_type, 0)

        if msg_len == 0:
            break

        if offset + 1 + msg_len > len(data):
            break

        payload = data[offset + 1: offset + 1 + msg_len]
        decoder = _GB_DECODERS.get(msg_type)
        if decoder and len(payload) >= 1:
            parsed = decoder(payload)
            if msg_type == MSG_BASIC_ID:
                result.basic_id = parsed
            elif msg_type == MSG_LOCATION:
                result.location = parsed
            elif msg_type == MSG_SELF_ID:
                result.self_id = parsed
            elif msg_type == MSG_SYSTEM:
                result.system = parsed

        offset += 1 + msg_len


PROTOCOL = RIDProtocol(
    name="gb46750",
    ble_service_uuid=BLE_SERVICE_UUID,
    wifi_oui=WIFI_OUI,
    pack_parser=_parse_gb46750_pack,
    message_decoders=_GB_DECODERS,
    valid_msg_types=VALID_MSG_TYPES,
    valid_id_types=VALID_ID_TYPES,
    ble_app_code=APP_CODE,
    max_messages=MAX_MESSAGES,
    max_pack_size=MAX_PACK_SIZE,
)
