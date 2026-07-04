import json
import logging
import os
from pathlib import Path
import signal
import sqlite3
import time
import uuid

from app.domain.events import WORKER_TASK_FAILED
from app.event_engine.risk_engine import RiskEngine
from app.models.database import Database
from app.services.notification_service import NotificationService


BASE_DIR = Path(__file__).resolve().parents[2]
DATABASE_PATH = Path(os.environ.get("LABOUR_OS_DATABASE_PATH", BASE_DIR / "labor.db")).resolve()
LOGGER = logging.getLogger("labour_os.worker")
RUNNING = True


def _stop(_signal, _frame):
    global RUNNING
    RUNNING = False


class EventDispatcher:
    def __init__(self, database, worker_id=None):
        self.database = database
        self.worker_id = worker_id or f"event-dispatcher-{uuid.uuid4().hex[:8]}"
        self.notifications = NotificationService(database)
        self.risks = RiskEngine(database)

    def heartbeat(self, status="running", detail=None):
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO worker_heartbeats(worker_name,worker_id,status,last_seen,detail)
                   VALUES ('event_dispatcher',?,?,CURRENT_TIMESTAMP,?)
                   ON CONFLICT(worker_name) DO UPDATE SET worker_id=excluded.worker_id,
                     status=excluded.status,last_seen=CURRENT_TIMESTAMP,detail=excluded.detail""",
                (self.worker_id, status, detail),
            )

    def claim_event(self):
        with self.database.transaction() as connection:
            row = connection.execute(
                """SELECT * FROM domain_events
                   WHERE status IN ('pending','retry') AND next_retry_at<=CURRENT_TIMESTAMP
                   ORDER BY id LIMIT 1"""
            ).fetchone()
            if not row:
                return None
            cursor = connection.execute(
                """UPDATE domain_events SET status='processing',worker_id=?,
                          started_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND status IN ('pending','retry')""",
                (self.worker_id, row["id"]),
            )
            return dict(row) if cursor.rowcount else None

    def _handle(self, event):
        payload = json.loads(event["payload"] or "{}")
        event_type = event["event_type"]
        risk_result = self.risks.evaluate(event_type, payload, event["trace_id"])
        if not risk_result["success"]:
            raise RuntimeError(risk_result["error"]["message"])
        if event_type == "PERSON_CREATED":
            title, message = "新人员已建立", "人员档案已进入 Labour OS。"
        elif event_type == "DOCUMENT_UPLOADED":
            title, message = "资料上传完成", "人员资料已进入后台处理队列。"
        elif event_type == "VISA_STATUS_UPDATED":
            title, message = "VISA 状态已更新", "请检查后续办理事项。"
        elif event_type == "CONTRACT_RENEWED":
            title, message = "合同续约事件", "续约风险与时间节点已重新计算。"
        elif event_type == "WORKER_TASK_FAILED":
            title, message = "后台任务需要关注", payload.get("reason", "任务处理失败。")
        else:
            title, message = "业务操作已记录", "操作事件已安全归档。"
        notification = self.notifications.in_app_notification(
            event.get("user_id") or "all", title, message,
            event_id=event["event_id"], trace_id=event["trace_id"],
        )
        if not notification["success"]:
            raise RuntimeError(notification["error"]["message"])

    def mark_done(self, event):
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE domain_events SET status='done',completed_at=CURRENT_TIMESTAMP,
                          updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (event["id"],),
            )

    def mark_failed(self, event, error):
        retries = int(event["retry_count"] or 0) + 1
        status = "dead_letter" if retries >= int(event["max_retries"] or 3) else "retry"
        delay = min(300, 5 * (2 ** retries))
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE domain_events SET status=?,retry_count=?,last_error=?,
                          next_retry_at=datetime('now',?),updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (status, retries, str(error)[:2000], f"+{delay} seconds", event["id"]),
            )
            if status == "dead_letter" and event["event_type"] != WORKER_TASK_FAILED:
                connection.execute(
                    """INSERT INTO domain_events
                       (event_id,event_type,payload,status,retry_count,max_retries,trace_id,next_retry_at)
                       VALUES (?,? ,?,'pending',0,3,?,CURRENT_TIMESTAMP)""",
                    (str(uuid.uuid4()), WORKER_TASK_FAILED, json.dumps({
                        "failed_event_id": event["event_id"], "reason": str(error)[:500],
                    }, ensure_ascii=False), event["trace_id"]),
                )
        LOGGER.error(
            "event_failed trace_id=%s event_type=%s retry=%s status=%s",
            event["trace_id"], event["event_type"], retries, status,
        )

    def recover_from_failure(self):
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """UPDATE domain_events SET status='retry',worker_id=NULL,
                          next_retry_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                   WHERE status='processing' AND started_at<datetime('now','-5 minutes')"""
            )
        return cursor.rowcount

    def run_forever(self, poll_seconds=2):
        last_heartbeat = 0
        while RUNNING:
            try:
                now = time.monotonic()
                if now - last_heartbeat >= 30:
                    recovered = self.recover_from_failure()
                    self.heartbeat("running", f"recovered={recovered}")
                    last_heartbeat = now
                event = self.claim_event()
                if not event:
                    time.sleep(poll_seconds)
                    continue
                try:
                    self._handle(event)
                    self.mark_done(event)
                except Exception as error:
                    self.mark_failed(event, error)
            except Exception as error:
                LOGGER.exception("dispatcher_loop_recovered error=%s", type(error).__name__)
                try:
                    self.heartbeat("degraded", type(error).__name__)
                except Exception:
                    LOGGER.exception("heartbeat_failed")
                time.sleep(5)
        try:
            self.heartbeat("stopped")
        except Exception:
            LOGGER.exception("stopped_heartbeat_failed")


def main():
    (BASE_DIR / "logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s trace_id=system user_id=None event_type=WORKER_EVENT %(message)s",
        handlers=[logging.FileHandler(BASE_DIR / "logs" / "worker.log"), logging.StreamHandler()],
    )
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    database = Database(DATABASE_PATH)
    database.initialize()
    EventDispatcher(database).run_forever()


if __name__ == "__main__":
    main()
