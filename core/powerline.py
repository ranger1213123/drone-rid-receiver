"""
电力线模块 - 电力线数据管理

管理电力线段的加载、查询、以及与无人机的距离计算。
参考标准: GB 50545-2010《110kV～750kV架空输电线路设计规范》
"""

import math
import yaml
from typing import List, Dict, Optional, Tuple

from core.coords import gcj02_to_wgs84

# GB 50545-2010 非居民区最小对地距离 (m)
VOLTAGE_CLEARANCE = {
    '10kV': 5.5,
    '35kV': 6.5,
    '66kV': 7.0,
    '110kV': 7.0,
    '220kV': 8.5,
    '330kV': 9.5,
    '500kV': 14.0,
    '750kV': 19.5,
    '±800kV': 21.0,
    '1000kV': 27.0,
}

VOLTAGE_LEVELS = list(VOLTAGE_CLEARANCE.keys())


class PowerLineSegment:
    """电力线段 - 两个三维端点定义一个线段

    缓存端点 2 相对于端点 1 的平面偏移和线段长度平方，
    避免对同一线段重复进行经纬度→米制转换。
    """

    __slots__ = ('name', 'line_id', 'lat1', 'lon1', 'alt1',
                 'lat2', 'lon2', 'alt2', 'voltage_level', 'sag',
                 'tower_height1', 'tower_height2',
                 '_cache_dx2', '_cache_dy2', '_cache_len_sq', '_cache_span_m')

    def __init__(self, name: str,
                 lat1: float, lon1: float, alt1: float,
                 lat2: float, lon2: float, alt2: float,
                 line_id: int = 0, voltage_level: str = '',
                 sag: float = 0.0,
                 tower_height1: Optional[float] = None,
                 tower_height2: Optional[float] = None):
        self.name = name
        self.line_id = line_id
        self.lat1, self.lon1, self.alt1 = lat1, lon1, alt1
        self.lat2, self.lon2, self.alt2 = lat2, lon2, alt2
        self.voltage_level = voltage_level
        self.sag = sag
        self.tower_height1 = tower_height1
        self.tower_height2 = tower_height2
        self._cache_dx2: Optional[float] = None
        self._cache_dy2: Optional[float] = None
        self._cache_len_sq: Optional[float] = None
        self._cache_span_m: Optional[float] = None

    def ensure_cache(self):
        """惰性计算并缓存端点 2 相对于端点 1 的平面偏移及档距"""
        if self._cache_dx2 is not None:
            return
        self._cache_dx2, self._cache_dy2 = _latlon_to_meters(
            self.lat2, self.lon2, self.lat1, self.lon1
        )
        self._cache_len_sq = (self._cache_dx2 * self._cache_dx2 +
                              self._cache_dy2 * self._cache_dy2)
        self._cache_span_m = math.sqrt(self._cache_len_sq)

    def invalidate_cache(self):
        """坐标变更后清除缓存"""
        self._cache_dx2 = None
        self._cache_dy2 = None
        self._cache_len_sq = None
        self._cache_span_m = None

    def get_clearance(self) -> Optional[float]:
        """返回该电压等级对应的 GB 50545 最小对地距离，无等级则返回 None"""
        return VOLTAGE_CLEARANCE.get(self.voltage_level)

    def __repr__(self):
        vl = f" {self.voltage_level}" if self.voltage_level else ""
        return (f"PowerLine({self.name}{vl}: "
                f"[{self.lat1:.4f},{self.lon1:.4f},{self.alt1:.1f}] → "
                f"[{self.lat2:.4f},{self.lon2:.4f},{self.alt2:.1f}])")


class PowerLineManager:
    """电力线管理器 - 加载、存储、查询"""

    def __init__(self):
        self.lines: List[PowerLineSegment] = []

    def load_from_yaml(self, yaml_path: str):
        """从 YAML 配置文件加载电力线

        若 coordinate_system 为 gcj02, 自动将坐标转换为 WGS-84,
        以对齐无人机 GPS 坐标系, 避免 100–700m 的系统性测距偏差。
        """
        with open(yaml_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)

        auto_estimate = config.get("auto_estimate_sag", False)
        coord_system = config.get("coordinate_system", "wgs84").lower()
        needs_convert = (coord_system == "gcj02")

        self.lines = []
        line_id = 1
        for line_data in config.get("power_lines", []):
            lat1, lon1 = line_data["lat1"], line_data["lon1"]
            lat2, lon2 = line_data["lat2"], line_data["lon2"]

            if needs_convert:
                lat1, lon1 = gcj02_to_wgs84(lat1, lon1)
                lat2, lon2 = gcj02_to_wgs84(lat2, lon2)

            sag = line_data.get("sag", -1.0)
            voltage_level = line_data.get("voltage_level", "")
            segment = PowerLineSegment(
                name=line_data["name"],
                lat1=lat1, lon1=lon1, alt1=line_data["alt1"],
                lat2=lat2, lon2=lon2, alt2=line_data["alt2"],
                line_id=line_id,
                voltage_level=voltage_level,
                sag=0.0,
            )
            if sag >= 0:
                segment.sag = sag
            elif auto_estimate and voltage_level:
                segment.ensure_cache()
                segment.sag = estimate_sag(segment)
            self.lines.append(segment)
            line_id += 1

        return len(self.lines)

    def load_from_list(self, lines: List[Dict]):
        """从字典列表加载电力线 (用于数据库恢复)"""
        self.lines = []
        for line_data in lines:
            sag = line_data.get("sag", -1.0)
            voltage_level = line_data.get("voltage_level", "")
            segment = PowerLineSegment(
                name=line_data["name"],
                lat1=line_data["lat1"], lon1=line_data["lon1"],
                alt1=line_data["alt1"],
                lat2=line_data["lat2"], lon2=line_data["lon2"],
                alt2=line_data["alt2"],
                line_id=line_data.get("id", 0),
                voltage_level=voltage_level,
                sag=0.0,
                tower_height1=line_data.get("tower_height1"),
                tower_height2=line_data.get("tower_height2"),
            )
            if sag >= 0:
                segment.sag = sag
            elif voltage_level:
                segment.ensure_cache()
                segment.sag = estimate_sag(segment)
            self.lines.append(segment)
        return len(self.lines)

    def find_nearest_line(self, lat: float, lon: float, alt: float
                           ) -> Tuple[Optional[PowerLineSegment], float]:
        """
        查找距离无人机最近的电力线段，返回 (线段, 3D欧氏距离_米)

        使用三维欧氏距离: sqrt(水平距离² + 垂直距离²)
        线段平面坐标会被缓存以避免重复转换。
        """
        if not self.lines:
            return None, float('inf')

        best_line = None
        best_distance = float('inf')

        for line in self.lines:
            line.ensure_cache()
            dist = _distance_to_line(lat, lon, alt, line)
            if dist < best_distance:
                best_distance = dist
                best_line = line

        return best_line, best_distance

    def find_all_within(self, lat: float, lon: float, alt: float,
                        max_distance: float
                        ) -> List[Tuple[PowerLineSegment, float]]:
        """
        查找所有距离小于指定值的电力线段
        返回 [(线段, 3D欧氏距离), ...] 按距离升序排列
        """
        results = []
        for line in self.lines:
            line.ensure_cache()
            dist = _distance_to_line(lat, lon, alt, line)
            if dist <= max_distance:
                results.append((line, dist))

        results.sort(key=lambda x: x[1])
        return results


# ─────────────────── 经纬度转换 ───────────────────

def _latlon_to_meters(lat: float, lon: float,
                      ref_lat: float, ref_lon: float) -> Tuple[float, float]:
    """
    将经纬度转换为以参考点为原点的近似平面坐标 (米)
    使用 WGS84 简化公式
    """
    lat_mid = (ref_lat + lat) / 2.0
    meters_per_deg_lat = 111132.954 - 559.822 * math.cos(2 * math.radians(lat_mid))
    meters_per_deg_lon = (math.pi / 180) * 6378137.0 * math.cos(math.radians(lat_mid))

    dy = (lat - ref_lat) * meters_per_deg_lat
    dx = (lon - ref_lon) * meters_per_deg_lon
    return dx, dy


# ─────────────────── 导线垂度 ───────────────────

# 垂度安全系数: 查表值为理想值, 实际中受温度/覆冰/施工偏差影响更大
# 乘以 1.5 确保垂度估算偏保守 (导线实际位置更低 = 告警更灵敏)
SAG_SAFETY_FACTOR = 1.5

# 各电压等级典型垂度/档距比值 (GB 50545-2010)
TYPICAL_SAG_RATIO = {
    (0, 66): 0.03,      # 配电线路 (10kV–66kV), 档距 <200m
    (66, 220): 0.05,    # 输电线路 (110kV–220kV), 档距 200–400m
    (220, 750): 0.07,   # 高压输电 (330kV–750kV), 档距 300–800m
    (750, 1200): 0.08,  # 特高压 (±800kV–1000kV), 档距 >500m
}

# GSS 迭代次数
_GSS_ITERATIONS = 15
# 黄金分割常数
_PHI = (math.sqrt(5.0) - 1.0) / 2.0  # ≈ 0.618


def _parse_voltage_kv(voltage_level: str) -> int:
    """从电压等级字符串中提取数值 (kV)"""
    try:
        s = voltage_level.upper().replace('±', '').replace('KV', '').strip()
        return int(float(s))
    except (ValueError, AttributeError):
        return 0


def estimate_sag(line: PowerLineSegment) -> float:
    """根据电压等级和档距估算最大垂度 (m)

    查表 TYPICAL_SAG_RATIO, 未匹配则返回 0 (直线)。
    """
    kv = _parse_voltage_kv(line.voltage_level)
    if kv <= 0 or line._cache_span_m is None:
        return 0.0
    for (lo, hi), ratio in TYPICAL_SAG_RATIO.items():
        if lo < kv <= hi:
            return line._cache_span_m * ratio * SAG_SAFETY_FACTOR
    return 0.0


# ─────────────────── 3D 欧氏距离 ───────────────────


def _catenary_altitude(t: float, line: PowerLineSegment) -> float:
    """抛物线垂曲线在 t ∈ [0,1] 处的导线实际高度 (m)"""
    sag_offset = 4.0 * line.sag * t * (1.0 - t)
    return line.alt1 + t * (line.alt2 - line.alt1) - sag_offset


def _squared_distance(t: float, dx_drone: float, dy_drone: float,
                      drone_alt: float, line: PowerLineSegment) -> float:
    """计算无人机到抛物线垂曲线上 t 位置的距离平方"""
    closest_x = t * line._cache_dx2
    closest_y = t * line._cache_dy2
    line_alt = _catenary_altitude(t, line)
    h_sq = (dx_drone - closest_x) ** 2 + (dy_drone - closest_y) ** 2
    v = drone_alt - line_alt
    return h_sq + v * v


def _distance_to_line_straight(drone_alt: float,
                               dx_drone: float, dy_drone: float,
                               line: PowerLineSegment) -> float:
    """计算无人机到直线线段的 3D 距离 (不含垂度)

    调用方已计算 dx_drone/dy_drone 并确保 seg_len_sq ≥ 1e-6。
    """
    dx2, dy2 = line._cache_dx2, line._cache_dy2
    seg_len_sq = line._cache_len_sq

    t = (dx_drone * dx2 + dy_drone * dy2) / seg_len_sq
    t = max(0.0, min(1.0, t))

    closest_x = t * dx2
    closest_y = t * dy2
    line_alt = line.alt1 + t * (line.alt2 - line.alt1)

    h_sq = (dx_drone - closest_x) ** 2 + (dy_drone - closest_y) ** 2
    v = drone_alt - line_alt
    return math.sqrt(h_sq + v * v)


def _distance_to_line_sag(drone_alt: float,
                          dx_drone: float, dy_drone: float,
                          line: PowerLineSegment) -> float:
    """Golden Section Search 找无人机到抛物线垂曲线段的最近 3D 距离

    在 t ∈ [0, 1] 上最小化 squared_distance(t)。
    调用方已计算 dx_drone/dy_drone 并确保 sag > 0 且 seg_len_sq ≥ 1e-6。
    """
    # GSS 初始区间 [a, b] = [0, 1]
    a, b = 0.0, 1.0
    # 内点: c = b - φ(b-a), d = a + φ(b-a)
    c = b - _PHI * (b - a)
    d = a + _PHI * (b - a)
    fc = _squared_distance(c, dx_drone, dy_drone, drone_alt, line)
    fd = _squared_distance(d, dx_drone, dy_drone, drone_alt, line)

    for _ in range(_GSS_ITERATIONS):
        if fc < fd:
            b, d = d, c
            fd = fc
            c = b - _PHI * (b - a)
            fc = _squared_distance(c, dx_drone, dy_drone, drone_alt, line)
        else:
            a, c = c, d
            fc = fd
            d = a + _PHI * (b - a)
            fd = _squared_distance(d, dx_drone, dy_drone, drone_alt, line)

    return math.sqrt(min(fc, fd))


def _distance_to_line(drone_lat: float, drone_lon: float, drone_alt: float,
                      line: PowerLineSegment) -> float:
    """计算无人机到电力线段的三维欧氏距离 (米) — 两阶段路由

    Stage 1: 直线距离 (O(1))
    Stage 2: 若 sag>0 且修正可能影响告警 → GSS 精修 (O(15))
    """
    line.ensure_cache()

    # 无人机相对线段起点的平面偏移
    dx_drone, dy_drone = _latlon_to_meters(
        drone_lat, drone_lon, line.lat1, line.lon1
    )

    if line._cache_len_sq < 1e-6:
        # 线段退化为点
        h_sq = dx_drone * dx_drone + dy_drone * dy_drone
        v = drone_alt - line.alt1
        return math.sqrt(h_sq + v * v)

    # Stage 1: 直线距离
    d_straight = _distance_to_line_straight(drone_alt, dx_drone, dy_drone, line)

    # Stage 2: 保守下界检查 → GSS 精修
    if line.sag > 0.0 and max(0.0, d_straight - line.sag) <= 200.0:
        return _distance_to_line_sag(drone_alt, dx_drone, dy_drone, line)

    return d_straight
