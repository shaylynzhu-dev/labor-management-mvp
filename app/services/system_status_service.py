from datetime import datetime, timezone

from app.utils.safe_execution import safe_execute


class SystemStatusService:
    def __init__(self, database):
        self.database = database

    def worker_status(self):
        def operation():
            with self.database.connect() as connection:
                rows = connection.execute("SELECT * FROM worker_heartbeats ORDER BY worker_name").fetchall()
            workers = []
            for row in rows:
                item = dict(row)
                try:
                    seen = datetime.fromisoformat(item["last_seen"].replace("Z", "+00:00"))
                    if seen.tzinfo is None:
                        seen = seen.replace(tzinfo=timezone.utc)
                    age = (datetime.now(timezone.utc) - seen).total_seconds()
                except (TypeError, ValueError):
                    age = 999999
                item["effective_status"] = item["status"] if age <= 90 and item["status"] != "stopped" else "stopped"
                workers.append(item)
            overall = "running" if any(
                item["worker_name"] == "event_dispatcher" and item["effective_status"] == "running"
                for item in workers
            ) else "stopped"
            return {"status": overall, "workers": workers}
        return safe_execute(operation, fallback={"status": "stopped", "workers": []})
