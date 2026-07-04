import argparse
import logging
import os
from pathlib import Path
import sqlite3
import time

from app.services.person_system_service import (
    process_person_events, recover_failed_person_events, refresh_person_events,
)


BASE_DIR = Path(__file__).resolve().parents[2]
DATABASE = Path(
    os.environ.get("LABOUR_OS_DATABASE_PATH", BASE_DIR / "labor.db")
).expanduser().resolve()
LOGGER = logging.getLogger("labour_os.person_event_worker")


def notification_hook(event, old_status, new_status):
    """Extension point for email, webhook or push notification adapters."""
    LOGGER.info(
        "person_event_transition id=%s type=%s from=%s to=%s",
        event["id"], event["event_type"], old_status, new_status,
    )


def run_once(database_path=DATABASE):
    connection = sqlite3.connect(database_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")
    try:
        refresh_person_events(connection)
        recover_failed_person_events(connection)
        return process_person_events(connection, notification_hook=notification_hook)
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description="Labour OS person event scheduler")
    parser.add_argument(
        "--interval", type=int, default=0,
        help="Repeat interval in seconds; omit or use 0 for a single cron-friendly pass.",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s trace_id=system user_id=None event_type=PERSON_EVENT %(message)s",
    )
    while True:
        result = run_once()
        LOGGER.info("person_event_scan %s", result)
        if args.interval <= 0:
            break
        time.sleep(max(args.interval, 60))


if __name__ == "__main__":
    main()
