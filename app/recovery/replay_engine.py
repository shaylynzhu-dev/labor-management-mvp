from app.errors import AppError, NOT_FOUND, service_result
from app.utils.safe_execution import safe_execute


class ReplayEngine:
    def __init__(self, database):
        self.database = database

    def replay_event(self, event_id):
        def operation():
            with self.database.transaction() as connection:
                cursor = connection.execute(
                    """UPDATE domain_events SET status='retry',retry_count=0,last_error=NULL,
                              worker_id=NULL,next_retry_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                       WHERE event_id=? AND status IN ('failed','dead_letter','retry')""",
                    (event_id,),
                )
            if not cursor.rowcount:
                return service_result(error=AppError(NOT_FOUND, "没有可重放的事件", {"event_id": event_id}))
            return {"event_id": event_id, "status": "retry"}
        return safe_execute(operation, context={"event_id": event_id})

    def replay_dead_letters(self, limit=100):
        def operation():
            with self.database.transaction() as connection:
                ids = connection.execute(
                    "SELECT id FROM domain_events WHERE status='dead_letter' ORDER BY id LIMIT ?",
                    (int(limit),),
                ).fetchall()
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    connection.execute(
                        f"""UPDATE domain_events SET status='retry',retry_count=0,last_error=NULL,
                                   worker_id=NULL,next_retry_at=CURRENT_TIMESTAMP,
                                   updated_at=CURRENT_TIMESTAMP WHERE id IN ({placeholders})""",
                        tuple(row["id"] for row in ids),
                    )
            return {"replayed": len(ids)}
        return safe_execute(operation)
