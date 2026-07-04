from flask import Blueprint, current_app, g, request, session

from app.errors import AppError, VALIDATION_ERROR, service_result
from app.utils.responses import api_response
from app.utils.security import roles_required


operations_bp = Blueprint("operations", __name__, url_prefix="/api")


def _respond(result, success_message="ok"):
    if result.get("success"):
        return api_response(0, success_message, result.get("data"))
    error = result.get("error") or {"code": "UNKNOWN_ERROR", "message": "操作失败"}
    return api_response(error.get("code", "UNKNOWN_ERROR"), error.get("message", "操作失败"), {
        "error": error, "safe": True,
    })


@operations_bp.get("/notifications")
def notifications():
    return _respond(
        current_app.extensions["notification_service"].list_in_app(session.get("user_id")), "ok"
    )


@operations_bp.get("/system/worker-status")
def worker_status():
    return _respond(current_app.extensions["system_status_service"].worker_status(), "ok")


@operations_bp.get("/people/<int:person_id>/merge-candidates")
def merge_candidates(person_id):
    return _respond(
        current_app.extensions["merge_service"].duplicate_candidates_list(person_id), "ok"
    )


@operations_bp.post("/merge-workflows")
@roles_required("admin", "hr")
def create_merge_workflow():
    payload = request.get_json(silent=True) or {}
    try:
        source_id = int(payload.get("source_person_id"))
        target_id = int(payload.get("target_person_id"))
    except (TypeError, ValueError):
        return _respond(service_result(error=AppError(
            VALIDATION_ERROR, "请选择源人员和目标人员", {},
        )))
    return _respond(current_app.extensions["merge_service"].create_workflow(
        source_id, target_id, session.get("user_id"), getattr(g, "trace_id", None),
    ), "合并候选已建立，等待人工确认")


@operations_bp.post("/merge-workflows/<workflow_id>/confirm")
@roles_required("admin", "hr")
def confirm_merge(workflow_id):
    return _respond(
        current_app.extensions["merge_service"].confirm_merge(workflow_id, session.get("user_id")),
        "人员合并已确认",
    )


@operations_bp.post("/merge-workflows/<workflow_id>/rollback")
@roles_required("admin")
def rollback_merge(workflow_id):
    return _respond(
        current_app.extensions["merge_service"].rollback_merge(workflow_id, session.get("user_id")),
        "人员合并已回滚",
    )


@operations_bp.post("/events/<event_id>/replay")
@roles_required("admin")
def replay_event(event_id):
    return _respond(
        current_app.extensions["replay_engine"].replay_event(event_id), "事件已进入重放队列"
    )
