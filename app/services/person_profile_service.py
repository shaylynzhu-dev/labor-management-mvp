from datetime import date, datetime, timedelta
import calendar
import hashlib
import json
from pathlib import Path
import mimetypes
import re
import uuid

from werkzeug.utils import secure_filename


DOCUMENT_TYPES = {
    "resume": "简历",
    "contract": "合同",
    "mainland_id": "国内身份证",
    "hkmo_permit": "港澳通行证",
    "social_security_statement": "社保声明书",
    "no_criminal_record": "无犯罪证明",
    "medical_report": "体检报告",
    "household_register": "户口本",
    "visa_document": "签证资料",
    "hk_id_appointment": "香港身份证预约资料",
    "work_proof": "工作证明",
    "other": "其他",
    "unknown": "未分类",
}

BASE_REQUIRED_DOCUMENTS = ("resume", "contract", "mainland_id", "hkmo_permit")
CONTRACT_RESTART_LEAD_DAYS = 30
DOCUMENT_COLLECTION_LEAD_MONTHS = 1
DATA_PRECEDENCE_ORDER = (
    "manual_input",
    "confirmed_binding",
    "folder_recognition",
    "filename_recognition",
    "excel_import",
    "auto_inference",
)
DATA_PRECEDENCE_RANK = {source: index + 1 for index, source in enumerate(DATA_PRECEDENCE_ORDER)}
CASE_LIFECYCLE = ("active", "completed", "archived", "frozen")
HUMAN_REVIEW_STATUS = "human_review_required"


def _value(person, key):
    try:
        return person[key]
    except (KeyError, TypeError, IndexError):
        return getattr(person, key, None)


def get_entry_visa_query_key(person):
    worker_type = _value(person, "worker_type") or "new"
    if worker_type == "renewal":
        field = "hkmo_permit_first4"
        label = "港澳通行证前四位"
    else:
        field = "mainland_id_first4"
        label = "国内身份证前四位"
    value = (_value(person, field) or "").strip()
    return {"field": field, "label": label, "value": value, "complete": bool(value)}


def check_hk_id_appointment_ready(person):
    requirements = {
        "entry_permit_no": "入境编号",
        "birth_year_month": "出生年月",
        "hkmo_permit_last6": "港澳通行证后六位",
    }
    missing = [label for field, label in requirements.items() if not _value(person, field)]
    return {
        "ready": not missing,
        "message": "可以预约香港身份证" if not missing else "香港身份证预约资料不完整",
        "missing": missing,
    }


def get_missing_documents(person, available_document_types):
    available = set(available_document_types)
    missing = [DOCUMENT_TYPES[item] for item in BASE_REQUIRED_DOCUMENTS if item not in available]
    if not (_value(person, "mainland_id_first4") or _value(person, "mainland_id_last4") or _value(person, "id_last4")):
        missing.append("身份证号码缺失")
    visa_query = get_entry_visa_query_key(person)
    if not visa_query["complete"]:
        if visa_query["label"] != "国内身份证前四位" or "身份证号码缺失" not in missing:
            missing.append(visa_query["label"])
    appointment = check_hk_id_appointment_ready(person)
    missing.extend(item for item in appointment["missing"] if item not in missing)
    return missing


def suggest_person_by_filename(db, filename):
    filename = (filename or "").casefold()
    if not filename:
        return []
    candidates = []
    for row in db.execute(
        "SELECT id,name,company_name FROM people WHERE is_deleted=0 ORDER BY length(name) DESC,id"
    ).fetchall():
        if row["name"] and row["name"].casefold() in filename:
            candidates.append(dict(row))
    return candidates


def data_precedence_rank(source):
    return DATA_PRECEDENCE_RANK.get(source or "auto_inference", DATA_PRECEDENCE_RANK["auto_inference"])


def choose_higher_precedence(current_source, incoming_source):
    return incoming_source if data_precedence_rank(incoming_source) <= data_precedence_rank(current_source) else current_source


def queue_conflict(db, conflict_type, entity_type, payload, entity_id=None, source="auto_inference"):
    db.execute(
        """INSERT INTO conflict_queue
           (entity_type,entity_id,conflict_type,status,source,data_precedence_rank,payload)
           VALUES (?,?,?,?,?,?,?)""",
        (entity_type, entity_id, conflict_type, HUMAN_REVIEW_STATUS,
         source, data_precedence_rank(source), json.dumps(payload or {}, ensure_ascii=False)),
    )


def queue_retry(db, operation, entity_type, filename, reason, payload=None, entity_id=None):
    db.execute(
        """INSERT INTO retry_queue
           (operation,entity_type,entity_id,filename,reason,payload,status)
           VALUES (?,?,?,?,?,?,?)""",
        (operation, entity_type, entity_id, filename, reason,
         json.dumps(payload or {}, ensure_ascii=False), "pending"),
    )


def next_case_status(current_status, target_status):
    if target_status not in CASE_LIFECYCLE:
        raise ValueError("办理周期状态无效")
    if not current_status:
        return target_status
    if current_status not in CASE_LIFECYCLE:
        return target_status
    if CASE_LIFECYCLE.index(target_status) < CASE_LIFECYCLE.index(current_status):
        raise ValueError("办理周期不能回退状态")
    return target_status


def document_hash_from_path(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_duplicate_document(db, document_hash, original_filename, person_id, person_case_id):
    return db.execute(
        """SELECT * FROM person_documents
           WHERE is_deleted=0 AND document_hash=? AND original_filename=? AND person_id=?
             AND COALESCE(person_case_id,0)=COALESCE(?,0)
           ORDER BY version_no DESC,id DESC LIMIT 1""",
        (document_hash, original_filename, person_id, person_case_id),
    ).fetchone()


def suggest_case_for_document(person, file_info):
    """Return a case suggestion without ever using the person's name as evidence."""
    cases = [dict(item) for item in file_info.get("cases", [])]
    manual_case_id = file_info.get("person_case_id")
    if manual_case_id:
        matched = next((item for item in cases if item["id"] == int(manual_case_id)), None)
        return {
            "case": matched, "confidence": 1.0, "status": "confirmed",
            "source": "manual_input", "conflict_type": None,
        } if matched else None
    text = " ".join(filter(None, [file_info.get("folder_name"), file_info.get("filename")])).lower()
    normalized = text.replace("–", "-").replace("—", "-")
    type_tokens = {
        "new_contract": ("新人", "首次", "新合约"),
        "renewal": ("续约", "续约合同", "visa", "hk入表"),
        "replacement": ("替补", "24个月"),
    }
    scored = []
    ranges = set(re.findall(r"(?:19|20)\d{2}\s*-\s*(?:19|20)\d{2}", normalized))
    for case in cases:
        score = 0.0
        if any(token in normalized for token in type_tokens.get(case["case_type"], ())):
            score = max(score, 0.78)
        label = (case.get("case_label") or "").lower().replace("–", "-").replace("—", "-")
        if any(value.replace(" ", "") in label.replace(" ", "") for value in ranges):
            score = max(score, 0.95)
        if case.get("start_date") and case.get("end_date"):
            date_range = f"{case['start_date'][:4]}-{case['end_date'][:4]}"
            if date_range in normalized.replace(" ", ""):
                score = max(score, 0.95)
        if score:
            scored.append((score, case))
    scored.sort(key=lambda item: item[0], reverse=True)
    if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        source = "folder_recognition" if any(token in (file_info.get("folder_name") or "").lower() for token in ("新人", "首次", "新合约", "续约", "替补", "24个月")) else "filename_recognition"
        return {
            "case": scored[0][1], "confidence": scored[0][0], "status": "suggested",
            "source": source, "conflict_type": None,
        }
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return {
            "case": None, "confidence": scored[0][0], "status": "unassigned",
            "source": "auto_inference", "conflict_type": "multiple_case_match",
        }
    active = [item for item in cases if item["status"] == "active"]
    if len(active) == 1:
        return {
            "case": active[0], "confidence": 0.55, "status": "suggested",
            "source": "auto_inference", "conflict_type": None,
        }
    return {
        "case": None, "confidence": 0.0, "status": "unassigned",
        "source": "auto_inference",
        "conflict_type": "multiple_case_match" if len(active) > 1 else "unclassified_document",
    }


def _parse_date(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _subtract_months(value, months):
    target_month = value.month - months
    year = value.year + (target_month - 1) // 12
    month = (target_month - 1) % 12 + 1
    return value.replace(year=year, month=month, day=min(value.day, calendar.monthrange(year, month)[1]))


def calculate_renewal_alert_dates(person_case):
    """Calculate renewal dates in the service layer; missing dates stay safely empty."""
    contract_end = _parse_date(_value(person_case, "contract_end_date") or _value(person_case, "end_date"))
    endorsement_expiry = _parse_date(_value(person_case, "endorsement_expiry_date"))
    return {
        "contract_restart_due_date": (
            contract_end - timedelta(days=CONTRACT_RESTART_LEAD_DAYS)
        ).isoformat() if contract_end else None,
        "document_collection_due_date": (
            _subtract_months(endorsement_expiry, DOCUMENT_COLLECTION_LEAD_MONTHS).isoformat()
            if endorsement_expiry else None
        ),
    }


def get_renewal_alert_summary(person_case, today=None):
    today = today or date.today()
    calculated = calculate_renewal_alert_dates(person_case)
    alerts = []
    for key, upcoming_label in (
        ("contract_restart_due_date", "即将合同重启"),
        ("document_collection_due_date", "即将收证件办理续签"),
    ):
        due = _parse_date(_value(person_case, key) or calculated[key])
        if not due:
            continue
        remaining = (due - today).days
        if remaining < 0:
            label = "已逾期未处理"
        elif remaining <= CONTRACT_RESTART_LEAD_DAYS:
            label = upcoming_label
        else:
            label = "日期已设置"
        alerts.append({"type": key, "due_date": due.isoformat(), "remaining_days": remaining, "label": label})
    return alerts


def save_person_document_batch(
    db, upload_root, person_id, files, document_type, remarks=None, person_case_id=None,
):
    files = [item for item in files if item and item.filename]
    if not files:
        raise ValueError("请选择至少一个文件")
    if len(files) > 50:
        raise ValueError("单次最多上传50个文件")
    if document_type not in DOCUMENT_TYPES:
        document_type = "unknown"
    person = db.execute(
        "SELECT * FROM people WHERE id=? AND is_deleted=0", (person_id,)
    ).fetchone()
    if not person:
        raise ValueError("绑定人员不存在")
    cases = db.execute(
        "SELECT * FROM person_cases WHERE person_id=? AND is_deleted=0 ORDER BY status='active' DESC,start_date DESC,id DESC",
        (person_id,),
    ).fetchall()
    if person_case_id and not any(item["id"] == int(person_case_id) for item in cases):
        raise ValueError("办理周期不属于所选人员")

    allowed = {"pdf", "jpg", "jpeg", "png", "doc", "docx"}
    max_size = 25 * 1024 * 1024
    batch_id = uuid.uuid4().hex
    batch_dir = Path(upload_root) / "person_documents" / str(person_id) / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    result = {"batch_id": batch_id, "success": 0, "skipped": 0, "failed": 0, "errors": []}
    used_names = set()

    for uploaded in files:
        source_name = uploaded.filename.replace("\\", "/")
        original = Path(source_name).name
        folder_name = source_name.rsplit("/", 1)[0] if "/" in source_name else ""
        suggestion = suggest_case_for_document(
            person,
            {"filename": original, "folder_name": folder_name, "cases": cases,
             "person_case_id": person_case_id},
        )
        extension = original.rsplit(".", 1)[-1].lower() if "." in original else ""
        if extension not in allowed:
            result["skipped"] += 1
            result["errors"].append({"filename": original, "reason": "不支持的文件格式"})
            queue_retry(
                db, "manual_fix", "person_document", original, "不支持的文件格式",
                {"person_id": person_id, "batch_id": batch_id, "document_type": document_type},
            )
            continue
        safe_name = secure_filename(original) or f"document.{extension}"
        stem, suffix = Path(safe_name).stem, Path(safe_name).suffix
        counter = 1
        while safe_name.casefold() in used_names or (batch_dir / safe_name).exists():
            safe_name = f"{stem}_{counter}{suffix}"
            counter += 1
        used_names.add(safe_name.casefold())
        destination = batch_dir / safe_name
        size = 0
        try:
            with destination.open("wb") as output:
                while True:
                    chunk = uploaded.stream.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_size:
                        raise ValueError("单文件超过25MB")
                    output.write(chunk)
            relative_path = destination.relative_to(Path(upload_root)).as_posix()
            document_hash = document_hash_from_path(destination)
            suggested_case_id = suggestion["case"]["id"] if suggestion and suggestion["case"] else None
            duplicate = find_duplicate_document(db, document_hash, original, person_id, suggested_case_id)
            version_no = int(duplicate["version_no"] or 1) + 1 if duplicate else 1
            binding_status = suggestion["status"] if suggestion else "unassigned"
            document_status = "duplicate" if duplicate else (
                HUMAN_REVIEW_STATUS if binding_status == "unassigned" else "active"
            )
            db.execute(
                """INSERT INTO person_documents
                   (person_id,document_type,original_filename,stored_path,mime_type,
                    file_size,upload_batch_id,status,remarks,person_case_id,
                    inferred_case_confidence,case_binding_status,document_hash,
                    duplicate_of_document_id,version_no,binding_source,data_source,
                    data_precedence_rank)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (person_id, document_type, original, relative_path,
                 uploaded.mimetype or mimetypes.guess_type(original)[0] or "application/octet-stream",
                 size, batch_id, document_status, remarks, suggested_case_id,
                 suggestion["confidence"] if suggestion else 0.0, binding_status,
                 document_hash, duplicate["id"] if duplicate else None, version_no,
                 suggestion["source"] if suggestion else "auto_inference",
                 suggestion["source"] if suggestion else "auto_inference",
                 data_precedence_rank(suggestion["source"] if suggestion else "auto_inference")),
            )
            document_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            if duplicate:
                result["duplicates"] = result.get("duplicates", 0) + 1
            if suggestion and suggestion.get("conflict_type"):
                queue_conflict(
                    db, suggestion["conflict_type"], "person_document",
                    {"filename": original, "person_id": person_id, "batch_id": batch_id},
                    document_id, suggestion.get("source"),
                )
            result["success"] += 1
        except Exception as error:
            if destination.exists():
                destination.unlink()
            result["failed"] += 1
            result["errors"].append({"filename": original, "reason": str(error)})
            queue_retry(
                db, "upload", "person_document", original, str(error),
                {"person_id": person_id, "batch_id": batch_id, "document_type": document_type},
            )
    db.commit()
    return result


def _mask(value, visible_start=2, visible_end=2):
    if not value:
        return "—"
    value = str(value)
    if len(value) <= visible_start + visible_end:
        return "•" * len(value)
    return f"{value[:visible_start]}{'•' * (len(value)-visible_start-visible_end)}{value[-visible_end:]}"


def get_person_profile(db, person_id, can_view_sensitive=False):
    person_row = db.execute(
        "SELECT * FROM people WHERE id=? AND is_deleted=0", (person_id,)
    ).fetchone()
    if not person_row:
        return None
    person = dict(person_row)
    person["worker_type"] = person.get("worker_type") or "new"
    person["visa_status"] = person.get("visa_status") or "未出"
    person["worker_type_label"] = "续约" if person["worker_type"] == "renewal" else "新人"
    person["display_entry_permit_no"] = (
        person.get("entry_permit_no") or "—"
        if can_view_sensitive else _mask(person.get("entry_permit_no"))
    )
    documents = [
        dict(row) for row in db.execute(
            """SELECT pd.*,
                      COALESCE((SELECT title FROM documents old
                                WHERE old.stored_name=pd.stored_path LIMIT 1),
                               pd.original_filename) AS display_title
               FROM person_documents pd
               WHERE pd.person_id=? AND pd.is_deleted=0
               ORDER BY pd.uploaded_at DESC,pd.id DESC""",
            (person_id,),
        ).fetchall()
    ]
    today = date.today().isoformat()
    for document in documents:
        document["document_type_label"] = DOCUMENT_TYPES.get(document["document_type"], "其他")
        document["is_expired"] = bool(document.get("expiry_date") and document["expiry_date"] < today)
        document["file_size_label"] = _file_size_label(document.get("file_size"))
        document["is_duplicate"] = document.get("status") == "duplicate"
        document["needs_human_review"] = document.get("status") == HUMAN_REVIEW_STATUS
    document_groups = []
    for document_type, label in DOCUMENT_TYPES.items():
        items = [item for item in documents if item["document_type"] == document_type]
        if items:
            document_groups.append({"type": document_type, "label": label, "items": items})
    cases = [dict(row) for row in db.execute(
        "SELECT * FROM person_cases WHERE person_id=? AND is_deleted=0 ORDER BY status='active' DESC,start_date DESC,id DESC",
        (person_id,),
    ).fetchall()]
    for case in cases:
        calculated = calculate_renewal_alert_dates(case)
        case["contract_restart_due_date"] = case.get("contract_restart_due_date") or calculated["contract_restart_due_date"]
        case["document_collection_due_date"] = case.get("document_collection_due_date") or calculated["document_collection_due_date"]
        case["renewal_alerts"] = get_renewal_alert_summary(case)
    case_sections = []
    for case in cases:
        items = [item for item in documents if item.get("person_case_id") == case["id"] and item.get("case_binding_status") == "confirmed"]
        case_sections.append({"case": case, "items": items, "current": case["status"] == "active"})
    unconfirmed_documents = [
        item for item in documents
        if not item.get("person_case_id") or item.get("case_binding_status") != "confirmed"
    ]
    document_types = {item["document_type"] for item in documents}
    visa_query = get_entry_visa_query_key(person)
    appointment = check_hk_id_appointment_ready(person)
    missing = get_missing_documents(person, document_types)
    checks_total = len(BASE_REQUIRED_DOCUMENTS) + 4
    completeness = round((checks_total - min(len(missing), checks_total)) * 100 / checks_total)
    return {
        "person": person,
        "documents": documents,
        "document_groups": document_groups,
        "person_cases": cases,
        "case_sections": case_sections,
        "unconfirmed_documents": unconfirmed_documents,
        "contracts": db.execute(
            "SELECT * FROM contracts WHERE person_id=? AND is_deleted=0 ORDER BY created_at DESC",
            (person_id,),
        ).fetchall(),
        "workflows": db.execute(
            "SELECT * FROM workflow_instances WHERE person_id=? AND is_deleted=0 ORDER BY id DESC",
            (person_id,),
        ).fetchall(),
        "events": db.execute(
            "SELECT * FROM events WHERE person_id=? AND is_deleted=0 ORDER BY created_at DESC,id DESC LIMIT 20",
            (person_id,),
        ).fetchall(),
        "visa_query": visa_query,
        "appointment": appointment,
        "missing": missing,
        "completeness": max(0, completeness),
        "document_types": DOCUMENT_TYPES,
    }


def _file_size_label(value):
    size = int(value or 0)
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"
