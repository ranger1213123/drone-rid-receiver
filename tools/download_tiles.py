#!/usr/bin/env python3
"""地图瓦片预下载工具 — 从高德下载瓦片到本地离线存储。

用法:
  python tools/download_tiles.py --bounds 30,100,40,120 --minzoom 3 --maxzoom 12
  python tools/download_tiles.py --bounds 39.5,116.0,40.5,117.0 --minzoom 8 --maxzoom 14 --layer satellite --output data/tiles
  python tools/download_tiles.py --bounds 30,100,40,120 --minzoom 3 --maxzoom 8 --dry-run
"""

import argparse
import math
import os
import random as _random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import Request, urlopen, ProxyHandler, build_opener, install_opener
from urllib.error import HTTPError

_UA = "DroneRID-TileDownloader/1.0 (Windows; OSM/Amap tile caching)"

# ── 瓦片源 ──
_SOURCES = {
    "road": {
        "url": "https://{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scl=1&style=7&x={x}&y={y}&z={z}&key=fe811acf8e8fbe4056ab24775b0cd7d4",
        "max_zoom": 18,
        "subdomains": ["wprd01", "wprd02", "wprd03", "wprd04"],
    },
    "satellite": {
        "url": "https://webst01.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}&key=fe811acf8e8fbe4056ab24775b0cd7d4",
        "max_zoom": 18,
        "subdomains": [],
    },
    "terrain": {
        "url": "https://{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scl=1&style=8&x={x}&y={y}&z={z}&key=fe811acf8e8fbe4056ab24775b0cd7d4",
        "max_zoom": 18,
        "subdomains": ["wprd01", "wprd02", "wprd03", "wprd04"],
    },
    "osm": {
        "url": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        "max_zoom": 19,
        "subdomains": ["a", "b", "c"],
    },
}


def latlon_to_tile(lat, lon, z):
    lat_rad = math.radians(lat)
    n = 1 << z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def estimate_tile_count(lat1, lon1, lat2, lon2, min_zoom, max_zoom):
    total = 0
    lat_min, lat_max = min(lat1, lat2), max(lat1, lat2)
    lon_min, lon_max = min(lon1, lon2), max(lon1, lon2)
    for z in range(min_zoom, max_zoom + 1):
        x1, y1 = latlon_to_tile(lat_max, lon_min, z)
        x2, y2 = latlon_to_tile(lat_min, lon_max, z)
        total += (abs(x2 - x1) + 1) * (abs(y2 - y1) + 1)
    return total


def download_tile(z, x, y, layer, output_dir, dry_run, proxy_url=None):
    out_path = Path(output_dir) / layer / str(z) / str(x) / f"{y}.png"
    if out_path.exists() and out_path.stat().st_size >= 200:
        return z, x, y, True, True
    if dry_run:
        return z, x, y, True, False

    src = _SOURCES[layer]
    subs = src.get("subdomains", [])
    sub = _random.choice(subs) if subs else ""
    url = src["url"].replace("{s}", sub).replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))

    try:
        req = Request(url, headers={"User-Agent": _UA})
        if proxy_url:
            opener = build_opener(ProxyHandler({"https": proxy_url, "http": proxy_url}))
            resp = opener.open(req, timeout=15)
        else:
            resp = urlopen(req, timeout=15)
        data = resp.read()
        if len(data) < 100:
            return z, x, y, False, False
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data)
        return z, x, y, True, False
    except HTTPError as e:
        if e.code == 404:
            return z, x, y, True, False
        return z, x, y, False, False
    except Exception:
        return z, x, y, False, False


def iter_tiles(lat1, lon1, lat2, lon2, min_zoom, max_zoom):
    lat_min, lat_max = min(lat1, lat2), max(lat1, lat2)
    lon_min, lon_max = min(lon1, lon2), max(lon1, lon2)
    for z in range(min_zoom, max_zoom + 1):
        x1, y1 = latlon_to_tile(lat_max, lon_min, z)
        x2, y2 = latlon_to_tile(lat_min, lon_max, z)
        for x in range(min(x1, x2), max(x1, x2) + 1):
            for y in range(min(y1, y2), max(y1, y2) + 1):
                yield z, x, y


def main():
    parser = argparse.ArgumentParser(description="预下载地图瓦片到本地")
    parser.add_argument("--bounds", required=True,
                        help="经纬度边界: lat1,lon1,lat2,lon2 (例如 30,100,40,120)")
    parser.add_argument("--minzoom", type=int, default=3, help="最小缩放级别 (默认 3)")
    parser.add_argument("--maxzoom", type=int, default=12, help="最大缩放级别 (默认 12)")
    parser.add_argument("--layer", choices=["road", "satellite", "terrain", "osm"], default="osm",
                        help="图层类型 (默认 osm)")
    parser.add_argument("--output", default="data/tiles", help="输出目录 (默认 data/tiles)")
    parser.add_argument("--threads", type=int, default=6, help="并发线程数 (默认 6)")
    parser.add_argument("--dry-run", action="store_true", help="仅统计不下载")
    parser.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "",
                        help="HTTP 代理地址，如 http://127.0.0.1:7890 (默认读取 HTTPS_PROXY 环境变量)")
    args = parser.parse_args()

    proxy_url = args.proxy.strip() or None

    parts = args.bounds.split(",")
    if len(parts) != 4:
        print("错误: --bounds 格式为 lat1,lon1,lat2,lon2", file=sys.stderr)
        sys.exit(1)
    lat1, lon1, lat2, lon2 = map(float, parts)

    total = estimate_tile_count(lat1, lon1, lat2, lon2, args.minzoom, args.maxzoom)
    src = _SOURCES[args.layer]
    print(f"区域: ({lat1:.4f}, {lon1:.4f}) -> ({lat2:.4f}, {lon2:.4f})")
    print(f"缩放: {args.minzoom}-{args.maxzoom}  图层: {args.layer}  最大级别: {src['max_zoom']}")
    print(f"预估瓦片数: {total:,}  线程: {args.threads}")
    print(f"源: {src['url']}")
    if proxy_url:
        print(f"代理: {proxy_url}")
    if args.dry_run:
        print("*** DRY RUN — 不实际下载 ***")
    print()

    futures = {}
    with ThreadPoolExecutor(max_workers=args.threads) as executor:
        for z, x, y in iter_tiles(lat1, lon1, lat2, lon2, args.minzoom, args.maxzoom):
            fut = executor.submit(download_tile, z, x, y, args.layer, args.output, args.dry_run, proxy_url)
            futures[fut] = (z, x, y)

    downloaded = 0
    skipped = 0
    failed = 0
    start = time.time()

    for i, fut in enumerate(as_completed(futures), 1):
        z, x, y, ok, was_skipped = fut.result()
        if ok:
            if was_skipped:
                skipped += 1
            else:
                downloaded += 1
        else:
            failed += 1
        if i % 500 == 0 or i == total:
            elapsed = time.time() - start
            pct = i / total * 100
            rate = i / elapsed if elapsed > 0 else 0
            eta = (total - i) / rate if rate > 0 else 0
            print(f"\r进度: {i:,}/{total:,} ({pct:.1f}%)  "
                  f"下载:{downloaded} 跳过:{skipped} 失败:{failed}  "
                  f"速率:{rate:.0f}/s ETA:{eta:.0f}s", end="", flush=True)

    elapsed = time.time() - start
    print(f"\n\n完成. 耗时 {elapsed:.0f}s  下载:{downloaded}  跳过:{skipped}  失败:{failed}")


if __name__ == "__main__":
    main()
