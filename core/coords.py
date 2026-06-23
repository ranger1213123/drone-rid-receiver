"""
坐标系转换 — GCJ-02 ↔ WGS-84

中国境内电力线坐标可能使用 GCJ-02 (国测局坐标系, 又称火星坐标系),
而无人机 RID 上报的 GPS 坐标为 WGS-84, 两者偏移 100–700m。
电力线加载时自动检测并转换, 确保测距基准一致。

转换算法参考:
  https://github.com/wandergis/coordtransform
"""

import math

# 椭球参数
_A = 6378245.0  # 长半轴
_EE = 0.00669342162296594323  # 偏心率平方


def _out_of_china(lat: float, lon: float) -> bool:
    """判断坐标是否在中国境外 (境外不做 GCJ-02 偏移)"""
    return not (72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _transform_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def _delta(lat: float, lon: float) -> tuple:
    """计算 GCJ-02 相对于 WGS-84 的偏移量 (lat, lon)"""
    if _out_of_china(lat, lon):
        return 0.0, 0.0
    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)
    rad_lat = lat / 180.0 * math.pi
    magic = 1.0 - _EE * math.sin(rad_lat) * math.sin(rad_lat)
    sqrt_magic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1.0 - _EE)) / (magic * sqrt_magic) * math.pi)
    dlon = (dlon * 180.0) / (_A / sqrt_magic * math.cos(rad_lat) * math.pi)
    return dlat, dlon


def wgs84_to_gcj02(lat: float, lon: float) -> tuple:
    """WGS-84 → GCJ-02"""
    if _out_of_china(lat, lon):
        return lat, lon
    dlat, dlon = _delta(lat, lon)
    return lat + dlat, lon + dlon


def gcj02_to_wgs84(lat: float, lon: float) -> tuple:
    """GCJ-02 → WGS-84 (迭代逼近, 精度 <0.5m)"""
    if _out_of_china(lat, lon):
        return lat, lon
    # 牛顿迭代: 从 GCJ-02 坐标出发, 反向逼近 WGS-84
    wgs_lat, wgs_lon = lat, lon
    for _ in range(8):
        gcj_lat, gcj_lon = wgs84_to_gcj02(wgs_lat, wgs_lon)
        wgs_lat += lat - gcj_lat
        wgs_lon += lon - gcj_lon
    return wgs_lat, wgs_lon
