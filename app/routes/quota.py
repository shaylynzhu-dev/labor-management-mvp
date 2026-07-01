from flask import Blueprint, abort, render_template

from app.services.quota_service import get_quota_detail, get_quota_timeline
from app.utils.responses import api_response


quota_bp = Blueprint("quota", __name__, url_prefix="/quota")
quota_legacy_bp = Blueprint("quota_legacy", __name__)
quota_api_bp = Blueprint("quota_api", __name__)


def _render_detail(quota_id):
    context = get_quota_detail(quota_id)
    if context is None:
        abort(404)
    return render_template("quota/detail.html", **context)


@quota_bp.get("/<int:quota_id>")
def detail(quota_id):
    return _render_detail(quota_id)


@quota_legacy_bp.get("/quotas/<int:quota_id>")
def legacy_detail(quota_id):
    """Keep existing bookmarks and integrations working during migration."""
    return _render_detail(quota_id)


@quota_api_bp.get("/api/quota/<int:quota_id>/timeline")
def timeline(quota_id):
    items = get_quota_timeline(quota_id)
    if items is None:
        return api_response(404, "名额不存在。", None, 404)
    return api_response(0, "ok", items)
