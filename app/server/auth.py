"""
JWT 鉴权 — Token 签发 + 验证

POST  /api/auth/token  → { access_token, expires_in }
所有 /api/* 端点 → Bearer token 验证
"""

import json
import os
import secrets
import threading
import time

import jwt
from flask import Blueprint, request, jsonify, current_app, g
from functools import wraps

from logging_config import get_logger

logger = get_logger(__name__)

bp = Blueprint("auth", __name__)

# ── Token 端点限流: {ip: [(timestamp, ...)]} ──
_token_rate_store: dict = {}
_token_rate_lock = threading.Lock()

def _rate_limit_token(ip: str, max_req: int = 10, window: int = 60) -> bool:
    """每窗口最多 max_req 次，返回 True 表示允许"""
    now = time.time()
    with _token_rate_lock:
        timestamps = [t for t in _token_rate_store.get(ip, []) if now - t < window]
        if len(timestamps) >= max_req:
            _token_rate_store[ip] = timestamps
            return False
        timestamps.append(now)
        _token_rate_store[ip] = timestamps
        return True


def _get_jwt_secret():
    """优先从 Flask app config 读取，回落环境变量"""
    try:
        secret = current_app.config.get("JWT_SECRET_KEY") or os.environ.get("JWT_SECRET_KEY", "")
    except RuntimeError:
        secret = os.environ.get("JWT_SECRET_KEY", "")
    if not secret:
        raise RuntimeError("JWT_SECRET_KEY 未配置，无法签发或验证令牌")
    if secret == "dev-secret-change-me":
        try:
            if not current_app.debug:
                raise RuntimeError("JWT_SECRET_KEY 仍为默认值，生产环境禁止使用")
        except RuntimeError:
            raise RuntimeError("JWT_SECRET_KEY 仍为默认值，生产环境禁止使用")
    return secret


def _load_device_secrets():
    # 1) DB device_secrets 表 (优先)
    try:
        from .models import get_device_secrets
        db_secrets = get_device_secrets()
        if db_secrets:
            return {d['device_name']: d['device_secret'] for d in db_secrets}
    except Exception:
        pass

    # 2) Flask app config
    try:
        secrets = current_app.config.get("DEVICE_SECRETS")
        if secrets:
            return secrets
    except RuntimeError:
        pass

    # 3) 环境变量 (回退)
    raw = os.environ.get("DEVICE_SECRETS", "{}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


@bp.route("/api/auth/token", methods=["POST"])
def issue_token():
    ip = request.remote_addr or "127.0.0.1"
    if not _rate_limit_token(ip):
        return jsonify({"error": "too many requests"}), 429
    try:
        data = request.json or {}
        device_name = data.get("device_name", "")
        device_secret = data.get("device_secret", "")

        if not device_name or not device_secret:
            return jsonify({"error": "missing device_name or device_secret"}), 400

        device_secrets = _load_device_secrets()
        expected = device_secrets.get(device_name)
        if not expected:
            return jsonify({"error": "unknown device"}), 403
        if not secrets.compare_digest(device_secret, expected):
            return jsonify({"error": "invalid device_secret"}), 403

        # 检查设备证书是否已吊销 (fail-close: 管理器异常时拒绝)
        try:
            from .cert_manager import get_cert_manager
            cm = get_cert_manager()
            if cm and cm.is_device_revoked(device_name):
                return jsonify({"error": "device certificate revoked"}), 403
        except Exception as e:
            logger.error("certificate revocation check failed for %s: %s", device_name, e)
            return jsonify({"error": "certificate verification unavailable"}), 503

        expire_seconds = int(os.environ.get("JWT_EXPIRE_SECONDS", "259200"))
        now = int(time.time())
        payload = {
            "sub": device_name,
            "iat": now,
            "exp": now + expire_seconds,
        }
        token = jwt.encode(payload, _get_jwt_secret(), algorithm="HS256")
        return jsonify({
            "access_token": token,
            "expires_in": expire_seconds,
        })
    except Exception as e:
        logger.error("token issue error: %s", e)
        return jsonify({"error": "authentication service unavailable"}), 500


def _verify_token(token: str) -> str | None:
    """验证 token 并返回 device_name, 失败返回 None"""
    try:
        payload = jwt.decode(token, _get_jwt_secret(), algorithms=["HS256"])
        return payload.get("sub")
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "missing or invalid Authorization header"}), 401

        token = auth_header[7:]
        device_name = _verify_token(token)
        if not device_name:
            return jsonify({"error": "token expired or invalid"}), 401

        # 将 JWT 主体注入请求上下文，端点须使用 g.device_name 而非请求体中的 device
        g.device_name = device_name
        return f(*args, **kwargs)
    return decorated
