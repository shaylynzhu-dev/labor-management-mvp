import tempfile
import unittest
from pathlib import Path

from app.background.job_queue import JobQueue
from app.domain.events import DomainEventPublisher
from app.models.database import Database
from app.services.notification_service import NotificationService
from app.utils.safe_execution import safe_execute


class ProductionHardeningV3Test(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "v3.db")
        self.database.initialize()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_domain_event_notification_and_job_are_durable(self):
        event = DomainEventPublisher(self.database).emit(
            "PERSON_CREATED", {"person_id": 1}, "trace-test", 9,
        )
        notification = NotificationService(self.database).in_app_notification(
            9, "测试", "通知", event["data"]["event_id"], "trace-test",
        )
        job = JobQueue(self.database).enqueue("TEST", {"event": event["data"]}, "trace-test")
        self.assertTrue(event["success"] and notification["success"] and job["success"])
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM domain_events").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM notification_queue").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM background_jobs").fetchone()[0], 1)

    def test_safe_executor_never_leaks_exception(self):
        result = safe_execute(lambda: 1 / 0, fallback={"items": []})
        self.assertFalse(result["success"])
        self.assertEqual(result["data"], {"items": []})
        self.assertEqual(result["error"]["code"], "UNKNOWN_ERROR")


if __name__ == "__main__":
    unittest.main()
