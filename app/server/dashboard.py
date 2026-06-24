"""页面路由: /, /map, /login, /logout, /register"""

from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for

from .models import verify_web_user

bp = Blueprint("dashboard", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")
    # 兼容 form POST 和 JSON
    if request.is_json:
        data = request.json or {}
    else:
        data = request.form
    username = (data.get("username") or "").strip()
    password = data.get("password", "")
    user = verify_web_user(username, password)
    if user:
        session["user"] = user
        return redirect(url_for("dashboard.dashboard"))
    return render_template("login.html", error="用户名或密码错误")


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("dashboard.login"))


@bp.route("/")
def dashboard():
    return render_template("map.html")


@bp.route("/list")
def list_view():
    return render_template("dashboard.html")


@bp.route("/register", methods=["GET"])
def register_page():
    return render_template("register.html")
