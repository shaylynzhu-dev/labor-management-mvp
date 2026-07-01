from datetime import date

from flask import current_app
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.models.quota import Quota
from app.models.legacy_runtime import build_quota_views, get_db


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


def get_quota_detail(quota_id):
    """Load the complete quota-detail context without leaking queries to routes."""
    with Session(_engine()) as session:
        quota_model = session.scalar(
            select(Quota).where(Quota.id == quota_id, Quota.is_deleted == 0)
        )
        if quota_model is None:
            return None
        quota_row = quota_model.as_dict()
        if quota_model.person_id:
            quota_row["person_name"] = session.execute(
                text("SELECT name FROM people WHERE id=:id AND is_deleted=0"),
                {"id": quota_model.person_id},
            ).scalar_one_or_none()
        else:
            quota_row["person_name"] = None

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

    # Preserve the established SWD/LD calculation behavior while moving its
    # orchestration out of the route layer.
    quota = build_quota_views(get_db(), [quota_row])[0]
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
