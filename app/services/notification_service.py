import json
import logging
import uuid

from app.utils.safe_execution import safe_execute


LOGGER = logging.getLogger("labour_os.event")


class NotificationService:
    def __init__(self, database):
        self.database = database

    def _enqueue(self, channel, recipient, title, message, event_id=None, trace_id=None):
        def operation():
            notification_id = str(uuid.uuid4())
            with self.database.transaction() as connection:
                connection.execute(
                    """INSERT INTO notification_queue
                       (notification_id,channel,recipient,title,message,event_id,status,
                        retry_count,max_retries,trace_id,next_retry_at)
                       VALUES (?,?,?,?,?,?,'pending',0,3,?,CURRENT_TIMESTAMP)""",
                    (notification_id, channel, recipient, title, message, event_id,
                     trace_id or str(uuid.uuid4())),
                )
            LOGGER.info("notification_enqueued event_type=%s notification_id=%s", event_id, notification_id)
            return {"notification_id": notification_id, "channel": channel, "status": "pending"}
        return safe_execute(operation, context={"event_type": event_id}, logger=LOGGER)

    def in_app_notification(self, recipient, title, message, event_id=None, trace_id=None):
        return self._enqueue("in_app", str(recipient or "all"), title, message, event_id, trace_id)

    def email_notification(self, recipient, title, message, event_id=None, trace_id=None):
        return self._enqueue("email", recipient, title, message, event_id, trace_id)

    def webhook_notification(self, recipient, title, message, event_id=None, trace_id=None):
        return self._enqueue("webhook", recipient, title, message, event_id, trace_id)

    def list_in_app(self, recipient=None, limit=30):
        def operation():
            with self.database.connect() as connection:
                rows = connection.execute(
                    """SELECT * FROM notification_queue
                       WHERE channel='in_app' AND status IN ('pending','sent')
                         AND (? IS NULL OR recipient IN (?, 'all'))
                       ORDER BY id DESC LIMIT ?""",
                    (recipient, str(recipient), int(limit)),
                ).fetchall()
            return [dict(row) for row in rows]
        return safe_execute(operation, fallback=[])
