from datetime import date, datetime, timedelta
import hashlib
import uuid


PERSON_KEY_RULE_VERSION = "person-global-key-v1"
DOCUMENT_BINDING_RULE_VERSION = "document-binding-v1"
EVENT_ENGINE_RULE_VERSION = "person-event-engine-v1"


def _clean(value):
    return str(value or "").strip().casefold()


def generate_person_global_key(
    name, hkmo_permit_first4=None, hkmo_permit_last6=None,
    birth_date=None, mainland_id_last4=None,
):
    """Generate a non-sensitive stable key using the documented precedence."""
    normalized_name = _clean(name)
    name_hash = hashlib.sha256(normalized_name.encode("utf-8")).hexdigest()[:12]
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


def backfill_person_global_keys(db):
    rows = db.execute(
        """SELECT id,name,hkmo_permit_first4,hkmo_permit_last6,birth_date,
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


def _event_status(due_date, current_status=None):
    if current_status == "completed":
        return "completed"
    return "overdue" if due_date < date.today().isoformat() else "pending"


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
             _event_status(due.isoformat(), existing["status"] if existing else None),
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
             _event_status(due.isoformat(), existing["status"] if existing else None),
             "visa", source_ref, EVENT_ENGINE_RULE_VERSION),
        )
    db.commit()
