from datetime import date, datetime, timedelta
import calendar
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import uuid

from flask import (
    Flask, abort, flash, g, jsonify, redirect, render_template, request, session,
    send_from_directory, url_for,
)
from werkzeug.utils import secure_filename

from app.utils.responses import api_response
from app.utils.excel import validate_excel_shape, validate_excel_upload
from app.services.dispatch_engine import (
    CONTRACT_LIFECYCLE, QUOTA_STATUSES, create_replacement_cycle,
    normalize_contract_status, quota_replacement_limit, register_worker_entry,
    transition_contract, trigger_worker_departure,
)
from app.services.person_profile_service import (
    DOCUMENT_TYPES, calculate_renewal_alert_dates, get_person_profile, save_person_document_batch,
    suggest_case_for_document, suggest_person_by_filename,
)


BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATABASE = Path(
    os.environ.get("LABOUR_OS_DATABASE_PATH", BASE_DIR / "labor.db")
).expanduser().resolve()
SCHEMA_FILE = BASE_DIR / "schema.sql"
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

app = Flask(
    __name__,
    root_path=str(BASE_DIR),
    template_folder="templates",
    static_folder="static",
)
app.config.update(
    SECRET_KEY=os.environ.get("LABOUR_OS_SECRET_KEY", "change-this-in-production"),
    DATABASE=DATABASE,
    UPLOAD_FOLDER=UPLOAD_DIR,
    MAX_CONTENT_LENGTH=1300 * 1024 * 1024,
    TEMPLATES_AUTO_RELOAD=True,
    SEND_FILE_MAX_AGE_DEFAULT=0,
)
CONTRACT_STATUSES = CONTRACT_LIFECYCLE
QUOTA_TYPES = ("SWD", "LD")
QUOTA_TOTAL_MONTHS = 24
EXPIRY_CHECKS = ("待检查", "通过", "不通过")
RENEWAL_STATUSES = ("可续", "不可续", "风险")
WORKFLOW_STEPS = CONTRACT_LIFECYCLE
TASK_STATUSES = ("待办", "进行中", "已完成", "逾期")
ALLOWED_DOCUMENT_EXTENSIONS = {"txt", "png", "jpg", "jpeg", "tif", "tiff", "pdf"}
STANDARD_TABLES = {"person", "quota", "contract", "event", "risk"}


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def _quoted_identifier(value):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("Invalid SQL identifier")
    return f'"{value}"'


def _standard_table(table):
    if table not in STANDARD_TABLES:
        raise ValueError(f"Unsupported table: {table}")
    return _quoted_identifier(table)


def db_insert(table, values):
    if not values:
        raise ValueError("Insert values are required")
    values = dict(values)
    if table == "contract":
        forbidden_dates = {
            "entry_date", "arrival_date", "contract_start_date", "contract_end_date",
            "start_date", "end_date",
        }
        if forbidden_dates.intersection(values):
            raise ValueError("Contract dates must come from an arrival event")
        values["status"] = normalize_contract_status(values.get("status") or "制作合同")
        if values["status"] not in CONTRACT_STATUSES:
            raise ValueError("Invalid contract status")
        if values["status"] != "制作合同":
            raise ValueError("New contracts must start at 制作合同")
    if table == "event":
        values["event_date"] = values.get("event_date") or date.today().isoformat()
    table_sql = _standard_table(table)
    columns = list(values)
    column_sql = ", ".join(_quoted_identifier(column) for column in columns)
    placeholders = ", ".join("?" for _ in columns)
    db = get_db()
    cursor = db.execute(
        f"INSERT INTO {table_sql} ({column_sql}) VALUES ({placeholders})",
        tuple(values[column] for column in columns),
    )
    record_id = cursor.lastrowid
    if table in {"person", "quota", "contract"}:
        db.execute(
            """INSERT INTO event
               (event_type, person_id, quota_id, contract_id, description, event_date)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                f"{table}_created",
                record_id if table == "person" else values.get("person_id") or values.get("user_id"),
                record_id if table == "quota" else values.get("quota_id"),
                record_id if table == "contract" else None,
                f"{table} record created",
                date.today().isoformat(),
            ),
        )
    if table == "event" and values.get("event_type") in {"入境", "工人入境"}:
        apply_arrival_event(db, values.get("person_id"), values["event_date"])
    db.commit()
    return record_id


def db_query(table, filters=None, order_by="id", limit=None):
    table_sql = _standard_table(table)
    filters = filters or {}
    sql = f"SELECT * FROM {table_sql} WHERE is_deleted=0"
    parameters = []
    if filters:
        sql += " AND " + " AND ".join(
            f"{_quoted_identifier(column)} = ?" for column in filters
        )
        parameters.extend(filters.values())
    if order_by:
        sql += f" ORDER BY {_quoted_identifier(order_by)}"
    if limit is not None:
        sql += " LIMIT ?"
        parameters.append(int(limit))
    return get_db().execute(sql, tuple(parameters)).fetchall()


def db_update(table, values, filters):
    if not values or not filters:
        raise ValueError("Update values and filters are required")
    values = dict(values)
    contract_date_fields = {
        "entry_date", "arrival_date", "contract_start_date", "contract_end_date",
        "start_date", "end_date",
    }
    if table == "contract" and contract_date_fields.intersection(values):
        raise ValueError("Contract dates must come from an arrival event")
    if table == "contract" and "status" in values:
        values["status"] = normalize_contract_status(values["status"])
        if values["status"] not in CONTRACT_STATUSES:
            raise ValueError("Invalid contract status")
    table_sql = _standard_table(table)
    db = get_db()
    record_ids = [
        row["id"] for row in db_query(table, filters=filters, order_by=None)
    ]
    if table == "contract" and "status" in values:
        for row in db_query(table, filters=filters, order_by=None):
            current = normalize_contract_status(row["status"])
            target = values["status"]
            if current == target:
                continue
            if current == "完成合约" or CONTRACT_STATUSES.index(target) != CONTRACT_STATUSES.index(current) + 1:
                raise ValueError("Contract lifecycle transitions must be sequential")
            if target == "工人入境":
                raise ValueError("Worker entry must come from an entry event")
    set_sql = ", ".join(f"{_quoted_identifier(column)} = ?" for column in values)
    where_sql = "is_deleted=0 AND " + " AND ".join(
        f"{_quoted_identifier(column)} = ?" for column in filters
    )
    cursor = db.execute(
        f"UPDATE {table_sql} SET {set_sql} WHERE {where_sql}",
        tuple(values.values()) + tuple(filters.values()),
    )
    if table in {"person", "quota", "contract"}:
        for record_id in record_ids:
            db.execute(
                """INSERT INTO event
                   (event_type, person_id, quota_id, contract_id, description, event_date)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    f"{table}_updated",
                    record_id if table == "person" else values.get("person_id") or values.get("user_id"),
                    record_id if table == "quota" else values.get("quota_id"),
                    record_id if table == "contract" else None,
                    f"{table} record updated",
                    date.today().isoformat(),
                ),
            )
    db.commit()
    return cursor.rowcount


def refresh_standard_risks(db=None):
    db = db or get_db()
    db.execute(
        """INSERT OR IGNORE INTO risk
           (person_id, quota_id, risk_type, status, description)
           SELECT user_id, id, 'quota_expired', 'open',
                  'Quota expired on ' || end_date
           FROM quota
           WHERE is_deleted=0 AND end_date IS NOT NULL
             AND date(end_date) < date('now', 'localtime')"""
    )


def record_standard_event(
    db, event_type, description, person_id=None, quota_id=None,
    contract_id=None, severity="normal",
):
    db.execute(
        """INSERT INTO event
           (event_type, person_id, quota_id, contract_id,
            description, severity, event_date)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            event_type, person_id, quota_id, contract_id,
            description, severity, date.today().isoformat(),
        ),
    )


def read_excel_upload(uploaded, required_columns):
    validate_excel_upload(uploaded)
    try:
        import pandas as pd
    except ImportError as error:
        raise RuntimeError("缺少 pandas/openpyxl，请先安装 requirements.txt。") from error
    try:
        frame = pd.read_excel(uploaded, engine="openpyxl", dtype=object)
    except Exception as error:
        raise ValueError("Excel 文件无法读取或格式损坏。") from error
    validate_excel_shape(frame)
    frame.columns = [str(column).strip() for column in frame.columns]
    missing = [column for column in required_columns if column not in frame.columns]
    if missing:
        raise ValueError(f"Excel 缺少必填列：{', '.join(missing)}")
    if len(frame.index) > 5000:
        raise ValueError("单次导入最多支持 5000 行。")
    return frame.dropna(how="all"), pd


def excel_text(value, pd):
    if value is None or pd.isna(value):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    return text or None


def excel_last4(value, pd, row_number, column_name):
    text = excel_text(value, pd)
    if text is None:
        return None
    if text.isdigit() and len(text) < 4:
        text = text.zfill(4)
    if len(text) != 4 or not text.isdigit():
        raise ValueError(f"第 {row_number} 行“{column_name}”必须为4位数字。")
    return text


def excel_date(value, pd, row_number, column_name):
    if value is None or pd.isna(value):
        return None
    try:
        return pd.to_datetime(value, errors="raise").date().isoformat()
    except Exception as error:
        raise ValueError(f"第 {row_number} 行“{column_name}”日期无效。") from error


def excel_import_result(label, view, imported, skipped, failed, errors):
    payload = {
        "type": label,
        "success": imported,
        "skipped": skipped,
        "failed": failed,
        "errors": errors[:20],
    }
    if request.args.get("format") == "json":
        return api_response(0, "导入完成", payload)
    category = "success" if failed == 0 else "error"
    flash(
        f"{label} 导入完成：成功 {imported}，跳过 {skipped}，失败 {failed}。",
        category,
    )
    if errors:
        flash("；".join(errors[:3]), "error")
    return redirect(url_for("index", view=view))


def _column_expression(columns, name, fallback="NULL"):
    return name if name in columns else fallback


def ensure_legacy_dispatch_schema(db):
    quota_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(quotas)").fetchall()
    }
    quota_additions = {
        "usage_count": "INTEGER NOT NULL DEFAULT 0",
        "replacement_count": "INTEGER NOT NULL DEFAULT 0",
        "max_replacement_count": "INTEGER NOT NULL DEFAULT 1",
        "status": "TEXT NOT NULL DEFAULT 'active'",
    }
    for name, definition in quota_additions.items():
        if name not in quota_columns:
            db.execute(f"ALTER TABLE quotas ADD COLUMN {name} {definition}")
    db.execute(
        """UPDATE quotas SET
               max_replacement_count=CASE quota_type WHEN 'LD' THEN 2 ELSE 1 END,
               usage_count=COALESCE(usage_count, 0),
               replacement_count=COALESCE(replacement_count, 0),
               status=CASE
                   WHEN status='invalid' THEN 'invalid'
                   WHEN person_id IS NOT NULL THEN 'in_use'
                   ELSE 'active' END"""
    )

    columns = {
        row["name"] for row in db.execute("PRAGMA table_info(contracts)").fetchall()
    }
    table_sql = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='contracts'"
    ).fetchone()[0]
    required = {"entry_date", "quota_id", "cycle_index", "parent_contract_id", "is_replaced"}
    if required.issubset(columns) and "制作合同" in table_sql:
        db.execute("CREATE INDEX IF NOT EXISTS idx_contracts_quota_cycle ON contracts(quota_id,cycle_index)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_contracts_parent ON contracts(parent_contract_id)")
        return
    db.commit()
    db.execute("PRAGMA foreign_keys=OFF")
    try:
        db.execute("BEGIN")
        db.execute(
            """CREATE TABLE contracts_dispatch_new (
                contract_id TEXT PRIMARY KEY COLLATE NOCASE,
                person_name TEXT NOT NULL,
                company TEXT NOT NULL,
                entry_date TEXT,
                arrival_date TEXT,
                contract_start_date TEXT,
                contract_end_date TEXT,
                start_date TEXT,
                end_date TEXT,
                status TEXT NOT NULL DEFAULT '制作合同'
                    CHECK(status IN ('制作合同','交接香港同事','交表香港入境处',
                                     '批出入境签证','工人入境','完成合约')),
                person_id INTEGER,
                quota_id INTEGER,
                cycle_index INTEGER NOT NULL DEFAULT 1 CHECK(cycle_index>=1),
                parent_contract_id TEXT,
                is_replaced INTEGER NOT NULL DEFAULT 0 CHECK(is_replaced IN (0,1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK(entry_date IS NULL OR date(contract_start_date)=date(entry_date)),
                CHECK(arrival_date IS NULL OR date(arrival_date)=date(entry_date)),
                CHECK(start_date IS NULL OR date(start_date)=date(contract_start_date)),
                CHECK(end_date IS NULL OR date(end_date)=date(contract_end_date)),
                FOREIGN KEY(person_id) REFERENCES people(id) ON DELETE SET NULL,
                FOREIGN KEY(quota_id) REFERENCES quotas(id) ON DELETE SET NULL,
                FOREIGN KEY(parent_contract_id) REFERENCES contracts(contract_id) ON DELETE RESTRICT
            )"""
        )
        entry_expr = _column_expression(columns, "entry_date", _column_expression(columns, "arrival_date"))
        arrival_expr = _column_expression(columns, "arrival_date", entry_expr)
        quota_expr = _column_expression(
            columns, "quota_id",
            "(SELECT q.id FROM quotas q WHERE q.person_id=contracts.person_id LIMIT 1)",
        )
        cycle_expr = _column_expression(columns, "cycle_index", "1")
        parent_expr = _column_expression(columns, "parent_contract_id")
        replaced_expr = _column_expression(columns, "is_replaced", "0")
        db.execute(
            f"""INSERT INTO contracts_dispatch_new
                (contract_id,person_name,company,entry_date,arrival_date,
                 contract_start_date,contract_end_date,start_date,end_date,status,
                 person_id,quota_id,cycle_index,parent_contract_id,is_replaced,created_at)
                SELECT contract_id,person_name,company,{entry_expr},{arrival_expr},
                       contract_start_date,contract_end_date,start_date,end_date,
                       CASE status
                           WHEN '登记' THEN '制作合同' WHEN '付款' THEN '制作合同'
                           WHEN '签证中' THEN '交表香港入境处'
                           WHEN '已签' THEN '批出入境签证'
                           WHEN '入境' THEN '工人入境' WHEN '完成' THEN '完成合约'
                           WHEN 'active' THEN '制作合同' WHEN 'completed' THEN '完成合约'
                           ELSE status END,
                       person_id,{quota_expr},{cycle_expr},{parent_expr},{replaced_expr},created_at
                FROM contracts"""
        )
        db.execute("DROP TABLE contracts")
        db.execute("ALTER TABLE contracts_dispatch_new RENAME TO contracts")
        db.execute("CREATE INDEX IF NOT EXISTS idx_contracts_person ON contracts(person_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_contracts_quota_cycle ON contracts(quota_id,cycle_index)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_contracts_parent ON contracts(parent_contract_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_contracts_end_date ON contracts(end_date)")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.execute("PRAGMA foreign_keys=ON")


def ensure_standard_dispatch_schema(db):
    quota_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(quota)").fetchall()
    }
    for name, definition in {
        "usage_count": "INTEGER NOT NULL DEFAULT 0",
        "replacement_count": "INTEGER NOT NULL DEFAULT 0",
        "max_replacement_count": "INTEGER NOT NULL DEFAULT 1",
        "status": "TEXT NOT NULL DEFAULT 'active'",
    }.items():
        if name not in quota_columns:
            db.execute(f"ALTER TABLE quota ADD COLUMN {name} {definition}")
    db.execute(
        """UPDATE quota SET
               max_replacement_count=CASE quota_type WHEN 'LD' THEN 2 ELSE 1 END,
               usage_count=COALESCE(usage_count,0),
               replacement_count=COALESCE(replacement_count,0),
               status=CASE WHEN status='invalid' THEN 'invalid'
                           WHEN user_id IS NOT NULL THEN 'in_use' ELSE 'active' END"""
    )
    columns = {
        row["name"] for row in db.execute("PRAGMA table_info(contract)").fetchall()
    }
    table_sql = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='contract'"
    ).fetchone()[0]
    required = {"entry_date", "cycle_index", "parent_contract_id", "is_replaced"}
    if required.issubset(columns) and "制作合同" in table_sql:
        db.execute("CREATE INDEX IF NOT EXISTS idx_contract_quota_cycle ON contract(quota_id,cycle_index)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_contract_parent ON contract(parent_contract_id)")
        return
    db.commit()
    db.execute("PRAGMA foreign_keys=OFF")
    try:
        db.execute("BEGIN")
        db.execute(
            """CREATE TABLE contract_dispatch_new (
                id INTEGER PRIMARY KEY,
                contract_no TEXT NULL,
                company_name TEXT NOT NULL,
                person_id INTEGER NULL,
                quota_id INTEGER NULL,
                entry_date DATE NULL,
                arrival_date DATE NULL,
                contract_start_date DATE NULL,
                contract_end_date DATE NULL,
                start_date DATE NULL,
                end_date DATE NULL,
                cycle_index INTEGER NOT NULL DEFAULT 1 CHECK(cycle_index>=1),
                parent_contract_id INTEGER NULL,
                is_replaced INTEGER NOT NULL DEFAULT 0 CHECK(is_replaced IN (0,1)),
                status TEXT NOT NULL DEFAULT '制作合同'
                    CHECK(status IN ('制作合同','交接香港同事','交表香港入境处',
                                     '批出入境签证','工人入境','完成合约')),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                CHECK(entry_date IS NULL OR date(contract_start_date)=date(entry_date)),
                CHECK(arrival_date IS NULL OR date(arrival_date)=date(entry_date)),
                CHECK(contract_start_date IS NULL OR date(start_date)=date(contract_start_date)),
                CHECK(contract_end_date IS NULL OR date(end_date)=date(contract_end_date)),
                FOREIGN KEY(person_id) REFERENCES person(id) ON DELETE SET NULL,
                FOREIGN KEY(quota_id) REFERENCES quota(id) ON DELETE SET NULL,
                FOREIGN KEY(parent_contract_id) REFERENCES contract(id) ON DELETE RESTRICT
            )"""
        )
        entry_expr = _column_expression(columns, "entry_date", _column_expression(columns, "arrival_date"))
        arrival_expr = _column_expression(columns, "arrival_date", entry_expr)
        cycle_expr = _column_expression(columns, "cycle_index", "1")
        parent_expr = _column_expression(columns, "parent_contract_id")
        replaced_expr = _column_expression(columns, "is_replaced", "0")
        db.execute(
            f"""INSERT INTO contract_dispatch_new
                (id,contract_no,company_name,person_id,quota_id,entry_date,arrival_date,
                 contract_start_date,contract_end_date,start_date,end_date,cycle_index,
                 parent_contract_id,is_replaced,status,created_at)
                SELECT id,contract_no,company_name,person_id,quota_id,{entry_expr},{arrival_expr},
                       contract_start_date,contract_end_date,start_date,end_date,{cycle_expr},
                       {parent_expr},{replaced_expr},
                       CASE status
                           WHEN '登记' THEN '制作合同' WHEN '付款' THEN '制作合同'
                           WHEN '签证中' THEN '交表香港入境处'
                           WHEN '已签' THEN '批出入境签证'
                           WHEN '入境' THEN '工人入境' WHEN '完成' THEN '完成合约'
                           WHEN 'active' THEN '制作合同' WHEN 'completed' THEN '完成合约'
                           ELSE status END,created_at
                FROM contract"""
        )
        db.execute("DROP TABLE contract")
        db.execute("ALTER TABLE contract_dispatch_new RENAME TO contract")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_contract_contract_no ON contract(contract_no) WHERE contract_no IS NOT NULL")
        db.execute("CREATE INDEX IF NOT EXISTS idx_contract_quota_cycle ON contract(quota_id,cycle_index)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_contract_parent ON contract(parent_contract_id)")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.execute("PRAGMA foreign_keys=ON")


def initialize_standard_schema(db):
    db.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
    ensure_standard_dispatch_schema(db)
    standard_contract_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(contract)").fetchall()
    }
    for column in ("entry_date", "arrival_date", "contract_start_date", "contract_end_date"):
        if column not in standard_contract_columns:
            db.execute(f"ALTER TABLE contract ADD COLUMN {column} DATE NULL")
    standard_event_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(event)").fetchall()
    }
    if "event_date" not in standard_event_columns:
        db.execute("ALTER TABLE event ADD COLUMN event_date DATE")
        db.execute("UPDATE event SET event_date=date(created_at) WHERE event_date IS NULL")
    db.execute(
        """UPDATE contract SET status=CASE status
               WHEN 'active' THEN '制作合同'
               WHEN 'completed' THEN '完成合约'
               ELSE status END"""
    )
    db.executescript(
        """
        INSERT INTO person
            (id, name, gender, company_name, introducer,
             id_last4, hk_macao_last4, created_at)
        SELECT id, name, COALESCE(NULLIF(gender, ''), '未填写'), company_name,
               introducer, id_last4, permit_last4, created_at
        FROM people
        WHERE 1
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, gender=excluded.gender,
            company_name=excluded.company_name, introducer=excluded.introducer,
            id_last4=excluded.id_last4,
            hk_macao_last4=excluded.hk_macao_last4;

        INSERT INTO quota
            (id, quota_type, company_name, approval_no, quota_no,
             user_id, start_date, end_date, usage_count, replacement_count,
             max_replacement_count, status, created_at)
        SELECT id, quota_type, company_name, approval_number,
               COALESCE(quota_serial, quota_number), person_id,
               start_date, expiry_date, usage_count, replacement_count,
               max_replacement_count, status, created_at
        FROM quotas
        WHERE 1
        ON CONFLICT(id) DO UPDATE SET
            quota_type=excluded.quota_type, company_name=excluded.company_name,
            approval_no=excluded.approval_no, quota_no=excluded.quota_no,
            user_id=excluded.user_id, start_date=excluded.start_date,
            end_date=excluded.end_date, usage_count=excluded.usage_count,
            replacement_count=excluded.replacement_count,
            max_replacement_count=excluded.max_replacement_count,
            status=excluded.status;

        INSERT OR IGNORE INTO contract
            (contract_no, company_name, person_id, quota_id,
             entry_date, arrival_date, contract_start_date, contract_end_date,
             start_date, end_date, cycle_index, parent_contract_id,
             is_replaced, status, created_at)
        SELECT c.contract_id, c.company, c.person_id, c.quota_id,
               c.entry_date, c.arrival_date, c.contract_start_date, c.contract_end_date,
               c.start_date, c.end_date, c.cycle_index,
               (SELECT p.id FROM contract p WHERE p.contract_no=c.parent_contract_id),
               c.is_replaced, c.status, c.created_at
        FROM contracts c;

        UPDATE contract SET
            company_name=(SELECT c.company FROM contracts c
                          WHERE c.contract_id=contract.contract_no),
            person_id=(SELECT c.person_id FROM contracts c
                       WHERE c.contract_id=contract.contract_no),
            quota_id=(SELECT c.quota_id FROM contracts c
                      WHERE c.contract_id=contract.contract_no),
            entry_date=(SELECT c.entry_date FROM contracts c
                        WHERE c.contract_id=contract.contract_no),
            arrival_date=(SELECT c.arrival_date FROM contracts c
                          WHERE c.contract_id=contract.contract_no),
            contract_start_date=(SELECT c.contract_start_date FROM contracts c
                                 WHERE c.contract_id=contract.contract_no),
            contract_end_date=(SELECT c.contract_end_date FROM contracts c
                               WHERE c.contract_id=contract.contract_no),
            start_date=(SELECT c.start_date FROM contracts c
                        WHERE c.contract_id=contract.contract_no),
            end_date=(SELECT c.end_date FROM contracts c
                      WHERE c.contract_id=contract.contract_no),
            cycle_index=(SELECT c.cycle_index FROM contracts c
                         WHERE c.contract_id=contract.contract_no),
            parent_contract_id=(SELECT p.id FROM contract p
                                WHERE p.contract_no=(SELECT c.parent_contract_id
                                  FROM contracts c WHERE c.contract_id=contract.contract_no)),
            is_replaced=(SELECT c.is_replaced FROM contracts c
                         WHERE c.contract_id=contract.contract_no),
            status=(SELECT c.status FROM contracts c
                    WHERE c.contract_id=contract.contract_no)
        WHERE contract_no IN (SELECT contract_id FROM contracts);

        INSERT OR IGNORE INTO event
            (id, event_type, person_id, description, event_date, created_at)
        SELECT id, event_type, person_id, note, event_date, created_at FROM events;

        INSERT OR IGNORE INTO risk
            (id, person_id, quota_id, contract_id, risk_type,
             status, description, created_at)
        SELECT r.id, r.person_id, r.quota_id,
               (SELECT c.id FROM contract c WHERE c.contract_no=r.contract_id),
               r.risk_type,
               CASE r.status WHEN '开放' THEN 'open' ELSE 'resolved' END,
               r.reason, r.created_at
        FROM risks r;

        DROP TRIGGER IF EXISTS sync_people_insert;
        DROP TRIGGER IF EXISTS sync_people_update;
        DROP TRIGGER IF EXISTS sync_quotas_insert;
        DROP TRIGGER IF EXISTS sync_quotas_update;
        DROP TRIGGER IF EXISTS sync_contracts_insert;
        DROP TRIGGER IF EXISTS sync_contracts_update;
        DROP TRIGGER IF EXISTS sync_events_insert;
        DROP TRIGGER IF EXISTS sync_risks_insert;
        DROP TRIGGER IF EXISTS sync_risks_update;

        CREATE TRIGGER sync_people_insert AFTER INSERT ON people BEGIN
            INSERT INTO person
                (id,name,gender,company_name,introducer,id_last4,hk_macao_last4,created_at)
            VALUES
                (NEW.id,NEW.name,COALESCE(NULLIF(NEW.gender,''),'未填写'),
                 NEW.company_name,NEW.introducer,NEW.id_last4,NEW.permit_last4,NEW.created_at)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,gender=excluded.gender,
                company_name=excluded.company_name,introducer=excluded.introducer,
                id_last4=excluded.id_last4,hk_macao_last4=excluded.hk_macao_last4;
        END;

        CREATE TRIGGER sync_people_update AFTER UPDATE ON people BEGIN
            UPDATE person SET name=NEW.name,
                gender=COALESCE(NULLIF(NEW.gender,''),'未填写'),
                company_name=NEW.company_name,introducer=NEW.introducer,
                id_last4=NEW.id_last4,hk_macao_last4=NEW.permit_last4
            WHERE id=NEW.id;
        END;

        CREATE TRIGGER sync_quotas_insert AFTER INSERT ON quotas BEGIN
            INSERT INTO quota
                (id,quota_type,company_name,approval_no,quota_no,user_id,
                 start_date,end_date,usage_count,replacement_count,
                 max_replacement_count,status,created_at)
            VALUES
                (NEW.id,NEW.quota_type,NEW.company_name,NEW.approval_number,
                 COALESCE(NEW.quota_serial,NEW.quota_number),NEW.person_id,
                 NEW.start_date,NEW.expiry_date,NEW.usage_count,NEW.replacement_count,
                 NEW.max_replacement_count,NEW.status,NEW.created_at)
            ON CONFLICT(id) DO UPDATE SET
                quota_type=excluded.quota_type,company_name=excluded.company_name,
                approval_no=excluded.approval_no,quota_no=excluded.quota_no,
                user_id=excluded.user_id,start_date=excluded.start_date,
                end_date=excluded.end_date,usage_count=excluded.usage_count,
                replacement_count=excluded.replacement_count,
                max_replacement_count=excluded.max_replacement_count,
                status=excluded.status;
        END;

        CREATE TRIGGER sync_quotas_update AFTER UPDATE ON quotas BEGIN
            UPDATE quota SET quota_type=NEW.quota_type,
                company_name=NEW.company_name,approval_no=NEW.approval_number,
                quota_no=COALESCE(NEW.quota_serial,NEW.quota_number),
                user_id=NEW.person_id,start_date=NEW.start_date,
                end_date=NEW.expiry_date,usage_count=NEW.usage_count,
                replacement_count=NEW.replacement_count,
                max_replacement_count=NEW.max_replacement_count,status=NEW.status
            WHERE id=NEW.id;
        END;

        CREATE TRIGGER sync_contracts_insert AFTER INSERT ON contracts BEGIN
            INSERT OR IGNORE INTO contract
                (contract_no,company_name,person_id,quota_id,
                 entry_date,arrival_date,contract_start_date,contract_end_date,
                 start_date,end_date,cycle_index,parent_contract_id,is_replaced,
                 status,created_at)
            VALUES
                (NEW.contract_id,NEW.company,NEW.person_id,
                 NEW.quota_id,NEW.entry_date,NEW.arrival_date,
                 NEW.contract_start_date,NEW.contract_end_date,
                 NEW.start_date,NEW.end_date,NEW.cycle_index,
                 (SELECT id FROM contract WHERE contract_no=NEW.parent_contract_id),
                 NEW.is_replaced,NEW.status,NEW.created_at);
        END;

        CREATE TRIGGER sync_contracts_update AFTER UPDATE ON contracts BEGIN
            UPDATE contract SET company_name=NEW.company,person_id=NEW.person_id,
                quota_id=NEW.quota_id,entry_date=NEW.entry_date,arrival_date=NEW.arrival_date,
                contract_start_date=NEW.contract_start_date,
                contract_end_date=NEW.contract_end_date,
                start_date=NEW.start_date,end_date=NEW.end_date,
                cycle_index=NEW.cycle_index,
                parent_contract_id=(SELECT id FROM contract WHERE contract_no=NEW.parent_contract_id),
                is_replaced=NEW.is_replaced,status=NEW.status
            WHERE contract_no=NEW.contract_id;
        END;

        CREATE TRIGGER sync_events_insert AFTER INSERT ON events BEGIN
            INSERT OR IGNORE INTO event
                (id,event_type,person_id,quota_id,contract_id,description,event_date,created_at)
            VALUES (
                NEW.id,NEW.event_type,NEW.person_id,
                (SELECT quota_id FROM contracts WHERE person_id=NEW.person_id
                 ORDER BY cycle_index DESC,created_at DESC LIMIT 1),
                (SELECT id FROM contract WHERE person_id=NEW.person_id
                 ORDER BY id DESC LIMIT 1),
                NEW.note,NEW.event_date,NEW.created_at
            );
        END;

        CREATE TRIGGER sync_risks_insert AFTER INSERT ON risks BEGIN
            INSERT OR IGNORE INTO risk
                (id,person_id,quota_id,contract_id,risk_type,status,description,created_at)
            VALUES
                (NEW.id,NEW.person_id,NEW.quota_id,
                 (SELECT id FROM contract WHERE contract_no=NEW.contract_id),
                 NEW.risk_type,CASE NEW.status WHEN '开放' THEN 'open' ELSE 'resolved' END,
                 NEW.reason,NEW.created_at);
        END;

        CREATE TRIGGER sync_risks_update AFTER UPDATE ON risks BEGIN
            UPDATE risk SET person_id=NEW.person_id,quota_id=NEW.quota_id,
                contract_id=(SELECT id FROM contract WHERE contract_no=NEW.contract_id),
                risk_type=NEW.risk_type,
                status=CASE NEW.status WHEN '开放' THEN 'open' ELSE 'resolved' END,
                description=NEW.reason
            WHERE id=NEW.id;
        END;
        """
    )
    refresh_standard_risks(db)


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


SOFT_DELETE_TABLES = (
    "people", "quotas", "contracts", "events", "renewals",
    "workflow_instances", "documents", "person_cases", "person_documents", "lifecycle_nodes", "risks", "tasks",
    "person", "quota", "contract", "event", "risk",
)

SOFT_DELETE_RESOURCES = {
    "people": ("people", "id", "person", "id"),
    "quotas": ("quotas", "id", "quota", "id"),
    "contracts": ("contracts", "contract_id", "contract", "contract_no"),
    "events": ("events", "id", "event", "id"),
    "renewals": ("renewals", "id", None, None),
    "workflows": ("workflow_instances", "id", None, None),
    "documents": ("documents", "id", None, None),
    "person_documents": ("person_documents", "id", None, None),
    "person_cases": ("person_cases", "id", None, None),
    "lifecycle": ("lifecycle_nodes", "id", None, None),
    "risks": ("risks", "id", "risk", "id"),
    "tasks": ("tasks", "id", None, None),
}
SOFT_DELETE_ALIASES = {
    "person": "people", "persons": "people",
    "quota": "quotas", "contract": "contracts",
    "event": "events", "renewal": "renewals", "workflow": "workflows",
    "document": "documents", "risk": "risks", "task": "tasks",
    "person_document": "person_documents", "person-documents": "person_documents",
    "person_case": "person_cases", "person-cases": "person_cases",
    "workflow_instances": "workflows", "lifecycle-node": "lifecycle",
    "lifecycle_nodes": "lifecycle",
}


def ensure_soft_delete_schema(db):
    existing_tables = {
        row["name"]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    for table in SOFT_DELETE_TABLES:
        if table not in existing_tables:
            continue
        columns = {
            row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if "is_deleted" not in columns:
            db.execute(
                f"ALTER TABLE {table} ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0"
            )
        if "deleted_at" not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN deleted_at DATETIME NULL")
        if "deleted_by" not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN deleted_by INTEGER NULL")
        db.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_soft_delete ON {table}(is_deleted)"
        )


def init_db():
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            gender TEXT CHECK(gender IN ('男','女')),
            company_name TEXT,
            introducer TEXT,
            id_last4 TEXT CHECK(id_last4 IS NULL OR length(id_last4) = 4),
            permit_last4 TEXT CHECK(permit_last4 IS NULL OR length(permit_last4) = 4),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS quotas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quota_number TEXT UNIQUE,
            company_name TEXT NOT NULL,
            quota_type TEXT NOT NULL DEFAULT 'SWD' CHECK(quota_type IN ('SWD','LD')),
            approval_number TEXT,
            quota_serial TEXT,
            person_id INTEGER UNIQUE,
            start_date TEXT,
            expiry_date TEXT,
            usage_count INTEGER NOT NULL DEFAULT 0,
            replacement_count INTEGER NOT NULL DEFAULT 0,
            max_replacement_count INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            event_date TEXT NOT NULL DEFAULT CURRENT_DATE,
            created_at TEXT NOT NULL,
            FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS contracts (
            contract_id TEXT PRIMARY KEY COLLATE NOCASE,
            person_name TEXT NOT NULL,
            company TEXT NOT NULL,
            entry_date TEXT,
            arrival_date TEXT,
            contract_start_date TEXT,
            contract_end_date TEXT,
            start_date TEXT,
            end_date TEXT,
            status TEXT NOT NULL DEFAULT '制作合同'
                CHECK(status IN ('制作合同','交接香港同事','交表香港入境处',
                                 '批出入境签证','工人入境','完成合约')),
            person_id INTEGER,
            quota_id INTEGER,
            cycle_index INTEGER NOT NULL DEFAULT 1,
            parent_contract_id TEXT,
            is_replaced INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK(date(end_date) >= date(start_date)),
            CHECK(entry_date IS NULL OR date(contract_start_date) = date(entry_date)),
            CHECK(arrival_date IS NULL OR date(arrival_date) = date(entry_date)),
            CHECK(date(contract_end_date) >= date(contract_start_date)),
            CHECK(date(start_date) = date(contract_start_date)),
            CHECK(date(end_date) = date(contract_end_date)),
            FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE SET NULL,
            FOREIGN KEY (quota_id) REFERENCES quotas(id) ON DELETE SET NULL,
            FOREIGN KEY (parent_contract_id) REFERENCES contracts(contract_id) ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS quota_usages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quota_id INTEGER NOT NULL,
            person_id INTEGER NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK(end_date IS NULL OR date(end_date) >= date(start_date)),
            FOREIGN KEY (quota_id) REFERENCES quotas(id) ON DELETE CASCADE,
            FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS renewals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER NOT NULL,
            quota_id INTEGER NOT NULL,
            current_contract_end_date TEXT NOT NULL,
            renewal_start_date TEXT NOT NULL,
            passport_expiry_check TEXT NOT NULL
                CHECK(passport_expiry_check IN ('待检查','通过','不通过')),
            id_card_expiry_check TEXT NOT NULL
                CHECK(id_card_expiry_check IN ('待检查','通过','不通过')),
            submission_deadline TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('可续','不可续','风险')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(person_id, quota_id, current_contract_end_date),
            FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE CASCADE,
            FOREIGN KEY (quota_id) REFERENCES quotas(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS workflow_instances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_code TEXT NOT NULL UNIQUE,
            workflow_name TEXT NOT NULL,
            person_id INTEGER NOT NULL,
            quota_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT '进行中'
                CHECK(status IN ('进行中','已完成','已拒绝')),
            current_step TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE RESTRICT,
            FOREIGN KEY (quota_id) REFERENCES quotas(id) ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS workflow_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id INTEGER NOT NULL,
            step_name TEXT NOT NULL,
            step_order INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT '待处理'
                CHECK(status IN ('待处理','已通过','已拒绝')),
            note TEXT NOT NULL DEFAULT '',
            action_at TEXT,
            UNIQUE(workflow_id, step_order),
            FOREIGN KEY (workflow_id) REFERENCES workflow_instances(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            filename TEXT NOT NULL,
            stored_name TEXT NOT NULL UNIQUE,
            mime_type TEXT NOT NULL,
            person_id INTEGER NOT NULL,
            quota_id INTEGER NOT NULL,
            workflow_id INTEGER NOT NULL,
            ocr_text TEXT NOT NULL DEFAULT '',
            ocr_status TEXT NOT NULL DEFAULT '待OCR',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE RESTRICT,
            FOREIGN KEY (quota_id) REFERENCES quotas(id) ON DELETE RESTRICT,
            FOREIGN KEY (workflow_id) REFERENCES workflow_instances(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS person_cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER NOT NULL,
            case_type TEXT NOT NULL DEFAULT 'other',
            case_label TEXT NOT NULL,
            start_date TEXT,
            end_date TEXT,
            contract_start_date TEXT,
            contract_end_date TEXT,
            contract_restart_due_date TEXT,
            endorsement_expiry_date TEXT,
            document_collection_due_date TEXT,
            renewal_alert_status TEXT NOT NULL DEFAULT 'pending',
            quota_id INTEGER,
            contract_id TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            remarks TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            is_deleted INTEGER NOT NULL DEFAULT 0,
            deleted_at DATETIME,
            deleted_by INTEGER,
            FOREIGN KEY(person_id) REFERENCES people(id) ON DELETE RESTRICT,
            FOREIGN KEY(quota_id) REFERENCES quotas(id) ON DELETE SET NULL,
            FOREIGN KEY(contract_id) REFERENCES contracts(contract_id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS person_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER NOT NULL,
            document_type TEXT NOT NULL DEFAULT 'other',
            original_filename TEXT NOT NULL,
            stored_path TEXT NOT NULL,
            mime_type TEXT,
            file_size INTEGER NOT NULL DEFAULT 0,
            upload_batch_id TEXT,
            person_case_id INTEGER,
            inferred_case_confidence REAL,
            case_binding_status TEXT NOT NULL DEFAULT 'unassigned',
            ocr_text TEXT NOT NULL DEFAULT '',
            issue_date TEXT,
            expiry_date TEXT,
            uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'active',
            remarks TEXT,
            is_deleted INTEGER NOT NULL DEFAULT 0,
            deleted_at DATETIME,
            deleted_by INTEGER,
            FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE RESTRICT,
            FOREIGN KEY (person_case_id) REFERENCES person_cases(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS lifecycle_nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER NOT NULL,
            quota_id INTEGER NOT NULL,
            workflow_id INTEGER,
            node_type TEXT NOT NULL,
            due_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT '待处理'
                CHECK(status IN ('待处理','已完成','逾期')),
            source_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE CASCADE,
            FOREIGN KEY (quota_id) REFERENCES quotas(id) ON DELETE CASCADE,
            FOREIGN KEY (workflow_id) REFERENCES workflow_instances(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS risks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER,
            quota_id INTEGER,
            contract_id TEXT,
            person_case_id INTEGER,
            due_date TEXT,
            risk_type TEXT NOT NULL,
            risk_level TEXT NOT NULL CHECK(risk_level IN ('低','中','高')),
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT '开放' CHECK(status IN ('开放','已解决')),
            source_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE SET NULL,
            FOREIGN KEY (quota_id) REFERENCES quotas(id) ON DELETE CASCADE,
            FOREIGN KEY (contract_id) REFERENCES contracts(contract_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER NOT NULL,
            quota_id INTEGER,
            workflow_id INTEGER,
            person_case_id INTEGER,
            title TEXT NOT NULL,
            due_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT '待办'
                CHECK(status IN ('待办','进行中','已完成','逾期')),
            trigger_type TEXT NOT NULL,
            source_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE CASCADE,
            FOREIGN KEY (quota_id) REFERENCES quotas(id) ON DELETE SET NULL,
            FOREIGN KEY (workflow_id) REFERENCES workflow_instances(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS quota_worker_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quota_id INTEGER NOT NULL,
            worker_id INTEGER NOT NULL,
            contract_id TEXT,
            start_date TEXT,
            end_date TEXT,
            status TEXT NOT NULL CHECK(status IN ('active','closed')),
            event_type TEXT NOT NULL
                CHECK(event_type IN ('initial','replacement','renewal','resignation')),
            replacement_round INTEGER NOT NULL DEFAULT 0 CHECK(replacement_round >= 0),
            source_type TEXT NOT NULL CHECK(source_type IN ('SWD','LD')),
            source_note TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (quota_id) REFERENCES quotas(id) ON DELETE RESTRICT,
            FOREIGN KEY (worker_id) REFERENCES people(id) ON DELETE RESTRICT,
            FOREIGN KEY (contract_id) REFERENCES contracts(contract_id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_people_name ON people(name);
        CREATE INDEX IF NOT EXISTS idx_quotas_number ON quotas(quota_number);
        CREATE INDEX IF NOT EXISTS idx_events_person ON events(person_id);
        CREATE INDEX IF NOT EXISTS idx_contracts_person ON contracts(person_name);
        CREATE INDEX IF NOT EXISTS idx_contracts_status ON contracts(status);
        CREATE INDEX IF NOT EXISTS idx_contracts_end_date ON contracts(end_date);
        CREATE INDEX IF NOT EXISTS idx_quota_usages_quota ON quota_usages(quota_id, start_date);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_quota_usages_active
            ON quota_usages(quota_id) WHERE end_date IS NULL;
        CREATE INDEX IF NOT EXISTS idx_renewals_deadline ON renewals(submission_deadline);
        CREATE INDEX IF NOT EXISTS idx_renewals_status ON renewals(status);
        CREATE INDEX IF NOT EXISTS idx_documents_binding ON documents(person_id, quota_id, workflow_id);
        CREATE INDEX IF NOT EXISTS idx_person_documents_person ON person_documents(person_id,is_deleted);
        CREATE INDEX IF NOT EXISTS idx_person_documents_type ON person_documents(document_type,status);
        CREATE INDEX IF NOT EXISTS idx_workflows_status ON workflow_instances(status);
        CREATE INDEX IF NOT EXISTS idx_lifecycle_due ON lifecycle_nodes(due_date, status);
        CREATE INDEX IF NOT EXISTS idx_risks_level ON risks(risk_level, status);
        CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due_date, status);
        CREATE INDEX IF NOT EXISTS idx_quota_worker_history_chain
            ON quota_worker_history(quota_id,replacement_round,id);
        CREATE INDEX IF NOT EXISTS idx_quota_worker_history_worker
            ON quota_worker_history(worker_id,created_at);
        """
    )

    # Non-destructive Person migration: preserve IDs and every dependent FK while
    # making legacy document fields optional and adding the operator-facing fields.
    person_columns = {
        row["name"]: row for row in db.execute("PRAGMA table_info(people)").fetchall()
    }
    for name in ("visa_number", "hkid"):
        if name not in person_columns:
            db.execute(f"ALTER TABLE people ADD COLUMN {name} TEXT")
    person_needs_rebuild = (
        not {"gender", "company_name", "introducer"}.issubset(person_columns)
        or person_columns.get("id_last4", {"notnull": 0})["notnull"]
        or person_columns.get("permit_last4", {"notnull": 0})["notnull"]
    )
    if person_needs_rebuild:
        db.commit()
        db.execute("PRAGMA foreign_keys = OFF")
        select_value = lambda column: column if column in person_columns else "NULL"
        try:
            db.execute("BEGIN")
            db.execute(
                """
                CREATE TABLE people_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    gender TEXT CHECK(gender IN ('男','女')),
                    company_name TEXT,
                    introducer TEXT,
                    id_last4 TEXT CHECK(id_last4 IS NULL OR length(id_last4) = 4),
                    permit_last4 TEXT CHECK(permit_last4 IS NULL OR length(permit_last4) = 4),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            db.execute(
                f"""
                INSERT INTO people_new
                    (id, name, gender, company_name, introducer,
                     id_last4, permit_last4, created_at)
                SELECT id, name, {select_value('gender')},
                       {select_value('company_name')}, {select_value('introducer')},
                       {select_value('id_last4')}, {select_value('permit_last4')},
                       created_at
                FROM people
                """
            )
            db.execute("DROP TABLE people")
            db.execute("ALTER TABLE people_new RENAME TO people")
            db.execute("CREATE INDEX IF NOT EXISTS idx_people_name ON people(name)")
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.execute("PRAGMA foreign_keys = ON")

    current_person_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(people)").fetchall()
    }
    for name in ("visa_number", "hkid"):
        if name not in current_person_columns:
            db.execute(f"ALTER TABLE people ADD COLUMN {name} TEXT")

    profile_columns = {
        "person_name": "TEXT", "worker_type": "TEXT DEFAULT 'new'",
        "birth_date": "TEXT", "birth_year_month": "TEXT",
        "mainland_id_first4": "TEXT", "mainland_id_last4": "TEXT",
        "hkmo_permit_first4": "TEXT", "hkmo_permit_last6": "TEXT",
        "entry_permit_no": "TEXT", "hk_submission_date": "TEXT",
        "visa_status_date": "TEXT", "visa_status": "TEXT",
        "hk_id_appointment_status": "TEXT", "remarks": "TEXT",
    }
    current_person_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(people)").fetchall()
    }
    for column, definition in profile_columns.items():
        if column not in current_person_columns:
            db.execute(f"ALTER TABLE people ADD COLUMN {column} {definition}")

    person_document_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(person_documents)").fetchall()
    }
    for column, definition in {
        "mime_type": "TEXT", "file_size": "INTEGER NOT NULL DEFAULT 0",
        "upload_batch_id": "TEXT", "person_case_id": "INTEGER",
        "inferred_case_confidence": "REAL",
        "case_binding_status": "TEXT NOT NULL DEFAULT 'unassigned'",
    }.items():
        if column not in person_document_columns:
            db.execute(f"ALTER TABLE person_documents ADD COLUMN {column} {definition}")
    person_case_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(person_cases)").fetchall()
    }
    for column, definition in {
        "contract_start_date": "TEXT", "contract_end_date": "TEXT",
        "contract_restart_due_date": "TEXT", "endorsement_expiry_date": "TEXT",
        "document_collection_due_date": "TEXT",
        "renewal_alert_status": "TEXT NOT NULL DEFAULT 'pending'",
    }.items():
        if column not in person_case_columns:
            db.execute(f"ALTER TABLE person_cases ADD COLUMN {column} {definition}")
    for table, additions in {
        "risks": {"person_case_id": "INTEGER", "due_date": "TEXT"},
        "tasks": {"person_case_id": "INTEGER"},
    }.items():
        existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
        for column, definition in additions.items():
            if column not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_person_documents_case ON person_documents(person_case_id,case_binding_status)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_person_cases_person ON person_cases(person_id,status,is_deleted)"
    )
    db.execute(
        """UPDATE people SET person_name=COALESCE(NULLIF(person_name,''),name),
                  worker_type=CASE WHEN worker_type IN ('new','renewal') THEN worker_type ELSE 'new' END,
                  mainland_id_last4=COALESCE(mainland_id_last4,id_last4),
                  birth_year_month=COALESCE(birth_year_month,substr(birth_date,1,7)),
                  visa_status=CASE visa_status
                    WHEN '未提交' THEN '未出' WHEN '处理中' THEN '未出'
                    WHEN '待补资料' THEN '待缴费'
                    WHEN '未出' THEN '未出' WHEN '待缴费' THEN '待缴费'
                    WHEN '已出' THEN '已出' ELSE NULL END
           """
    )

    document_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(documents)").fetchall()
    }
    active_document_filter = "d.is_deleted=0" if "is_deleted" in document_columns else "1=1"
    db.execute(
        f"""INSERT INTO person_documents
           (person_id,document_type,original_filename,stored_path,ocr_text,uploaded_at,status)
           SELECT d.person_id,'other',d.filename,d.stored_name,d.ocr_text,d.created_at,
                  CASE WHEN d.ocr_status='待OCR' THEN 'pending_ocr' ELSE 'active' END
           FROM documents d
           WHERE {active_document_filter} AND NOT EXISTS (
               SELECT 1 FROM person_documents pd
               WHERE pd.person_id=d.person_id AND pd.stored_path=d.stored_name
           )"""
    )

    # Non-destructive Quota migration. Legacy quota_number values remain intact,
    # while new records may stay intentionally incomplete until details arrive.
    quota_columns = {
        row["name"]: row for row in db.execute("PRAGMA table_info(quotas)").fetchall()
    }
    quota_was_pre_type = "quota_type" not in quota_columns
    quota_needs_rebuild = (
        not {"approval_number", "quota_serial", "start_date", "expiry_date"}.issubset(
            quota_columns
        )
        or quota_columns.get("quota_number", {"notnull": 0})["notnull"]
        or quota_was_pre_type
    )
    if quota_needs_rebuild:
        db.commit()
        db.execute("PRAGMA foreign_keys = OFF")
        quota_value = lambda column: column if column in quota_columns else "NULL"
        quota_type_value = "quota_type" if "quota_type" in quota_columns else "'SWD'"
        try:
            db.execute("BEGIN")
            db.execute(
                """
                CREATE TABLE quotas_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    quota_number TEXT UNIQUE,
                    company_name TEXT NOT NULL,
                    quota_type TEXT NOT NULL DEFAULT 'SWD'
                        CHECK(quota_type IN ('SWD','LD')),
                    approval_number TEXT,
                    quota_serial TEXT,
                    person_id INTEGER UNIQUE,
                    start_date TEXT,
                    expiry_date TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE SET NULL
                )
                """
            )
            db.execute(
                f"""
                INSERT INTO quotas_new
                    (id, quota_number, company_name, quota_type, approval_number,
                     quota_serial, person_id, start_date, expiry_date, created_at)
                SELECT id, quota_number, company_name, {quota_type_value},
                       {quota_value('approval_number')}, {quota_value('quota_serial')},
                       person_id, {quota_value('start_date')},
                       {quota_value('expiry_date')}, created_at
                FROM quotas
                """
            )
            db.execute("DROP TABLE quotas")
            db.execute("ALTER TABLE quotas_new RENAME TO quotas")
            db.execute("CREATE INDEX IF NOT EXISTS idx_quotas_number ON quotas(quota_number)")
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.execute("PRAGMA foreign_keys = ON")
    if quota_was_pre_type:
        db.execute(
            """
            INSERT INTO quota_usages (quota_id, person_id, start_date)
            SELECT q.id, q.person_id, date(q.created_at)
            FROM quotas q
            WHERE q.person_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM quota_usages u WHERE u.quota_id = q.id)
            """
        )
    event_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(events)").fetchall()
    }
    if "event_date" not in event_columns:
        db.execute("ALTER TABLE events ADD COLUMN event_date TEXT")
    db.execute("UPDATE events SET event_date=date(created_at) WHERE event_date IS NULL")

    contract_columns = {
        row["name"]: row for row in db.execute("PRAGMA table_info(contracts)").fetchall()
    }
    for column in ("arrival_date", "contract_start_date", "contract_end_date"):
        if column not in contract_columns:
            db.execute(f"ALTER TABLE contracts ADD COLUMN {column} TEXT")
    contract_columns = {
        row["name"]: row for row in db.execute("PRAGMA table_info(contracts)").fetchall()
    }
    if any(contract_columns[column]["notnull"] for column in (
        "arrival_date", "contract_start_date", "contract_end_date", "start_date", "end_date"
    )):
        db.commit()
        db.execute("PRAGMA foreign_keys = OFF")
        try:
            db.execute("BEGIN")
            db.execute(
                """CREATE TABLE contracts_new (
                    contract_id TEXT PRIMARY KEY COLLATE NOCASE,
                    person_name TEXT NOT NULL,
                    company TEXT NOT NULL,
                    arrival_date TEXT,
                    contract_start_date TEXT,
                    contract_end_date TEXT,
                    start_date TEXT,
                    end_date TEXT,
                    status TEXT NOT NULL
                        CHECK(status IN ('登记','付款','签证中','已签','入境','完成')),
                    person_id INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CHECK(arrival_date IS NULL OR date(contract_start_date)=date(arrival_date)),
                    CHECK(start_date IS NULL OR date(start_date)=date(contract_start_date)),
                    CHECK(end_date IS NULL OR date(end_date)=date(contract_end_date)),
                    FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE SET NULL
                )"""
            )
            db.execute(
                """INSERT INTO contracts_new
                    (contract_id,person_name,company,arrival_date,
                     contract_start_date,contract_end_date,start_date,end_date,
                     status,person_id,created_at)
                   SELECT contract_id,person_name,company,arrival_date,
                          contract_start_date,contract_end_date,start_date,end_date,
                          status,person_id,created_at FROM contracts"""
            )
            db.execute("DROP TABLE contracts")
            db.execute("ALTER TABLE contracts_new RENAME TO contracts")
            db.execute("CREATE INDEX IF NOT EXISTS idx_contracts_person ON contracts(person_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_contracts_end_date ON contracts(end_date)")
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.execute("PRAGMA foreign_keys = ON")
    ensure_legacy_dispatch_schema(db)
    initialize_standard_schema(db)
    ensure_soft_delete_schema(db)
    db.commit()


@app.cli.command("init-db")
def init_db_command():
    init_db()
    print("数据库已初始化。")


def four_digits(value):
    return len(value) == 4 and value.isdigit()


def quota_display_label(quota):
    return (
        quota["quota_serial"]
        or quota["approval_number"]
        or quota["quota_number"]
        or f"未编号 #{quota['id']}"
    )


def shift_months(value, months):
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def calculate_contract_period(arrival_date):
    return arrival_date, shift_months(arrival_date, 24)


def apply_arrival_event(db, person_id, event_date):
    """Route every real entry through the unified dispatch engine."""
    if not person_id or not event_date:
        return None
    arrival = datetime.strptime(event_date, "%Y-%m-%d").date()
    contract_start, contract_end = calculate_contract_period(arrival)
    contract = db.execute(
        """SELECT contract_id FROM contracts
           WHERE person_id=? AND is_deleted=0 AND status!='完成合约'
           ORDER BY cycle_index DESC, created_at DESC, contract_id DESC LIMIT 1""",
        (person_id,),
    ).fetchone()
    if contract:
        result = register_worker_entry(db, contract["contract_id"], arrival.isoformat())
        standard_contract = db.execute(
            "SELECT id FROM contract WHERE contract_no=?", (contract["contract_id"],)
        ).fetchone()
        if standard_contract:
            db.execute(
                """UPDATE event SET contract_id=?, quota_id=?
                   WHERE person_id=? AND event_type IN ('入境','工人入境') AND event_date=?
                     AND contract_id IS NULL""",
                (
                    standard_contract["id"], result["quota_id"], person_id,
                    arrival.isoformat(),
                ),
            )
        return contract["contract_id"]

    # Standard-table compatibility for direct helper/API users.
    standard_contract = db.execute(
        """SELECT id,contract_no,quota_id,entry_date FROM contract
           WHERE person_id=? AND is_deleted=0 AND status!='完成合约'
           ORDER BY cycle_index DESC,created_at DESC,id DESC LIMIT 1""",
        (person_id,),
    ).fetchone()
    if not standard_contract:
        return None
    first_entry = not standard_contract["entry_date"]
    db.execute(
        """UPDATE contract SET entry_date=?,arrival_date=?,contract_start_date=?,
                  contract_end_date=?,start_date=?,end_date=?,status='工人入境'
           WHERE id=?""",
        (
            arrival.isoformat(), arrival.isoformat(), contract_start.isoformat(),
            contract_end.isoformat(), contract_start.isoformat(), contract_end.isoformat(),
            standard_contract["id"],
        ),
    )
    if first_entry and standard_contract["quota_id"]:
        db.execute(
            """UPDATE quota SET usage_count=usage_count+1,status='in_use',
                      user_id=? WHERE id=? AND status NOT IN ('invalid','exhausted')""",
            (person_id, standard_contract["quota_id"]),
        )
    db.execute(
        """UPDATE event SET contract_id=?,quota_id=?
           WHERE person_id=? AND event_type IN ('入境','工人入境') AND event_date=?
             AND contract_id IS NULL""",
        (
            standard_contract["id"], standard_contract["quota_id"], person_id,
            arrival.isoformat(),
        ),
    )
    return standard_contract["contract_no"] or str(standard_contract["id"])


def calculate_usage_months(start_date, end_date=None, today=None):
    """Count complete calendar months, then round any remaining days up."""
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    effective_end = (
        datetime.strptime(end_date, "%Y-%m-%d").date()
        if end_date
        else (today or date.today())
    )
    if effective_end <= start:
        return 0
    complete_months = (effective_end.year - start.year) * 12 + effective_end.month - start.month

    anniversary = shift_months(start, complete_months)
    if anniversary > effective_end:
        complete_months -= 1
        anniversary = shift_months(start, complete_months)
    return complete_months + (1 if effective_end > anniversary else 0)


def determine_renewal_status(contract_end, passport_check, id_card_check, today=None):
    today = today or date.today()
    deadline = shift_months(contract_end, -1)
    if passport_check == "不通过" or id_card_check == "不通过":
        return "不可续"
    if contract_end < today:
        return "不可续"
    if passport_check == "通过" and id_card_check == "通过" and today <= deadline:
        return "可续"
    return "风险"


def refresh_renewal_statuses(db):
    changed = False
    rows = db.execute(
        """SELECT id, current_contract_end_date, passport_expiry_check,
                  id_card_expiry_check, status FROM renewals WHERE is_deleted=0"""
    ).fetchall()
    for row in rows:
        contract_end = datetime.strptime(row["current_contract_end_date"], "%Y-%m-%d").date()
        status = determine_renewal_status(
            contract_end, row["passport_expiry_check"], row["id_card_expiry_check"]
        )
        if status != row["status"]:
            db.execute(
                "UPDATE renewals SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, row["id"]),
            )
            changed = True
    if changed:
        db.commit()


def extract_document_text(path):
    suffix = path.suffix.lower()
    if suffix == ".txt":
        return path.read_text(encoding="utf-8", errors="ignore"), "已提取"
    if suffix == ".pdf" and shutil.which("pdftotext"):
        result = subprocess.run(
            ["pdftotext", str(path), "-"], capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip(), "已提取"
    if suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff"} and shutil.which("tesseract"):
        result = subprocess.run(
            ["tesseract", str(path), "stdout", "-l", "chi_sim+eng"],
            capture_output=True, text=True, timeout=45,
        )
        if result.returncode != 0:
            result = subprocess.run(
                ["tesseract", str(path), "stdout", "-l", "eng"],
                capture_output=True, text=True, timeout=45,
            )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip(), "已OCR"
    return "", "待OCR"


def upsert_lifecycle_node(db, person_id, quota_id, workflow_id, node_type, due_date, source_key):
    db.execute(
        """
        INSERT INTO lifecycle_nodes
            (person_id, quota_id, workflow_id, node_type, due_date, source_key)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_key) DO UPDATE SET
            person_id=excluded.person_id, quota_id=excluded.quota_id,
            workflow_id=excluded.workflow_id, node_type=excluded.node_type,
            due_date=excluded.due_date
        """,
        (person_id, quota_id, workflow_id, node_type, due_date, source_key),
    )


def generate_lifecycle_nodes(db):
    renewals = db.execute("SELECT * FROM renewals WHERE is_deleted=0").fetchall()
    for renewal in renewals:
        workflow = db.execute(
            """SELECT id FROM workflow_instances
               WHERE person_id=? AND quota_id=? AND is_deleted=0 ORDER BY id DESC LIMIT 1""",
            (renewal["person_id"], renewal["quota_id"]),
        ).fetchone()
        workflow_id = workflow["id"] if workflow else None
        prefix = f"renewal:{renewal['id']}"
        upsert_lifecycle_node(
            db, renewal["person_id"], renewal["quota_id"], workflow_id,
            "续期启动", renewal["renewal_start_date"], f"{prefix}:start",
        )
        upsert_lifecycle_node(
            db, renewal["person_id"], renewal["quota_id"], workflow_id,
            "提交截止", renewal["submission_deadline"], f"{prefix}:submit",
        )
        upsert_lifecycle_node(
            db, renewal["person_id"], renewal["quota_id"], workflow_id,
            "合同到期", renewal["current_contract_end_date"], f"{prefix}:expiry",
        )
    today = date.today().isoformat()
    db.execute(
        """UPDATE lifecycle_nodes SET status='逾期'
           WHERE is_deleted=0 AND status='待处理' AND date(due_date) < date(?)""",
        (today,),
    )
    db.execute(
        """UPDATE tasks SET status='逾期', updated_at=CURRENT_TIMESTAMP
           WHERE is_deleted=0 AND status IN ('待办','进行中') AND date(due_date) < date(?)""",
        (today,),
    )
    db.commit()


def refresh_risks(db, quota_views):
    active_keys = []

    def record_risk(person_id, quota_id, contract_id, risk_type, risk_level, reason, source_key):
        active_keys.append(source_key)
        db.execute(
            """INSERT INTO risks
               (person_id, quota_id, contract_id, risk_type, risk_level, reason, source_key)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_key) DO UPDATE SET
                 person_id=excluded.person_id,
                 quota_id=excluded.quota_id,
                 contract_id=excluded.contract_id,
                 risk_type=excluded.risk_type,
                 risk_level=excluded.risk_level,
                 reason=excluded.reason,
                 updated_at=CURRENT_TIMESTAMP""",
            (person_id, quota_id, contract_id, risk_type, risk_level, reason, source_key),
        )

    today = date.today()
    for contract in db.execute("SELECT * FROM contracts WHERE is_deleted=0").fetchall():
        if not contract["end_date"]:
            continue
        end = datetime.strptime(contract["end_date"], "%Y-%m-%d").date()
        remaining = (end - today).days
        if contract["status"] != "完成合约" and remaining < 0:
            record_risk(
                contract["person_id"], None, contract["contract_id"], "合同", "高",
                f"合同已到期 {abs(remaining)} 天且未完成",
                f"AUTO:contract:{contract['contract_id']}",
            )
        elif contract["status"] != "完成合约" and remaining <= 30:
            record_risk(
                contract["person_id"], None, contract["contract_id"], "合同", "中",
                f"合同将在 {remaining} 天内到期",
                f"AUTO:contract:{contract['contract_id']}",
            )
    for renewal in db.execute("SELECT * FROM renewals WHERE is_deleted=0").fetchall():
        if renewal["status"] in {"不可续", "风险"}:
            level = "高" if renewal["status"] == "不可续" else "中"
            record_risk(
                renewal["person_id"], renewal["quota_id"], None, "证件/续期", level,
                f"续期评估状态为{renewal['status']}", f"AUTO:renewal:{renewal['id']}",
            )
    for quota in quota_views:
        if quota.get("status") == "invalid":
            record_risk(
                None, quota["id"], None, "名额替补超限", "高",
                f"{quota['quota_type']} 名额替补次数超过上限，已失效",
                f"AUTO:quota-invalid:{quota['id']}",
            )
        if quota["remaining_months"] <= 3:
            level = "高" if quota["remaining_months"] == 0 else "中"
            record_risk(
                None, quota["id"], None, "名额", level,
                f"名额仅剩 {quota['remaining_months']} 个月", f"AUTO:quota:{quota['id']}",
            )
    for document in db.execute(
        "SELECT id, person_id, quota_id, ocr_status FROM documents WHERE is_deleted=0"
    ).fetchall():
        if document["ocr_status"] == "待OCR":
            record_risk(
                document["person_id"], document["quota_id"], None, "资料", "中",
                "文件尚未完成OCR", f"AUTO:document:{document['id']}",
            )
    if active_keys:
        placeholders = ",".join("?" for _ in active_keys)
        db.execute(
            f"""UPDATE risks SET is_deleted=1,deleted_at=CURRENT_TIMESTAMP
                WHERE is_deleted=0 AND source_key LIKE 'AUTO:%'
                  AND source_key NOT IN ({placeholders})""",
            active_keys,
        )
    else:
        db.execute(
            """UPDATE risks SET is_deleted=1,deleted_at=CURRENT_TIMESTAMP
               WHERE is_deleted=0 AND source_key LIKE 'AUTO:%'"""
        )
    db.commit()


def create_entry_tasks(db, person_id, event_day=None, workflow_id=None, quota_id=None):
    event_day = event_day or date.today()
    if quota_id is None:
        quota = db.execute(
            "SELECT id FROM quotas WHERE person_id=? AND is_deleted=0", (person_id,)
        ).fetchone()
        quota_id = quota["id"] if quota else None
    if workflow_id is None:
        workflow = db.execute(
            "SELECT id FROM workflow_instances WHERE person_id=? AND is_deleted=0 ORDER BY id DESC LIMIT 1",
            (person_id,),
        ).fetchone()
        workflow_id = workflow["id"] if workflow else None
    for offset, title, key in (
        (7, "入境后7天资料核验", "7d"),
        (30, "入境后30天跟进", "30d"),
    ):
        due = event_day.fromordinal(event_day.toordinal() + offset).isoformat()
        source_key = f"ENTRY:{person_id}:{event_day.isoformat()}:{key}"
        db.execute(
            """INSERT OR IGNORE INTO tasks
               (person_id, quota_id, workflow_id, title, due_date, trigger_type, source_key)
               VALUES (?, ?, ?, ?, ?, '入境', ?)""",
            (person_id, quota_id, workflow_id, title, due, source_key),
        )


def refresh_person_case_alerts(db):
    """Synchronize person-case reminder dates into the existing risk and task centers."""
    today = date.today()
    for row in db.execute(
        "SELECT * FROM person_cases WHERE is_deleted=0"
    ).fetchall():
        case = dict(row)
        calculated = calculate_renewal_alert_dates(case)
        restart_due = case.get("contract_restart_due_date") or calculated["contract_restart_due_date"]
        collection_due = case.get("document_collection_due_date") or calculated["document_collection_due_date"]
        db.execute(
            """UPDATE person_cases SET contract_restart_due_date=?,document_collection_due_date=?,
                      updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (restart_due, collection_due, case["id"]),
        )
        closed = case.get("renewal_alert_status") in {"completed", "ignored"}
        reminders = (
            ("contract_restart_due", "合同重启提醒", "合同重启待办", restart_due),
            ("endorsement_collection_due", "收证件办理续签提醒", "收证件办理签注待办", collection_due),
        )
        emitted = False
        for risk_type, risk_label, task_title, due_value in reminders:
            source_key = f"AUTO:person-case:{case['id']}:{risk_type}"
            task_key = f"PERSON_CASE:{case['id']}:{risk_type}"
            if closed or not due_value:
                db.execute(
                    "UPDATE risks SET status='已解决',updated_at=CURRENT_TIMESTAMP WHERE source_key=?",
                    (source_key,),
                )
                if closed:
                    db.execute(
                        "UPDATE tasks SET status='已完成',updated_at=CURRENT_TIMESTAMP WHERE source_key=?",
                        (task_key,),
                    )
                continue
            try:
                due = datetime.strptime(due_value, "%Y-%m-%d").date()
            except (TypeError, ValueError):
                continue
            remaining = (due - today).days
            if remaining > 30:
                continue
            emitted = True
            level = "高" if remaining < 0 else "中"
            timing = f"已逾期 {abs(remaining)} 天" if remaining < 0 else f"剩余 {remaining} 天"
            db.execute(
                """INSERT INTO risks
                   (person_id,quota_id,contract_id,person_case_id,due_date,risk_type,
                    risk_level,reason,status,source_key,is_deleted,deleted_at)
                   VALUES (?,?,?,?,?,?,?,?, '开放',?,0,NULL)
                   ON CONFLICT(source_key) DO UPDATE SET
                     person_id=excluded.person_id,quota_id=excluded.quota_id,
                     contract_id=excluded.contract_id,person_case_id=excluded.person_case_id,
                     due_date=excluded.due_date,risk_type=excluded.risk_type,
                     risk_level=excluded.risk_level,reason=excluded.reason,status='开放',
                     is_deleted=0,deleted_at=NULL,updated_at=CURRENT_TIMESTAMP""",
                (case["person_id"], case.get("quota_id"), case.get("contract_id"), case["id"],
                 due_value, risk_type, level, f"{risk_label}：{timing}", source_key),
            )
            db.execute(
                """INSERT INTO tasks
                   (person_id,quota_id,workflow_id,person_case_id,title,due_date,
                    status,trigger_type,source_key,is_deleted,deleted_at)
                   VALUES (?,?,NULL,?,?,?,'待办','续约周期',?,0,NULL)
                   ON CONFLICT(source_key) DO UPDATE SET
                     person_id=excluded.person_id,quota_id=excluded.quota_id,
                     person_case_id=excluded.person_case_id,title=excluded.title,
                     due_date=excluded.due_date,is_deleted=0,deleted_at=NULL,
                     status=CASE WHEN tasks.status='已完成' THEN tasks.status ELSE excluded.status END,
                     updated_at=CURRENT_TIMESTAMP""",
                (case["person_id"], case.get("quota_id"), case["id"], task_title, due_value, task_key),
            )
        if emitted and case.get("renewal_alert_status") == "pending":
            db.execute(
                "UPDATE person_cases SET renewal_alert_status='reminded',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (case["id"],),
            )
    db.commit()


def create_workflow_record(db, person_id, quota_id, workflow_name):
    contract = db.execute(
        """SELECT status FROM contracts WHERE person_id=? AND quota_id=? AND is_deleted=0
           ORDER BY cycle_index DESC,created_at DESC LIMIT 1""",
        (person_id, quota_id),
    ).fetchone()
    if not contract:
        raise ValueError("请先创建并绑定合同周期")
    current_index = WORKFLOW_STEPS.index(normalize_contract_status(contract["status"]))
    next_index = current_index + 1
    workflow_status = "已完成" if next_index >= len(WORKFLOW_STEPS) else "进行中"
    current_step = "完成" if workflow_status == "已完成" else WORKFLOW_STEPS[next_index]
    workflow_code = f"WF-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    cursor = db.execute(
        """INSERT INTO workflow_instances
           (workflow_code, workflow_name, person_id, quota_id, status, current_step)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (workflow_code, workflow_name, person_id, quota_id, workflow_status, current_step),
    )
    for order, step_name in enumerate(WORKFLOW_STEPS, 1):
        db.execute(
            """INSERT INTO workflow_steps
               (workflow_id,step_name,step_order,status,note,action_at)
               VALUES(?,?,?,?,?,?)""",
            (
                cursor.lastrowid, step_name, order,
                "已通过" if order - 1 <= current_index else "待处理",
                "由合同当前状态自动同步" if order - 1 <= current_index else "",
                datetime.now().strftime("%Y-%m-%d %H:%M") if order - 1 <= current_index else None,
            ),
        )
    return workflow_code


def collect_push_reminders():
    """Read-only push payload from existing operational tables."""
    connection = sqlite3.connect(app.config["DATABASE"])
    connection.row_factory = sqlite3.Row
    reminders = []
    try:
        for row in connection.execute(
            """SELECT r.id, r.risk_level, r.risk_type, r.reason, r.contract_id,
                      p.name AS person_name, q.quota_number
               FROM risks r
               LEFT JOIN people p ON p.id=r.person_id AND p.is_deleted=0
               LEFT JOIN quotas q ON q.id=r.quota_id AND q.is_deleted=0
               WHERE r.is_deleted=0 AND r.status='开放' AND r.risk_level IN ('高','中')
               ORDER BY CASE r.risk_level WHEN '高' THEN 0 ELSE 1 END, r.id DESC LIMIT 8"""
        ).fetchall():
            reminders.append({
                "key": f"risk:{row['id']}",
                "level": row["risk_level"],
                "title": f"{row['risk_level']}风险 · {row['person_name'] or row['quota_number'] or row['contract_id'] or row['risk_type']}",
                "message": row["reason"],
                "url": "/risks",
                "priority": 300 if row["risk_level"] == "高" else 200,
            })
        for row in connection.execute(
            """SELECT t.id, t.title, t.due_date, t.status, p.name AS person_name,
                      CAST(julianday(t.due_date)-julianday(date('now','localtime')) AS INTEGER) AS days
               FROM tasks t JOIN people p ON p.id=t.person_id AND p.is_deleted=0
               WHERE t.is_deleted=0 AND t.status!='已完成'
                 AND date(t.due_date)<=date('now','localtime','+7 days')
               ORDER BY t.due_date LIMIT 8"""
        ).fetchall():
            reminders.append({
                "key": f"task:{row['id']}:{row['status']}",
                "level": "高" if row["days"] < 0 else "中",
                "title": f"任务 · {row['person_name']}",
                "message": f"{row['title']} · {row['due_date']} · {row['status']}",
                "url": "/tasks",
                "priority": 260 if row["days"] < 0 else 180 - max(row["days"], 0),
            })
        for row in connection.execute(
            """SELECT contract_id, person_name, end_date,
                      CAST(julianday(end_date)-julianday(date('now','localtime')) AS INTEGER) AS days
               FROM contracts WHERE is_deleted=0 AND status!='完成合约'
                 AND date(end_date) BETWEEN date('now','localtime') AND date('now','localtime','+30 days')
               ORDER BY end_date LIMIT 8"""
        ).fetchall():
            reminders.append({
                "key": f"contract:{row['contract_id']}:{row['end_date']}",
                "level": "中",
                "title": f"合同即将到期 · {row['person_name']}",
                "message": f"{row['contract_id']} 剩余 {row['days']} 天",
                "url": f"/contracts/{row['contract_id']}",
                "priority": 190 - max(row["days"], 0),
            })
    finally:
        connection.close()
    reminders.sort(key=lambda item: -item["priority"])
    return reminders[:10]


def build_quota_views(db, quotas):
    """Apply SWD or LD duration logic based only on quota_type."""
    usages = db.execute(
        """
        SELECT u.*, p.name AS person_name
        FROM quota_usages u JOIN people p ON p.id = u.person_id AND p.is_deleted=0
        JOIN quotas q ON q.id=u.quota_id AND q.is_deleted=0
        ORDER BY u.quota_id, u.start_date, u.id
        """
    ).fetchall()
    usages_by_quota = {}
    for usage in usages:
        item = dict(usage)
        item["months"] = calculate_usage_months(usage["start_date"], usage["end_date"])
        usages_by_quota.setdefault(usage["quota_id"], []).append(item)

    results = []
    for quota_row in quotas:
        quota = dict(quota_row)
        display_label = quota_display_label(quota)
        if not quota["quota_number"]:
            quota["quota_number"] = display_label
        history = usages_by_quota.get(quota["id"], [])
        active = next((usage for usage in reversed(history) if usage["end_date"] is None), None)
        if quota["quota_type"] == "SWD":
            used_months = active["months"] if active else 0
            calculation_label = "仅当前使用人"
        else:
            used_months = sum(usage["months"] for usage in history)
            calculation_label = "全部人员累计"
        remaining_months = max(QUOTA_TOTAL_MONTHS - used_months, 0)
        if remaining_months == 0 and quota.get("status") != "invalid":
            db.execute("UPDATE quotas SET status='exhausted' WHERE id=?", (quota["id"],))
            quota["status"] = "exhausted"
        quota.update(
            history=history,
            active_usage=active,
            person_name=active["person_name"] if active else quota.get("person_name"),
            used_months=used_months,
            remaining_months=remaining_months,
            calculation_label=calculation_label,
            display_label=display_label,
        )
        results.append(quota)
    return results


def find_question_subject(db, question):
    """Resolve a contract first, then fall back to the retained person structure."""
    identifier_candidates = re.findall(r"[A-Za-z][A-Za-z0-9_-]*\d[A-Za-z0-9_-]*", question)
    for candidate in identifier_candidates:
        row = db.execute(
            """
            SELECT c.contract_id, c.person_name, c.company, c.start_date,
                   c.end_date, c.status, c.person_id,
                   p.id_last4, p.permit_last4
            FROM contracts c LEFT JOIN people p ON p.id = c.person_id AND p.is_deleted=0
            WHERE c.contract_id = ? AND c.is_deleted=0
            """,
            (candidate,),
        ).fetchone()
        if row:
            return row, "contract"

    contract_people = db.execute(
        "SELECT DISTINCT person_name FROM contracts WHERE is_deleted=0 ORDER BY length(person_name) DESC"
    ).fetchall()
    matched_name = next(
        (row["person_name"] for row in contract_people if row["person_name"] in question), None
    )
    if matched_name:
        row = db.execute(
            """
            SELECT c.contract_id, c.person_name, c.company, c.start_date,
                   c.end_date, c.status, c.person_id,
                   p.id_last4, p.permit_last4
            FROM contracts c LEFT JOIN people p ON p.id = c.person_id AND p.is_deleted=0
            WHERE c.person_name = ? AND c.is_deleted=0
            ORDER BY c.end_date DESC, c.created_at DESC LIMIT 1
            """,
            (matched_name,),
        ).fetchone()
        return row, "contract_person"

    suffixes = re.findall(r"(?<!\d)\d{4}(?!\d)", question)
    for suffix in suffixes:
        row = db.execute(
            """
            SELECT c.contract_id, c.person_name, c.company, c.start_date,
                   c.end_date, c.status, c.person_id,
                   p.id_last4, p.permit_last4
            FROM people p JOIN contracts c ON c.person_id = p.id AND c.is_deleted=0
            WHERE p.is_deleted=0 AND (p.id_last4 = ? OR p.permit_last4 = ?)
            ORDER BY c.end_date DESC, c.created_at DESC LIMIT 1
            """,
            (suffix, suffix),
        ).fetchone()
        if row:
            return row, "certificate"

    # Backward-compatible lookup for people who do not yet have a contract.
    people = db.execute("SELECT name FROM people WHERE is_deleted=0 ORDER BY length(name) DESC").fetchall()
    matched_name = next((row["name"] for row in people if row["name"] in question), None)
    if matched_name:
        row = db.execute(
            """
            SELECT NULL AS contract_id, p.name AS person_name, NULL AS company,
                   NULL AS start_date, NULL AS end_date, NULL AS status,
                   p.id AS person_id, p.id_last4, p.permit_last4
            FROM people p WHERE p.name = ? AND p.is_deleted=0 ORDER BY p.id DESC LIMIT 1
            """,
            (matched_name,),
        ).fetchone()
        return row, "person"
    for suffix in suffixes:
        row = db.execute(
            """
            SELECT NULL AS contract_id, p.name AS person_name, NULL AS company,
                   NULL AS start_date, NULL AS end_date, NULL AS status,
                   p.id AS person_id, p.id_last4, p.permit_last4
            FROM people p
            WHERE p.is_deleted=0 AND (p.id_last4 = ? OR p.permit_last4 = ?)
            ORDER BY p.id DESC LIMIT 1
            """,
            (suffix, suffix),
        ).fetchone()
        if row:
            return row, "certificate"
    return None, None


def contract_remaining_days(subject):
    if not subject["end_date"]:
        return None
    return (datetime.strptime(subject["end_date"], "%Y-%m-%d").date() - date.today()).days


def assess_contract_risk(subject, remaining_days):
    if not subject["contract_id"]:
        return {"level": "中", "label": "待建合同", "reason": "人员存在，但尚未建立合同。"}
    if subject["status"] == "完成合约":
        return {"level": "低", "label": "已完成", "reason": "合同流程已完成。"}
    if remaining_days is not None and remaining_days < 0:
        return {"level": "高", "label": "已经到期", "reason": f"合同已到期 {abs(remaining_days)} 天且尚未完成。"}
    if remaining_days is not None and remaining_days <= 30:
        return {"level": "中", "label": "临近到期", "reason": f"合同将在 {remaining_days} 天后到期，请及时处理。"}
    if subject["status"] in {"制作合同", "交接香港同事", "交表香港入境处", "批出入境签证"}:
        return {"level": "中", "label": "流程进行中", "reason": f"合同当前处于“{subject['status']}”阶段。"}
    return {"level": "低", "label": "进度正常", "reason": "合同有效期和当前流程状态正常。"}


def answer_question(db, question):
    subject, matched_by = find_question_subject(db, question)
    if not subject:
        return {
            "found": False,
            "summary": "没有找到与问题匹配的合同或人员。",
            "suggestion": "请尝试输入完整合同编号、人员姓名或证件后四位，例如“C2026-001什么时候到期？”。",
        }

    events = []
    if subject["person_id"]:
        events = db.execute(
            """
            SELECT event_type, note, created_at FROM events
            WHERE person_id = ? AND is_deleted=0 ORDER BY created_at DESC, id DESC LIMIT 3
            """,
            (subject["person_id"],),
        ).fetchall()
    remaining_days = contract_remaining_days(subject)
    risk = assess_contract_risk(subject, remaining_days)
    status = subject["status"] or "未建立合同"
    contract_label = subject["contract_id"] or "未建立合同"
    person_label = subject["person_name"] or "暂无人员"
    if remaining_days is None:
        remaining_label = "—"
    elif remaining_days >= 0:
        remaining_label = f"{remaining_days} 天"
    else:
        remaining_label = f"已到期 {abs(remaining_days)} 天"

    return {
        "found": True,
        "matched_by": matched_by,
        "summary": f"{contract_label} 归属人员为{person_label}，当前状态为{status}，剩余时间{remaining_label}。",
        "contract": {
            "id": subject["contract_id"] or "—",
            "company": subject["company"] or "—",
            "start_date": subject["start_date"] or "—",
            "end_date": subject["end_date"] or "—",
            "remaining_days": remaining_days,
            "remaining_label": remaining_label,
        },
        "person": {
            "name": person_label,
            "id_last4": subject["id_last4"] or "—",
            "permit_last4": subject["permit_last4"] or "—",
        },
        "status": status,
        "recent_events": [
            {"type": event["event_type"], "note": event["note"] or "无备注", "time": event["created_at"]}
            for event in events
        ],
        "risk": risk,
    }


@app.route("/")
def index(default_view="overview"):
    db = get_db()
    refresh_renewal_statuses(db)
    refresh_standard_risks(db)
    valid_views = {
        "overview", "contracts", "people", "quotas", "renewals", "events", "ai",
        "documents", "workflows", "lifecycle", "risks", "tasks",
    }
    active_view = request.args.get("view", default_view)
    if active_view not in valid_views:
        active_view = "overview"
    keyword = request.args.get("q", "").strip()
    document_keyword = request.args.get("doc_q", "").strip()
    selected_case_scope = request.args.get("case_scope", "").strip()
    if selected_case_scope not in {"current", "history", "unbound_person", "unconfirmed"}:
        selected_case_scope = ""
    document_upload_result = session.pop("person_document_upload_result", None)
    selected_status = request.args.get("status", "").strip()
    selected_visa_status = request.args.get("visa_status", "").strip()
    if selected_visa_status not in {"未出", "待缴费", "已出"}:
        selected_visa_status = ""
    import_result = None
    import_log_id = request.args.get("import_log", "").strip()
    if import_log_id.isdigit():
        import_log = db.execute(
            """SELECT * FROM import_logs WHERE id=?
               AND (?='admin' OR user_id=?)""",
            (int(import_log_id), session.get("role"), session.get("user_id")),
        ).fetchone()
        if import_log:
            try:
                import_result = json.loads(import_log["result_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                import_result = {"errors": json.loads(import_log["errors_json"] or "[]")}
            import_result.update(
                filename=import_log["filename"], import_type=import_log["import_type"],
                created_at=import_log["created_at"], log_id=import_log["id"],
            )
    if selected_status not in CONTRACT_STATUSES:
        selected_status = ""
    like = f"%{keyword}%"

    contracts = db.execute(
        """
        SELECT c.*, p.id_last4, p.permit_last4,
               CAST(julianday(c.end_date) - julianday(date('now', 'localtime')) AS INTEGER) AS remaining_days
        FROM contracts c LEFT JOIN people p ON p.id = c.person_id AND p.is_deleted=0
        WHERE c.is_deleted=0
          AND (? = '' OR c.contract_id LIKE ? OR c.person_name LIKE ? OR c.company LIKE ?)
          AND (? = '' OR c.status = ?)
        ORDER BY CASE WHEN c.status = '完成合约' THEN 1 ELSE 0 END,
                 c.end_date ASC, c.created_at DESC
        """,
        (keyword, like, like, like, selected_status, selected_status),
    ).fetchall()

    people = db.execute(
        """
        SELECT p.*, q.quota_number,
               (SELECT event_type FROM events e WHERE e.person_id = p.id AND e.is_deleted=0
                ORDER BY e.created_at DESC, e.id DESC LIMIT 1) AS latest_status,
               (SELECT created_at FROM events e WHERE e.person_id = p.id AND e.is_deleted=0
                ORDER BY e.created_at DESC, e.id DESC LIMIT 1) AS latest_event_at
        FROM people p
        LEFT JOIN quotas q ON q.person_id = p.id AND q.is_deleted=0
        WHERE p.is_deleted=0 AND (? = '' OR p.name LIKE ? OR p.company_name LIKE ?
          OR p.entry_permit_no LIKE ? OR p.mainland_id_first4 LIKE ?
          OR p.mainland_id_last4 LIKE ? OR p.hkmo_permit_first4 LIKE ?
          OR p.hkmo_permit_last6 LIKE ? OR p.visa_status LIKE ?
          OR p.hk_submission_date LIKE ? OR p.id_last4 LIKE ?
          OR p.permit_last4 LIKE ? OR COALESCE(q.quota_number, '') LIKE ?
          OR EXISTS (SELECT 1 FROM person_documents pd WHERE pd.person_id=p.id
                     AND pd.is_deleted=0 AND (pd.original_filename LIKE ? OR pd.ocr_text LIKE ?)))
          AND (?='' OR COALESCE(p.visa_status,'未出')=?)
        ORDER BY p.id DESC
        """,
        (keyword, *([like] * 14), selected_visa_status, selected_visa_status),
    ).fetchall()

    quota_rows = db.execute(
        """
        SELECT q.*, p.name AS person_name
        FROM quotas q LEFT JOIN people p ON p.id = q.person_id AND p.is_deleted=0
        WHERE q.is_deleted=0 AND (? = '' OR COALESCE(q.quota_number, '') LIKE ?
          OR COALESCE(q.approval_number, '') LIKE ?
          OR COALESCE(q.quota_serial, '') LIKE ? OR q.company_name LIKE ?
          OR COALESCE(p.name, '') LIKE ?)
        ORDER BY q.id DESC
        """,
        (keyword, like, like, like, like, like),
    ).fetchall()
    quotas = build_quota_views(db, quota_rows)
    all_quota_rows = db.execute(
        """SELECT q.*, p.name AS person_name
           FROM quotas q LEFT JOIN people p ON p.id=q.person_id AND p.is_deleted=0
           WHERE q.is_deleted=0 ORDER BY q.id DESC"""
    ).fetchall()
    all_quota_views = build_quota_views(db, all_quota_rows)
    generate_lifecycle_nodes(db)
    refresh_risks(db, all_quota_views)
    refresh_person_case_alerts(db)

    events = db.execute(
        """
        SELECT e.*, p.name AS person_name
        FROM events e JOIN people p ON p.id = e.person_id AND p.is_deleted=0
        WHERE e.is_deleted=0
          AND (? = '' OR p.name LIKE ? OR e.event_type LIKE ? OR e.note LIKE ?)
        ORDER BY e.created_at DESC, e.id DESC LIMIT 20
        """,
        (keyword, like, like, like),
    ).fetchall()

    renewals = db.execute(
        """
        SELECT r.*, p.name AS person_name, q.quota_number, q.quota_type
        FROM renewals r
        JOIN people p ON p.id = r.person_id AND p.is_deleted=0
        JOIN quotas q ON q.id = r.quota_id AND q.is_deleted=0
        WHERE r.is_deleted=0
        ORDER BY CASE r.status WHEN '风险' THEN 0 WHEN '不可续' THEN 1 ELSE 2 END,
                 r.submission_deadline ASC
        """
    ).fetchall()

    effective_document_keyword = document_keyword or keyword
    doc_like = f"%{effective_document_keyword}%"
    documents = db.execute(
        """
        SELECT d.*, p.name AS person_name, p.company_name,
               pc.case_label,pc.status AS case_status,
               COALESCE((SELECT title FROM documents old
                         WHERE old.stored_name=d.stored_path LIMIT 1),d.original_filename) AS display_title,
               CASE d.document_type
                 WHEN 'resume' THEN '简历' WHEN 'contract' THEN '合同'
                 WHEN 'mainland_id' THEN '国内身份证' WHEN 'hkmo_permit' THEN '港澳通行证'
                 WHEN 'social_security_statement' THEN '社保声明书'
                 WHEN 'no_criminal_record' THEN '无犯罪证明'
                 WHEN 'medical_report' THEN '体检报告' WHEN 'household_register' THEN '户口本'
                 WHEN 'visa_document' THEN '签证资料'
                 WHEN 'hk_id_appointment' THEN '香港身份证预约资料'
                 WHEN 'work_proof' THEN '工作证明' ELSE '其他' END AS document_type_label,
               CASE WHEN d.expiry_date IS NOT NULL AND date(d.expiry_date)<date('now','localtime')
                    THEN 1 ELSE 0 END AS is_expired
        FROM person_documents d
        JOIN people p ON p.id=d.person_id AND p.is_deleted=0
        LEFT JOIN person_cases pc ON pc.id=d.person_case_id AND pc.is_deleted=0
        WHERE d.is_deleted=0
          AND (?='' OR d.original_filename LIKE ? OR d.ocr_text LIKE ?
               OR p.name LIKE ? OR p.company_name LIKE ?)
          AND (?='' OR (?='current' AND pc.status='active' AND d.case_binding_status='confirmed')
                    OR (?='history' AND pc.status IN ('completed','archived') AND d.case_binding_status='confirmed')
                    OR (?='unbound_person' AND d.person_id IS NULL)
                    OR (?='unconfirmed' AND (d.person_case_id IS NULL OR d.case_binding_status!='confirmed')))
        ORDER BY d.uploaded_at DESC,d.id DESC
        """,
        (effective_document_keyword, doc_like, doc_like, doc_like, doc_like,
         selected_case_scope, selected_case_scope, selected_case_scope,
         selected_case_scope, selected_case_scope),
    ).fetchall()
    workflows = db.execute(
        """
        SELECT w.*, p.name AS person_name, q.quota_number,
               (SELECT id FROM workflow_steps s WHERE s.workflow_id=w.id
                AND s.status='待处理' ORDER BY s.step_order LIMIT 1) AS active_step_id
        FROM workflow_instances w
        JOIN people p ON p.id=w.person_id AND p.is_deleted=0
        JOIN quotas q ON q.id=w.quota_id AND q.is_deleted=0
        WHERE w.is_deleted=0
        ORDER BY CASE w.status WHEN '进行中' THEN 0 ELSE 1 END, w.id DESC
        """
    ).fetchall()
    lifecycle_nodes = db.execute(
        """
        SELECT n.*, p.name AS person_name, q.quota_number,
               w.workflow_code
        FROM lifecycle_nodes n
        JOIN people p ON p.id=n.person_id AND p.is_deleted=0
        JOIN quotas q ON q.id=n.quota_id AND q.is_deleted=0
        LEFT JOIN workflow_instances w ON w.id=n.workflow_id AND w.is_deleted=0
        WHERE n.is_deleted=0
        ORDER BY CASE n.status WHEN '逾期' THEN 0 WHEN '待处理' THEN 1 ELSE 2 END,
                 n.due_date
        """
    ).fetchall()
    risks = db.execute(
        """
        SELECT r.*, p.name AS person_name, p.company_name, q.quota_number,
               pc.case_label,
               CAST(julianday(r.due_date)-julianday(date('now','localtime')) AS INTEGER) AS remaining_days
        FROM risks r
        LEFT JOIN people p ON p.id=r.person_id AND p.is_deleted=0
        LEFT JOIN quotas q ON q.id=r.quota_id AND q.is_deleted=0
        LEFT JOIN person_cases pc ON pc.id=r.person_case_id AND pc.is_deleted=0
        WHERE r.is_deleted=0
        ORDER BY CASE r.risk_level WHEN '高' THEN 0 WHEN '中' THEN 1 ELSE 2 END,
                 r.id DESC
        """
    ).fetchall()
    tasks = db.execute(
        """
        SELECT t.*, p.name AS person_name, q.quota_number, w.workflow_code,
               pc.case_label
        FROM tasks t
        JOIN people p ON p.id=t.person_id AND p.is_deleted=0
        LEFT JOIN quotas q ON q.id=t.quota_id AND q.is_deleted=0
        LEFT JOIN workflow_instances w ON w.id=t.workflow_id AND w.is_deleted=0
        LEFT JOIN person_cases pc ON pc.id=t.person_case_id AND pc.is_deleted=0
        WHERE t.is_deleted=0
        ORDER BY CASE t.status WHEN '逾期' THEN 0 WHEN '待办' THEN 1
                              WHEN '进行中' THEN 2 ELSE 3 END, t.due_date
        """
    ).fetchall()

    counts = {
        "documents": db.execute("SELECT COUNT(*) FROM person_documents WHERE is_deleted=0").fetchone()[0],
        "active_workflows": db.execute(
            "SELECT COUNT(*) FROM workflow_instances WHERE is_deleted=0 AND status='进行中'"
        ).fetchone()[0],
        "open_risks": db.execute("SELECT COUNT(*) FROM risks WHERE is_deleted=0 AND status='开放'").fetchone()[0],
        "open_tasks": db.execute(
            "SELECT COUNT(*) FROM tasks WHERE is_deleted=0 AND status!='已完成'"
        ).fetchone()[0],
        "contracts": db.execute("SELECT COUNT(*) FROM contracts WHERE is_deleted=0").fetchone()[0],
        "active_contracts": db.execute(
            "SELECT COUNT(*) FROM contracts WHERE is_deleted=0 AND status != '完成合约'"
        ).fetchone()[0],
        "expiring_contracts": db.execute(
            """SELECT COUNT(*) FROM contracts
               WHERE is_deleted=0 AND status != '完成合约'
                 AND date(end_date) BETWEEN date('now', 'localtime')
                 AND date('now', 'localtime', '+30 days')"""
        ).fetchone()[0],
        "completed_contracts": db.execute(
            "SELECT COUNT(*) FROM contracts WHERE is_deleted=0 AND status = '完成合约'"
        ).fetchone()[0],
        "people": db.execute("SELECT COUNT(*) FROM people WHERE is_deleted=0").fetchone()[0],
        "quotas": db.execute("SELECT COUNT(*) FROM quotas WHERE is_deleted=0").fetchone()[0],
        "occupied": db.execute("SELECT COUNT(*) FROM quotas WHERE is_deleted=0 AND person_id IS NOT NULL").fetchone()[0],
    }
    counts["available"] = counts["quotas"] - counts["occupied"]
    risk_counts = {
        "high": sum(1 for item in risks if item["risk_level"] == "高" and item["status"] == "开放"),
        "medium": sum(1 for item in risks if item["risk_level"] == "中" and item["status"] == "开放"),
        "low": sum(1 for item in risks if item["risk_level"] == "低" and item["status"] == "开放"),
        "resolved": sum(1 for item in risks if item["status"] == "已解决"),
    }
    task_counts = {
        status: sum(1 for item in tasks if item["status"] == status)
        for status in TASK_STATUSES
    }
    expiring_contracts_90 = db.execute(
        """SELECT c.*,
                  CAST(julianday(c.end_date)-julianday(date('now','localtime')) AS INTEGER)
                    AS remaining_days
           FROM contracts c
           WHERE c.is_deleted=0 AND c.status!='完成合约'
             AND date(c.end_date) BETWEEN date('now','localtime')
                                      AND date('now','localtime','+90 days')
           ORDER BY c.end_date LIMIT 12"""
    ).fetchall()
    certificate_risks = [
        item for item in risks
        if item["status"] == "开放" and item["risk_type"].startswith("证件")
    ]
    certificate_due_90 = []
    certificate_without_date = []
    horizon_90 = date.today() + timedelta(days=90)
    for risk in certificate_risks:
        parsed_dates = []
        for value in re.findall(r"\d{4}-\d{2}-\d{2}", risk["reason"]):
            try:
                parsed_dates.append(datetime.strptime(value, "%Y-%m-%d").date())
            except ValueError:
                continue
        due_dates = [value for value in parsed_dates if date.today() <= value <= horizon_90]
        if due_dates:
            certificate_due_90.append({"risk": risk, "due_date": min(due_dates).isoformat()})
        else:
            certificate_without_date.append(risk)

    quota_summary = {}
    for quota_type in QUOTA_TYPES:
        typed = [item for item in all_quota_views if item["quota_type"] == quota_type]
        quota_summary[quota_type] = {
            "total": len(typed),
            "occupied": sum(1 for item in typed if item["person_name"]),
            "used_months": sum(item["used_months"] for item in typed),
            "remaining_months": sum(item["remaining_months"] for item in typed),
        }

    latest_entries = db.execute(
        """SELECT e.*, p.name AS person_name
           FROM events e JOIN people p ON p.id=e.person_id AND p.is_deleted=0
           WHERE e.is_deleted=0 AND e.event_type IN ('入境','工人入境')
             AND e.id=(SELECT e2.id FROM events e2
                       WHERE e2.person_id=e.person_id AND e2.is_deleted=0
                         AND e2.event_type IN ('入境','工人入境')
                       ORDER BY e2.event_date DESC, e2.id DESC LIMIT 1)
           ORDER BY e.event_date DESC, e.id DESC LIMIT 20"""
    ).fetchall()
    entry_milestones = []
    for entry in latest_entries:
        try:
            entry_date = datetime.strptime(entry["event_date"], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        for days in (30, 45, 60):
            due_date = entry_date + timedelta(days=days)
            entry_milestones.append({
                "event_id": entry["id"],
                "person_id": entry["person_id"],
                "person_name": entry["person_name"],
                "entry_date": entry_date.isoformat(),
                "days": days,
                "due_date": due_date.isoformat(),
                "remaining_days": (due_date - date.today()).days,
                "status": "已到期" if due_date < date.today() else "待跟进",
            })
    entry_milestones.sort(key=lambda item: (item["due_date"], item["person_name"]))
    entry_milestones = entry_milestones[:18]

    risk_weight = {"高": 300, "中": 200, "低": 100}
    decision_actions = []
    for contract in expiring_contracts_90:
        related_risks = [
            risk for risk in risks
            if risk["status"] == "开放" and risk["contract_id"] == contract["contract_id"]
        ]
        related_risks.sort(key=lambda item: risk_weight.get(item["risk_level"], 0), reverse=True)
        risk_level = related_risks[0]["risk_level"] if related_risks else None
        days = contract["remaining_days"]
        urgency = max(0, 90 - max(days, 0)) + (30 if days < 0 else 0)
        decision_actions.append({
            "kind": "contract_renewal",
            "title": f"合同 {contract['contract_id']} · {contract['person_name']}",
            "reason": f"合同将在 {days} 天后到期",
            "suggestion": "建议续约 / 替补",
            "risk_level": risk_level,
            "remaining_days": days,
            "score": risk_weight.get(risk_level, 0) + urgency,
            "contract_id": contract["contract_id"],
            "can_execute": bool(contract["person_id"]),
        })
    for item in certificate_due_90:
        risk = item["risk"]
        due_date = datetime.strptime(item["due_date"], "%Y-%m-%d").date()
        days = (due_date - date.today()).days
        decision_actions.append({
            "kind": "risk_reminder",
            "title": f"证件预警 · {risk['person_name'] or '关联人员'}",
            "reason": risk["reason"],
            "suggestion": "建议启动更新流程",
            "risk_level": risk["risk_level"],
            "remaining_days": days,
            "score": risk_weight.get(risk["risk_level"], 0) + max(0, 90 - days),
            "risk_id": risk["id"],
            "can_execute": bool(risk["person_id"]),
        })
    quota_by_id = {item["id"]: item for item in all_quota_views}
    raw_quota_risks = [
        risk for risk in risks
        if risk["status"] == "开放" and risk["risk_type"] == "名额" and risk["quota_id"]
    ]
    quota_risk_by_quota = {}
    for risk in raw_quota_risks:
        current = quota_risk_by_quota.get(risk["quota_id"])
        if not current or risk_weight.get(risk["risk_level"], 0) > risk_weight.get(current["risk_level"], 0):
            quota_risk_by_quota[risk["quota_id"]] = risk
    quota_risks = list(quota_risk_by_quota.values())
    for risk in quota_risks:
        quota = quota_by_id.get(risk["quota_id"])
        decision_actions.append({
            "kind": "quota_replacement",
            "title": f"名额风险 · {quota['display_label'] if quota else risk['quota_number'] or '关联名额'}",
            "reason": risk["reason"],
            "suggestion": "建议释放或替补",
            "risk_level": risk["risk_level"],
            "remaining_days": None,
            "score": risk_weight.get(risk["risk_level"], 0),
            "risk_id": risk["id"],
            "can_execute": bool(quota and quota["person_name"]),
        })
    for milestone in entry_milestones:
        days = milestone["remaining_days"]
        decision_actions.append({
            "kind": "event_reminder",
            "title": f"入境后{milestone['days']}天 · {milestone['person_name']}",
            "reason": f"入境日 {milestone['entry_date']}，节点 {milestone['due_date']}",
            "suggestion": "建议创建提醒任务",
            "risk_level": None,
            "remaining_days": days,
            "score": max(0, 90 - max(days, 0)) + (30 if days < 0 else 0),
            "event_id": milestone["event_id"],
            "days": milestone["days"],
            "can_execute": True,
        })
    decision_actions.sort(
        key=lambda item: (-item["score"], item["remaining_days"] if item["remaining_days"] is not None else 9999)
    )
    for rank, action in enumerate(decision_actions, 1):
        action["rank"] = rank
        action["priority"] = "P0" if action["score"] >= 300 else "P1" if action["score"] >= 200 else "P2"
    decision_actions = decision_actions[:12]

    boss_dashboard = {
        "risk_counts": risk_counts,
        "expiring_contracts_90": expiring_contracts_90,
        "certificate_due_90": certificate_due_90,
        "certificate_without_date": certificate_without_date,
        "quota_summary": quota_summary,
        "entry_milestones": entry_milestones,
        "quota_risks": quota_risks,
        "decision_actions": decision_actions,
    }
    search_results = {"people": [], "contracts": [], "quotas": [], "events": []}
    quick_result = None
    one_line_result = None
    if keyword:
        search_results = {
            "people": people[:6],
            "contracts": contracts[:6],
            "quotas": quotas[:6],
            "events": events[:6],
        }
        matched_person = db.execute(
            """SELECT p.*, q.id AS quota_id, q.quota_number, q.quota_type,
                      q.company_name AS quota_company
               FROM people p LEFT JOIN quotas q ON q.person_id=p.id AND q.is_deleted=0
               WHERE p.is_deleted=0 AND p.id=? LIMIT 1""",
            (people[0]["id"],),
        ).fetchone() if people else None
        if matched_person:
            person_contracts = db.execute(
                """SELECT c.*,
                          CAST(julianday(c.end_date)-julianday(date('now','localtime')) AS INTEGER)
                            AS remaining_days
                   FROM contracts c
                   WHERE c.is_deleted=0 AND (c.person_id=? OR c.person_name=?)
                   ORDER BY c.end_date DESC""",
                (matched_person["id"], matched_person["name"]),
            ).fetchall()
            person_risks = db.execute(
                """SELECT * FROM risks
                   WHERE is_deleted=0 AND (person_id=? OR contract_id IN
                     (SELECT contract_id FROM contracts WHERE is_deleted=0 AND (person_id=? OR person_name=?)))
                   ORDER BY CASE risk_level WHEN '高' THEN 0 WHEN '中' THEN 1 ELSE 2 END""",
                (matched_person["id"], matched_person["id"], matched_person["name"]),
            ).fetchall()
            quick_result = {
                "person": matched_person,
                "contracts": person_contracts,
                "risks": person_risks,
            }
        if quick_result:
            open_risks = [item for item in quick_result["risks"] if item["status"] == "开放"]
            latest_contract = quick_result["contracts"][0] if quick_result["contracts"] else None
            if open_risks:
                leading = open_risks[0]
                next_action = (
                    "建议续约 / 替补" if leading["risk_type"] == "合同"
                    else "建议启动更新流程" if leading["risk_type"].startswith("证件")
                    else "建议释放或替补" if leading["risk_type"] == "名额"
                    else "建议处理开放风险"
                )
            else:
                next_action = "按当前业务状态继续跟进"
            one_line_result = {
                "subject": quick_result["person"]["name"],
                "current_status": (
                    f"合同 {latest_contract['status']} · 名额 {quick_result['person']['quota_number'] or '未分配'}"
                    if latest_contract else f"暂无合同 · 名额 {quick_result['person']['quota_number'] or '未分配'}"
                ),
                "next_action": next_action,
                "needs_action": "Yes" if open_risks else "No",
                "detail_url": url_for("person_detail", person_id=quick_result["person"]["id"]),
            }
        elif search_results["contracts"]:
            contract = search_results["contracts"][0]
            needs_action = contract["remaining_days"] <= 90 and contract["status"] != "完成合约"
            one_line_result = {
                "subject": contract["contract_id"],
                "current_status": f"{contract['status']} · 剩余 {contract['remaining_days']} 天",
                "next_action": "建议续约 / 替补" if needs_action else "按当前合同状态继续跟进",
                "needs_action": "Yes" if needs_action else "No",
                "detail_url": url_for("contract_detail", contract_id=contract["contract_id"]),
            }
        elif search_results["quotas"]:
            quota = search_results["quotas"][0]
            quota_risk = next(
                (item for item in quota_risks if item["quota_id"] == quota["id"]), None
            )
            one_line_result = {
                "subject": quota["display_label"],
                "current_status": f"{quota['quota_type']} · 当前人员 {quota['person_name'] or '暂无'} · 剩余 {quota['remaining_months']} 个月",
                "next_action": "建议释放或替补" if quota_risk else "继续按当前使用记录跟进",
                "needs_action": "Yes" if quota_risk else "No",
                "detail_url": url_for("quota.detail", quota_id=quota["id"]),
            }
        elif search_results["events"]:
            event = search_results["events"][0]
            is_entry = event["event_type"] in {"入境", "工人入境"}
            one_line_result = {
                "subject": f"{event['person_name']} · {event['event_type']}",
                "current_status": f"最近事件 {event['event_type']} · {event['created_at']}",
                "next_action": "建议检查30 / 45 / 60天提醒" if is_entry else "按事件状态继续跟进",
                "needs_action": "Yes" if is_entry else "No",
                "detail_url": url_for("index", view="events", q=event["person_name"]),
            }
    all_people = db.execute(
        "SELECT id,name,company_name,worker_type,entry_permit_no,visa_status FROM people WHERE is_deleted=0 ORDER BY name"
    ).fetchall()
    all_person_cases = db.execute(
        """SELECT pc.*,p.name AS person_name FROM person_cases pc
           JOIN people p ON p.id=pc.person_id AND p.is_deleted=0
           WHERE pc.is_deleted=0 ORDER BY pc.status='active' DESC,pc.start_date DESC,pc.id DESC"""
    ).fetchall()
    available_people = db.execute(
        """SELECT p.id, p.name FROM people p
           LEFT JOIN quotas q ON q.person_id = p.id AND q.is_deleted=0
           WHERE p.is_deleted=0 AND q.id IS NULL ORDER BY p.name"""
    ).fetchall()

    return render_template(
        "index.html",
        contracts=contracts,
        people=people,
        quotas=quotas,
        events=events,
        renewals=renewals,
        documents=documents,
        workflows=workflows,
        lifecycle_nodes=lifecycle_nodes,
        risks=risks,
        tasks=tasks,
        risk_counts=risk_counts,
        task_counts=task_counts,
        search_results=search_results,
        quick_result=quick_result,
        one_line_result=one_line_result,
        boss_dashboard=boss_dashboard,
        counts=counts,
        all_people=all_people,
        all_person_cases=all_person_cases,
        available_people=available_people,
        keyword=keyword,
        selected_status=selected_status,
        selected_visa_status=selected_visa_status,
        contract_statuses=CONTRACT_STATUSES,
        quota_types=QUOTA_TYPES,
        expiry_checks=EXPIRY_CHECKS,
        task_statuses=TASK_STATUSES,
        active_view=active_view,
        document_keyword=document_keyword,
        selected_case_scope=selected_case_scope,
        document_upload_result=document_upload_result,
        import_result=import_result,
        today=date.today().isoformat(),
    )


@app.get("/people")
def people_page():
    return index("people")


@app.get("/people/<int:person_id>")
def person_detail(person_id):
    profile = get_person_profile(
        get_db(), person_id, can_view_sensitive=session.get("role") == "admin"
    )
    if profile is None:
        abort(404)
    return render_template(
        "person_detail.html", **profile,
        document_upload_result=session.pop("person_document_upload_result", None),
    )


@app.post("/people/<int:person_id>/profile")
def update_person_profile(person_id):
    db = get_db()
    if not db.execute(
        "SELECT 1 FROM people WHERE id=? AND is_deleted=0", (person_id,)
    ).fetchone():
        abort(404)
    name = request.form.get("person_name", "").strip()
    gender = request.form.get("gender", "").strip()
    worker_type = request.form.get("worker_type", "new").strip()
    if not name or gender not in {"男", "女"} or worker_type not in {"new", "renewal"}:
        flash("姓名、性别和人员类型不完整。", "error")
        return redirect(url_for("person_detail", person_id=person_id))
    fields = (
        "company_name", "birth_date", "birth_year_month", "mainland_id_first4",
        "mainland_id_last4", "hkmo_permit_first4", "hkmo_permit_last6",
        "entry_permit_no", "hk_submission_date", "visa_status_date", "visa_status",
        "hk_id_appointment_status", "remarks",
    )
    values = [(request.form.get(field) or "").strip() or None for field in fields]
    if values[1] and not values[2]:
        values[2] = values[1][:7]
    if values[5]:
        values[5] = values[5].upper()
    digit_fragments = {
        "身份证前四位": values[3], "身份证后四位": values[4],
        "通行证后六位": values[6],
    }
    expected_lengths = {"身份证前四位": 4, "身份证后四位": 4, "通行证后六位": 6}
    for label, value in digit_fragments.items():
        if value and (not value.isdigit() or len(value) != expected_lengths[label]):
            flash(f"{label}必须为{expected_lengths[label]}位数字。", "error")
            return redirect(url_for("person_detail", person_id=person_id))
    if values[5] and len(values[5]) != 4:
        flash("通行证前四位必须为4位字符。", "error")
        return redirect(url_for("person_detail", person_id=person_id))
    if values[10] and values[10] not in {"未出", "待缴费", "已出"}:
        flash("VISA状态只能是：未出、待缴费、已出。", "error")
        return redirect(url_for("person_detail", person_id=person_id))
    if values[2] and not re.fullmatch(r"\d{4}-\d{2}", values[2]):
        flash("出生年月格式必须为 YYYY-MM。", "error")
        return redirect(url_for("person_detail", person_id=person_id))
    db.execute(
        f"""UPDATE people SET name=?,person_name=?,gender=?,worker_type=?,
                   {','.join(f'{field}=?' for field in fields)},
                   id_last4=?,permit_last4=? WHERE id=?""",
        (
            name, name, gender, worker_type, *values,
            (request.form.get("mainland_id_last4") or "").strip() or None,
            ((request.form.get("hkmo_permit_last6") or "").strip()[-4:] or None),
            person_id,
        ),
    )
    record_standard_event(db, "person_updated", "人员档案字段已更新", person_id=person_id)
    db.commit()
    flash("人员档案已保存。", "success")
    return redirect(url_for("person_detail", person_id=person_id))


@app.post("/people/<int:person_id>/cases")
def create_person_case(person_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM people WHERE id=? AND is_deleted=0", (person_id,)).fetchone():
        abort(404)
    case_type = request.form.get("case_type", "other")
    status = request.form.get("status", "active")
    case_label = request.form.get("case_label", "").strip()
    if case_type not in {"new_contract", "renewal", "replacement", "other"} or status not in {"active", "completed", "archived"} or not case_label:
        flash("办理周期类型、名称或状态无效。", "error")
        return redirect(url_for("person_detail", person_id=person_id))
    contract_start_date = request.form.get("contract_start_date") or request.form.get("start_date") or None
    contract_end_date = request.form.get("contract_end_date") or request.form.get("end_date") or None
    endorsement_expiry_date = request.form.get("endorsement_expiry_date") or None
    calculated = calculate_renewal_alert_dates({
        "contract_end_date": contract_end_date,
        "endorsement_expiry_date": endorsement_expiry_date,
    })
    db.execute(
        """INSERT INTO person_cases
           (person_id,case_type,case_label,start_date,end_date,contract_start_date,
            contract_end_date,contract_restart_due_date,endorsement_expiry_date,
            document_collection_due_date,quota_id,contract_id,status,remarks)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (person_id, case_type, case_label,
         request.form.get("start_date") or None, request.form.get("end_date") or None,
         contract_start_date, contract_end_date, calculated["contract_restart_due_date"],
         endorsement_expiry_date, calculated["document_collection_due_date"],
         request.form.get("quota_id") or None, request.form.get("contract_id") or None,
         status, request.form.get("remarks") or None),
    )
    db.commit()
    flash("办理周期已创建。", "success")
    return redirect(url_for("person_detail", person_id=person_id))


@app.post("/people/<int:person_id>/cases/<int:case_id>/renewal-alerts")
def update_person_case_renewal_alerts(person_id, case_id):
    db = get_db()
    person_case = db.execute(
        "SELECT * FROM person_cases WHERE id=? AND person_id=? AND is_deleted=0",
        (case_id, person_id),
    ).fetchone()
    if not person_case:
        abort(404)
    alert_status = request.form.get("renewal_alert_status", "pending")
    if alert_status not in {"pending", "reminded", "completed", "ignored"}:
        alert_status = "pending"
    values = {
        "contract_start_date": request.form.get("contract_start_date") or None,
        "contract_end_date": request.form.get("contract_end_date") or None,
        "endorsement_expiry_date": request.form.get("endorsement_expiry_date") or None,
    }
    calculated = calculate_renewal_alert_dates(values)
    db.execute(
        """UPDATE person_cases SET contract_start_date=?,contract_end_date=?,
                  contract_restart_due_date=?,endorsement_expiry_date=?,
                  document_collection_due_date=?,renewal_alert_status=?,
                  updated_at=CURRENT_TIMESTAMP WHERE id=?""",
        (values["contract_start_date"], values["contract_end_date"],
         calculated["contract_restart_due_date"], values["endorsement_expiry_date"],
         calculated["document_collection_due_date"], alert_status, case_id),
    )
    db.commit()
    refresh_person_case_alerts(db)
    flash("续约提醒日期已保存。", "success")
    return redirect(url_for("person_detail", person_id=person_id))


@app.get("/contracts")
def contracts_page():
    return index("contracts")


@app.get("/quotas")
def quotas_page():
    return index("quotas")


@app.post("/upload/person_excel")
def upload_person_excel():
    try:
        frame, pd = read_excel_upload(request.files.get("file"), ("姓名", "性别"))
    except (ValueError, RuntimeError) as error:
        app.logger.warning("Person Excel import rejected: %s", error)
        return excel_import_result("人员 Excel", "people", 0, 0, 1, [str(error)])
    if frame.empty:
        return excel_import_result(
            "人员 Excel", "people", 0, 0, 1, ["Excel 中没有可导入的数据。"]
        )
    db = get_db()
    imported = skipped = failed = 0
    errors = []
    try:
        db.execute("BEGIN IMMEDIATE")
        for index, row in frame.iterrows():
            row_number = int(index) + 2
            db.execute("SAVEPOINT excel_person_row")
            try:
                name = excel_text(row.get("姓名"), pd)
                gender = excel_text(row.get("性别"), pd)
                if not name:
                    raise ValueError(f"第 {row_number} 行“姓名”不能为空。")
                if gender not in {"男", "女"}:
                    raise ValueError(f"第 {row_number} 行“性别”必须为男或女。")
                company_name = excel_text(row.get("公司名"), pd)
                introducer = excel_text(row.get("介绍人"), pd)
                id_last4 = excel_last4(
                    row.get("身份证后四位"), pd, row_number, "身份证后四位"
                )
                permit_last4 = excel_last4(
                    row.get("港澳通行证后四位"), pd, row_number, "港澳通行证后四位"
                )
                if id_last4:
                    duplicate = db.execute(
                        "SELECT 1 FROM people WHERE name=? AND id_last4=? AND is_deleted=0 LIMIT 1",
                        (name, id_last4),
                    ).fetchone()
                elif permit_last4:
                    duplicate = db.execute(
                        "SELECT 1 FROM people WHERE name=? AND permit_last4=? AND is_deleted=0 LIMIT 1",
                        (name, permit_last4),
                    ).fetchone()
                else:
                    duplicate = db.execute(
                        """SELECT 1 FROM people WHERE name=? AND gender=? AND is_deleted=0
                           AND COALESCE(company_name,'')=COALESCE(?,'') LIMIT 1""",
                        (name, gender, company_name),
                    ).fetchone()
                if duplicate:
                    skipped += 1
                else:
                    cursor = db.execute(
                        """INSERT INTO people
                           (name, gender, company_name, introducer, id_last4, permit_last4)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (name, gender, company_name, introducer, id_last4, permit_last4),
                    )
                    db.execute(
                        """INSERT INTO events
                           (person_id, event_type, note, event_date, created_at)
                           VALUES (?, '登记', 'Excel批量导入', ?, ?)""",
                        (
                            cursor.lastrowid, date.today().isoformat(),
                            datetime.now().strftime("%Y-%m-%d %H:%M"),
                        ),
                    )
                    imported += 1
                db.execute("RELEASE SAVEPOINT excel_person_row")
            except (ValueError, sqlite3.IntegrityError) as error:
                db.execute("ROLLBACK TO SAVEPOINT excel_person_row")
                db.execute("RELEASE SAVEPOINT excel_person_row")
                failed += 1
                errors.append(str(error))
        db.commit()
    except Exception:
        db.rollback()
        raise
    if failed:
        app.logger.warning("Person Excel row errors: %s", errors[:20])
    return excel_import_result(
        "人员 Excel", "people", imported, skipped, failed, errors
    )


@app.post("/upload/quota_excel")
def upload_quota_excel():
    try:
        frame, pd = read_excel_upload(request.files.get("file"), ("配额类型", "公司名"))
    except (ValueError, RuntimeError) as error:
        app.logger.warning("Quota Excel import rejected: %s", error)
        return excel_import_result("配额 Excel", "quotas", 0, 0, 1, [str(error)])
    if frame.empty:
        return excel_import_result(
            "配额 Excel", "quotas", 0, 0, 1, ["Excel 中没有可导入的数据。"]
        )
    db = get_db()
    imported = skipped = failed = 0
    errors = []
    try:
        db.execute("BEGIN IMMEDIATE")
        for index, row in frame.iterrows():
            row_number = int(index) + 2
            db.execute("SAVEPOINT excel_quota_row")
            try:
                quota_type = excel_text(row.get("配额类型"), pd)
                company_name = excel_text(row.get("公司名"), pd)
                if quota_type not in QUOTA_TYPES:
                    raise ValueError(f"第 {row_number} 行“配额类型”必须为 SWD 或 LD。")
                if not company_name:
                    raise ValueError(f"第 {row_number} 行“公司名”不能为空。")
                approval_no = excel_text(row.get("批文号"), pd)
                quota_no = excel_text(row.get("配额序号"), pd)
                user_name = excel_text(row.get("使用人"), pd)
                start_date = excel_date(row.get("开始日期"), pd, row_number, "开始日期")
                end_date = excel_date(row.get("结束日期"), pd, row_number, "结束日期")
                user_id = None
                if user_name:
                    matches = db.execute(
                        "SELECT id FROM people WHERE name=? AND is_deleted=0 ORDER BY id LIMIT 2",
                        (user_name,),
                    ).fetchall()
                    if not matches:
                        raise ValueError(f"第 {row_number} 行使用人“{user_name}”不存在。")
                    if len(matches) > 1:
                        raise ValueError(f"第 {row_number} 行使用人“{user_name}”存在重名。")
                    user_id = matches[0]["id"]
                if quota_no:
                    duplicate = db.execute(
                        """SELECT 1 FROM quotas
                           WHERE is_deleted=0 AND (quota_serial=? OR quota_number=?) LIMIT 1""",
                        (quota_no, quota_no),
                    ).fetchone()
                elif approval_no:
                    duplicate = db.execute(
                        """SELECT 1 FROM quotas WHERE is_deleted=0 AND approval_number=?
                           AND quota_type=? AND company_name=? LIMIT 1""",
                        (approval_no, quota_type, company_name),
                    ).fetchone()
                else:
                    duplicate = db.execute(
                        """SELECT 1 FROM quotas WHERE is_deleted=0 AND quota_type=? AND company_name=?
                           AND COALESCE(person_id,0)=COALESCE(?,0)
                           AND COALESCE(start_date,'')=COALESCE(?,'')
                           AND COALESCE(expiry_date,'')=COALESCE(?,'') LIMIT 1""",
                        (quota_type, company_name, user_id, start_date, end_date),
                    ).fetchone()
                if duplicate:
                    skipped += 1
                else:
                    cursor = db.execute(
                        """INSERT INTO quotas
                           (quota_number, company_name, quota_type, approval_number,
                            quota_serial, person_id, start_date, expiry_date,
                            max_replacement_count,status)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            quota_no, company_name, quota_type, approval_no,
                            quota_no, user_id, start_date, end_date,
                            quota_replacement_limit(quota_type),
                            "in_use" if user_id and start_date else "active",
                        ),
                    )
                    if user_id and start_date:
                        db.execute(
                            """INSERT INTO quota_usages
                               (quota_id, person_id, start_date) VALUES (?, ?, ?)""",
                            (cursor.lastrowid, user_id, start_date),
                        )
                        db.execute(
                            "UPDATE quotas SET usage_count=usage_count+1 WHERE id=?",
                            (cursor.lastrowid,),
                        )
                    record_standard_event(
                        db, "quota_imported",
                        f"配额 Excel 导入：{quota_no or cursor.lastrowid}",
                        person_id=user_id, quota_id=cursor.lastrowid,
                    )
                    imported += 1
                db.execute("RELEASE SAVEPOINT excel_quota_row")
            except (ValueError, sqlite3.IntegrityError) as error:
                db.execute("ROLLBACK TO SAVEPOINT excel_quota_row")
                db.execute("RELEASE SAVEPOINT excel_quota_row")
                failed += 1
                errors.append(str(error))
        refresh_standard_risks(db)
        db.commit()
    except Exception:
        db.rollback()
        raise
    if failed:
        app.logger.warning("Quota Excel row errors: %s", errors[:20])
    return excel_import_result(
        "配额 Excel", "quotas", imported, skipped, failed, errors
    )


@app.get("/risks")
def risks_page():
    return index("risks")


@app.get("/tasks")
def tasks_page():
    return index("tasks")


@app.get("/api/dashboard/status")
def dashboard_status():
    """Recalculate time-sensitive business state for the live dashboard."""
    db = get_db()
    refresh_renewal_statuses(db)
    refresh_standard_risks(db)
    generate_lifecycle_nodes(db)
    quota_rows = db.execute(
        """SELECT q.*, p.name AS person_name FROM quotas q
           LEFT JOIN people p ON p.id=q.person_id AND p.is_deleted=0
           WHERE q.is_deleted=0 ORDER BY q.id DESC"""
    ).fetchall()
    quota_views = build_quota_views(db, quota_rows)
    refresh_risks(db, quota_views)
    certificate_due_count = 0
    certificate_horizon = date.today() + timedelta(days=90)
    for row in db.execute(
        "SELECT reason FROM risks WHERE is_deleted=0 AND status='开放' AND risk_type LIKE '证件%'"
    ).fetchall():
        for value in re.findall(r"\d{4}-\d{2}-\d{2}", row["reason"]):
            try:
                expiry = datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError:
                continue
            if date.today() <= expiry <= certificate_horizon:
                certificate_due_count += 1
                break
    payload = {
        "risks": {
            "open": db.execute("SELECT COUNT(*) FROM risks WHERE is_deleted=0 AND status='开放'").fetchone()[0],
            "high": db.execute(
                "SELECT COUNT(*) FROM risks WHERE is_deleted=0 AND status='开放' AND risk_level='高'"
            ).fetchone()[0],
            "medium": db.execute(
                "SELECT COUNT(*) FROM risks WHERE is_deleted=0 AND status='开放' AND risk_level='中'"
            ).fetchone()[0],
            "low": db.execute(
                "SELECT COUNT(*) FROM risks WHERE is_deleted=0 AND status='开放' AND risk_level='低'"
            ).fetchone()[0],
        },
        "tasks": {
            "open": db.execute("SELECT COUNT(*) FROM tasks WHERE is_deleted=0 AND status!='已完成'").fetchone()[0],
            "overdue": db.execute("SELECT COUNT(*) FROM tasks WHERE is_deleted=0 AND status='逾期'").fetchone()[0],
        },
        "contracts_90": db.execute(
            """SELECT COUNT(*) FROM contracts WHERE is_deleted=0 AND status!='完成合约'
               AND date(end_date) BETWEEN date('now','localtime')
                                      AND date('now','localtime','+90 days')"""
        ).fetchone()[0],
        "certificate_risks": certificate_due_count,
        "quotas": {
            quota_type: {
                "total": sum(1 for item in quota_views
                             if item["quota_type"] == quota_type),
                "occupied": sum(1 for item in quota_views
                                if item["quota_type"] == quota_type and item["person_name"]),
            }
            for quota_type in QUOTA_TYPES
        },
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    response, _status = api_response(0, "ok", payload)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/stream/reminders")
def reminder_stream():
    """Compatibility endpoint: one fast response, never a long-lived worker."""
    response, status = api_response(0, "ok", collect_push_reminders())
    response.headers["Cache-Control"] = "no-store"
    return response, status


@app.get("/api/reminders")
def reminders_api():
    return api_response(0, "ok", collect_push_reminders())


@app.get("/api/reminders/stream")
def legacy_reminder_stream_api():
    return api_response(
        0, "Reminder stream now uses short polling", collect_push_reminders()
    )


@app.post("/dashboard/actions/contract-renewal")
def create_contract_renewal_task():
    contract_id = request.form.get("contract_id", "").strip()
    db = get_db()
    contract = db.execute(
        "SELECT * FROM contracts WHERE contract_id=? AND is_deleted=0", (contract_id,)
    ).fetchone()
    if not contract or not contract["person_id"]:
        flash("合同不存在或尚未关联人员，无法创建续约任务。", "error")
        return redirect(url_for("index"))
    quota = db.execute(
        "SELECT id FROM quotas WHERE person_id=? AND is_deleted=0",
        (contract["person_id"],),
    ).fetchone()
    source_key = f"DECISION:RENEWAL:{contract['contract_id']}:{contract['end_date']}"
    cursor = db.execute(
        """INSERT OR IGNORE INTO tasks
           (person_id, quota_id, title, due_date, trigger_type, source_key)
           VALUES (?, ?, ?, ?, '决策驾驶舱', ?)""",
        (
            contract["person_id"], quota["id"] if quota else None,
            f"合同续约评估：{contract['contract_id']}", contract["end_date"], source_key,
        ),
    )
    db.commit()
    flash("续约任务已创建。" if cursor.rowcount else "该合同的续约任务已存在。", "success")
    return redirect(url_for("index"))


@app.post("/dashboard/actions/quota-replacement")
def create_quota_replacement_flow():
    risk_id = request.form.get("risk_id", "").strip()
    db = get_db()
    risk = db.execute(
        """SELECT r.*, q.person_id, q.quota_number FROM risks r
           JOIN quotas q ON q.id=r.quota_id AND q.is_deleted=0
           WHERE r.id=? AND r.is_deleted=0 AND r.status='开放' AND r.risk_type='名额'""",
        (risk_id,),
    ).fetchone()
    if not risk or not risk["person_id"]:
        flash("该名额风险没有当前使用人，无法创建替补流程。", "error")
        return redirect(url_for("index"))
    existing = db.execute(
        """SELECT workflow_code FROM workflow_instances
           WHERE quota_id=? AND is_deleted=0
             AND workflow_name='名额替补流程' AND status='进行中'
           ORDER BY id DESC LIMIT 1""",
        (risk["quota_id"],),
    ).fetchone()
    if existing:
        flash(f"名额替补流程已存在：{existing['workflow_code']}", "success")
        return redirect(url_for("index"))
    workflow_code = create_workflow_record(
        db, risk["person_id"], risk["quota_id"], "名额替补流程"
    )
    db.commit()
    flash(f"名额替补流程已创建：{workflow_code}", "success")
    return redirect(url_for("index"))


@app.post("/dashboard/actions/reminder")
def create_decision_reminder_task():
    db = get_db()
    risk_id = request.form.get("risk_id", "").strip()
    event_id = request.form.get("event_id", "").strip()
    days_value = request.form.get("days", "").strip()
    person_id = quota_id = None
    title = due_date = source_key = None
    if risk_id:
        risk = db.execute(
            "SELECT * FROM risks WHERE id=? AND is_deleted=0 AND status='开放' AND person_id IS NOT NULL",
            (risk_id,),
        ).fetchone()
        if risk:
            parsed_dates = []
            for value in re.findall(r"\d{4}-\d{2}-\d{2}", risk["reason"]):
                try:
                    parsed_dates.append(datetime.strptime(value, "%Y-%m-%d").date())
                except ValueError:
                    continue
            future_dates = [value for value in parsed_dates if value >= date.today()]
            if future_dates:
                person_id = risk["person_id"]
                due_date = min(future_dates).isoformat()
                title = f"证件更新提醒：{risk['reason']}"
                source_key = f"DECISION:RISK:{risk['id']}:{due_date}"
    elif event_id and days_value in {"30", "45", "60"}:
        event = db.execute(
            "SELECT * FROM events WHERE id=? AND is_deleted=0 AND event_type IN ('入境','工人入境')", (event_id,)
        ).fetchone()
        if event:
            try:
                entry_date = datetime.strptime(event["event_date"], "%Y-%m-%d").date()
            except (ValueError, TypeError):
                entry_date = None
            if entry_date:
                person_id = event["person_id"]
                due_date = (entry_date + timedelta(days=int(days_value))).isoformat()
                title = f"入境后{days_value}天提醒"
                source_key = f"DECISION:EVENT:{event['id']}:{days_value}"
    if not all((person_id, title, due_date, source_key)):
        flash("现有风险或事件数据不足，无法创建提醒任务。", "error")
        return redirect(url_for("index"))
    quota = db.execute(
        "SELECT id FROM quotas WHERE person_id=? AND is_deleted=0", (person_id,)
    ).fetchone()
    quota_id = quota["id"] if quota else None
    cursor = db.execute(
        """INSERT OR IGNORE INTO tasks
           (person_id, quota_id, title, due_date, trigger_type, source_key)
           VALUES (?, ?, ?, ?, '决策驾驶舱', ?)""",
        (person_id, quota_id, title, due_date, source_key),
    )
    db.commit()
    flash("提醒任务已创建。" if cursor.rowcount else "该提醒任务已存在。", "success")
    return redirect(url_for("index"))


@app.get("/contracts/<contract_id>")
def contract_detail(contract_id):
    db = get_db()
    contract = db.execute(
        """SELECT c.*, p.id_last4, p.permit_last4,
                  CAST(julianday(c.end_date)-julianday(date('now','localtime')) AS INTEGER)
                    AS remaining_days
           FROM contracts c LEFT JOIN people p ON p.id=c.person_id AND p.is_deleted=0
           WHERE c.contract_id=? AND c.is_deleted=0""",
        (contract_id,),
    ).fetchone()
    if not contract:
        abort(404)
    person_id = contract["person_id"]
    quota = None
    events = []
    workflows = []
    documents = []
    tasks = []
    if person_id:
        quota = db.execute(
            """SELECT q.*, p.name AS person_name FROM quotas q
               LEFT JOIN people p ON p.id=q.person_id AND p.is_deleted=0
               WHERE q.person_id=? AND q.is_deleted=0""",
            (person_id,),
        ).fetchone()
        events = db.execute(
            "SELECT * FROM events WHERE person_id=? AND is_deleted=0 ORDER BY created_at DESC, id DESC",
            (person_id,),
        ).fetchall()
        workflows = db.execute(
            """SELECT w.*, q.quota_number FROM workflow_instances w
               JOIN quotas q ON q.id=w.quota_id AND q.is_deleted=0
               WHERE w.person_id=? AND w.is_deleted=0 ORDER BY w.id DESC""",
            (person_id,),
        ).fetchall()
        documents = db.execute(
            """SELECT d.*, q.quota_number, w.workflow_code FROM documents d
               JOIN quotas q ON q.id=d.quota_id
               JOIN workflow_instances w ON w.id=d.workflow_id
               WHERE d.person_id=? AND d.is_deleted=0 AND q.is_deleted=0
                 AND w.is_deleted=0 ORDER BY d.id DESC""",
            (person_id,),
        ).fetchall()
        tasks = db.execute(
            """SELECT t.*, q.quota_number FROM tasks t
               LEFT JOIN quotas q ON q.id=t.quota_id
               WHERE t.person_id=? AND t.is_deleted=0 ORDER BY t.due_date""",
            (person_id,),
        ).fetchall()
    risks = db.execute(
        "SELECT * FROM risks WHERE contract_id=? AND is_deleted=0 ORDER BY id DESC", (contract_id,)
    ).fetchall()
    return render_template(
        "contract_detail.html", contract=contract, quota=quota, events=events,
        workflows=workflows, documents=documents, tasks=tasks, risks=risks,
        contract_statuses=CONTRACT_STATUSES, task_statuses=TASK_STATUSES,
    )


@app.post("/contracts/<contract_id>/status")
def update_contract_status(contract_id):
    status = normalize_contract_status(request.form.get("status", ""))
    if status not in CONTRACT_STATUSES:
        flash("合同状态无效。", "error")
        return redirect(url_for("contract_detail", contract_id=contract_id))
    db = get_db()
    try:
        transition_contract(db, contract_id, status, date.today().isoformat())
    except ValueError as error:
        db.rollback()
        flash(str(error), "error")
        return redirect(url_for("contract_detail", contract_id=contract_id))
    standard_contract = db.execute(
        "SELECT id, person_id, quota_id FROM contract WHERE contract_no=?",
        (contract_id,),
    ).fetchone()
    if standard_contract:
        record_standard_event(
            db, "contract_updated", f"合同状态更新为：{status}",
            person_id=standard_contract["person_id"],
            quota_id=standard_contract["quota_id"],
            contract_id=standard_contract["id"],
        )
    db.commit()
    flash(f"合同状态已更新为：{status}", "success")
    return redirect(url_for("contract_detail", contract_id=contract_id))


@app.post("/contracts")
def add_contract():
    contract_id = request.form.get("contract_id", "").strip()
    person_name = request.form.get("person_name", "").strip()
    company = request.form.get("company", "").strip()
    status = "制作合同"
    person_id = request.form.get("person_id") or None
    quota_id = request.form.get("quota_id") or None

    if (
        not all((contract_id, company, quota_id))
        or (not person_name and not person_id)
    ):
        flash("请完整填写合同、人员与配额信息。", "error")
        return redirect(url_for("index"))
    db = get_db()
    if person_id:
        person = db.execute("SELECT id, name FROM people WHERE id=? AND is_deleted=0", (person_id,)).fetchone()
        if not person:
            flash("选择的人员不存在。", "error")
            return redirect(url_for("index"))
        person_name = person["name"]
    quota = db.execute("SELECT * FROM quotas WHERE id=? AND is_deleted=0", (quota_id,)).fetchone()
    if not quota or quota["status"] in {"invalid", "exhausted"}:
        flash("配额不存在、已失效或已耗尽。", "error")
        return redirect(url_for("index"))
    try:
        db.execute(
            """INSERT INTO contracts
               (contract_id, person_name, company, status, person_id, quota_id, cycle_index)
               VALUES (?, ?, ?, ?, ?, ?, 1)""",
            (contract_id, person_name, company, status, person_id, quota_id),
        )
        standard_contract = db.execute(
            "SELECT id, quota_id FROM contract WHERE contract_no=?", (contract_id,)
        ).fetchone()
        record_standard_event(
            db, "contract_created", f"合同已创建：{contract_id}",
            person_id=int(person_id) if person_id else None,
            quota_id=int(quota_id),
            contract_id=standard_contract["id"] if standard_contract else None,
        )
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        flash("合同编号已存在，或合同数据不符合约束。", "error")
        return redirect(url_for("index"))
    flash(f"已添加合同：{contract_id}", "success")
    return redirect(url_for("index"))


@app.post("/renewals")
def add_renewal():
    person_id = request.form.get("person_id", "").strip()
    quota_id = request.form.get("quota_id", "").strip()
    end_date_value = request.form.get("current_contract_end_date", "").strip()
    passport_check = request.form.get("passport_expiry_check", "").strip()
    id_card_check = request.form.get("id_card_expiry_check", "").strip()
    if (
        not person_id
        or not quota_id
        or passport_check not in EXPIRY_CHECKS
        or id_card_check not in EXPIRY_CHECKS
    ):
        flash("请完整填写续期评估信息。", "error")
        return redirect(url_for("index"))
    try:
        contract_end = datetime.strptime(end_date_value, "%Y-%m-%d").date()
    except ValueError:
        flash("当前合同结束日期无效。", "error")
        return redirect(url_for("index"))

    renewal_start = shift_months(contract_end, -3)
    submission_deadline = shift_months(contract_end, -1)
    status = determine_renewal_status(contract_end, passport_check, id_card_check)
    db = get_db()
    if not db.execute("SELECT 1 FROM people WHERE id=? AND is_deleted=0", (person_id,)).fetchone():
        flash("选择的人员不存在。", "error")
        return redirect(url_for("index"))
    if not db.execute("SELECT 1 FROM quotas WHERE id=? AND is_deleted=0", (quota_id,)).fetchone():
        flash("选择的名额不存在。", "error")
        return redirect(url_for("index"))
    db.execute(
        """
        INSERT INTO renewals
            (person_id, quota_id, current_contract_end_date, renewal_start_date,
             passport_expiry_check, id_card_expiry_check, submission_deadline, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(person_id, quota_id, current_contract_end_date) DO UPDATE SET
            renewal_start_date = excluded.renewal_start_date,
            passport_expiry_check = excluded.passport_expiry_check,
            id_card_expiry_check = excluded.id_card_expiry_check,
            submission_deadline = excluded.submission_deadline,
            status = excluded.status,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            person_id, quota_id, end_date_value, renewal_start.isoformat(),
            passport_check, id_card_check, submission_deadline.isoformat(), status,
        ),
    )
    db.commit()
    flash(f"续期评估已保存：{status}", "success")
    return redirect(url_for("index", view="renewals"))


@app.post("/people")
def add_person():
    name = (request.form.get("person_name") or request.form.get("name") or "").strip()
    gender = request.form.get("gender", "").strip()
    company_name = request.form.get("company_name", "").strip() or None
    introducer = request.form.get("introducer", "").strip() or None
    worker_type = request.form.get("worker_type", "new").strip()
    if not name or gender not in {"男", "女"}:
        flash("请填写人员姓名并选择性别。", "error")
        return redirect(url_for("index"))
    db = get_db()
    cursor = db.execute(
        """INSERT INTO people
           (name,person_name,gender,company_name,introducer,worker_type)
           VALUES (?,?,?,?,?,?)""",
        (name, name, gender, company_name, introducer,
         worker_type if worker_type in {"new", "renewal"} else "new"),
    )
    db.execute(
        """INSERT INTO events
           (person_id, event_type, note, event_date, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (
            cursor.lastrowid, "登记", "人员资料已登记", date.today().isoformat(),
            datetime.now().strftime("%Y-%m-%d %H:%M"),
        ),
    )
    db.commit()
    flash(f"已添加人员：{name}", "success")
    return redirect(url_for("index"))


@app.post("/quotas")
def add_quota():
    company_name = request.form.get("company_name", "").strip()
    quota_type = request.form.get("quota_type", "").strip()
    approval_number = request.form.get("approval_number", "").strip() or None
    quota_serial = (
        request.form.get("quota_serial", "").strip()
        or request.form.get("quota_number", "").strip()
        or None
    )
    person_id = (
        request.form.get("assigned_user") or request.form.get("person_id") or None
    )
    expiry_date = request.form.get("expiry_date", "").strip() or None
    if not company_name or quota_type not in QUOTA_TYPES:
        flash("请填写公司名并选择有效的配额类型。", "error")
        return redirect(url_for("index"))
    db = get_db()
    try:
        if expiry_date:
            datetime.strptime(expiry_date, "%Y-%m-%d")
        if person_id and not db.execute(
            "SELECT 1 FROM people WHERE id=? AND is_deleted=0", (person_id,)
        ).fetchone():
            raise ValueError("使用人不存在")
        cursor = db.execute(
            """INSERT INTO quotas
               (quota_number, company_name, quota_type, approval_number,
                quota_serial, person_id, start_date, expiry_date,
                max_replacement_count, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                quota_serial, company_name, quota_type, approval_number,
                quota_serial, person_id, None, expiry_date,
                quota_replacement_limit(quota_type),
                "active" if not person_id else "in_use",
            ),
        )
        record_standard_event(
            db, "quota_created", f"配额已创建：{quota_serial or approval_number or cursor.lastrowid}",
            person_id=int(person_id) if person_id else None,
            quota_id=cursor.lastrowid,
        )
        refresh_standard_risks(db)
        db.commit()
    except (sqlite3.IntegrityError, ValueError):
        db.rollback()
        flash("配额资料格式无效、序号已存在，或该人员已占用其他配额。", "error")
        return redirect(url_for("index"))
    label = quota_serial or approval_number or f"未编号 #{cursor.lastrowid}"
    flash(f"已添加配额：{label}", "success")
    return redirect(url_for("index"))


@app.post("/quotas/<int:quota_id>/details")
def update_quota_details(quota_id):
    db = get_db()
    quota = db.execute("SELECT * FROM quotas WHERE id=? AND is_deleted=0", (quota_id,)).fetchone()
    if not quota:
        abort(404)
    company_name = request.form.get("company_name", "").strip()
    quota_type = request.form.get("quota_type", "").strip()
    approval_number = request.form.get("approval_number", "").strip() or None
    quota_serial = request.form.get("quota_serial", "").strip() or None
    person_id = request.form.get("assigned_user", "").strip() or None
    expiry_date = request.form.get("expiry_date", "").strip() or None
    if not company_name or quota_type not in QUOTA_TYPES:
        flash("请填写公司名并选择有效的配额类型。", "error")
        return redirect(url_for("quota.detail", quota_id=quota_id))
    try:
        if expiry_date:
            datetime.strptime(expiry_date, "%Y-%m-%d")
        if person_id and not db.execute(
            "SELECT 1 FROM people WHERE id=? AND is_deleted=0", (person_id,)
        ).fetchone():
            raise ValueError("使用人不存在")
        active = db.execute(
            "SELECT * FROM quota_usages WHERE quota_id=? AND end_date IS NULL",
            (quota_id,),
        ).fetchone()
        if active and person_id and str(active["person_id"]) != str(person_id):
            raise ValueError("更换当前使用人请使用替补流程")
        db.execute(
            """UPDATE quotas SET quota_number=?, company_name=?, quota_type=?,
                      approval_number=?, quota_serial=?, person_id=?,
                      start_date=?, expiry_date=?,max_replacement_count=? WHERE id=?""",
            (
                quota_serial or quota["quota_number"], company_name, quota_type,
                approval_number, quota_serial, person_id, quota["start_date"], expiry_date,
                quota_replacement_limit(quota_type), quota_id,
            ),
        )
        record_standard_event(
            db, "quota_updated", f"配额资料已更新：{quota_id}",
            person_id=int(person_id) if person_id else None,
            quota_id=quota_id,
        )
        refresh_standard_risks(db)
        db.commit()
    except (sqlite3.IntegrityError, ValueError) as error:
        db.rollback()
        flash(str(error) or "配额资料更新失败。", "error")
        return redirect(url_for("quota.detail", quota_id=quota_id))
    flash("配额资料已更新。", "success")
    return redirect(url_for("quota.detail", quota_id=quota_id))


@app.post("/quotas/<int:quota_id>/assign")
def assign_quota(quota_id):
    return_to_detail = request.form.get("return_to") == "detail"

    def destination():
        return url_for("quota.detail", quota_id=quota_id) if return_to_detail else url_for("index", view="quotas")

    person_id = request.form.get("person_id", "").strip()
    start_date = request.form.get("start_date", "").strip()
    if not person_id or not start_date:
        flash("请选择替补人员和开始日期。", "error")
        return redirect(destination())
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
    except ValueError:
        flash("开始日期格式无效。", "error")
        return redirect(destination())

    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        quota = db.execute(
            "SELECT * FROM quotas WHERE id=? AND is_deleted=0", (quota_id,)
        ).fetchone()
        person = db.execute(
            "SELECT id, name FROM people WHERE id=? AND is_deleted=0", (person_id,)
        ).fetchone()
        if not quota or not person:
            raise ValueError("名额或人员不存在")
        if str(quota["person_id"] or "") == str(person_id):
            raise ValueError("该人员已经是当前使用人")
        departure_result = None
        if quota["person_id"]:
            departure_result = trigger_worker_departure(
                db, int(quota["person_id"]), start_date
            )
            if not departure_result["allowed"]:
                db.commit()
                flash(
                    f"{quota['quota_type']} 配额替补次数已超过上限，状态已设为 invalid。",
                    "error",
                )
                return redirect(destination())
        cycle = create_replacement_cycle(db, quota_id, int(person_id), start_date)
        record_standard_event(
            db, "quota_replacement_created",
            f"配额替补周期已生成：{cycle.get('contract_id') or '初次分配'}",
            person_id=int(person_id), quota_id=quota_id,
            contract_id=(
                db.execute("SELECT id FROM contract WHERE contract_no=?", (cycle["contract_id"],)).fetchone()["id"]
                if cycle.get("contract_id") else None
            ),
        )
        db.commit()
    except (sqlite3.IntegrityError, ValueError) as error:
        db.rollback()
        flash(str(error) or "替补失败，请检查人员与日期。", "error")
        return redirect(destination())
    message = (
        f"已生成替补合同 {cycle['contract_id']}（Cycle {cycle['cycle_index']}）。"
        if cycle.get("contract_id") else
        f"{quota['quota_type']} 名额已完成初次人员分配。"
    )
    flash(message, "success")
    return redirect(destination())


@app.post("/events")
def add_event():
    person_id = request.form.get("person_id", "").strip()
    raw_event_type = request.form.get("event_type", "").strip()
    event_type = {
        "登记": "制作合同", "签证": "交表香港入境处",
        "入境": "工人入境", "离境": "离职",
    }.get(raw_event_type, raw_event_type)
    event_date = request.form.get("event_date", "").strip()
    note = request.form.get("note", "").strip()
    if (
        not person_id
        or event_type not in set(CONTRACT_LIFECYCLE) | {"离职", "其他"}
        or not event_date
    ):
        flash("请选择人员和有效的事件状态。", "error")
        return redirect(url_for("index"))
    try:
        datetime.strptime(event_date, "%Y-%m-%d")
    except ValueError:
        flash("事件日期无效。", "error")
        return redirect(url_for("index"))
    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """INSERT INTO events
               (person_id, event_type, note, event_date, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                person_id, event_type, note, event_date,
                datetime.now().strftime("%Y-%m-%d %H:%M"),
            ),
        )
        if event_type in CONTRACT_LIFECYCLE:
            contract = db.execute(
                """SELECT contract_id FROM contracts WHERE person_id=? AND is_deleted=0
                   ORDER BY cycle_index DESC,created_at DESC LIMIT 1""",
                (person_id,),
            ).fetchone()
            if not contract:
                raise ValueError("未找到该人员可关联的合同周期")
            result = transition_contract(
                db, contract["contract_id"], event_type, event_date
            )
            if event_type == "工人入境" and result.get("changed"):
                create_entry_tasks(
                    db, int(person_id), datetime.strptime(event_date, "%Y-%m-%d").date()
                )
        elif event_type == "离职":
            result = trigger_worker_departure(db, int(person_id), event_date)
            if not result["allowed"]:
                db.execute(
                    """INSERT OR IGNORE INTO risks
                       (person_id,quota_id,contract_id,risk_type,risk_level,reason,status,source_key)
                       VALUES(?,?,?,'名额替补超限','高',?,'开放',?)""",
                    (
                        person_id, result["quota_id"], result["parent_contract_id"],
                        f"替补次数 {result['replacement_count']} 已超过上限 {result['max_replacement_count']}",
                        f"quota_invalid_{result['quota_id']}_{result['replacement_count']}",
                    ),
                )
        db.commit()
    except (sqlite3.IntegrityError, ValueError) as error:
        db.rollback()
        app.logger.warning("Dispatch event rejected: person=%s event=%s error=%s", person_id, event_type, error)
        flash(str(error) or "事件处理失败。", "error")
        return redirect(url_for("index", view="events"))
    flash("事件记录已保存。", "success")
    return redirect(url_for("index", view="events"))


@app.post("/workflows")
def create_workflow():
    person_id = request.form.get("person_id", "").strip()
    quota_id = request.form.get("quota_id", "").strip()
    workflow_name = request.form.get("workflow_name", "劳优标准流程").strip()
    db = get_db()
    if not person_id or not quota_id or not workflow_name:
        flash("请完整填写流程信息。", "error")
        return redirect(url_for("index", view="workflows"))
    if not db.execute("SELECT 1 FROM people WHERE id=? AND is_deleted=0", (person_id,)).fetchone() or not db.execute(
        "SELECT 1 FROM quotas WHERE id=? AND is_deleted=0", (quota_id,)
    ).fetchone():
        flash("人员或名额不存在。", "error")
        return redirect(url_for("index", view="workflows"))
    try:
        workflow_code = create_workflow_record(db, int(person_id), int(quota_id), workflow_name)
        db.commit()
    except ValueError as error:
        db.rollback()
        flash(str(error), "error")
        return redirect(url_for("index", view="workflows"))
    flash(f"流程已创建：{workflow_code}", "success")
    return redirect(url_for("index", view="workflows"))


@app.post("/workflow-steps/<int:step_id>/action")
def workflow_step_action(step_id):
    action = request.form.get("action", "")
    note = request.form.get("note", "").strip()
    if action not in {"approve", "reject"}:
        flash("无效的审批操作。", "error")
        return redirect(url_for("index", view="workflows"))
    db = get_db()
    step = db.execute(
        """SELECT s.*, w.person_id, w.quota_id FROM workflow_steps s
           JOIN workflow_instances w ON w.id=s.workflow_id
           WHERE s.id=? AND w.is_deleted=0""",
        (step_id,),
    ).fetchone()
    if not step or step["status"] != "待处理":
        flash("该步骤已处理或不存在。", "error")
        return redirect(url_for("index", view="workflows"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    if action == "reject":
        db.execute(
            "UPDATE workflow_steps SET status='已拒绝', note=?, action_at=? WHERE id=?",
            (note, now, step_id),
        )
        db.execute(
            "UPDATE workflow_instances SET status='已拒绝', updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (step["workflow_id"],),
        )
    else:
        contract = db.execute(
            """SELECT contract_id FROM contracts
               WHERE person_id=? AND quota_id=? AND is_deleted=0
               ORDER BY cycle_index DESC,created_at DESC LIMIT 1""",
            (step["person_id"], step["quota_id"]),
        ).fetchone()
        if not contract:
            flash("流程未绑定有效合同周期。", "error")
            return redirect(url_for("index", view="workflows"))
        try:
            transition_contract(
                db, contract["contract_id"], step["step_name"], date.today().isoformat()
            )
        except ValueError as error:
            db.rollback()
            flash(str(error), "error")
            return redirect(url_for("index", view="workflows"))
        db.execute(
            "UPDATE workflow_steps SET status='已通过', note=?, action_at=? WHERE id=?",
            (note, now, step_id),
        )
        if step["step_name"] == "工人入境":
            existing_arrival = db.execute(
                """SELECT event_date FROM events
                   WHERE person_id=? AND is_deleted=0
                     AND event_type IN ('入境','工人入境')
                   ORDER BY event_date DESC, id DESC LIMIT 1""",
                (step["person_id"],),
            ).fetchone()
            if not existing_arrival:
                arrival_date = date.today().isoformat()
                db.execute(
                    """INSERT INTO events
                       (person_id,event_type,note,event_date,created_at)
                       VALUES (?,'工人入境','流程自动确认入境',?,?)""",
                    (step["person_id"], arrival_date, now),
                )
                create_entry_tasks(
                    db, step["person_id"], date.today(),
                    step["workflow_id"], step["quota_id"],
                )
        next_step = db.execute(
            """SELECT * FROM workflow_steps WHERE workflow_id=? AND status='待处理'
               ORDER BY step_order LIMIT 1""",
            (step["workflow_id"],),
        ).fetchone()
        if next_step:
            db.execute(
                """UPDATE workflow_instances SET current_step=?, updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (next_step["step_name"], step["workflow_id"]),
            )
        else:
            db.execute(
                """UPDATE workflow_instances SET status='已完成', current_step='完成',
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (step["workflow_id"],),
            )
    db.commit()
    flash("审批状态已更新。", "success")
    return redirect(url_for("index", view="workflows"))


@app.post("/documents")
def upload_document():
    uploaded = request.files.get("file")
    title = request.form.get("title", "").strip()
    person_id = request.form.get("person_id", "").strip()
    quota_id = request.form.get("quota_id", "").strip()
    workflow_id = request.form.get("workflow_id", "").strip()
    document_type = request.form.get("document_type", "other").strip()
    issue_date = request.form.get("issue_date", "").strip() or None
    expiry_date = request.form.get("expiry_date", "").strip() or None
    remarks = request.form.get("remarks", "").strip() or None
    manual_text = request.form.get("ocr_text", "").strip()
    db = get_db()
    if uploaded and uploaded.filename and not person_id:
        matches = db.execute(
            "SELECT id FROM people WHERE is_deleted=0 AND instr(?,name)>0 ORDER BY length(name) DESC",
            (uploaded.filename,),
        ).fetchall()
        if len(matches) == 1:
            person_id = str(matches[0]["id"])
    if not uploaded or not uploaded.filename or not title or not person_id:
        flash("文件必须绑定具体人员。", "error")
        return redirect(url_for("index", view="documents"))
    if document_type not in DOCUMENT_TYPES:
        document_type = "other"
    try:
        for value in (issue_date, expiry_date):
            if value:
                datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        flash("资料签发日期或到期日期格式无效。", "error")
        return redirect(url_for("index", view="documents"))
    original_filename = Path(uploaded.filename).name
    extension = original_filename.rsplit(".", 1)[-1].lower() if "." in original_filename else ""
    filename = secure_filename(original_filename) or f"document.{extension}"
    if extension not in ALLOWED_DOCUMENT_EXTENSIONS:
        flash("仅支持 TXT、图片和 PDF 文件。", "error")
        return redirect(url_for("index", view="documents"))
    if not db.execute("SELECT 1 FROM people WHERE id=? AND is_deleted=0", (person_id,)).fetchone():
        flash("绑定人员不存在。", "error")
        return redirect(url_for("index", view="documents"))
    stored_name = f"{uuid.uuid4().hex}_{filename}"
    path = Path(app.config["UPLOAD_FOLDER"]) / stored_name
    uploaded.save(path)
    if manual_text:
        ocr_text, ocr_status = manual_text, "人工补录"
    else:
        try:
            ocr_text, ocr_status = extract_document_text(path)
        except (OSError, subprocess.SubprocessError):
            ocr_text, ocr_status = "", "待OCR"
    mime_type = uploaded.mimetype or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    db.execute(
        """INSERT INTO person_documents
           (person_id,document_type,original_filename,stored_path,ocr_text,
            issue_date,expiry_date,status,remarks)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (person_id, document_type, original_filename, stored_name, ocr_text, issue_date,
         expiry_date, "pending_ocr" if ocr_status == "待OCR" else "active", remarks),
    )
    if quota_id and workflow_id:
        valid_quota = db.execute(
            "SELECT 1 FROM quotas WHERE id=? AND is_deleted=0", (quota_id,)
        ).fetchone()
        valid_workflow = db.execute(
            "SELECT 1 FROM workflow_instances WHERE id=? AND is_deleted=0", (workflow_id,)
        ).fetchone()
        if valid_quota and valid_workflow:
            db.execute(
                """INSERT INTO documents
                   (title,filename,stored_name,mime_type,person_id,quota_id,workflow_id,ocr_text,ocr_status)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (title, original_filename, stored_name, mime_type, person_id, quota_id,
                 workflow_id, ocr_text, ocr_status),
            )
    db.commit()
    flash(f"文件已入库，OCR状态：{ocr_status}", "success")
    return redirect(
        url_for("person_detail", person_id=person_id)
        if request.form.get("return_to") == "person"
        else url_for("index", view="documents")
    )


@app.get("/person-documents/<int:document_id>/download")
def download_person_document(document_id):
    document = get_db().execute(
        "SELECT * FROM person_documents WHERE id=? AND is_deleted=0", (document_id,)
    ).fetchone()
    if not document:
        abort(404)
    return send_from_directory(
        app.config["UPLOAD_FOLDER"], document["stored_path"],
        as_attachment=True, download_name=document["original_filename"],
    )


@app.get("/person-documents/<int:document_id>/view")
def view_person_document(document_id):
    document = get_db().execute(
        "SELECT * FROM person_documents WHERE id=? AND is_deleted=0", (document_id,)
    ).fetchone()
    if not document:
        abort(404)
    return send_from_directory(
        app.config["UPLOAD_FOLDER"], document["stored_path"], as_attachment=False,
    )


@app.get("/api/person-documents/suggest")
def suggest_person_document_owner():
    filename = request.args.get("filename", "")[:255]
    return api_response(0, "ok", suggest_person_by_filename(get_db(), filename))


@app.post("/person-documents/upload-batch")
def upload_person_document_batch():
    person_id = request.form.get("person_id", "").strip()
    return_to = request.form.get("return_to", "library")
    try:
        person_id_value = int(person_id)
        result = save_person_document_batch(
            get_db(), app.config["UPLOAD_FOLDER"], person_id_value,
            request.files.getlist("files"),
            request.form.get("document_type", "unknown"),
            (request.form.get("remarks") or "").strip() or None,
            request.form.get("person_case_id") or None,
        )
        session["person_document_upload_result"] = result
        flash(
            f"批次上传完成：成功 {result['success']}，跳过 {result['skipped']}，失败 {result['failed']}。",
            "success" if result["failed"] == 0 else "error",
        )
    except (TypeError, ValueError) as error:
        session["person_document_upload_result"] = {
            "success": 0, "skipped": 0, "failed": 1,
            "errors": [{"filename": "—", "reason": str(error)}],
        }
        flash(str(error), "error")
        person_id_value = int(person_id) if person_id.isdigit() else None
    if return_to == "person" and person_id_value:
        return redirect(url_for("person_detail", person_id=person_id_value))
    return redirect(url_for("index", view="documents"))


@app.post("/person-documents/<int:document_id>/case")
def confirm_person_document_case(document_id):
    db = get_db()
    document = db.execute(
        "SELECT * FROM person_documents WHERE id=? AND is_deleted=0", (document_id,)
    ).fetchone()
    if not document:
        abort(404)
    person_case_id = request.form.get("person_case_id", "").strip()
    if person_case_id and not db.execute(
        "SELECT 1 FROM person_cases WHERE id=? AND person_id=? AND is_deleted=0",
        (person_case_id, document["person_id"]),
    ).fetchone():
        flash("选择的办理周期无效。", "error")
    else:
        db.execute(
            """UPDATE person_documents SET person_case_id=?,case_binding_status=?,
                      inferred_case_confidence=? WHERE id=?""",
            (person_case_id or None, "confirmed" if person_case_id else "unassigned",
             1.0 if person_case_id else 0.0, document_id),
        )
        db.commit()
        flash("资料办理周期已确认。", "success")
    return redirect(url_for("person_detail", person_id=document["person_id"]))


@app.get("/api/people/<int:person_id>/cases/suggest")
def suggest_person_case_api(person_id):
    db = get_db()
    person = db.execute("SELECT * FROM people WHERE id=? AND is_deleted=0", (person_id,)).fetchone()
    if not person:
        return api_response(404, "人员不存在", None, 404)
    cases = db.execute("SELECT * FROM person_cases WHERE person_id=? AND is_deleted=0", (person_id,)).fetchall()
    suggestion = suggest_case_for_document(
        person, {"filename": request.args.get("filename", "")[:255], "cases": cases}
    )
    return api_response(0, "ok", suggestion)


@app.get("/documents/<int:document_id>/download")
def download_document(document_id):
    document = get_db().execute(
        "SELECT * FROM documents WHERE id=? AND is_deleted=0", (document_id,)
    ).fetchone()
    if not document:
        return "Not found", 404
    return send_from_directory(
        app.config["UPLOAD_FOLDER"], document["stored_name"],
        as_attachment=True, download_name=document["filename"],
    )


@app.post("/lifecycle/<int:node_id>/complete")
def complete_lifecycle_node(node_id):
    get_db().execute("UPDATE lifecycle_nodes SET status='已完成' WHERE id=?", (node_id,))
    get_db().commit()
    return redirect(url_for("index", view="lifecycle"))


@app.post("/risks/<int:risk_id>/resolve")
def resolve_risk(risk_id):
    get_db().execute(
        "UPDATE risks SET status='已解决', updated_at=CURRENT_TIMESTAMP WHERE id=?", (risk_id,)
    )
    get_db().commit()
    return redirect(url_for("index", view="risks"))


@app.post("/tasks/<int:task_id>/status")
def update_task_status(task_id):
    status = request.form.get("status", "")
    if status not in TASK_STATUSES:
        flash("任务状态无效。", "error")
        return redirect(url_for("index", view="tasks"))
    get_db().execute(
        "UPDATE tasks SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, task_id)
    )
    get_db().commit()
    return redirect(url_for("index", view="tasks"))


@app.delete("/api/<resource>/<resource_id>")
def soft_delete_resource(resource, resource_id):
    canonical = SOFT_DELETE_ALIASES.get(resource, resource)
    definition = SOFT_DELETE_RESOURCES.get(canonical)
    if not definition:
        return api_response(404, "不支持的删除资源", None, 404)
    table, primary_key, mirror_table, mirror_key = definition
    value = resource_id
    if primary_key == "id":
        try:
            value = int(resource_id)
        except ValueError:
            return api_response(400, "资源编号无效", None, 400)
    db = get_db()
    try:
        cursor = db.execute(
            f"""UPDATE {table}
                SET is_deleted=1, deleted_at=CURRENT_TIMESTAMP, deleted_by=?
                WHERE {primary_key}=? AND is_deleted=0""",
            (session.get("user_id"), value),
        )
        if canonical == "person_documents":
            db.execute(
                "UPDATE person_documents SET status='deleted' WHERE id=?", (value,)
            )
        if cursor.rowcount == 0:
            db.rollback()
            return api_response(404, "记录不存在或已经删除", None, 404)
        if mirror_table:
            db.execute(
                f"""UPDATE {mirror_table}
                    SET is_deleted=1, deleted_at=CURRENT_TIMESTAMP, deleted_by=?
                    WHERE {mirror_key}=? AND is_deleted=0""",
                (session.get("user_id"), value),
            )
        db.commit()
    except sqlite3.Error:
        db.rollback()
        app.logger.exception(
            "Soft delete failed: resource=%s id=%s", canonical, resource_id
        )
        return api_response(500, "删除失败，请稍后重试", None, 500)
    return api_response(
        0,
        "已删除",
        {"resource": canonical, "id": value},
    )


@app.post("/api/<resource>/<resource_id>/restore")
def restore_resource(resource, resource_id):
    canonical = SOFT_DELETE_ALIASES.get(resource, resource)
    definition = SOFT_DELETE_RESOURCES.get(canonical)
    if not definition:
        return api_response(404, "不支持的恢复资源", None, 404)
    table, primary_key, mirror_table, mirror_key = definition
    try:
        value = int(resource_id) if primary_key == "id" else resource_id
    except ValueError:
        return api_response(400, "资源编号无效", None, 400)
    db = get_db()
    cursor = db.execute(
        f"""UPDATE {table} SET is_deleted=0,deleted_at=NULL,deleted_by=NULL
            WHERE {primary_key}=? AND is_deleted=1""",
        (value,),
    )
    if cursor.rowcount == 0:
        db.rollback()
        return api_response(404, "回收站中没有该记录", None, 404)
    if canonical == "person_documents":
        db.execute("UPDATE person_documents SET status='active' WHERE id=?", (value,))
    if mirror_table:
        db.execute(
            f"""UPDATE {mirror_table}
                SET is_deleted=0,deleted_at=NULL,deleted_by=NULL
                WHERE {mirror_key}=?""",
            (value,),
        )
    db.commit()
    return api_response(0, "已恢复", {"resource": canonical, "id": value})


@app.delete("/api/<resource>/<resource_id>/permanent")
def permanently_delete_resource(resource, resource_id):
    canonical = SOFT_DELETE_ALIASES.get(resource, resource)
    definition = SOFT_DELETE_RESOURCES.get(canonical)
    if not definition:
        return api_response(404, "不支持的删除资源", None, 404)
    table, primary_key, mirror_table, mirror_key = definition
    try:
        value = int(resource_id) if primary_key == "id" else resource_id
    except ValueError:
        return api_response(400, "资源编号无效", None, 400)
    db = get_db()
    try:
        if mirror_table:
            db.execute(
                f"DELETE FROM {mirror_table} WHERE {mirror_key}=? AND is_deleted=1",
                (value,),
            )
        cursor = db.execute(
            f"DELETE FROM {table} WHERE {primary_key}=? AND is_deleted=1",
            (value,),
        )
        if cursor.rowcount == 0:
            db.rollback()
            return api_response(404, "回收站中没有该记录", None, 404)
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        return api_response(409, "存在关联历史，请先处理关联记录", None, 409)
    return api_response(0, "已永久删除", {"resource": canonical, "id": value})


@app.get("/recycle-bin")
def recycle_bin_page():
    db = get_db()
    users = {
        row["id"]: row["username"]
        for row in db.execute("SELECT id,username FROM users").fetchall()
    }
    items = []
    for resource, (table, primary_key, _mirror_table, _mirror_key) in SOFT_DELETE_RESOURCES.items():
        rows = db.execute(
            f"""SELECT {primary_key} AS entity_id,deleted_at,deleted_by
                FROM {table} WHERE is_deleted=1 ORDER BY deleted_at DESC"""
        ).fetchall()
        for row in rows:
            items.append({
                "resource": resource,
                "entity_id": row["entity_id"],
                "deleted_at": row["deleted_at"],
                "deleted_by": users.get(row["deleted_by"], "系统/未知"),
            })
    items.sort(key=lambda item: item["deleted_at"] or "", reverse=True)
    return render_template("recycle_bin.html", items=items)


@app.get("/audit-logs")
def audit_logs_page():
    rows = get_db().execute(
        """SELECT a.*,u.username FROM audit_logs a
           LEFT JOIN users u ON u.id=a.user_id
           ORDER BY a.id DESC LIMIT 500"""
    ).fetchall()
    return render_template("audit_logs.html", audit_logs=rows)


@app.patch("/api/people/<int:person_id>")
def api_update_person(person_id):
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip()
    gender = str(payload.get("gender", "")).strip()
    company_name = str(payload.get("company_name", "")).strip() or None
    introducer = str(payload.get("introducer", "")).strip() or None
    if not name or gender not in {"男", "女"}:
        return api_response(400, "姓名必填，性别必须为男或女。", None, 400)
    db = get_db()
    person = db.execute(
        "SELECT id FROM people WHERE id=? AND is_deleted=0", (person_id,)
    ).fetchone()
    if not person:
        return api_response(404, "人员不存在。", None, 404)
    try:
        db.execute(
            """UPDATE people SET name=?, person_name=?, gender=?, company_name=?, introducer=?
               WHERE id=?""",
            (name, name, gender, company_name, introducer, person_id),
        )
        record_standard_event(
            db, "person_updated", f"人员资料已更新：{name}", person_id=person_id,
        )
        db.commit()
    except sqlite3.IntegrityError as error:
        db.rollback()
        app.logger.warning("Person inline update rejected: id=%s error=%s", person_id, error)
        return api_response(409, "人员资料与现有数据冲突。", None, 409)
    return api_response(0, "保存成功", {"message": "人员资料已保存", "id": person_id})


@app.patch("/api/contracts/<contract_id>")
def api_update_contract(contract_id):
    payload = request.get_json(silent=True) or {}
    company = str(payload.get("company", "")).strip()
    status = normalize_contract_status(payload.get("status", ""))
    if not company or status not in CONTRACT_STATUSES:
        return api_response(400, "公司必填，合同状态无效。", None, 400)
    db = get_db()
    contract = db.execute(
        "SELECT contract_id, person_id FROM contracts WHERE contract_id=? AND is_deleted=0",
        (contract_id,),
    ).fetchone()
    if not contract:
        return api_response(404, "合同不存在。", None, 404)
    try:
        db.execute("UPDATE contracts SET company=? WHERE contract_id=?", (company, contract_id))
        transition_contract(db, contract_id, status, date.today().isoformat())
        standard_contract = db.execute(
            "SELECT id, quota_id FROM contract WHERE contract_no=?", (contract_id,)
        ).fetchone()
        record_standard_event(
            db, "contract_updated", f"合同资料已更新，状态：{status}",
            person_id=contract["person_id"],
            quota_id=standard_contract["quota_id"] if standard_contract else None,
            contract_id=standard_contract["id"] if standard_contract else None,
        )
        db.commit()
    except (sqlite3.IntegrityError, ValueError) as error:
        db.rollback()
        app.logger.warning("Contract inline update rejected: id=%s error=%s", contract_id, error)
        return api_response(409, str(error) or "合同资料与现有数据冲突。", None, 409)
    return api_response(0, "保存成功", {"message": "合同资料已保存", "id": contract_id})


@app.patch("/api/quotas/<int:quota_id>")
def api_update_quota(quota_id):
    payload = request.get_json(silent=True) or {}
    quota_type = str(payload.get("quota_type", "")).strip()
    company_name = str(payload.get("company_name", "")).strip()
    approval_number = str(payload.get("approval_number", "")).strip() or None
    quota_serial = str(payload.get("quota_serial", "")).strip() or None
    expiry_date = str(payload.get("expiry_date", "")).strip() or None
    if quota_type not in QUOTA_TYPES or not company_name:
        return api_response(400, "配额类型和公司名必填。", None, 400)
    if expiry_date:
        try:
            datetime.strptime(expiry_date, "%Y-%m-%d")
        except ValueError:
            return api_response(400, "批文有效期格式无效。", None, 400)
    db = get_db()
    quota = db.execute(
        "SELECT * FROM quotas WHERE id=? AND is_deleted=0", (quota_id,)
    ).fetchone()
    if not quota:
        return api_response(404, "名额不存在。", None, 404)
    try:
        db.execute(
            """UPDATE quotas SET quota_number=?, quota_type=?, company_name=?,
                      approval_number=?, quota_serial=?, expiry_date=?,
                      max_replacement_count=? WHERE id=?""",
            (
                quota_serial or quota["quota_number"], quota_type, company_name,
                approval_number, quota_serial, expiry_date,
                quota_replacement_limit(quota_type), quota_id,
            ),
        )
        record_standard_event(
            db, "quota_updated", f"名额资料已更新：{quota_id}",
            person_id=quota["person_id"], quota_id=quota_id,
        )
        refresh_standard_risks(db)
        db.commit()
    except sqlite3.IntegrityError as error:
        db.rollback()
        app.logger.warning("Quota inline update rejected: id=%s error=%s", quota_id, error)
        return api_response(409, "名额序号已存在或资料冲突。", None, 409)
    return api_response(0, "保存成功", {"message": "名额资料已保存", "id": quota_id})


@app.post("/api/ai/ask")
def ai_ask():
    payload = request.get_json(silent=True) or {}
    question = str(payload.get("question", "")).strip()
    if not question:
        return api_response(400, "请输入问题。", None, 400)
    if len(question) > 300:
        return api_response(400, "问题不能超过300个字符。", None, 400)
    return api_response(0, "ok", answer_question(get_db(), question))


@app.get("/health")
def health():
    db = get_db()
    integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_key_issues = db.execute("PRAGMA foreign_key_check").fetchall()
    payload = {
            "status": "ok" if integrity == "ok" and not foreign_key_issues else "error",
            "database": integrity,
            "foreign_key_issues": len(foreign_key_issues),
            "modules": ["Person", "Quota", "Contract", "Workflow", "Document", "Risk", "Task"],
        }
    http_status = 200 if integrity == "ok" and not foreign_key_issues else 500
    return api_response(0 if http_status == 200 else 500, payload["status"], payload, http_status)


@app.errorhandler(413)
def file_too_large(error):
    app.logger.warning(
        "Upload rejected: method=%s path=%s content_length=%s",
        request.method, request.path, request.content_length,
    )
    if request.path.startswith(("/api/", "/imports/")) or request.args.get("format") == "json":
        return api_response(413, "上传文件超过16MB限制", None, 413)
    flash("文件超过16MB上传限制。", "error")
    return redirect(url_for("index", view="documents"))


with app.app_context():
    init_db()
