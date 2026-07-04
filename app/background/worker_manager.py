import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

from app.models.database import Database


BASE_DIR = Path(__file__).resolve().parents[2]
DATABASE_PATH = Path(os.environ.get("LABOUR_OS_DATABASE_PATH", BASE_DIR / "labor.db")).resolve()
LOGGER = logging.getLogger("labour_os.worker_manager")
RUNNING = True


def _stop(_signal, _frame):
    global RUNNING
    RUNNING = False


def _heartbeat(database, manager_id, status, restart_count):
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO worker_heartbeats(worker_name,worker_id,status,last_seen,detail)
               VALUES ('worker_manager',?,?,CURRENT_TIMESTAMP,?)
               ON CONFLICT(worker_name) DO UPDATE SET worker_id=excluded.worker_id,
                 status=excluded.status,last_seen=CURRENT_TIMESTAMP,detail=excluded.detail""",
            (manager_id, status, f"restart_count={restart_count}"),
        )


def main():
    (BASE_DIR / "logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s trace_id=system user_id=None event_type=WORKER_MANAGER %(message)s",
        handlers=[logging.FileHandler(BASE_DIR / "logs" / "worker.log"), logging.StreamHandler()],
    )
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    database = Database(DATABASE_PATH)
    database.initialize()
    manager_id = f"manager-{uuid.uuid4().hex[:8]}"
    child = None
    restart_count = 0
    last_heartbeat = 0
    while RUNNING:
        try:
            if child is None or child.poll() is not None:
                if child is not None:
                    restart_count += 1
                    LOGGER.error("event_dispatcher exited code=%s; restarting", child.returncode)
                    time.sleep(min(30, 2 ** min(restart_count, 5)))
                child = subprocess.Popen([sys.executable, "-m", "app.workers.event_dispatcher"], cwd=BASE_DIR)
            if time.monotonic() - last_heartbeat >= 30:
                _heartbeat(database, manager_id, "running", restart_count)
                last_heartbeat = time.monotonic()
            time.sleep(1)
        except Exception:
            LOGGER.exception("worker_manager recovered from failure")
            time.sleep(5)
    if child and child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
    _heartbeat(database, manager_id, "stopped", restart_count)


if __name__ == "__main__":
    main()
