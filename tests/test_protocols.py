"""
GB 46750-2025 协议解析测试
"""

import sys
import os
import unittest
import struct

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.parser import (
    parse_rid_pack, ParsedRID, BasicIDMessage, LocationMessage,
    set_active_protocol, get_active_protocol, RIDProtocol,
    MSG_BASIC_ID, MSG_LOCATION, MSG_SELF_ID, MSG_SYSTEM,
    ID_TYPE_SERIAL, ID_TYPE_CAA, ID_TYPE_UTM, ID_TYPE_SESSION,
    UA_TYPE_HELICOPTER,
)
from core.parser.gb46750 import _crc16_ibm, parse_location_gb, parse_system_gb
from core.parser.astm import parse_location_astm


class TestProtocolManagement(unittest.TestCase):
    """协议管理测试"""

    def test_default_is_gb46750(self):
        p = get_active_protocol()
        self.assertEqual(p.name, "gb46750")
        self.assertEqual(p.ble_service_uuid, 0xFFFF)

    def test_switch_to_astm(self):
        set_active_protocol("astm_f3411")
        p = get_active_protocol()
        self.assertEqual(p.name, "astm_f3411")
        self.assertEqual(p.ble_service_uuid, 0xFFFA)
        set_active_protocol("gb46750")

    def test_invalid_protocol_raises(self):
        with self.assertRaises(ValueError):
            set_active_protocol("nonexistent")

    def test_wifi_oui_differs(self):
        set_active_protocol("gb46750")
        gb_oui = get_active_protocol().wifi_oui
        set_active_protocol("astm_f3411")
        astm_oui = get_active_protocol().wifi_oui
        set_active_protocol("gb46750")
        self.assertNotEqual(gb_oui, astm_oui)
        self.assertEqual(gb_oui, bytes([0xBC, 0xFB, 0xFA]))

    def tearDown(self):
        set_active_protocol("gb46750")


class TestGB46750Parser(unittest.TestCase):
    """GB 46750-2025 协议解析测试"""

    def setUp(self):
        set_active_protocol("gb46750")

    def _build_0x0f(self, messages: list) -> bytes:
        """构建 GB 46750 0x0F Message Pack"""
        inner = bytes([0x0F, 25, len(messages)])
        for msg_type, payload in messages:
            assert len(payload) == 24, f"payload must be 24 bytes, got {len(payload)}"
            inner += bytes([msg_type & 0x0F]) + payload
        crc = struct.pack('<H', _crc16_ibm(inner))
        return inner + crc

    def _make_basic(self, uas_id: str, id_type=ID_TYPE_SERIAL,
                    ua_type=UA_TYPE_HELICOPTER) -> bytes:
        hdr = (ua_type << 4) | id_type
        # 24 bytes: 1(header_field) + 20(ID) + 3(reserved)
        payload = bytes([hdr, 0]) + uas_id.encode('ascii').ljust(20, b'\x00')[:20] + b'\x00'
        return payload[:24].ljust(24, b'\x00')

    def _make_location(self, lat=30.29, lon=120.155, alt_g=152.0,
                       baro=150.0, agl=100.0, speed_m=0.75) -> bytes:
        """构建 GB 46750 Location 24字节 payload"""
        flags = 0x01  # speed_mult=1 (0.75)
        track = 45
        gspeed = 10
        vspeed = 2
        lat_raw = int(lat * 1e7)
        lon_raw = int(lon * 1e7)
        baro_raw = int(baro / 0.5)
        alt_g_raw = int(alt_g / 0.5)
        agl_raw = int(agl / 0.5)

        data = bytes([flags, track, gspeed, vspeed])
        data += struct.pack('<i', lat_raw)
        data += struct.pack('<i', lon_raw)
        data += struct.pack('<h', baro_raw)
        data += struct.pack('<h', alt_g_raw)
        data += struct.pack('<H', agl_raw)
        data += bytes([0x33, 0x44, 0, 0, 0, 0])  # acc + ts + reserved
        return data[:24].ljust(24, b'\x00')

    def test_basic_id_parse(self):
        payload = self._make_basic("DRONE-GB-001")
        pack = self._build_0x0f([(MSG_BASIC_ID, payload)])
        result = parse_rid_pack(pack)
        self.assertEqual(result.drone_id, "DRONE-GB-001")

    def test_location_parse(self):
        basic = self._make_basic("GB-LOC-TEST")
        loc = self._make_location(lat=30.29, lon=120.155, alt_g=152.0)
        pack = self._build_0x0f([(MSG_BASIC_ID, basic), (MSG_LOCATION, loc)])
        result = parse_rid_pack(pack)
        self.assertIsNotNone(result.location)
        self.assertAlmostEqual(result.location.latitude, 30.29, places=4)
        self.assertAlmostEqual(result.location.longitude, 120.155, places=4)
        self.assertAlmostEqual(result.location.altitude_geodetic, 152.0, places=1)
        self.assertEqual(result.location.track_angle, 45)
        self.assertGreater(result.location.speed_horizontal, 0)

    def test_0x0f_single_message(self):
        basic = self._make_basic("SINGLE")
        pack = self._build_0x0f([(MSG_BASIC_ID, basic)])
        result = parse_rid_pack(pack)
        self.assertEqual(result.drone_id, "SINGLE")

    def test_0x0f_multi_message(self):
        basic = self._make_basic("MULTI")
        loc = self._make_location()
        self_id_text = b"PATROL\x00".ljust(24, b'\x00')
        pack = self._build_0x0f([
            (MSG_BASIC_ID, basic),
            (MSG_LOCATION, loc),
            (MSG_SELF_ID, self_id_text),
        ])
        result = parse_rid_pack(pack)
        self.assertIsNotNone(result.basic_id)
        self.assertIsNotNone(result.location)
        self.assertIsNotNone(result.self_id)

    def test_invalid_crc_still_parses(self):
        """CRC 不匹配时宽容解析"""
        basic = self._make_basic("CRC-FAIL")
        pack = self._build_0x0f([(MSG_BASIC_ID, basic)])
        corrupted = pack[:-2] + b'\x00\x00'
        result = parse_rid_pack(corrupted)
        self.assertEqual(result.drone_id, "CRC-FAIL")

    def test_reserved_types_skipped(self):
        """0x02 (Auth) 和 0x05 (Operator ID) 被跳过"""
        basic = self._make_basic("SKIP-RESV")
        pack = self._build_0x0f([
            (MSG_BASIC_ID, basic),
            (0x02, b'\x00' * 24),  # Auth - should be skipped
            (0x05, b'\x00' * 24),  # Operator ID - should be skipped
        ])
        result = parse_rid_pack(pack)
        self.assertEqual(result.drone_id, "SKIP-RESV")
        self.assertIsNone(result.operator_id)

    def test_id_type_validation(self):
        p = get_active_protocol()
        self.assertTrue(p.is_id_type_valid(ID_TYPE_SERIAL))
        self.assertTrue(p.is_id_type_valid(ID_TYPE_CAA))
        self.assertTrue(p.is_id_type_valid(ID_TYPE_UTM))
        self.assertFalse(p.is_id_type_valid(ID_TYPE_SESSION))

    def test_msg_type_validation(self):
        p = get_active_protocol()
        self.assertTrue(p.is_msg_type_valid(MSG_BASIC_ID))
        self.assertTrue(p.is_msg_type_valid(MSG_LOCATION))
        self.assertFalse(p.is_msg_type_valid(0x02))  # Auth reserved
        self.assertFalse(p.is_msg_type_valid(0x05))  # Operator ID reserved

    def tearDown(self):
        set_active_protocol("gb46750")


class TestASTMProtocol(unittest.TestCase):
    """ASTM F3411 协议解析测试"""

    def setUp(self):
        set_active_protocol("astm_f3411")

    def test_location_with_helicopter(self):
        loc_data = bytes([0x10, 0x00])
        loc_data += struct.pack('<H', 500)
        loc_data += struct.pack('<h', 100)
        loc_data += struct.pack('<i', 302900000)
        loc_data += struct.pack('<i', 1201550000)
        loc_data += struct.pack('<h', 300)
        loc_data += struct.pack('<h', 304)
        loc_data += struct.pack('<H', 200)
        loc_data += bytes([0, 0, 0, 0, 0])
        result = parse_location_astm(loc_data)
        self.assertAlmostEqual(result.latitude, 30.29, places=4)
        self.assertAlmostEqual(result.longitude, 120.155, places=4)
        self.assertAlmostEqual(result.altitude_geodetic, 152.0, places=1)
        self.assertEqual(result.speed_multiplier, 1.0)

    def test_speed_multiplier_025(self):
        """ASTM speed multiplier = 0.25 when direction & 0x01"""
        loc_data = bytes([0x10, 0x01])  # bit0=1 → ×0.25
        loc_data += struct.pack('<H', 500)
        loc_data += struct.pack('<h', 100)
        loc_data += struct.pack('<i', 0)
        loc_data += struct.pack('<i', 0)
        loc_data += struct.pack('<h', 0)
        loc_data += struct.pack('<h', 0)
        loc_data += struct.pack('<H', 0)
        loc_data += bytes([0, 0, 0, 0, 0])
        result = parse_location_astm(loc_data)
        self.assertEqual(result.speed_multiplier, 0.25)

    def test_astm_allows_operator_id(self):
        p = get_active_protocol()
        self.assertTrue(p.is_msg_type_valid(0x05))

    def test_astm_allows_session_id(self):
        p = get_active_protocol()
        self.assertTrue(p.is_id_type_valid(ID_TYPE_SESSION))

    def tearDown(self):
        set_active_protocol("gb46750")


class TestGBLocationDecode(unittest.TestCase):
    """GB 46750 Location 解码器细节"""

    def test_speed_mult_075(self):
        """flags bit0=1 → speed_mult = 0.75"""
        data = bytes([0x01, 0, 20, 0])  # mult=0.75, gspeed=20
        data += struct.pack('<i', 0) + struct.pack('<i', 0)
        data += struct.pack('<h', 0) + struct.pack('<h', 0)
        data += struct.pack('<H', 0) + bytes([0, 0, 0, 0, 0, 0])
        result = parse_location_gb(data[:24].ljust(24, b'\x00'))
        self.assertEqual(result.speed_multiplier, 0.75)
        self.assertAlmostEqual(result.speed_horizontal, 15.0)  # 20 * 0.75

    def test_speed_mult_025(self):
        """flags bit0=0 → speed_mult = 0.25"""
        data = bytes([0x00, 0, 20, 0])
        data += struct.pack('<i', 0) + struct.pack('<i', 0)
        data += struct.pack('<h', 0) + struct.pack('<h', 0)
        data += struct.pack('<H', 0) + bytes([0, 0, 0, 0, 0, 0])
        result = parse_location_gb(data[:24].ljust(24, b'\x00'))
        self.assertEqual(result.speed_multiplier, 0.25)
        self.assertAlmostEqual(result.speed_horizontal, 5.0)  # 20 * 0.25

    def test_track_angle_east(self):
        """TrackEW=1 → angle += 180"""
        data = bytes([0x02, 45, 0, 0])  # bit1=1
        data += struct.pack('<i', 0) + struct.pack('<i', 0)
        data += struct.pack('<h', 0) + struct.pack('<h', 0)
        data += struct.pack('<H', 0) + bytes([0, 0, 0, 0, 0, 0])
        result = parse_location_gb(data[:24].ljust(24, b'\x00'))
        self.assertEqual(result.track_angle, 225)  # 45 + 180

    def test_track_angle_west(self):
        """TrackEW=0 → angle unchanged"""
        data = bytes([0x00, 45, 0, 0])
        data += struct.pack('<i', 0) + struct.pack('<i', 0)
        data += struct.pack('<h', 0) + struct.pack('<h', 0)
        data += struct.pack('<H', 0) + bytes([0, 0, 0, 0, 0, 0])
        result = parse_location_gb(data[:24].ljust(24, b'\x00'))
        self.assertEqual(result.track_angle, 45)

    def test_wgs84_coords(self):
        data = bytes([0x00, 0, 0, 0])
        data += struct.pack('<i', 302900000)  # 30.29
        data += struct.pack('<i', 1201550000)  # 120.155
        data += struct.pack('<h', int(150.0 / 0.5))  # baro alt
        data += struct.pack('<h', int(152.0 / 0.5))  # geo alt
        data += struct.pack('<H', int(100.0 / 0.5))
        data += bytes([0, 0, 0, 0, 0, 0])
        result = parse_location_gb(data[:24].ljust(24, b'\x00'))
        self.assertAlmostEqual(result.latitude, 30.29, places=4)
        self.assertAlmostEqual(result.longitude, 120.155, places=4)
        self.assertAlmostEqual(result.altitude_geodetic, 152.0, places=1)
        self.assertAlmostEqual(result.altitude_pressure, 150.0, places=1)
        self.assertAlmostEqual(result.height_agl, 100.0, places=1)


class TestGBSystemDecode(unittest.TestCase):
    """GB 46750 System 消息解码测试"""

    def test_wgs84_coordinate_system(self):
        """flags bits 5-4 = 0 → WGS-84"""
        data = bytes([0x00])  # coord_sys=0, op_pos_type=0
        data += struct.pack('<i', 0) + struct.pack('<i', 0)  # no op pos
        data += struct.pack('<H', 0)  # area_count
        data += bytes([0])  # area_radius
        data += struct.pack('<h', 0) + struct.pack('<h', 0)  # ceiling, floor
        data += bytes([0x00])  # category/class
        data += struct.pack('<h', 0)  # op_alt
        data += struct.pack('<I', 0)  # timestamp
        data += b'\x00'  # reserved
        result = parse_system_gb(data[:24].ljust(24, b'\x00'))
        self.assertEqual(result.coordinate_system, 0)

    def test_cgcs2000_coordinate_system(self):
        """flags bits 5-4 = 1 → CGCS2000"""
        data = bytes([0x10])  # coord_sys=1
        data += b'\x00' * 23
        result = parse_system_gb(data[:24].ljust(24, b'\x00'))
        self.assertEqual(result.coordinate_system, 1)

    def test_operator_position_present(self):
        """op_pos_type != 0 → operator lat/lon parsed"""
        data = bytes([0x01])  # op_pos_type=1 (dynamic)
        data += struct.pack('<i', 302900000)  # op_lat
        data += struct.pack('<i', 1201550000)  # op_lon
        data += struct.pack('<H', 3)  # area_count
        data += bytes([10])  # area_radius
        data += struct.pack('<h', int(500 / 0.5))  # ceiling
        data += struct.pack('<h', 0)  # floor
        data += bytes([0x00])
        data += struct.pack('<h', int(100 / 0.5))  # op_alt
        data += struct.pack('<I', 100000)  # ts
        data += b'\x00'
        result = parse_system_gb(data[:24].ljust(24, b'\x00'))
        self.assertAlmostEqual(result.operator_lat, 30.29, places=4)
        self.assertAlmostEqual(result.operator_lon, 120.155, places=4)
        self.assertEqual(result.area_count, 3)
        self.assertAlmostEqual(result.operator_alt_geo, 100.0, places=1)

    def test_timestamp_unix(self):
        data = bytes([0x00])
        data += b'\x00' * 18
        ts_val = 86400 * 365  # 1 year in seconds
        data += struct.pack('<I', ts_val)
        data += b'\x00'
        result = parse_system_gb(data[:24].ljust(24, b'\x00'))
        self.assertEqual(result.timestamp_unix, ts_val)


if __name__ == '__main__':
    unittest.main()
