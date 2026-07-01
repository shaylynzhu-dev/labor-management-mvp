from datetime import date
from pathlib import Path
import mimetypes
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


def save_person_document_batch(db, upload_root, person_id, files, document_type, remarks=None):
    files = [item for item in files if item and item.filename]
    if not files:
        raise ValueError("请选择至少一个文件")
    if len(files) > 50:
        raise ValueError("单次最多上传50个文件")
    if document_type not in DOCUMENT_TYPES:
        document_type = "unknown"
    if not db.execute(
        "SELECT 1 FROM people WHERE id=? AND is_deleted=0", (person_id,)
    ).fetchone():
        raise ValueError("绑定人员不存在")

    allowed = {"pdf", "jpg", "jpeg", "png", "doc", "docx"}
    max_size = 25 * 1024 * 1024
    batch_id = uuid.uuid4().hex
    batch_dir = Path(upload_root) / "person_documents" / str(person_id) / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    result = {"batch_id": batch_id, "success": 0, "skipped": 0, "failed": 0, "errors": []}
    used_names = set()

    for uploaded in files:
        original = Path(uploaded.filename).name
        extension = original.rsplit(".", 1)[-1].lower() if "." in original else ""
        if extension not in allowed:
            result["skipped"] += 1
            result["errors"].append({"filename": original, "reason": "不支持的文件格式"})
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
            db.execute(
                """INSERT INTO person_documents
                   (person_id,document_type,original_filename,stored_path,mime_type,
                    file_size,upload_batch_id,status,remarks)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (person_id, document_type, original, relative_path,
                 uploaded.mimetype or mimetypes.guess_type(original)[0] or "application/octet-stream",
                 size, batch_id, "active", remarks),
            )
            result["success"] += 1
        except Exception as error:
            if destination.exists():
                destination.unlink()
            result["failed"] += 1
            result["errors"].append({"filename": original, "reason": str(error)})
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
    document_groups = []
    for document_type, label in DOCUMENT_TYPES.items():
        items = [item for item in documents if item["document_type"] == document_type]
        if items:
            document_groups.append({"type": document_type, "label": label, "items": items})
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
