from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
import hashlib
import re
import unicodedata
import uuid


PERSON_KEY_RULE_VERSION = "person-global-key-v3"
DOCUMENT_BINDING_RULE_VERSION = "document-binding-v2"
EVENT_ENGINE_RULE_VERSION = "person-event-engine-v2"


def _clean(value):
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", value)


def generate_person_global_key(
    name, hkmo_permit_first4=None, hkmo_permit_last6=None,
    birth_date=None, mainland_id_last4=None, mainland_id_first4=None, company_name=None,
):
    """Generate a non-sensitive stable key using the documented precedence."""
    normalized_name = _clean(name)
    name_hash = hashlib.sha256(normalized_name.encode("utf-8")).hexdigest()[:12]
    id_prefix = _clean(mainland_id_first4)
    company = _clean(company_name)
    if id_prefix and company:
        identity_hash = hashlib.sha256(
            f"{normalized_name}|{id_prefix}|{company}".encode("utf-8")
        ).hexdigest()[:24]
        return f"PGK-{identity_hash}"
    permit = f"{_clean(hkmo_permit_first4)}{_clean(hkmo_permit_last6)}"
    if permit and _clean(birth_date):
        identity_hash = hashlib.sha256(
            f"{permit}|{_clean(birth_date)}|{name_hash}".encode("utf-8")
        ).hexdigest()[:24]
        return f"HKP-{identity_hash}"
    if _clean(mainland_id_last4):
        identity_hash = hashlib.sha256(
            f"{_clean(mainland_id_last4)}|{name_hash}".encode("utf-8")
        ).hexdigest()[:24]
        return f"MID-{identity_hash}"
    return f"P-{uuid.uuid4()}"


def sqlite_person_global_key(name, permit_first4, permit_last6, birth_date, mainland_last4):
    return generate_person_global_key(
        name, permit_first4, permit_last6, birth_date, mainland_last4,
    )


def sqlite_person_global_key_v3(name, mainland_id_first4, company_name):
    return generate_person_global_key(
        name, mainland_id_first4=mainland_id_first4, company_name=company_name,
    )


def backfill_person_global_keys(db):
    rows = db.execute(
        """SELECT id,name,company_name,mainland_id_first4,
                  hkmo_permit_first4,hkmo_permit_last6,birth_date,
                  COALESCE(mainland_id_last4,id_last4) AS mainland_id_last4
           FROM people WHERE person_global_key IS NULL OR person_global_key='' ORDER BY id"""
    ).fetchall()
    used = {
        row[0] for row in db.execute(
            "SELECT person_global_key FROM people WHERE person_global_key IS NOT NULL AND person_global_key!=''"
        ).fetchall()
    }
    for person in rows:
        key = generate_person_global_key(
            person["name"], person["hkmo_permit_first4"], person["hkmo_permit_last6"],
            person["birth_date"], person["mainland_id_last4"],
            person["mainland_id_first4"], person["company_name"],
        )
        if key in used:
            key = f"P-{uuid.uuid4()}"
        used.add(key)
        db.execute(
            "UPDATE people SET person_global_key=?,identity_rule_version=? WHERE id=?",
            (key, PERSON_KEY_RULE_VERSION, person["id"]),
        )
    db.execute(
        """UPDATE person_documents
           SET person_global_key=(SELECT p.person_global_key FROM people p WHERE p.id=person_documents.person_id),
               binding_rule_version=COALESCE(NULLIF(binding_rule_version,''),?)
           WHERE person_global_key IS NULL OR person_global_key=''""",
        (DOCUMENT_BINDING_RULE_VERSION,),
    )


def find_duplicate_people(db, person, threshold=0.85, exclude_person_id=None):
    """Return merge suggestions only; this function never mutates or merges records."""
    candidate_name = _clean(person.get("name") or person.get("person_name"))
    candidate_permit = _clean(
        f"{person.get('hkmo_permit_first4') or ''}{person.get('hkmo_permit_last6') or ''}"
    )
    candidate_id_last4 = _clean(person.get("mainland_id_last4") or person.get("id_last4"))
    candidate_birth = _clean(person.get("birth_date"))
    suggestions = []
    rows = db.execute(
        """SELECT id,name,company_name,birth_date,mainland_id_last4,id_last4,
                  hkmo_permit_first4,hkmo_permit_last6,person_global_key
           FROM people WHERE is_deleted=0 AND (? IS NULL OR id!=?)""",
        (exclude_person_id, exclude_person_id),
    ).fetchall()
    for row in rows:
        existing = dict(row)
        name_score = SequenceMatcher(None, candidate_name, _clean(existing["name"])).ratio()
        existing_permit = _clean(
            f"{existing.get('hkmo_permit_first4') or ''}{existing.get('hkmo_permit_last6') or ''}"
        )
        existing_last4 = _clean(existing.get("mainland_id_last4") or existing.get("id_last4"))
        permit_match = bool(candidate_permit and existing_permit and candidate_permit == existing_permit)
        id_match = bool(candidate_id_last4 and existing_last4 and candidate_id_last4 == existing_last4)
        birth_match = bool(candidate_birth and _clean(existing.get("birth_date")) == candidate_birth)
        confidence = name_score
        reasons = ["姓名相似"] if name_score > threshold else []
        if permit_match:
            confidence = max(confidence, 0.96 if name_score > 0.6 else 0.9)
            reasons.append("通行证一致")
        if id_match:
            confidence = max(confidence, 0.9 + min(name_score, 1.0) * 0.08)
            reasons.append("身份证后四位一致")
        if birth_match and name_score > 0.7:
            confidence = max(confidence, 0.88 + name_score * 0.1)
            reasons.append("出生日期一致")
        if confidence > threshold:
            suggestions.append({
                "id": existing["id"], "name": existing["name"],
                "company_name": existing.get("company_name"),
                "person_global_key": existing.get("person_global_key"),
                "confidence": round(min(confidence, 1.0), 4),
                "reasons": reasons,
                "merge_suggestion": "建议人工核对后选择保留主档案；系统不会自动合并。",
            })
    return sorted(suggestions, key=lambda item: item["confidence"], reverse=True)


def _event_status(trigger_date, due_date, current_status=None, today=None):
    if current_status == "completed":
        return "completed"
    today = (today or date.today()).isoformat()
    if due_date and due_date < today:
        return "overdue"
    if trigger_date <= today:
        return "due"
    return "pending"


def process_person_events(db, notification_hook=None, today=None):
    """Daily worker pass. Hooks receive (event, old_status, new_status)."""
    today = today or date.today()
    changed = 0
    scanned = 0
    rows = db.execute("SELECT * FROM person_events ORDER BY id").fetchall()
    for row in rows:
        scanned += 1
        event = dict(row)
        if event["status"] == "failed" and int(event.get("retry_count") or 0) >= int(event.get("max_retries") or 3):
            continue
        new_status = _event_status(
            event["trigger_date"], event.get("due_date"), event["status"], today,
        )
        if new_status == event["status"]:
            continue
        try:
            if notification_hook:
                notification_hook(event, event["status"], new_status)
            db.execute(
                """UPDATE person_events SET status=?,retry_count=0,last_error=NULL,
                          next_retry_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (new_status, event["id"]),
            )
            changed += 1
        except Exception as error:
            retries = int(event.get("retry_count") or 0) + 1
            db.execute(
                """UPDATE person_events SET status='failed',retry_count=?,last_error=?,
                          next_retry_at=datetime('now',?),updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (retries, str(error)[:1000], f"+{min(300, 5 * 2 ** retries)} seconds", event["id"]),
            )
    db.commit()
    return {"scanned": scanned, "changed": changed, "run_date": today.isoformat()}


def recover_failed_person_events(db):
    cursor = db.execute(
        """UPDATE person_events SET status='pending',next_retry_at=CURRENT_TIMESTAMP,
                  updated_at=CURRENT_TIMESTAMP
           WHERE status='failed' AND retry_count<max_retries AND next_retry_at<=CURRENT_TIMESTAMP"""
    )
    db.commit()
    return cursor.rowcount


def refresh_person_events(db):
    """Idempotently materialize extensible person events from current business data."""
    contract_rows = db.execute(
        """SELECT c.contract_id,c.end_date,p.person_global_key
           FROM contracts c JOIN people p ON p.id=c.person_id
           WHERE c.is_deleted=0 AND p.is_deleted=0 AND c.end_date IS NOT NULL
                 AND p.person_global_key IS NOT NULL"""
    ).fetchall()
    for row in contract_rows:
        try:
            due = date.fromisoformat(row["end_date"][:10])
        except (TypeError, ValueError):
            continue
        trigger = (due - timedelta(days=30)).isoformat()
        source_ref = f"contract:{row['contract_id']}:renewal"
        existing = db.execute(
            "SELECT status FROM person_events WHERE source_ref=?", (source_ref,)
        ).fetchone()
        db.execute(
            """INSERT INTO person_events
               (person_global_key,event_type,trigger_date,due_date,status,source,source_ref,rule_version)
               VALUES (?,'contract_renewal',?,?,?,?,?,?)
               ON CONFLICT(source_ref) DO UPDATE SET
                 person_global_key=excluded.person_global_key,
                 trigger_date=excluded.trigger_date,due_date=excluded.due_date,
                 status=CASE WHEN person_events.status='completed' THEN 'completed' ELSE excluded.status END,
                 rule_version=excluded.rule_version,updated_at=CURRENT_TIMESTAMP""",
            (row["person_global_key"], trigger, due.isoformat(),
             _event_status(trigger, due.isoformat(), existing["status"] if existing else None),
             "contract", source_ref, EVENT_ENGINE_RULE_VERSION),
        )

    permit_rows = db.execute(
        """SELECT pc.id,pc.endorsement_expiry_date,p.person_global_key
           FROM person_cases pc JOIN people p ON p.id=pc.person_id
           WHERE pc.is_deleted=0 AND p.is_deleted=0
                 AND pc.endorsement_expiry_date IS NOT NULL
                 AND p.person_global_key IS NOT NULL"""
    ).fetchall()
    for row in permit_rows:
        try:
            due = date.fromisoformat(row["endorsement_expiry_date"][:10])
        except (TypeError, ValueError):
            continue
        trigger = (due - timedelta(days=30)).isoformat()
        source_ref = f"person_case:{row['id']}:permit_expiry"
        existing = db.execute(
            "SELECT status FROM person_events WHERE source_ref=?", (source_ref,)
        ).fetchone()
        db.execute(
            """INSERT INTO person_events
               (person_global_key,event_type,trigger_date,due_date,status,source,source_ref,rule_version)
               VALUES (?,'permit_expiry',?,?,?,?,?,?)
               ON CONFLICT(source_ref) DO UPDATE SET
                 person_global_key=excluded.person_global_key,
                 trigger_date=excluded.trigger_date,due_date=excluded.due_date,
                 status=CASE WHEN person_events.status='completed' THEN 'completed' ELSE excluded.status END,
                 rule_version=excluded.rule_version,updated_at=CURRENT_TIMESTAMP""",
            (row["person_global_key"], trigger, due.isoformat(),
             _event_status(trigger, due.isoformat(), existing["status"] if existing else None),
             "visa", source_ref, EVENT_ENGINE_RULE_VERSION),
        )
    db.commit()
