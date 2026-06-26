"""页面路由: /, /map, /login, /logout, /register"""

import time
import secrets
import threading
from functools import wraps

from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for

from .models import verify_web_user

bp = Blueprint("dashboard", __name__)

# 登录限流: {ip: [(timestamp, ...)]}
_login_attempts: dict = {}
_login_lock = threading.Lock()


def _rate_limit_login(ip: str, max_attempts: int = 5, window: int = 300) -> bool:
    """5分钟内最多5次失败尝试，返回 True 表示允许"""
    now = time.time()
    with _login_lock:
        attempts = [t for t in _login_attempts.get(ip, []) if now - t < window]
        if len(attempts) >= max_attempts:
            _login_attempts[ip] = attempts
            return False
        _login_attempts[ip] = attempts
        return True


def _record_login_attempt(ip: str):
    with _login_lock:
        now = time.time()
        attempts = [t for t in _login_attempts.get(ip, []) if now - t < 300]
        attempts.append(now)
        _login_attempts[ip] = attempts


def _require_login(f):
    """页面级鉴权 — 未登录重定向到 /login"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("dashboard.login"))
        return f(*args, **kwargs)
    return decorated


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")
    ip = request.remote_addr or "127.0.0.1"
    if not _rate_limit_login(ip):
        return render_template("login.html", error="尝试次数过多，请 5 分钟后再试")
    # 兼容 form POST 和 JSON
    if request.is_json:
        data = request.json or {}
    else:
        data = request.form
    username = (data.get("username") or "").strip()
    password = data.get("password", "")
    user = verify_web_user(username, password)
    if user:
        session.clear()
        session["user"] = user
        return redirect(url_for("dashboard.dashboard"))
    _record_login_attempt(ip)
    return render_template("login.html", error="用户名或密码错误")


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("dashboard.login"))


@bp.route("/")
@_require_login
def dashboard():
    return render_template("map.html")


@bp.route("/list")
@_require_login
def list_view():
    return render_template("dashboard.html")


@bp.route("/register", methods=["GET"])
def register_page():
    return render_template("register.html")
