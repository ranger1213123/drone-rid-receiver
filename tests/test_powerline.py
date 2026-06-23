"""
电力线悬链线垂度算法测试 — 精度/兼容性/边界

用法:
  python tests/test_powerline.py
"""

import sys
import os
import unittest
import math

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.powerline import (
    PowerLineSegment, PowerLineManager,
    _latlon_to_meters, _catenary_altitude, _distance_to_line,
    _distance_to_line_straight, _distance_to_line_sag,
    _parse_voltage_kv, estimate_sag, _PHI, _GSS_ITERATIONS,
)


class TestVoltageParsing(unittest.TestCase):

    def test_standard_formats(self):
        self.assertEqual(_parse_voltage_kv("10kV"), 10)
        self.assertEqual(_parse_voltage_kv("220kV"), 220)
        self.assertEqual(_parse_voltage_kv("500kV"), 500)

    def test_special_formats(self):
        self.assertEqual(_parse_voltage_kv("±800kV"), 800)
        self.assertEqual(_parse_voltage_kv("1000kV"), 1000)

    def test_invalid(self):
        self.assertEqual(_parse_voltage_kv(""), 0)
        self.assertEqual(_parse_voltage_kv("abc"), 0)


class TestCatenaryAltitude(unittest.TestCase):

    def setUp(self):
        self.line = PowerLineSegment(
            name="test", lat1=30.0, lon1=120.0, alt1=100.0,
            lat2=30.0, lon2=120.01, alt2=200.0, sag=20.0,
        )
        self.line.ensure_cache()

    def test_endpoints_unaffected(self):
        """t=0 和 t=1 处高度不受垂度影响"""
        self.assertAlmostEqual(_catenary_altitude(0.0, self.line), 100.0)
        self.assertAlmostEqual(_catenary_altitude(1.0, self.line), 200.0)

    def test_midspan_sag(self):
        """t=0.5 处导线比直线低 sag 米"""
        self.assertAlmostEqual(_catenary_altitude(0.5, self.line), 130.0)  # 150 - 20

    def test_sag_zero_equals_straight(self):
        """sag=0 时垂曲线高度 = 直线插值高度"""
        line0 = PowerLineSegment(
            name="test0", lat1=30.0, lon1=120.0, alt1=50.0,
            lat2=30.0, lon2=120.01, alt2=150.0, sag=0.0,
        )
        line0.ensure_cache()
        for t in (0.0, 0.25, 0.5, 0.75, 1.0):
            expected = 50.0 + t * 100.0
            self.assertAlmostEqual(_catenary_altitude(t, line0), expected)


class TestDistanceSag(unittest.TestCase):
    """验证垂度距离计算的核心行为"""

    def setUp(self):
        # 400m 东西向线段, 两端高度平齐, sag=20m
        self.line = PowerLineSegment(
            name="400m-span", lat1=30.0, lon1=120.0, alt1=100.0,
            lat2=30.0, lon2=120.0045, alt2=100.0, sag=20.0,
        )
        self.line.ensure_cache()
        # 验证档距 ~400m
        self.assertAlmostEqual(self.line._cache_span_m, 434.0, delta=5.0)

    def test_sag_zero_matches_straight_line(self):
        """sag=0 时 GSS 距离 = 直线距离"""
        line0 = PowerLineSegment(
            name="no-sag", lat1=30.0, lon1=120.0, alt1=100.0,
            lat2=30.0, lon2=120.0045, alt2=100.0, sag=0.0,
        )
        line0.ensure_cache()

        # 中段正上方 50m
        d_straight = _distance_to_line_straight(150.0, 200.0, 0.0, line0)
        self.assertAlmostEqual(d_straight, 50.0, delta=0.5)

        # sag=0 时不进入 GSS 分支
        d = _distance_to_line(30.0, 120.00225, 150.0, line0)
        self.assertAlmostEqual(d, d_straight, delta=0.01)

    def test_drone_above_midspan(self):
        """无人机在中段正上方 30m: 直线距离 < 垂曲线距离"""
        # 中段正上方: lat≈30.0, lon≈120.00225, alt=130
        d = _distance_to_line(30.0, 120.00225, 130.0, self.line)
        # 直线: 30m (130-100), 垂曲线: √((30+sag_at_mid)² + 0)
        # sag_at_mid = 20, line_alt = 100 - 20 = 80, drone=130, distance=50
        self.assertAlmostEqual(d, 50.0, delta=1.0)
        self.assertGreater(d, 30.0)  # 应大于直线距离

    def test_drone_below_midspan(self):
        """无人机在中段正下方 30m: 直线距离 > 垂曲线距离"""
        # 中段正下方: lat≈30.0, lon≈120.00225, alt=50
        d = _distance_to_line(30.0, 120.00225, 50.0, self.line)
        # 直线: 50m (100-50), 垂曲线: 导线降到 80m, drone=50, distance=30
        self.assertAlmostEqual(d, 30.0, delta=1.0)
        self.assertLess(d, 50.0)  # 应小于直线距离

    def test_endpoints_distance_unchanged(self):
        """t=0 和 t=1 附近距离与直线几乎一致"""
        # 端点 1 正上方 20m (GSS 有限精度, 容许 <0.1m 偏差)
        d1 = _distance_to_line(30.0, 120.0, 120.0, self.line)
        self.assertAlmostEqual(d1, 20.0, delta=0.1)

        # 端点 2 正上方 (需要计算偏移)
        d2 = _distance_to_line(30.0, 120.0045, 120.0, self.line)
        self.assertAlmostEqual(d2, 20.0, delta=0.5)

    def test_stage2_skip_when_far(self):
        """远超告警阈值 → 跳过 GSS, 返回直线距离"""
        self.line.sag = 30.0
        # 中段正上方 300m → 直线距离=300, 保守下界=270 > 200 → 跳过 GSS
        d = _distance_to_line(30.0, 120.00225, 400.0, self.line)
        self.assertAlmostEqual(d, 300.0, delta=1.0)

    def test_stage2_enter_when_close(self):
        """接近告警阈值 → 进入 GSS 精修"""
        self.line.sag = 30.0
        # 中段下方 80m → 直线距离=80, 保守下界=50 ≤ 200 → 进入 GSS
        d = _distance_to_line(30.0, 120.00225, 20.0, self.line)
        # 垂曲线修正后距离应明显不同于 80
        self.assertNotAlmostEqual(d, 80.0, delta=10.0)


class TestGoldenSectionAccuracy(unittest.TestCase):
    """验证 GSS 精度: 与暴力采样对比"""

    def test_gss_vs_brute_force(self):
        """15 次 GSS vs 1000 点采样, 误差 <0.5m"""
        line = PowerLineSegment(
            name="accuracy-test", lat1=30.0, lon1=120.0, alt1=80.0,
            lat2=30.0, lon2=120.005, alt2=120.0, sag=15.0,
        )
        line.ensure_cache()

        # 测试多个无人机位置
        test_cases = [
            (30.0, 120.0025, 150.0),   # 中段上方
            (30.0, 120.0025, 60.0),    # 中段下方
            (30.0, 120.001, 100.0),    # 靠左
            (30.0, 120.004, 90.0),     # 靠右
            (30.0, 120.003, 50.0),     # 偏右下
        ]

        for lat, lon, alt in test_cases:
            # GSS 结果
            line.ensure_cache()
            dx, dy = _latlon_to_meters(lat, lon, line.lat1, line.lon1)
            d_gss = _distance_to_line_sag(alt, dx, dy, line)

            # 暴力采样 1000 点
            d_min = float('inf')
            for i in range(1001):
                t = i / 1000.0
                sag_off = 4.0 * line.sag * t * (1.0 - t)
                lx = t * line._cache_dx2
                ly = t * line._cache_dy2
                lalt = line.alt1 + t * (line.alt2 - line.alt1) - sag_off
                h_sq = (dx - lx) ** 2 + (dy - ly) ** 2
                v = alt - lalt
                d = math.sqrt(h_sq + v * v)
                if d < d_min:
                    d_min = d

            self.assertAlmostEqual(d_gss, d_min, delta=0.5,
                msg=f"GSS={d_gss:.3f} vs brute={d_min:.3f} at ({lat},{lon},{alt})")


class TestEstimateSag(unittest.TestCase):

    def test_typical_values(self):
        """典型电压等级 × 档距的垂度估算"""
        line = PowerLineSegment(
            name="est", lat1=30.0, lon1=120.0, alt1=100.0,
            lat2=30.0, lon2=120.0045, alt2=100.0,  # ~400m
            voltage_level="220kV",
        )
        line.ensure_cache()
        sag = estimate_sag(line)
        # 220kV → 5%, 400m → ~20m, ×SAG_SAFETY_FACTOR(1.5) → ~30m
        self.assertAlmostEqual(sag, 30.0, delta=5.0)

    def test_low_voltage_short_span(self):
        """10kV 短档距 → 小垂度"""
        line = PowerLineSegment(
            name="short", lat1=30.0, lon1=120.0, alt1=50.0,
            lat2=30.0, lon2=120.001, alt2=55.0,  # ~100m
            voltage_level="10kV",
        )
        line.ensure_cache()
        sag = estimate_sag(line)
        self.assertAlmostEqual(sag, 3.0, delta=2.0)

    def test_no_voltage_returns_zero(self):
        line = PowerLineSegment(
            name="no-v", lat1=30.0, lon1=120.0, alt1=100.0,
            lat2=30.0, lon2=120.005, alt2=100.0,
        )
        line.ensure_cache()
        self.assertEqual(estimate_sag(line), 0.0)


class TestGSSConvergence(unittest.TestCase):

    def test_convergence_rate(self):
        """验证 GSS 理论收敛: 0.618^(N-1)"""
        # 15 iterations → 0.618^14 ≈ 0.0012
        expected = _PHI ** (_GSS_ITERATIONS - 1)
        self.assertAlmostEqual(expected, 0.0012, delta=0.0002)

    def test_interval_shrinks_monotonically(self):
        """GSS 搜索区间单调缩小"""
        line = PowerLineSegment(
            name="conv", lat1=30.0, lon1=120.0, alt1=80.0,
            lat2=30.0, lon2=120.005, alt2=120.0, sag=15.0,
        )
        line.ensure_cache()
        dx, dy = _latlon_to_meters(30.0, 120.0025, line.lat1, line.lon1)

        # 模拟 GSS 迭代, 记录区间大小
        a, b = 0.0, 1.0
        intervals = []
        c = b - _PHI * (b - a)
        d = a + _PHI * (b - a)
        fc = (dx - c*line._cache_dx2)**2 + (dy - c*line._cache_dy2)**2
        fd = (dx - d*line._cache_dx2)**2 + (dy - d*line._cache_dy2)**2

        for i in range(_GSS_ITERATIONS):
            intervals.append(b - a)
            if fc < fd:
                b, d = d, c
                fd = fc
                c = b - _PHI * (b - a)
                fc = (dx - c*line._cache_dx2)**2 + (dy - c*line._cache_dy2)**2
            else:
                a, c = c, d
                fc = fd
                d = a + _PHI * (b - a)
                fd = (dx - d*line._cache_dx2)**2 + (dy - d*line._cache_dy2)**2

        self.assertEqual(len(intervals), _GSS_ITERATIONS)
        self.assertLess(intervals[-1], 0.01)  # 最终区间 <1%


if __name__ == "__main__":
    unittest.main()
