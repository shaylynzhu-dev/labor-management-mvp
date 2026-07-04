import json
import uuid

from app.errors import NOT_FOUND, AppError, service_result
from app.utils.safe_execution import safe_execute


class JobQueue:
    def __init__(self, database):
        self.database = database

    def enqueue(self, job_type, payload=None, trace_id=None, max_retries=3):
        def operation():
            job_id = str(uuid.uuid4())
            with self.database.transaction() as connection:
                connection.execute(
                    """INSERT INTO background_jobs
                       (job_id,job_type,payload,status,retry_count,max_retries,trace_id,next_run_at)
                       VALUES (?,?,?,'pending',0,?,?,CURRENT_TIMESTAMP)""",
                    (job_id, job_type, json.dumps(payload or {}, ensure_ascii=False),
                     max_retries, trace_id or str(uuid.uuid4())),
                )
            return {"job_id": job_id, "status": "pending"}
        return safe_execute(operation, context={"job_type": job_type})

    def claim_next(self, worker_id):
        def operation():
            with self.database.transaction() as connection:
                row = connection.execute(
                    """SELECT * FROM background_jobs
                       WHERE status IN ('pending','retry') AND next_run_at<=CURRENT_TIMESTAMP
                       ORDER BY id LIMIT 1"""
                ).fetchone()
                if not row:
                    return None
                updated = connection.execute(
                    """UPDATE background_jobs SET status='processing',worker_id=?,
                              started_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status IN ('pending','retry')""",
                    (worker_id, row["id"]),
                )
                if not updated.rowcount:
                    return None
                claimed = dict(row)
                claimed["status"] = "processing"
                claimed["worker_id"] = worker_id
                return claimed
        return safe_execute(operation)

    def retry_failed(self, job_id, error_message):
        def operation():
            with self.database.transaction() as connection:
                row = connection.execute(
                    "SELECT * FROM background_jobs WHERE job_id=?", (job_id,)
                ).fetchone()
                if not row:
                    return service_result(error=AppError(NOT_FOUND, "任务不存在", {"job_id": job_id}))
                retries = int(row["retry_count"] or 0) + 1
                status = "dead_letter" if retries >= int(row["max_retries"] or 3) else "retry"
                delay = min(300, 2 ** retries * 5)
                connection.execute(
                    """UPDATE background_jobs SET status=?,retry_count=?,last_error=?,
                              next_run_at=datetime('now',?),updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (status, retries, str(error_message)[:2000], f"+{delay} seconds", row["id"]),
                )
            return {"job_id": job_id, "status": status, "retry_count": retries}
        return safe_execute(operation)

    def mark_done(self, job_id):
        def operation():
            with self.database.transaction() as connection:
                cursor = connection.execute(
                    """UPDATE background_jobs SET status='done',completed_at=CURRENT_TIMESTAMP,
                              updated_at=CURRENT_TIMESTAMP WHERE job_id=?""",
                    (job_id,),
                )
            if not cursor.rowcount:
                return service_result(error=AppError(NOT_FOUND, "任务不存在", {"job_id": job_id}))
            return {"job_id": job_id, "status": "done"}
        return safe_execute(operation)
