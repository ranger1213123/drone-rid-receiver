"""GET /api/trajectories — 无人机轨迹回放（只读查询）"""

from flask import Blueprint, jsonify, request, session

from .models import get_trajectory_summaries, get_trajectory_points

bp = Blueprint("trajectory", __name__)


@bp.route("/api/trajectories")
def api_trajectories():
    """返回轨迹摘要，支持查询参数: drone_id, date_from, date_to"""
    if "user" not in session:
        return jsonify({"error": "未登录"}), 401
    drone_id = request.args.get("drone_id")
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    return jsonify(get_trajectory_summaries(
        drone_id=drone_id, date_from=date_from, date_to=date_to
    ))


@bp.route("/api/trajectories/<drone_id>/points")
def api_trajectory_points(drone_id):
    """返回指定无人机轨迹坐标点"""
    if "user" not in session:
        return jsonify({"error": "未登录"}), 401
    limit = request.args.get("limit", 500, type=int)
    points = get_trajectory_points(drone_id, limit)
    return jsonify(points)
