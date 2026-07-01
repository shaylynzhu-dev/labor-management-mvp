import calendar
from datetime import date, datetime

from flask import current_app
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.models.quota import Quota


QUOTA_TOTAL_MONTHS = 24


def init_quota_service(app):
    app.extensions["quota_sqlalchemy_engine"] = _create_engine(app.config["DATABASE"])


def _create_engine(database_path):
    return create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )


def _engine():
    configured_path = str(current_app.config["DATABASE"])
    engine = current_app.extensions.get("quota_sqlalchemy_engine")
    if engine is None or engine.url.database != configured_path:
        if engine is not None:
            engine.dispose()
        engine = _create_engine(configured_path)
        current_app.extensions["quota_sqlalchemy_engine"] = engine
    return engine


def _shift_months(value, months):
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _calculate_usage_months(start_date, end_date=None):
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    effective_end = (
        datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else date.today()
    )
    if effective_end <= start:
        return 0
    complete_months = (
        (effective_end.year - start.year) * 12 + effective_end.month - start.month
    )
    anniversary = _shift_months(start, complete_months)
    if anniversary > effective_end:
        complete_months -= 1
        anniversary = _shift_months(start, complete_months)
    return complete_months + (1 if effective_end > anniversary else 0)


def _build_quota_view(session, quota_model, assigned_person_name):
    quota = quota_model.as_dict()
    quota["person_name"] = assigned_person_name
    history = [
        dict(row)
        for row in session.execute(
            text(
                """SELECT u.*, p.name AS person_name
                   FROM quota_usages u
                   JOIN people p ON p.id=u.person_id AND p.is_deleted=0
                   WHERE u.quota_id=:id
                   ORDER BY u.start_date, u.id"""
            ),
            {"id": quota_model.id},
        ).mappings()
    ]
    for usage in history:
        usage["months"] = _calculate_usage_months(
            usage["start_date"], usage["end_date"]
        )
    active = next(
        (usage for usage in reversed(history) if usage["end_date"] is None), None
    )
    if quota_model.quota_type == "SWD":
        used_months = active["months"] if active else 0
        calculation_label = "仅当前使用人"
    else:
        used_months = sum(usage["months"] for usage in history)
        calculation_label = "全部人员累计"
    remaining_months = max(QUOTA_TOTAL_MONTHS - used_months, 0)
    if remaining_months == 0 and quota_model.status != "invalid":
        quota_model.status = "exhausted"
        quota["status"] = "exhausted"
    display_label = (
        quota_model.quota_serial
        or quota_model.approval_number
        or quota_model.quota_number
        or f"未编号 #{quota_model.id}"
    )
    if not quota["quota_number"]:
        quota["quota_number"] = display_label
    quota.update(
        history=history,
        active_usage=active,
        person_name=active["person_name"] if active else assigned_person_name,
        used_months=used_months,
        remaining_months=remaining_months,
        calculation_label=calculation_label,
        display_label=display_label,
    )
    return quota


def get_quota_detail(quota_id):
    """Load the complete quota-detail context without leaking queries to routes."""
    with Session(_engine()) as session:
        quota_model = session.scalar(
            select(Quota).where(Quota.id == quota_id, Quota.is_deleted == 0)
        )
        if quota_model is None:
            return None
        if quota_model.person_id:
            assigned_person_name = session.execute(
                text("SELECT name FROM people WHERE id=:id AND is_deleted=0"),
                {"id": quota_model.person_id},
            ).scalar_one_or_none()
        else:
            assigned_person_name = None

        quota = _build_quota_view(session, quota_model, assigned_person_name)

        related = {
            "workflows": session.execute(
                text(
                    """SELECT w.*, p.name AS person_name FROM workflow_instances w
                       JOIN people p ON p.id=w.person_id AND p.is_deleted=0
                       WHERE w.quota_id=:id AND w.is_deleted=0 ORDER BY w.id DESC"""
                ),
                {"id": quota_id},
            ).mappings().all(),
            "documents": session.execute(
                text(
                    """SELECT d.*, p.name AS person_name FROM documents d
                       JOIN people p ON p.id=d.person_id AND p.is_deleted=0
                       WHERE d.quota_id=:id AND d.is_deleted=0 ORDER BY d.id DESC"""
                ),
                {"id": quota_id},
            ).mappings().all(),
            "risks": session.execute(
                text(
                    "SELECT * FROM risks WHERE quota_id=:id AND is_deleted=0 ORDER BY id DESC"
                ),
                {"id": quota_id},
            ).mappings().all(),
            "worker_history": session.execute(
                text(
                    """SELECT h.*, p.name AS worker_name
                       FROM quota_worker_history h JOIN people p ON p.id=h.worker_id
                       WHERE h.quota_id=:id ORDER BY h.id DESC"""
                ),
                {"id": quota_id},
            ).mappings().all(),
            "people": session.execute(
                text("SELECT id, name FROM people WHERE is_deleted=0 ORDER BY name")
            ).mappings().all(),
        }
        session.commit()
    return {
        "quota": quota,
        "related": related,
        "people": related.pop("people"),
        "today": date.today().isoformat(),
    }


def get_quota_timeline(quota_id):
    with Session(_engine()) as session:
        exists = session.scalar(
            select(Quota.id).where(Quota.id == quota_id, Quota.is_deleted == 0)
        )
        if exists is None:
            return None
        rows = session.execute(
            text(
                """SELECT h.id, h.worker_id, h.start_date, h.end_date, h.status,
                          h.event_type, h.replacement_round, h.created_at,
                          p.name AS worker_name
                   FROM quota_worker_history h
                   JOIN people p ON p.id=h.worker_id AND p.is_deleted=0
                   WHERE h.quota_id=:id
                   ORDER BY COALESCE(
                       CASE WHEN h.event_type='resignation' THEN h.end_date ELSE h.start_date END,
                       h.created_at
                   ), h.id"""
            ),
            {"id": quota_id},
        ).mappings().all()

    current_index = None
    assignment_events = {"initial", "replacement", "renewal"}
    ended_values = {"ended", "closed", "inactive"}
    for index, row in enumerate(rows):
        event_type = (row["event_type"] or "").lower()
        if event_type in assignment_events:
            current_index = None if (row["status"] or "").lower() in ended_values else index
        elif event_type == "resignation" and current_index is not None:
            if rows[current_index]["worker_id"] == row["worker_id"]:
                current_index = None

    timeline = []
    for index, row in enumerate(rows):
        event_type = (row["event_type"] or "").lower()
        end_date = row["end_date"]
        if event_type in assignment_events and index != current_index and not end_date:
            for later in rows[index + 1:]:
                if later["worker_id"] == row["worker_id"] and later["event_type"] == "resignation":
                    end_date = later["end_date"] or later["start_date"]
                    break
                if later["event_type"] in assignment_events:
                    end_date = later["start_date"] or later["end_date"]
                    break
        timeline.append({
            "worker_name": row["worker_name"],
            "event_type": event_type or "initial",
            "start_date": row["start_date"],
            "end_date": end_date,
            "status": "active" if index == current_index else "ended",
            "replacement_round": row["replacement_round"] or 0,
        })
    return timeline
