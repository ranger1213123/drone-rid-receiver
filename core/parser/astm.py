"""
ASTM F3411 / ASD-STAN 4709-002 Open Drone ID 协议

物理层:
  BLE UUID: 0xFFFA
  WiFi OUI: 0xFA0B0C

报文类型: 0x0 Basic ID, 0x1 Location, 0x2 Auth, 0x3 Self-ID,
          0x4 System, 0x5 Operator ID
"""

import struct

from .base import RIDProtocol
from .types import (
    BasicIDMessage, LocationMessage, SelfIDMessage,
    SystemMessage, OperatorIDMessage, ParsedRID,
    MSG_BASIC_ID, MSG_LOCATION, MSG_AUTH, MSG_SELF_ID, MSG_SYSTEM, MSG_OPERATOR_ID,
    LOC_STATUS_TIMESTAMP_VALID,
    parse_basic_id, parse_self_id,
)

BLE_SERVICE_UUID = 0xFFFA
WIFI_OUI = bytes([0x0C, 0x0B, 0xFA])
APP_CODE = 0

VALID_MSG_TYPES = {0x0, 0x1, 0x2, 0x3, 0x4, 0x5}
VALID_ID_TYPES = {0, 1, 2, 3, 4}


def parse_location_astm(data: bytes) -> LocationMessage:
    """ASTM F3411 Location/Vector 消息 (msg_type=0x1)"""
    if len(data) < 2:
        return LocationMessage()

    status = data[0]
    direction_byte = data[1]
    speed_mult = 0.25 if (direction_byte & 0x01) else 1.0

    speed_h = struct.unpack_from('<H', data, 2)[0] * 0.01 * speed_mult
    speed_v_raw = struct.unpack_from('<h', data, 4)[0]
    speed_v = speed_v_raw * 0.01 * speed_mult

    lat = struct.unpack_from('<i', data, 6)[0] / 1e7
    lon = struct.unpack_from('<i', data, 10)[0] / 1e7

    alt_p = struct.unpack_from('<h', data, 14)[0] * 0.5
    alt_g = struct.unpack_from('<h', data, 16)[0] * 0.5
    height_agl = struct.unpack_from('<H', data, 18)[0] * 0.5

    height_type = data[20] & 0x0F
    h_acc = (data[21] >> 4) & 0x0F
    v_acc = data[21] & 0x0F
    baro_acc = (data[22] >> 4) & 0x0F
    spd_acc = data[22] & 0x0F
    ts = struct.unpack_from('<H', data, 23)[0] * 0.1

    return LocationMessage(
        status=status, speed_multiplier=speed_mult,
        speed_horizontal=speed_h, speed_vertical=speed_v,
        latitude=lat, longitude=lon,
        altitude_pressure=alt_p, altitude_geodetic=alt_g,
        height_agl=height_agl, height_type=height_type,
        horizontal_accuracy=h_acc, vertical_accuracy=v_acc,
        baro_accuracy=baro_acc, speed_accuracy=spd_acc,
        timestamp=ts,
    )


def parse_system_astm(data: bytes) -> SystemMessage:
    """ASTM F3411 System 消息 (msg_type=0x4)"""
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
        operator_lat=op_lat, operator_lon=op_lon,
        area_count=area_count, area_radius=area_radius,
        area_ceiling=area_ceiling, area_floor=area_floor,
        category_eu=cat_eu, class_eu=cls_eu,
        operator_alt_geo=op_alt,
    )


def parse_operator_id(data: bytes) -> OperatorIDMessage:
    """ASTM F3411 Operator ID 消息 (msg_type=0x5)"""
    if len(data) < 2:
        return OperatorIDMessage()
    op_type = data[0]
    op_id = data[1:21].split(b'\x00')[0].decode('ascii', errors='replace')
    return OperatorIDMessage(operator_id=op_id, operator_id_type=op_type)


_ASTM_DECODERS = {
    MSG_BASIC_ID: parse_basic_id,
    MSG_LOCATION: parse_location_astm,
    MSG_SELF_ID: parse_self_id,
    MSG_SYSTEM: parse_system_astm,
    MSG_OPERATOR_ID: parse_operator_id,
}

# 消息长度 (不含报头字节)
_MSG_LENGTHS = {
    MSG_BASIC_ID: 22,
    MSG_LOCATION: 25,
    MSG_AUTH: 0,     # skip
    MSG_SELF_ID: 24,
    MSG_SYSTEM: 19,
    MSG_OPERATOR_ID: 21,
}


def _parse_astm_pack(data: bytes, mac_address: str = "", rssi: int = 0) -> ParsedRID:
    """ASTM F3411 消息包解析 — 简单拼接格式

    BLE Service Data:
      Byte 0:    Message Counter (0-7)
      Byte 1:    Reserved | Protocol Version
      Byte 2+:   Messages concatenated

    WiFi Nanobeacon:
      Byte 0-5:  MAC address
      Byte 6:    Message Counter
      Byte 7+:   Messages concatenated
    """
    result = ParsedRID(raw_data=data, mac_address=mac_address, rssi=rssi)

    if len(data) < 2:
        return result

    # WiFi Nanobeacon 检测: 6-byte MAC + counter(0-7)
    if len(data) >= 8 and data[6] <= 7:
        offset = 7
    else:
        offset = 2

    while offset < len(data) - 1:
        header = data[offset]
        msg_type = header & 0x0F
        msg_len = _MSG_LENGTHS.get(msg_type, 0)

        if msg_len == 0:
            break

        payload = data[offset + 1: offset + 1 + msg_len]
        decoder = _ASTM_DECODERS.get(msg_type)

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
            elif msg_type == MSG_OPERATOR_ID:
                result.operator_id = parsed

        offset += 1 + msg_len

    return result


PROTOCOL = RIDProtocol(
    name="astm_f3411",
    ble_service_uuid=BLE_SERVICE_UUID,
    wifi_oui=WIFI_OUI,
    pack_parser=_parse_astm_pack,
    message_decoders=_ASTM_DECODERS,
    valid_msg_types=VALID_MSG_TYPES,
    valid_id_types=VALID_ID_TYPES,
    ble_app_code=APP_CODE,
)
