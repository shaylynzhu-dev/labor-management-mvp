import click

from flask import redirect, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException

from config import get_config

from .models.legacy_runtime import app as _legacy_app
from .models import Database, ImportLogRepository, LegacyBusinessRepository, UserRepository
from .routes import auth_bp, imports_bp
from .services import AuthService, ExcelImportService
from .utils.responses import api_response
from .utils.logging import configure_logging


def create_app(config_object=None):
    app = _legacy_app
    if app.extensions.get("production_architecture_ready"):
        return app

    app.config.from_object(config_object or get_config())
    app.debug = False
    log_dir = app.config["LOG_DIR"]
    log_dir.mkdir(parents=True, exist_ok=True)
    configure_logging(app)
    database = Database(app.config["DATABASE"])
    database.initialize()
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
        return response

    @app.cli.command("create-user")
    @click.option("--username", prompt=True)
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    @click.option("--role", type=click.Choice(["admin", "user"]), default="user")
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
        if request.method not in {"GET", "HEAD", "OPTIONS"} and session.get("role") != "admin":
            if request.path.startswith(("/api/", "/imports/", "/stream/")):
                return api_response(403, "admin role required", None, 403)
            return api_response(403, "admin role required", None, 403)
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
