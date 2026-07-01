import tempfile
import unittest
from pathlib import Path

from app import app, get_db, init_db


class QuotaTimelineTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        app.config.update(TESTING=True, DATABASE=root / "test.db", UPLOAD_FOLDER=root)
        with app.app_context():
            init_db()
            db = get_db()
            first = db.execute(
                "INSERT INTO people(name,gender) VALUES (?,?)", ("工人 A", "男")
            ).lastrowid
            second = db.execute(
                "INSERT INTO people(name,gender) VALUES (?,?)", ("工人 B", "女")
            ).lastrowid
            self.quota_id = db.execute(
                "INSERT INTO quotas(quota_type,company_name) VALUES (?,?)", ("SWD", "测试公司")
            ).lastrowid
            db.execute(
                """INSERT INTO quota_worker_history
                   (quota_id,worker_id,start_date,status,event_type,replacement_round,source_type)
                   VALUES (?,?,?,'active','initial',0,'SWD')""",
                (self.quota_id, first, "2025-01-01"),
            )
            db.execute(
                """INSERT INTO quota_worker_history
                   (quota_id,worker_id,end_date,status,event_type,replacement_round,source_type)
                   VALUES (?,?,?,'closed','resignation',0,'SWD')""",
                (self.quota_id, first, "2025-03-01"),
            )
            db.execute(
                """INSERT INTO quota_worker_history
                   (quota_id,worker_id,start_date,status,event_type,replacement_round,source_type)
                   VALUES (?,?,?,'active','replacement',1,'SWD')""",
                (self.quota_id, second, "2025-03-01"),
            )
            db.commit()
        self.client = app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_timeline_api_replays_history_and_detail_has_both_views(self):
        response = self.client.get(f"/api/quota/{self.quota_id}/timeline")
        self.assertEqual(response.status_code, 200)
        timeline = response.get_json()["data"]
        self.assertEqual([item["event_type"] for item in timeline], [
            "initial", "resignation", "replacement",
        ])
        self.assertEqual(timeline[0]["status"], "ended")
        self.assertEqual(timeline[0]["end_date"], "2025-03-01")
        self.assertEqual(timeline[-1]["worker_name"], "工人 B")
        self.assertEqual(timeline[-1]["status"], "active")

        detail = self.client.get(f"/quota/{self.quota_id}")
        html = detail.get_data(as_text=True)
        self.assertEqual(detail.status_code, 200)
        self.assertIn("Table View", html)
        self.assertIn("Timeline View", html)
        self.assertIn(f"/api/quota/{self.quota_id}/timeline", html)

        legacy_detail = self.client.get(f"/quotas/{self.quota_id}")
        self.assertEqual(legacy_detail.status_code, 200)


if __name__ == "__main__":
    unittest.main()
