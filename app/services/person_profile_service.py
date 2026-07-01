from datetime import date


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
    visa_query = get_entry_visa_query_key(person)
    if not visa_query["complete"]:
        missing.append(visa_query["label"])
    appointment = check_hk_id_appointment_ready(person)
    missing.extend(item for item in appointment["missing"] if item not in missing)
    return missing


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
    document_types = {item["document_type"] for item in documents}
    visa_query = get_entry_visa_query_key(person)
    appointment = check_hk_id_appointment_ready(person)
    missing = get_missing_documents(person, document_types)
    checks_total = len(BASE_REQUIRED_DOCUMENTS) + 4
    completeness = round((checks_total - min(len(missing), checks_total)) * 100 / checks_total)
    return {
        "person": person,
        "documents": documents,
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
