from flask import Blueprint, current_app, flash, redirect, request, send_file, session, url_for

from app.services.import_service import parse_mapping
from app.utils.responses import api_response
from app.utils.security import roles_required


imports_bp = Blueprint("production_imports", __name__, url_prefix="/imports")


def _import(kind, view):
    service = current_app.extensions["excel_import_service"]
    try:
        result = service.import_file(
            kind,
            request.files.get("file"),
            session.get("user_id"),
            parse_mapping(request.form.get("mapping")),
        )
    except Exception as error:
        current_app.logger.exception("Excel import failed: type=%s", kind)
        if request.args.get("format") == "json":
            return api_response(422, str(error), None, 422)
        flash(str(error), "error")
        return redirect(url_for("index", view=view))
    if result["failed"]:
        current_app.logger.warning(
            "Excel import completed with row errors: type=%s file=%s errors=%s",
            kind, getattr(request.files.get("file"), "filename", ""), result["errors"],
        )
    if request.args.get("format") == "json":
        return api_response(0, "导入完成", result)
    flash(
        f"导入完成：成功 {result['success']}，跳过 {result['skipped']}，失败 {result['failed']}。",
        "success" if result["failed"] == 0 else "error",
    )
    return redirect(url_for("index", view=view, import_log=result["log_id"]))


@imports_bp.post("/person")
@roles_required("admin", "hr")
def person_import():
    return _import("person", "people")


@imports_bp.post("/quota")
@roles_required("admin", "hr")
def quota_import():
    return _import("quota", "quotas")


@imports_bp.post("/contract")
@roles_required("admin", "hr")
def contract_import():
    return _import("contract", "contracts")


@imports_bp.post("/lifecycle")
@roles_required("admin", "hr")
def lifecycle_import():
    return _import("lifecycle", "quotas")


@imports_bp.get("/template/<kind>")
def download_template(kind):
    try:
        output = current_app.extensions["excel_import_service"].template(kind)
    except ValueError as error:
        return api_response(404, str(error), None, 404)
    return send_file(
        output,
        as_attachment=True,
        download_name=f"{kind}_import_template.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@imports_bp.get("/logs")
@roles_required("admin", "hr")
def logs():
    rows = current_app.extensions["import_log_repository"].list_recent()
    data = [dict(row) for row in rows]
    return api_response(0, "ok", data)
