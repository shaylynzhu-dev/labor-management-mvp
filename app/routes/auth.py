from flask import Blueprint, current_app, redirect, render_template, request, session, url_for

from app.utils.responses import api_response


auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")
    payload = request.get_json(silent=True) if request.is_json else request.form
    payload = payload or {}
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    user = current_app.extensions["auth_service"].authenticate(username, password)
    if not user:
        if request.is_json or request.args.get("format") == "json":
            return api_response(401, "用户名或密码错误", None, 401)
        return render_template("login.html", error="用户名或密码错误"), 401
    session.clear()
    session.permanent = True
    session.update(user_id=user["id"], username=user["username"], role=user["role"])
    if request.is_json or request.args.get("format") == "json":
        return api_response(0, "登录成功", {"username": user["username"], "role": user["role"]})
    return redirect(url_for("index"))


@auth_bp.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    if request.args.get("format") == "json":
        return api_response(0, "已退出")
    return redirect(url_for("auth.login"))


@auth_bp.get("/api/session")
def session_info():
    return api_response(
        0,
        "ok",
        {"user_id": session.get("user_id"), "username": session.get("username"), "role": session.get("role")},
    )
