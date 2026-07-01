import tempfile
import unittest
from pathlib import Path

from app import app, get_db, init_db
from app.services.person_profile_service import (
    check_hk_id_appointment_ready, get_entry_visa_query_key,
    get_missing_documents, get_person_profile,
)


class PersonProfileTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        app.config.update(TESTING=True, DATABASE=root / "profile.db", UPLOAD_FOLDER=root)
        with app.app_context():
            init_db()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_rules_and_safe_schema_migration(self):
        with app.app_context():
            db = get_db()
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(people)").fetchall()
            }
            self.assertTrue({
                "worker_type", "mainland_id_first4", "hkmo_permit_first4",
                "hkmo_permit_last6", "entry_permit_no", "visa_status",
            }.issubset(columns))
            self.assertIsNotNone(db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='person_documents'"
            ).fetchone())

            person = {
                "worker_type": "new", "mainland_id_first4": "1234",
                "entry_permit_no": "EP-001", "birth_year_month": "1992-06",
                "hkmo_permit_last6": "456789",
            }
            self.assertEqual(get_entry_visa_query_key(person)["field"], "mainland_id_first4")
            self.assertTrue(check_hk_id_appointment_ready(person)["ready"])
            self.assertEqual(get_missing_documents(
                person, {"resume", "contract", "mainland_id", "hkmo_permit"}
            ), [])

    def test_profile_masks_entry_permit_for_non_admin(self):
        with app.app_context():
            db = get_db()
            person_id = db.execute(
                """INSERT INTO people
                   (name,person_name,gender,worker_type,mainland_id_first4,entry_permit_no)
                   VALUES ('档案测试','档案测试','女','new','1234','ENTRY-9988')"""
            ).lastrowid
            db.commit()
            public_profile = get_person_profile(db, person_id, False)
            admin_profile = get_person_profile(db, person_id, True)
            self.assertNotEqual(public_profile["person"]["display_entry_permit_no"], "ENTRY-9988")
            self.assertEqual(admin_profile["person"]["display_entry_permit_no"], "ENTRY-9988")

            page = app.test_client().get(f"/people/{person_id}")
            self.assertEqual(page.status_code, 200)
            self.assertIn("入境签证查询", page.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
