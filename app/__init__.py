import click

from flask import redirect, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException

from config import get_config

from .models.legacy_runtime import app as _legacy_app, init_db as initialize_legacy_database
from .models import Database, ImportLogRepository, LegacyBusinessRepository, UserRepository
from .routes import auth_bp, imports_bp, quota_api_bp, quota_bp, quota_legacy_bp
from .services import AuthService, ExcelImportService, init_quota_service
from .utils.responses import api_response
from .utils.logging import configure_logging


def create_app(config_object=None):
    app = _legacy_app
    if app.extensions.get("production_architecture_ready"):
        return app

    app.config.from_object(config_object or get_config())
    app.debug = False
    with app.app_context():
        initialize_legacy_database()
    log_dir = app.config["LOG_DIR"]
    log_dir.mkdir(parents=True, exist_ok=True)
    configure_logging(app)
    database = Database(app.config["DATABASE"])
    database.initialize()
    init_quota_service(app)
    user_repository = UserRepository(database)
    import_log_repository = ImportLogRepository(database)
    business_repository = LegacyBusinessRepository(database)
    auth_service = AuthService(user_repository)
    auth_service.ensure_admin()
    excel_import_service = ExcelImportService(
        database, business_repository, import_log_repository
    )
    app.extensions.update(
        production_architecture_ready=True,
        production_database=database,
        auth_service=auth_service,
        import_log_repository=import_log_repository,
        excel_import_service=excel_import_service,
    )
    app.register_blueprint(auth_bp)
    app.register_blueprint(imports_bp)
    app.register_blueprint(quota_bp)
    app.register_blueprint(quota_legacy_bp)
    app.register_blueprint(quota_api_bp)

    role_permissions = {
        "admin": {"view", "create", "update", "delete", "restore", "permanent_delete", "audit"},
        "hr": {"view", "create", "update", "delete", "restore"},
        "manager": {"view", "create", "update"},
        "viewer": {"view"},
    }

    def can(permission):
        if app.config.get("TESTING"):
            return True
        return permission in role_permissions.get(session.get("role"), set())

    app.jinja_env.globals["can"] = can

    @app.get("/index.html")
    @app.get("/templates/index.html")
    def rendered_index_alias():
        return app.view_functions["index"]()

    @app.after_request
    def prevent_stale_html(response):
        if response.mimetype == "text/html":
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        app.logger.info(
            "%s %s status=%s", request.method, request.path, response.status_code
        )
        if request.method in {"POST", "PATCH", "PUT", "DELETE"} and response.status_code < 500:
            if request.endpoint == "auth.login":
                action = "login" if response.status_code < 400 else "login_failed"
                entity_type = "session"
                entity_id = (request.form.get("username") or "").strip() or None
            else:
                if request.path.endswith("/restore"):
                    action = "restore"
                elif request.path.endswith("/permanent"):
                    action = "permanent_delete"
                elif request.method == "DELETE":
                    action = "delete"
                elif request.method in {"PATCH", "PUT"}:
                    action = "update"
                elif any(token in (request.endpoint or "") for token in ("add_", "create_", "upload", "import")):
                    action = "create"
                else:
                    action = "update"
                parts = [part for part in request.path.split("/") if part]
                entity_type = parts[1] if parts and parts[0] == "api" and len(parts) > 1 else (parts[0] if parts else request.endpoint or "system")
                values = request.view_args or {}
                entity_id = next((str(value) for key, value in values.items() if key.endswith("_id") or key == "resource_id"), None)
            try:
                with database.transaction() as connection:
                    connection.execute(
                        """INSERT INTO audit_logs
                           (user_id,action,entity_type,entity_id)
                           VALUES (?,?,?,?)""",
                        (session.get("user_id"), action, entity_type, entity_id),
                    )
            except Exception:
                app.logger.exception("Failed to record audit log")
        return response

    @app.cli.command("create-user")
    @click.option("--username", prompt=True)
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    @click.option("--role", type=click.Choice(["admin", "hr", "manager", "viewer"]), default="viewer")
    def create_user_command(username, password, role):
        """Create an administrator or read-only operator account."""
        try:
            auth_service.create_user(username, password, role)
        except ValueError as error:
            raise click.ClickException(str(error)) from error
        click.echo(f"Created {role} user: {username}")

    @app.before_request
    def require_login():
        if app.config.get("TESTING"):
            return None
        if request.endpoint in {"auth.login", "static", "health"}:
            return None
        if not session.get("user_id"):
            if request.path.startswith(("/api/", "/imports/", "/stream/")):
                return api_response(401, "authentication required", None, 401)
            return redirect(url_for("auth.login", next=request.full_path))
        if request.endpoint == "audit_logs_page" and not can("audit"):
            return api_response(403, "需要审计权限", None, 403)
        if request.endpoint == "recycle_bin_page" and not can("restore"):
            return api_response(403, "需要回收站权限", None, 403)
        if request.path.endswith("/permanent"):
            permission = "permanent_delete"
        elif request.path.endswith("/restore"):
            permission = "restore"
        elif request.method == "DELETE":
            permission = "delete"
        elif request.method in {"PATCH", "PUT"}:
            permission = "update"
        elif request.method == "POST":
            permission = "create" if any(
                token in (request.endpoint or "")
                for token in ("add_", "create_", "upload", "import")
            ) else "update"
        else:
            permission = "view"
        if not can(permission):
            if request.path.startswith(("/api/", "/imports/", "/stream/")):
                return api_response(403, f"缺少 {permission} 权限", None, 403)
            return api_response(403, f"缺少 {permission} 权限", None, 403)
        return None

    @app.errorhandler(HTTPException)
    def handle_http_error(error):
        app.logger.warning(
            "HTTP error: method=%s path=%s status=%s message=%s",
            request.method, request.path, error.code, error.description,
        )
        if request.path.startswith(("/api/", "/imports/", "/stream/")):
            return api_response(error.code, error.description, None, error.code)
        return render_template(
            "error.html", status_code=error.code, message=error.description
        ), error.code

    @app.errorhandler(Exception)
    def handle_unexpected_error(error):
        app.logger.exception("Unhandled Labour OS request error")
        if request.path.startswith(("/api/", "/imports/", "/stream/")):
            return api_response(500, "internal server error", None, 500)
        return render_template(
            "error.html", status_code=500, message="系统暂时无法处理该请求。"
        ), 500

    return app


app = create_app()


# Compatibility exports for the existing regression suite and CLI helpers.
from .models.legacy_runtime import (  # noqa: E402,F401
    collect_push_reminders, db_insert, db_query, db_update, get_db, init_db,
    refresh_standard_risks, shift_months,
)

__all__ = [
    "app", "create_app", "collect_push_reminders", "db_insert", "db_query",
    "db_update", "get_db", "init_db", "refresh_standard_risks", "shift_months",
]
