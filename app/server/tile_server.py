"""本地瓦片缓存代理 — 首次从高德获取并缓存，后续直接读本地。

路由:
  /tiles/road/<z>/<x>/<y>.png      — 标准地图 (高德矢量)
  /tiles/satellite/<z>/<x>/<y>.png  — 卫星影像 (高德卫星)
  /tiles/terrain/<z>/<x>/<y>.png    — 地形图 (高德)
  /tiles/<z>/<x>/<y>.png           — 兼容旧路由，默认标准地图
"""

import io
import os
import random
import struct
import threading
import zlib
from pathlib import Path
from urllib.request import Request, urlopen

from flask import Blueprint, send_file

bp = Blueprint("tile_server", __name__, url_prefix="/tiles")

# ── 透明占位 PNG (256×256 RGBA) ──


def _make_empty_png(size=256):
    raw = b"\x00" * (size * size * 4)
    filtered = b""
    for i in range(size):
        filtered += b"\x00" + raw[i * size * 4 : (i + 1) * size * 4]

    def _chunk(chunk_type, data):
        c = chunk_type + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(filtered))
        + _chunk(b"IEND", b"")
    )


_EMPTY_PNG = _make_empty_png(256)


def _make_gray_png(size=256):
    """淡灰色瓦片，用于标识不可用区域（非透明，不会产生拼接缝隙）"""
    raw = b"\xf0\xf0\xf0\xff" * (size * size)  # RGBA light gray
    filtered = b""
    for i in range(size):
        filtered += b"\x00" + raw[i * size * 4 : (i + 1) * size * 4]

    def _chunk(chunk_type, data):
        c = chunk_type + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(filtered))
        + _chunk(b"IEND", b"")
    )


_GRAY_PNG = _make_gray_png(256)

# ── 上游瓦片源 (高德) ──
_AMAP_SUBDOMAINS = ["wprd01", "wprd02", "wprd03", "wprd04"]
_AMAP_ROAD = "https://{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scl=1&style=7&x={x}&y={y}&z={z}&key=fe811acf8e8fbe4056ab24775b0cd7d4"
_AMAP_SATELLITE = "https://webst01.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}&key=fe811acf8e8fbe4056ab24775b0cd7d4"
_AMAP_TERRAIN = "https://{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scl=1&style=8&x={x}&y={y}&z={z}&key=fe811acf8e8fbe4056ab24775b0cd7d4"

_TILE_SOURCES = {
    "road": _AMAP_ROAD,
    "satellite": _AMAP_SATELLITE,
    "terrain": _AMAP_TERRAIN,
}

_TILE_DIR = None

# 正在下载的瓦片: dedup_key → threading.Event
_PENDING: dict = {}
_PENDING_LOCK = threading.Lock()
_UA = "DroneRID-TileProxy/1.0 (Windows; Amap tile caching)"


def _init_dir():
    global _TILE_DIR
    if _TILE_DIR is not None:
        return _TILE_DIR
    base = Path(os.environ.get("MAP_TILE_OFFLINE_DIR", "data/tiles"))
    if not base.is_absolute():
        base = Path(__file__).resolve().parent.parent.parent / base
    _TILE_DIR = base
    return _TILE_DIR


def _cache_path(z, x, y, layer):
    return _init_dir() / layer / str(z) / str(x) / f"{y}.png"


def _fetch_remote(z, x, y, layer):
    url_template = _TILE_SOURCES.get(layer, _AMAP_ROAD)
    # 随机子域名实现负载均衡
    sub = random.choice(_AMAP_SUBDOMAINS)
    url = url_template.replace("{s}", sub).replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
    try:
        req = Request(url, headers={"User-Agent": _UA})
        resp = urlopen(req, timeout=3)
        data = resp.read()
        if len(data) >= 100 and data != _EMPTY_PNG:
            return data
    except Exception:
        pass
    return None


def _save_tile(z, x, y, layer, data):
    # 绝不缓存空/占位瓦片
    if data == _EMPTY_PNG or len(data) < 100:
        return
    path = _cache_path(z, x, y, layer)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _serve_tile(z, x, y, layer):
    # 1. 本地缓存
    cache = _cache_path(z, x, y, layer)
    if cache.is_file():
        # 检查之前是否缓存了占位瓦片（旧版 bug），删除后重新获取
        if cache.stat().st_size < 200:
            cache.unlink(missing_ok=True)
        else:
            return send_file(str(cache), mimetype="image/png", max_age=86400)

    # 2. 去重：同一瓦片只允许一个线程去外网获取
    dedup_key = (z, x, y, layer)
    event = None
    should_fetch = False
    with _PENDING_LOCK:
        if dedup_key not in _PENDING:
            event = threading.Event()
            _PENDING[dedup_key] = event
            should_fetch = True
        else:
            event = _PENDING[dedup_key]

    if should_fetch:
        data = _fetch_remote(z, x, y, layer)
        if data:
            _save_tile(z, x, y, layer, data)
        with _PENDING_LOCK:
            evt = _PENDING.pop(dedup_key, None)
        if evt:
            evt.set()  # 通知所有等待线程
        if data:
            return send_file(io.BytesIO(data), mimetype="image/png", max_age=86400)
        # 获取失败，返回淡灰瓦片避免拼接缝隙
        return send_file(io.BytesIO(_GRAY_PNG), mimetype="image/png", max_age=60)

    # 3. 等待正在下载的线程完成（最多 3 秒）
    if event:
        event.wait(timeout=3)
        if cache.is_file():
            return send_file(str(cache), mimetype="image/png", max_age=86400)

    # 4. 彻底失败，返回淡灰瓦片
    return send_file(io.BytesIO(_GRAY_PNG), mimetype="image/png", max_age=60)


# ── 路由 ──


@bp.route("/road/<int:z>/<int:x>/<int:y>.png")
def serve_road(z, x, y):
    return _serve_tile(z, x, y, "road")


@bp.route("/satellite/<int:z>/<int:x>/<int:y>.png")
def serve_satellite(z, x, y):
    return _serve_tile(z, x, y, "satellite")


@bp.route("/terrain/<int:z>/<int:x>/<int:y>.png")
def serve_terrain(z, x, y):
    return _serve_tile(z, x, y, "terrain")


@bp.route("/<int:z>/<int:x>/<int:y>.png")
def serve_tile(z, x, y):
    return _serve_tile(z, x, y, "road")
