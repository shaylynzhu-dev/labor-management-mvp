import tempfile
import unittest
import io
from pathlib import Path

from app import app, get_db, init_db
from app.services.person_profile_service import (
    check_hk_id_appointment_ready, get_entry_visa_query_key,
    get_missing_documents, get_person_profile, suggest_person_by_filename,
    save_person_document_batch, suggest_case_for_document,
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
            document_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(person_documents)")
            }
            self.assertTrue({"mime_type", "file_size", "upload_batch_id"}.issubset(document_columns))
            self.assertTrue({
                "person_case_id", "inferred_case_confidence", "case_binding_status",
            }.issubset(document_columns))
            self.assertIsNotNone(db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='person_cases'"
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
            db.execute("INSERT INTO people(name,person_name,gender) VALUES ('陈小明','陈小明','男')")
            db.commit()
            suggestions = suggest_person_by_filename(db, "陈小明_合同.pdf")
            self.assertEqual([item["name"] for item in suggestions], ["陈小明"])

    def test_case_suggestion_prefers_folder_and_leaves_ambiguous_unassigned(self):
        cases = [
            {"id": 1, "case_type": "new_contract", "case_label": "新人合约 2024-2026", "status": "completed", "start_date": "2024-01-01", "end_date": "2026-01-01"},
            {"id": 2, "case_type": "renewal", "case_label": "续约 2026-2028", "status": "active", "start_date": "2026-01-02", "end_date": "2028-01-01"},
        ]
        suggested = suggest_case_for_document({}, {
            "filename": "合同.pdf", "folder_name": "续约 2026-2028", "cases": cases,
        })
        self.assertEqual(suggested["case"]["id"], 2)
        self.assertEqual(suggested["status"], "suggested")
        ambiguous_cases = [dict(cases[0], status="active"), cases[1]]
        unassigned = suggest_case_for_document({}, {
            "filename": "scan001.pdf", "cases": ambiguous_cases,
        })
        self.assertIsNone(unassigned["case"])
        self.assertEqual(unassigned["status"], "unassigned")

    def test_batch_upload_records_manual_case_as_confirmed(self):
        with app.app_context():
            db = get_db()
            person_id = db.execute(
                "INSERT INTO people(name,person_name,gender) VALUES ('周期人员','周期人员','男')"
            ).lastrowid
            case_id = db.execute(
                "INSERT INTO person_cases(person_id,case_type,case_label,status) VALUES (?,?,?,?)",
                (person_id, "renewal", "续约 2026-2028", "active"),
            ).lastrowid
            db.commit()
            upload = type("Upload", (), {})()
            upload.filename = "续约/合同.pdf"
            upload.mimetype = "application/pdf"
            upload.stream = io.BytesIO(b"case-document")
            result = save_person_document_batch(
                db, app.config["UPLOAD_FOLDER"], person_id, [upload], "contract",
                person_case_id=case_id,
            )
            self.assertEqual(result["success"], 1)
            row = db.execute(
                "SELECT person_case_id,case_binding_status FROM person_documents WHERE person_id=?",
                (person_id,),
            ).fetchone()
            self.assertEqual(
                (row["person_case_id"], row["case_binding_status"]),
                (case_id, "confirmed"),
            )

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

    def test_folder_upload_binding_and_soft_delete_keeps_file(self):
        with app.app_context():
            db = get_db()
            person_id = db.execute(
                "INSERT INTO people(name,person_name,gender,worker_type) VALUES ('多文件人员','多文件人员','男','new')"
            ).lastrowid
            db.commit()
        client = app.test_client()
        response = client.post(
            "/person-documents/upload-batch",
            data={
                "person_id": str(person_id), "document_type": "resume",
                "return_to": "person",
                "files": [
                    (io.BytesIO(b"pdf-one"), "多文件人员_简历.pdf"),
                    (io.BytesIO(b"image-two"), "多文件人员_证件.jpg"),
                ],
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("成功 2", html)
        self.assertNotIn("{{", html)
        with app.app_context():
            documents = get_db().execute(
                "SELECT * FROM person_documents WHERE person_id=? ORDER BY id", (person_id,)
            ).fetchall()
            self.assertEqual(len(documents), 2)
            stored_file = Path(app.config["UPLOAD_FOLDER"]) / documents[0]["stored_path"]
            self.assertTrue(stored_file.is_file())
            document_id = documents[0]["id"]
        deleted = client.delete(f"/api/person_documents/{document_id}")
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(stored_file.is_file())
        with app.app_context():
            row = get_db().execute(
                "SELECT is_deleted,status FROM person_documents WHERE id=?", (document_id,)
            ).fetchone()
            self.assertEqual((row["is_deleted"], row["status"]), (1, "deleted"))

        library = client.get("/?view=documents")
        self.assertEqual(library.status_code, 200)
        self.assertNotIn("{{", library.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
