from datetime import datetime, timezone
import json
from pathlib import Path
import threading


class AppendOnlyAuditLog:
    """Filesystem audit trail: append is the only supported mutation."""
    _lock = threading.Lock()

    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, action, *, trace_id, user_id=None, event_type=None, context=None):
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trace_id": trace_id,
            "user_id": user_id,
            "event_type": event_type,
            "action": action,
            "context": context or {},
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock, self.path.open("a", encoding="utf-8") as output:
            output.write(line)
            output.flush()
        return record
