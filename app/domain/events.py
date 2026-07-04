import json
import logging
import uuid

from app.errors import service_result
from app.utils.safe_execution import safe_execute


PERSON_CREATED = "PERSON_CREATED"
CONTRACT_RENEWED = "CONTRACT_RENEWED"
DOCUMENT_UPLOADED = "DOCUMENT_UPLOADED"
VISA_STATUS_UPDATED = "VISA_STATUS_UPDATED"
WORKER_TASK_FAILED = "WORKER_TASK_FAILED"
BUSINESS_ACTION_COMPLETED = "BUSINESS_ACTION_COMPLETED"
DOMAIN_EVENT_TYPES = {
    PERSON_CREATED, CONTRACT_RENEWED, DOCUMENT_UPLOADED,
    VISA_STATUS_UPDATED, WORKER_TASK_FAILED, BUSINESS_ACTION_COMPLETED,
}
LOGGER = logging.getLogger("labour_os.event")


class DomainEventPublisher:
    def __init__(self, database):
        self.database = database

    def emit(self, event_type, payload=None, trace_id=None, user_id=None):
        def operation():
            if event_type not in DOMAIN_EVENT_TYPES:
                raise ValueError(f"unsupported event type: {event_type}")
            event_id = str(uuid.uuid4())
            with self.database.transaction() as connection:
                connection.execute(
                    """INSERT INTO domain_events
                       (event_id,event_type,payload,status,retry_count,max_retries,
                        trace_id,user_id,next_retry_at)
                       VALUES (?,?,?,'pending',0,3,?,?,CURRENT_TIMESTAMP)""",
                    (event_id, event_type, json.dumps(payload or {}, ensure_ascii=False),
                     trace_id or str(uuid.uuid4()), user_id),
                )
            LOGGER.info("domain_event_emitted event_type=%s event_id=%s", event_type, event_id)
            return {"event_id": event_id, "event_type": event_type, "status": "pending"}
        return safe_execute(operation, context={"event_type": event_type}, logger=LOGGER)
