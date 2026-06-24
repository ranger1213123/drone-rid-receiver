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
        self.assertEqual(result["ua_type"], 2)

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
