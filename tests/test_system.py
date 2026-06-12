"""
RID 系统集成测试 - 使用模拟数据进行端到端测试

用法:
  python tests/test_system.py
"""

import sys
import os
import unittest
import tempfile
import struct
import math

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.parser import (
    parse_rid_pack, parse_basic_id, parse_location,
    BasicIDMessage, LocationMessage, ParsedRID, UA_TYPE_NAMES,
    MSG_BASIC_ID, MSG_LOCATION, ID_TYPE_SERIAL, UA_TYPE_HELICOPTER,
)
from core.powerline import PowerLineManager, PowerLineSegment
from storage.database import Database
from core.alert import AlertSystem
from core.trajectory import TrajectoryRecorder
from core.pipeline import RIDPipeline


class TestRIDParser(unittest.TestCase):
    """RID 消息解析器测试"""

    def test_parse_basic_id(self):
        """测试 Basic ID 消息解析"""
        # 构造 Basic ID 消息数据
        id_type = ID_TYPE_SERIAL  # 0x01
        ua_type = UA_TYPE_HELICOPTER  # 0x02
        header = (ua_type << 4) | id_type  # 0x21

        uas_id = b"DRONE-ABC123\x00\x00\x00\x00\x00\x00\x00\x00"
        data = bytes([header, 0]) + uas_id  # proto_version=0

        result = parse_basic_id(data)
        self.assertEqual(result.id_type, ID_TYPE_SERIAL)
        self.assertEqual(result.ua_type, UA_TYPE_HELICOPTER)
        self.assertEqual(result.uas_id, "DRONE-ABC123")

    def test_parse_location(self):
        """测试 Location/Vector 消息解析"""
        # 构造 Location 消息
        status = 0x10  # timestamp valid
        direction = 0x01  # speed multiplier = 0.25

        # 水平速度 1000 cm/s → 10 m/s, multiplier 0.25 → 2.5 m/s
        speed_h = struct.pack('<H', 1000)
        speed_v = struct.pack('<h', 500)   # 5 m/s with mul 1.0 → 1.25 m/s

        # 纬度 30.2900000 → 302900000
        lat = struct.pack('<i', 302900000)
        lon = struct.pack('<i', 1201550000)

        # 气压高度 150m → 300 (x2)
        alt_p = struct.pack('<h', 300)
        alt_g = struct.pack('<h', 304)  # 152m

        height_agl = struct.pack('<H', 200)  # 100m

        data = bytes([status, direction])
        data += speed_h + speed_v
        data += lat + lon
        data += alt_p + alt_g
        data += height_agl
        data += bytes([0, 0, 0])  # height_type, h/v acc, baro/spd acc
        data += struct.pack('<H', 1234)  # timestamp
        data += bytes([0, 0])  # reserved

        result = parse_location(data)
        self.assertAlmostEqual(result.latitude, 30.29, places=5)
        self.assertAlmostEqual(result.longitude, 120.155, places=5)
        self.assertAlmostEqual(result.altitude_pressure, 150.0, places=1)
        self.assertAlmostEqual(result.altitude_geodetic, 152.0, places=1)
        self.assertAlmostEqual(result.height_agl, 100.0, places=1)
        self.assertAlmostEqual(result.speed_horizontal, 2.5, places=2)
        self.assertAlmostEqual(result.speed_vertical, 1.25, places=2)

    def test_parse_rid_pack_ble(self):
        """测试完整 BLE RID 消息包解析"""
        # 构造包含 Basic ID + Location 的消息包

        # Basic ID
        id_type = ID_TYPE_SERIAL
        ua_type = UA_TYPE_HELICOPTER
        basic_header = (ua_type << 4) | id_type
        msg_basic = bytes([MSG_BASIC_ID, basic_header, 0])
        msg_basic += b"DRONE-X0001\x00\x00\x00\x00\x00\x00\x00\x00\x00"

        # Location
        loc_data = bytes([0x10, 0x00])  # status, direction
        loc_data += struct.pack('<H', 500)   # speed_h cm/s
        loc_data += struct.pack('<h', 100)   # speed_v
        loc_data += struct.pack('<i', 302900000)  # lat
        loc_data += struct.pack('<i', 1201550000)  # lon
        loc_data += struct.pack('<h', 300)   # alt_p
        loc_data += struct.pack('<h', 304)   # alt_g
        loc_data += struct.pack('<H', 200)   # height_agl
        loc_data += bytes([0, 0, 0, 0, 0, 0])  # acc fields + ts + reserved
        msg_loc = bytes([MSG_LOCATION]) + loc_data

        # BLE message pack
        pack = bytes([0, 0]) + msg_basic + msg_loc  # counter=0, version=0

        result = parse_rid_pack(pack, mac_address="AA:BB:CC:DD:EE:FF", rssi=-45)

        self.assertIsNotNone(result.basic_id)
        self.assertEqual(result.basic_id.uas_id, "DRONE-X0001")
        self.assertIsNotNone(result.location)
        self.assertAlmostEqual(result.location.latitude, 30.29, places=4)
        self.assertEqual(result.mac_address, "AA:BB:CC:DD:EE:FF")
        self.assertEqual(result.rssi, -45)
        self.assertTrue(result.has_location)

    def test_ua_type_names(self):
        """测试无人机类型名称映射"""
        self.assertEqual(UA_TYPE_NAMES[2], "直升机/多旋翼")
        self.assertEqual(UA_TYPE_NAMES[1], "固定翼")


class TestPowerLineManager(unittest.TestCase):
    """电力线管理器测试"""

    def setUp(self):
        self.manager = PowerLineManager()

    def test_load_from_list(self):
        """测试从列表加载电力线"""
        lines = [
            {"name": "线A", "lat1": 30.0, "lon1": 120.0, "alt1": 100,
             "lat2": 30.01, "lon2": 120.01, "alt2": 110, "id": 1},
        ]
        count = self.manager.load_from_list(lines)
        self.assertEqual(count, 1)
        self.assertEqual(len(self.manager.lines), 1)

    def test_find_nearest_line_directly_above(self):
        """测试无人机在电力线正上方"""
        self.manager.lines = [
            PowerLineSegment("线A", 30.0, 120.0, 100.0,
                             30.01, 120.01, 110.0, line_id=1),
        ]
        # 无人机在电力线中点正上方 50m
        nearest, dist = self.manager.find_nearest_line(30.005, 120.005, 155.0)
        self.assertIsNotNone(nearest)
        # 电力线中点高度 ≈ 105m, 无人机 155m, 距离 ≈ 50m
        self.assertAlmostEqual(dist, 50.0, delta=1.0)

    def test_find_nearest_line_below(self):
        """测试无人机在电力线下方"""
        self.manager.lines = [
            PowerLineSegment("线B", 30.0, 120.0, 80.0,
                             30.0, 120.02, 80.0, line_id=1),
        ]
        # 无人机在电力线下方 30m
        nearest, dist = self.manager.find_nearest_line(30.0, 120.01, 50.0)
        self.assertIsNotNone(nearest)
        self.assertAlmostEqual(dist, 30.0, delta=1.0)

    def test_find_all_within(self):
        """测试查找范围内所有电力线"""
        self.manager.lines = [
            PowerLineSegment("线A", 30.0, 120.0, 100.0,
                             30.0, 120.01, 100.0, line_id=1),
            PowerLineSegment("线B", 30.0, 120.02, 60.0,
                             30.0, 120.03, 60.0, line_id=2),
        ]
        # 无人机在 120m 高度
        results = self.manager.find_all_within(30.0, 120.005, 120.0, 50.0)
        # 线A: |120-100|=20 ≤50, 线B: |120-60|=60 >50
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0].name, "线A")


class TestDatabase(unittest.TestCase):
    """数据库测试"""

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db = Database(self.tmpfile.name)

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmpfile.name)

    def test_upsert_drone(self):
        """测试无人机插入更新"""
        self.db.upsert_drone("DRONE-1", 30.0, 120.0, 100.0)
        drones = self.db.get_active_drones()
        self.assertEqual(len(drones), 1)
        self.assertEqual(drones[0]["id"], "DRONE-1")
        self.assertEqual(drones[0]["last_lat"], 30.0)

        # 更新
        self.db.upsert_drone("DRONE-1", 31.0, 121.0, 120.0)
        drones = self.db.get_active_drones()
        self.assertEqual(len(drones), 1)
        self.assertAlmostEqual(drones[0]["last_lat"], 31.0)

    def test_trajectory_recording(self):
        """测试轨迹记录"""
        self.db.upsert_drone("DRONE-1", 30.0, 120.0, 100.0)
        self.db.load_power_lines([{
            "name": "线1", "lat1": 30.0, "lon1": 120.0, "alt1": 100.0,
            "lat2": 30.01, "lon2": 120.01, "alt2": 100.0,
        }])
        self.db.add_trajectory_point("DRONE-1", 30.0, 120.0, 100.0, 50.0, 1)
        self.db.add_trajectory_point("DRONE-1", 30.001, 120.001, 95.0, 45.0, 1)

        traj = self.db.get_trajectory("DRONE-1")
        self.assertEqual(len(traj), 2)
        self.assertAlmostEqual(traj[0]["distance_to_line"], 45.0)  # 最新的在前

    def test_alert_recording(self):
        """测试告警记录"""
        self.db.upsert_drone("DRONE-1", 30.0, 120.0, 100.0)
        self.db.load_power_lines([{
            "name": "线1", "lat1": 30.0, "lon1": 120.0, "alt1": 100.0,
            "lat2": 30.01, "lon2": 120.01, "alt2": 100.0,
        }])
        self.db.add_alert("DRONE-1", "warning", 150.0, 1, "测试告警")
        alerts = self.db.get_last_alert("DRONE-1", "warning")
        self.assertIsNotNone(alerts)
        self.assertEqual(alerts["level"], "warning")


class TestAlertSystem(unittest.TestCase):
    """告警系统测试"""

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db = Database(self.tmpfile.name)
        self.thresholds = {"warning": 200, "severe": 100, "critical": 50}
        self.alert = AlertSystem(
            db=self.db,
            thresholds=self.thresholds,
        )

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmpfile.name)

    def _seed_alert_db(self):
        """预填充父表数据以满足外键约束"""
        self.db.upsert_drone("DRONE-T1", 30.0, 120.0, 200.0)
        self.db.load_power_lines([{
            "name": "高压线A", "lat1": 30.0, "lon1": 120.0, "alt1": 100.0,
            "lat2": 30.01, "lon2": 120.01, "alt2": 100.0,
        }, {
            "name": "高压线B", "lat1": 30.01, "lon1": 120.01, "alt1": 80.0,
            "lat2": 30.02, "lon2": 120.02, "alt2": 80.0,
        }])

    def test_get_level_warning(self):
        """测试告警级别判断 - warning"""
        self.assertEqual(self.alert.get_level(200), "warning")
        self.assertEqual(self.alert.get_level(150), "warning")
        self.assertEqual(self.alert.get_level(101), "warning")

    def test_get_level_severe(self):
        """测试告警级别判断 - severe"""
        self.assertEqual(self.alert.get_level(100), "severe")
        self.assertEqual(self.alert.get_level(75), "severe")
        self.assertEqual(self.alert.get_level(51), "severe")

    def test_get_level_critical(self):
        """测试告警级别判断 - critical"""
        self.assertEqual(self.alert.get_level(50), "critical")
        self.assertEqual(self.alert.get_level(10), "critical")

    def test_get_level_none(self):
        """测试告警级别判断 - 无需告警"""
        self.assertIsNone(self.alert.get_level(201))
        self.assertIsNone(self.alert.get_level(1000))

    def test_process_warning(self):
        """测试处理 warning 级别告警"""
        self._seed_alert_db()
        level = self.alert.process(
            "DRONE-T1", 150.0, "高压线A", 1,
            drone_alt=200.0, drone_lat=30.0, drone_lon=120.0
        )
        self.assertEqual(level, "warning")
        self.assertEqual(self.alert._drone_level.get("DRONE-T1"), "warning")

    def test_process_critical(self):
        """测试 critical 级别告警"""
        self._seed_alert_db()
        level = self.alert.process(
            "DRONE-T1", 30.0, "高压线B", 2,
            drone_alt=80.0, drone_lat=30.0, drone_lon=120.0
        )
        self.assertEqual(level, "critical")

    def test_dedup_same_level(self):
        """测试同级别去重"""
        self._seed_alert_db()
        # 第一次触发
        level1 = self.alert.process(
            "DRONE-T1", 150.0, "线A", 1, 200.0, 30.0, 120.0
        )
        self.assertEqual(level1, "warning")

        # 立即再触发应被去重
        level2 = self.alert.process(
            "DRONE-T1", 140.0, "线A", 1, 200.0, 30.0, 120.0
        )
        self.assertIsNone(level2)


class TestTrajectoryRecorder(unittest.TestCase):
    """轨迹记录器测试"""

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db = Database(self.tmpfile.name)
        self.recorder = TrajectoryRecorder(
            db=self.db,
            min_interval=0.5,
            max_points_per_drone=100,
        )

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmpfile.name)

    def _seed_traj_db(self):
        """预填充父表数据以满足外键约束"""
        self.db.upsert_drone("DRONE-1", 30.0, 120.0, 100.0)
        self.db.load_power_lines([{
            "name": "线1", "lat1": 30.0, "lon1": 120.0, "alt1": 100.0,
            "lat2": 30.01, "lon2": 120.01, "alt2": 100.0,
        }])

    def test_record_and_retrieve(self):
        """测试记录和检索轨迹"""
        self._seed_traj_db()
        recorded = self.recorder.record("DRONE-1", 30.0, 120.0, 100.0, 50.0, 1)
        self.assertTrue(recorded)

        traj = self.db.get_trajectory("DRONE-1")
        self.assertEqual(len(traj), 1)
        self.assertAlmostEqual(traj[0]["distance_to_line"], 50.0)

    def test_dedup_interval(self):
        """测试去重间隔"""
        self._seed_traj_db()
        ok1 = self.recorder.record("DRONE-1", 30.0, 120.0, 100.0, 50.0, 1)
        self.assertTrue(ok1)

        # 立即再记录应被去重
        ok2 = self.recorder.record("DRONE-1", 30.0, 120.0, 100.0, 50.0, 1)
        self.assertFalse(ok2)

        # 应该只有 1 条记录
        traj = self.db.get_trajectory("DRONE-1")
        self.assertEqual(len(traj), 1)


class TestRIDPipeline(unittest.TestCase):
    """数据处理管道测试"""

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db = Database(self.tmpfile.name)
        self.pl_manager = PowerLineManager()
        self.pl_manager.lines = [
            PowerLineSegment("测试线", 30.0, 120.0, 100.0,
                             30.01, 120.01, 100.0, line_id=1),
        ]
        self.db.load_power_lines([{
            "name": "测试线", "lat1": 30.0, "lon1": 120.0, "alt1": 100.0,
            "lat2": 30.01, "lon2": 120.01, "alt2": 100.0,
        }])
        thresholds = {"warning": 200, "severe": 100, "critical": 50}
        self.alert = AlertSystem(
            db=self.db,
            thresholds=thresholds,
        )
        self.trajectory = TrajectoryRecorder(
            db=self.db, min_interval=0.1, max_points_per_drone=100,
        )
        self.pipeline = RIDPipeline(
            db=self.db, pl_manager=self.pl_manager,
            alert_system=self.alert, trajectory_recorder=self.trajectory,
            thresholds=thresholds,
        )

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmpfile.name)

    def _make_parsed(self, drone_id="DRONE-1", lat=30.005, lon=120.005, alt=150.0):
        return ParsedRID(
            basic_id=BasicIDMessage(id_type=1, ua_type=2, uas_id=drone_id),
            location=LocationMessage(latitude=lat, longitude=lon, altitude_geodetic=alt),
        )

    def test_process_valid_drone(self):
        result = self.pipeline.process(self._make_parsed(alt=160.0))
        self.assertIsNotNone(result)
        self.assertEqual(result.drone_id, "DRONE-1")
        self.assertEqual(result.status, "severe")  # |160-100|=60 -> severe

    def test_process_critical(self):
        result = self.pipeline.process(self._make_parsed(alt=101.0))
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "critical")  # |101-100|=1 -> critical

    def test_process_active(self):
        result = self.pipeline.process(self._make_parsed(alt=350.0))
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "active")  # |350-100|=250 -> active

    def test_process_no_location(self):
        parsed = ParsedRID(basic_id=BasicIDMessage(uas_id="X"))
        result = self.pipeline.process(parsed)
        self.assertIsNone(result)

    def test_process_no_power_lines(self):
        self.pl_manager.lines = []
        result = self.pipeline.process(self._make_parsed())
        self.assertIsNone(result)

    def test_process_trajectory_recorded(self):
        self.pipeline.process(self._make_parsed(alt=120.0))
        points = self.db.get_trajectory("DRONE-1")
        self.assertEqual(len(points), 1)
        self.assertAlmostEqual(points[0]["distance_to_line"], 20.0)

    def test_process_trajectory_stopped(self):
        # First, get within warning range to start tracking
        self.pipeline.process(self._make_parsed(alt=120.0))
        # Then go far away
        result = self.pipeline.process(self._make_parsed(alt=500.0))
        self.assertEqual(result.status, "active")
        self.assertNotIn("DRONE-1", self.trajectory._last_record_time)


if __name__ == "__main__":
    unittest.main(verbosity=2)
