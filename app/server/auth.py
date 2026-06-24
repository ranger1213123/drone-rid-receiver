"""
JWT 鉴权 — Token 签发 + 验证

POST  /api/auth/token  → { access_token, expires_in }
所有 /api/* 端点 → Bearer token 验证
"""

import json
import os
import time

import jwt
from flask import Blueprint, request, jsonify, current_app
from functools import wraps

from logging_config import get_logger

logger = get_logger(__name__)

bp = Blueprint("auth", __name__)


def _get_jwt_secret():
    """优先从 Flask app config 读取，回落环境变量"""
    try:
        secret = current_app.config.get("JWT_SECRET_KEY") or os.environ.get("JWT_SECRET_KEY", "")
    except RuntimeError:
        secret = os.environ.get("JWT_SECRET_KEY", "")
    if not secret or secret == "dev-secret-change-me":
        import logging
        logging.getLogger(__name__).critical(
            "JWT_SECRET_KEY 未配置或使用默认值! 生产环境必须设置此环境变量, 否则令牌可被伪造"
        )
        secret = secret or "dev-secret-change-me"
    return secret


def _load_device_secrets():
    # 1) DB device_secrets 表 (优先)
    try:
        from .models import get_device_secrets
        db_secrets = get_device_secrets()
        if db_secrets:
            return db_secrets
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
    try:
        data = request.json or {}
        device_name = data.get("device_name", "")
        device_secret = data.get("device_secret", "")

        if not device_name or not device_secret:
            return jsonify({"error": "missing device_name or device_secret"}), 400

        secrets = _load_device_secrets()
        expected = secrets.get(device_name)
        if not expected:
            return jsonify({"error": "unknown device"}), 403
        if device_secret != expected:
            return jsonify({"error": "invalid device_secret"}), 403

        # 检查设备证书是否已吊销
        try:
            from .cert_manager import get_cert_manager
            cm = get_cert_manager()
            if cm and cm.is_device_revoked(device_name):
                return jsonify({"error": "device certificate revoked"}), 403
        except Exception:
            pass  # cert_manager 不可用时不阻断 (如未初始化)

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
        return jsonify({"error": str(e)}), 500


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

        return f(*args, **kwargs)
    return decorated
