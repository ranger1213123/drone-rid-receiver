"""GET /api/trajectories — 无人机轨迹回放 + CSV 导出"""

import csv
import io

from flask import Blueprint, jsonify, request, Response

from .models import get_trajectory_summaries, get_trajectory_points
from .api_web import require_web_auth

bp = Blueprint("trajectory", __name__)


@bp.route("/api/trajectories")
@require_web_auth
def api_trajectories():
    """返回轨迹摘要列表，支持查询参数: drone_id, from, to"""
    drone_id = request.args.get("drone_id")
    date_from = request.args.get("from") or request.args.get("date_from")
    date_to = request.args.get("to") or request.args.get("date_to")
    if date_to and "T" not in date_to:
        date_to = date_to + "T23:59:59"
    summaries = get_trajectory_summaries(
        drone_id=drone_id, date_from=date_from, date_to=date_to
    )
    # 前端期望数组格式: [{drone_id, point_count, min_distance, first_ts, last_ts, device_name}]
    def _fmt(ts):
        """2026-07-02T15:30:00 -> 2026/07/02 15:30:00"""
        if not ts:
            return ""
        return ts.replace("T", " ").replace("-", "/")

    result = []
    for did, s in (summaries or {}).items():
        result.append({
            "drone_id": did,
            "point_count": s.get("count", 0),
            "min_distance": s.get("min_dist"),
            "first_ts": _fmt(s.get("first", "")),
            "last_ts": _fmt(s.get("last", "")),
            "device_name": s.get("device_name", ""),
        })
    # 按最后更新时间倒序
    result.sort(key=lambda x: x.get("last_ts", ""), reverse=True)
    return jsonify(result)


@bp.route("/api/trajectories/<drone_id>/points")
@require_web_auth
def api_trajectory_points(drone_id):
    """返回指定无人机轨迹坐标点"""
    limit = request.args.get("limit", 500, type=int)
    date_from = request.args.get("from") or request.args.get("date_from")
    date_to = request.args.get("to") or request.args.get("date_to")
    if date_to and "T" not in date_to:
        date_to = date_to + "T23:59:59"
    points = get_trajectory_points(drone_id, limit,
                                   date_from=date_from, date_to=date_to)
    return jsonify(points)


@bp.route("/api/trajectories/<drone_id>/download")
@require_web_auth
def api_trajectory_download(drone_id):
    """下载无人机轨迹为 CSV 文件"""
    fmt = request.args.get("format", "csv")
    date_from = request.args.get("from") or request.args.get("date_from")
    date_to = request.args.get("to") or request.args.get("date_to")
    if date_to and "T" not in date_to:
        date_to = date_to + "T23:59:59"
    points = get_trajectory_points(drone_id, limit=100000,
                                   date_from=date_from, date_to=date_to)

    if fmt == "json":
        return jsonify(points)

    # CSV 导出
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["时间", "纬度", "经度", "高度(m)", "距离电力线(m)", "最近电力线"])
    for p in points:
        ts = p.get("time", "")
        writer.writerow([
            ts,
            p.get("lat", ""),
            p.get("lon", ""),
            p.get("alt", ""),
            p.get("distance_to_line", ""),
            p.get("nearest_line", ""),
        ])
    csv_str = output.getvalue()
    output.close()

    return Response(
        csv_str,
        mimetype="text/csv; charset=utf-8-sig",
        headers={
            "Content-Disposition":
                f"attachment; filename=trajectory_{drone_id}.csv",
        },
    )
