"""
Tests for SDRTU raw format translation in MqttConsumer.
验证 ESP32 原始 JSON → 内部 report 格式翻译逻辑。
"""

import json
from datetime import datetime
from unittest import TestCase

from app.mqtt_consumer import MqttConsumer


class TestSdrtuTranslation(TestCase):

    def setUp(self):
        self.consumer = MqttConsumer()

    # ═══════ 心跳 ═══════

    def test_heartbeat_returns_none(self):
        """心跳消息 (无 data 字段) 不产生 report"""
        hb = {"devId": "EXD001", "count": 86}
        result = self.consumer._translate_raw_to_report("EXD001", hb)
        self.assertIsNone(result)

    def test_heartbeat_data_is_null_returns_none(self):
        """data 字段为 null 时返回 None"""
        raw = {"devId": "EXD001", "data": None, "count": 100}
        result = self.consumer._translate_raw_to_report("EXD001", raw)
        self.assertIsNone(result)

    def test_heartbeat_data_is_not_dict_returns_none(self):
        """data 字段为非对象值时返回 None"""
        raw = {"devId": "EXD001", "data": "not_a_dict"}
        result = self.consumer._translate_raw_to_report("EXD001", raw)
        self.assertIsNone(result)

    # ═══════ 完整数据 ═══════

    def test_full_drone_data_translates_correctly(self):
        """完整 ESP32 数据 → report 格式"""
        raw = {
            "devId": "EXD001",
            "data": {
                "osid": "1581F8PJC245B0001KRC",
                "RSSI": -72,
                "Op_Lat": 30.61517, "Op_Lon": 104.06742, "Op_Alt": 469,
                "Lat": 0, "Lon": 0, "AltGeo": -1000,
                "Heading": 361, "Speed": 0, "UAType": 2,
                "Status": 0, "UATime": 1234567890,
            }
        }
        result = self.consumer._translate_raw_to_report("EXD001", raw)
        self.assertIsNotNone(result)
        self.assertEqual(result["drone_id"], "1581F8PJC245B0001KRC")
        self.assertEqual(result["latitude"], 30.61517)
        self.assertEqual(result["longitude"], 104.06742)
        self.assertEqual(result["altitude"], 469)
        self.assertEqual(result["device"], "EXD001")
        self.assertIsNone(result["distance_to_line"])
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["rssi"], -72)
        self.assertEqual(result.get("model", ""), "")

    # ═══════ 位置回退逻辑 ═══════

    def test_fallback_to_drone_gps_when_operator_zero(self):
        """operator 坐标为 0 时回退到 drone GPS"""
        raw = {
            "devId": "EXD002",
            "data": {
                "osid": "1581F8PJC245B0001KRC",
                "Op_Lat": 0, "Op_Lon": 0, "Op_Alt": 0,
                "Lat": 30.5, "Lon": 104.1, "AltGeo": 500,
                "Heading": 90, "Speed": 5, "UAType": 1,
                "Status": 0,
            }
        }
        result = self.consumer._translate_raw_to_report("EXD002", raw)
        self.assertEqual(result["latitude"], 30.5)
        self.assertEqual(result["longitude"], 104.1)
        self.assertEqual(result["altitude"], 500)

    def test_fallback_to_alt_geo_when_op_alt_zero(self):
        """Op_Alt=0 且 AltGeo 有效时回退到 AltGeo"""
        raw = {
            "devId": "EXD003",
            "data": {
                "osid": "1581F8PJC245B0001KRC",
                "Op_Lat": 30.5, "Op_Lon": 104.1, "Op_Alt": 0,
                "Lat": 0, "Lon": 0, "AltGeo": 300,
                "Heading": 0, "Speed": 0, "UAType": 0,
                "Status": 0,
            }
        }
        result = self.consumer._translate_raw_to_report("EXD003", raw)
        self.assertEqual(result["altitude"], 300)

    def test_alt_geo_sentinel_ignored(self):
        """AltGeo=-1000 (哨兵值) 时不使用"""
        raw = {
            "devId": "EXD004",
            "data": {
                "osid": "1581F8PJC245B0001KRC",
                "Op_Lat": 30.5, "Op_Lon": 104.1, "Op_Alt": 0,
                "Lat": 0, "Lon": 0, "AltGeo": -1000,
                "Heading": 0, "Speed": 0, "UAType": 0,
                "Status": 0,
            }
        }
        result = self.consumer._translate_raw_to_report("EXD004", raw)
        self.assertEqual(result["altitude"], 0)

    # ═══════ 边界值 ═══════

    def test_heading_over_360_reset_to_zero(self):
        """Heading > 360 时重置为 0"""
        raw = {
            "devId": "EXD005",
            "data": {
                "osid": "1581F8PJC245B0001KRC",
                "Op_Lat": 30.5, "Op_Lon": 104.1, "Op_Alt": 100,
                "Heading": 361, "Speed": 0, "UAType": 0,
                "Status": 0,
            }
        }
        result = self.consumer._translate_raw_to_report("EXD005", raw)
        self.assertEqual(result["heading"], 0)

    def test_empty_osid_returns_none(self):
        """osid 为空时返回 None"""
        raw = {
            "devId": "EXD006",
            "data": {
                "osid": "",
                "Op_Lat": 30.5, "Op_Lon": 104.1, "Op_Alt": 100,
                "Heading": 0, "Speed": 0, "UAType": 0,
            }
        }
        result = self.consumer._translate_raw_to_report("EXD006", raw)
        self.assertIsNone(result)

    def test_none_fields_coerced_to_zero(self):
        """None 值字段转为 0"""
        raw = {
            "devId": "EXD007",
            "data": {
                "osid": "1581F8PJC245B0001KRC",
                "Op_Lat": None, "Op_Lon": None, "Op_Alt": None,
                "Lat": None, "Lon": None, "AltGeo": None,
                "RSSI": None, "Heading": None, "Speed": None, "UAType": None,
                "Status": 0,
            }
        }
        result = self.consumer._translate_raw_to_report("EXD007", raw)
        self.assertIsNotNone(result)
        self.assertEqual(result["latitude"], 0)
        self.assertEqual(result["longitude"], 0)
        self.assertEqual(result["altitude"], 0)
        self.assertEqual(result["rssi"], 0)
        self.assertEqual(result["speed"], 0)

    # ═══════ ESP32 新增字段 ═══════

    def test_status_code_and_height_agl_extracted(self):
        """ESP32 Status 和 Height 字段被正确提取"""
        raw = {
            "devId": "EXD008",
            "data": {
                "osid": "1581F8PJC245B0001KRC",
                "Op_Lat": 30.5, "Op_Lon": 104.1, "Op_Alt": 100,
                "Heading": 0, "Speed": 0, "UAType": 2,
                "Status": 2, "Height": 45.5,
            }
        }
        result = self.consumer._translate_raw_to_report("EXD008", raw)
        self.assertEqual(result["status_code"], 2)
        self.assertEqual(result["height_agl"], 45.5)

    def test_status_code_defaults_to_zero(self):
        """Status 字段缺失时默认为 0"""
        raw = {
            "devId": "EXD009",
            "data": {
                "osid": "1581F8PJC245B0001KRC",
                "Op_Lat": 30.5, "Op_Lon": 104.1, "Op_Alt": 100,
                "Heading": 0, "Speed": 0, "UAType": 0,
            }
        }
        result = self.consumer._translate_raw_to_report("EXD009", raw)
        self.assertEqual(result["status_code"], 0)
        self.assertIsNone(result["height_agl"])

    # ═══════ _buffer_raw 端到端 ═══════

    def test_buffer_raw_heartbeat_updates_device_only(self):
        """_buffer_raw 心跳只更新 device, 不产生 drone 记录"""
        self.consumer._buffer_raw("EXD001", {"devId": "EXD001", "count": 86})
        self.assertIn("EXD001", self.consumer._buffer['devices'])
        self.assertEqual(
            self.consumer._buffer['devices']["EXD001"]["status"],
            "online"
        )
        # 没有 drone 数据
        self.assertEqual(len(self.consumer._buffer['drones']), 0)

    def test_buffer_raw_full_data_produces_drone(self):
        """_buffer_raw 完整数据产生 drone 和 device 记录"""
        raw = {
            "devId": "EXD001",
            "data": {
                "osid": "1581F8PJC245B0001KRC",
                "Op_Lat": 30.61517, "Op_Lon": 104.06742, "Op_Alt": 469,
                "Heading": 0, "Speed": 0, "UAType": 2,
                "Status": 0,
            }
        }
        self.consumer._buffer_raw("EXD001", raw)
        self.assertIn("EXD001", self.consumer._buffer['devices'])
        self.assertEqual(len(self.consumer._buffer['drones']), 1)
        key = ("1581F8PJC245B0001KRC", "EXD001")
        self.assertIn(key, self.consumer._buffer['drones'])
        drone = self.consumer._buffer['drones'][key]
        self.assertEqual(drone["device_name"], "EXD001")
        self.assertEqual(drone["last_lat"], 30.61517)

    # ═══════ BLE Raw hex 解析 (SDRTU 新格式) ═══════

    def _build_ble_hex(self, counter=0, version=2, basic_id="TEST12345",
                       lat=30.5, lon=104.1, alt_geo=500.0, id_type=1, ua_type=2):
        """构造 ASTM F3411 BLE 数据包的 hex 字符串 (用于测试)"""
        import struct
        buf = bytearray()
        # BLE header: counter + version
        buf.append(counter & 0x07)
        buf.append(version & 0x0F)

        # Basic ID message (msg_type=0x0, len=22)
        buf.append(0x00)  # header: msg_type=0
        bm = bytearray(22)
        bm[0] = ((ua_type & 0x0F) << 4) | (id_type & 0x0F)
        bid_bytes = basic_id.encode('ascii')
        bm[2:2 + len(bid_bytes)] = bid_bytes
        buf.extend(bm)

        # Location message (msg_type=0x1, len=25)
        buf.append(0x01)  # header: msg_type=1
        lm = bytearray(25)
        # parse_location_astm reads payload at:
        #   data[6:10] = lat (int32 LE / 1e7)
        #   data[10:14] = lon (int32 LE / 1e7)
        #   data[16:18] = alt_g (uint16 LE / 0.5)
        struct.pack_into('<i', lm, 6, int(lat * 1e7))
        struct.pack_into('<i', lm, 10, int(lon * 1e7))
        struct.pack_into('<H', lm, 16, int(alt_geo / 0.5))
        buf.extend(lm)

        return buf.hex()

    def test_ble_raw_parses_basic_id_and_location(self):
        """BLE hex → 解析出 drone_id + lat/lon/alt"""
        hex_str = self._build_ble_hex(
            basic_id="1581F8PJC245B0001KRC",
            lat=30.61517, lon=104.06742, alt_geo=469.0,
        )
        payload = {
            "dev_id": "EXD001", "raw_hex": hex_str,
            "len": len(bytes.fromhex(hex_str)), "count": 1,
            "type": "ble_raw",
        }
        reports = self.consumer._translate_ble_raw_to_reports("EXD001", payload)
        self.assertGreaterEqual(len(reports), 1,
            f"Should find at least 1 drone, got {len(reports)}")
        r = reports[0]
        self.assertEqual(r["drone_id"], "1581F8PJC245B0001KRC")
        self.assertAlmostEqual(r["latitude"], 30.61517, places=5)
        self.assertAlmostEqual(r["longitude"], 104.06742, places=5)
        self.assertAlmostEqual(r["altitude"], 469.0, delta=1.0)

    def test_ble_raw_skips_duplicate_drone_ids(self):
        """同一 drone_id 在同一个 hex 块中只取首次出现的位置"""
        # 两个相同 drone 的包拼接
        hex1 = self._build_ble_hex(basic_id="DRONE01", lat=30.0, lon=104.0, alt_geo=100)
        hex2 = self._build_ble_hex(basic_id="DRONE01", lat=31.0, lon=105.0, alt_geo=200)
        payload = {
            "dev_id": "EXD001", "raw_hex": hex1 + hex2,
            "len": 0, "count": 1, "type": "ble_raw",
        }
        reports = self.consumer._translate_ble_raw_to_reports("EXD001", payload)
        self.assertEqual(len(reports), 1,
            f"Should deduplicate, got {len(reports)}")
        self.assertEqual(reports[0]["latitude"], 30.0)

    def test_ble_raw_empty_hex(self):
        """空 hex 返回空列表"""
        reports = self.consumer._translate_ble_raw_to_reports(
            "EXD001", {"dev_id": "EXD001", "raw_hex": "", "len": 0, "count": 1, "type": "ble_raw"}
        )
        self.assertEqual(reports, [])

    def test_ble_raw_invalid_hex_returns_empty(self):
        """非法 hex 返回空列表 (不抛异常)"""
        reports = self.consumer._translate_ble_raw_to_reports(
            "EXD001", {"dev_id": "EXD001", "raw_hex": "ZZZZ", "len": 2, "count": 1, "type": "ble_raw"}
        )
        self.assertEqual(reports, [])

    def test_ble_raw_version_filter(self):
        """版本号 > 3 的偏移被跳过"""
        # 构造: 在有效包前面插入 2 字节随机数据 (版本=0xF > 3)
        valid_hex = self._build_ble_hex(basic_id="DRONE01", lat=30.0, lon=104.0, alt_geo=100)
        junk = "ffff"  # byte0=0xFF, byte1=0xFF (version=0xF > 3, 被跳过)
        payload = {
            "dev_id": "EXD001", "raw_hex": junk + valid_hex,
            "len": 0, "count": 1, "type": "ble_raw",
        }
        reports = self.consumer._translate_ble_raw_to_reports("EXD001", payload)
        self.assertGreaterEqual(len(reports), 1,
            f"Should find drone after skipping junk, got {len(reports)}")

    def test_buffer_raw_ble_dispatches_to_parser(self):
        """_buffer_raw 识别 type=ble_raw 并调用 BLE 解析器"""
        hex_str = self._build_ble_hex(
            basic_id="1581F8PJC245B0001KRC",
            lat=30.61517, lon=104.06742, alt_geo=469.0,
        )
        payload = {
            "dev_id": "EXD001", "raw_hex": hex_str,
            "len": len(bytes.fromhex(hex_str)), "count": 5,
            "type": "ble_raw",
        }
        # 不挂载电力线 → 直接进入 buffer
        self.consumer._buffer_raw("EXD001", payload)
        self.assertIn("EXD001", self.consumer._buffer['devices'])
        key = ("1581F8PJC245B0001KRC", "EXD001")
        self.assertIn(key, self.consumer._buffer['drones'],
            f"Drone should be in buffer, keys: {list(self.consumer._buffer['drones'].keys())}")
