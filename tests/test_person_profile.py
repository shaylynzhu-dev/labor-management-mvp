import tempfile
import unittest
import io
from pathlib import Path

from app import app, get_db, init_db
from app.services.person_profile_service import (
    calculate_renewal_alert_dates, check_hk_id_appointment_ready, get_entry_visa_query_key,
    get_missing_documents, get_person_profile, suggest_person_by_filename,
    save_person_document_batch, search_people_for_binding, suggest_case_for_document,
)
from app.services.person_system_service import (
    generate_person_global_key, refresh_person_events,
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
                "document_hash", "duplicate_of_document_id", "version_no",
                "binding_source", "data_source", "data_precedence_rank",
            }.issubset(document_columns))
            self.assertIsNotNone(db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='person_cases'"
            ).fetchone())
            self.assertIsNotNone(db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='conflict_queue'"
            ).fetchone())
            self.assertIsNotNone(db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='retry_queue'"
            ).fetchone())
            self.assertEqual(
                db.execute("SELECT rank FROM data_precedence_rules WHERE source='manual_input'").fetchone()[0],
                1,
            )
            self.assertIn("person_global_key", columns)
            self.assertTrue({"person_global_key", "binding_rule_version"}.issubset(document_columns))
            for table in ("person_events", "person_change_log", "import_batch_versions", "rule_versions"):
                self.assertIsNotNone(db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
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

    def test_global_key_event_engine_and_change_history(self):
        self.assertTrue(generate_person_global_key(
            "规则人员", "CJ52", "123456", "1990-01-01", "8899"
        ).startswith("HKP-"))
        with app.app_context():
            db = get_db()
            person_id = db.execute(
                """INSERT INTO people
                   (name,person_name,gender,mainland_id_last4,data_source)
                   VALUES ('规则人员','规则人员','男','8899','manual_input')"""
            ).lastrowid
            person = db.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchone()
            self.assertTrue(person["person_global_key"].startswith("MID-"))
            self.assertIsNotNone(db.execute(
                "SELECT 1 FROM person_change_log WHERE person_global_key=? AND action='create'",
                (person["person_global_key"],),
            ).fetchone())
            db.execute(
                """INSERT INTO contracts
                   (contract_id,person_name,company,status,person_id,end_date)
                   VALUES ('C-EVENT-1','规则人员','测试公司','制作合同',?,'2030-06-30')""",
                (person_id,),
            )
            db.commit()
            refresh_person_events(db)
            event = db.execute(
                "SELECT * FROM person_events WHERE source_ref='contract:C-EVENT-1:renewal'"
            ).fetchone()
            self.assertEqual(event["trigger_date"], "2030-05-31")
            self.assertEqual(event["event_type"], "contract_renewal")

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

    def test_renewal_alert_dates_are_calculated_and_missing_dates_are_safe(self):
        result = calculate_renewal_alert_dates({
            "contract_end_date": "2026-08-31",
            "endorsement_expiry_date": "2026-03-31",
        })
        self.assertEqual(result["contract_restart_due_date"], "2026-08-01")
        self.assertEqual(result["document_collection_due_date"], "2026-02-28")
        self.assertEqual(calculate_renewal_alert_dates({}), {
            "contract_restart_due_date": None,
            "document_collection_due_date": None,
        })

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

    def test_smart_binding_search_includes_recent_contract(self):
        with app.app_context():
            db = get_db()
            person_id = db.execute(
                "INSERT INTO people(name,person_name,gender,company_name) VALUES ('陈小明','陈小明','男','安康公司')"
            ).lastrowid
            db.execute(
                """INSERT INTO contracts
                   (contract_id,person_name,company,status,person_id)
                   VALUES ('C-SMART-001','陈小明','安康公司','制作合同',?)""",
                (person_id,),
            )
            db.commit()
            matches = search_people_for_binding(db, "C-SMART")
            self.assertEqual(matches[0]["id"], person_id)
            self.assertEqual(matches[0]["recent_contract"], "C-SMART-001")

    def test_batch_upload_duplicate_and_unclassified_go_to_control_queues(self):
        with app.app_context():
            db = get_db()
            person_id = db.execute(
                "INSERT INTO people(name,person_name,gender) VALUES ('重复人员','重复人员','男')"
            ).lastrowid
            active_one = db.execute(
                "INSERT INTO person_cases(person_id,case_type,case_label,status) VALUES (?,?,?,?)",
                (person_id, "renewal", "续约 2026-2028", "active"),
            ).lastrowid
            db.execute(
                "INSERT INTO person_cases(person_id,case_type,case_label,status) VALUES (?,?,?,?)",
                (person_id, "replacement", "替补 2026-2028", "active"),
            )
            db.commit()

            def make_upload(name, body):
                upload = type("Upload", (), {})()
                upload.filename = name
                upload.mimetype = "application/pdf"
                upload.stream = io.BytesIO(body)
                return upload

            first = save_person_document_batch(
                db, app.config["UPLOAD_FOLDER"], person_id,
                [make_upload("scan001.pdf", b"same-file")], "contract",
            )
            second = save_person_document_batch(
                db, app.config["UPLOAD_FOLDER"], person_id,
                [make_upload("scan001.pdf", b"same-file")], "contract",
            )
            self.assertEqual(first["success"], 1)
            self.assertEqual(second["success"], 1)
            rows = db.execute(
                "SELECT status,version_no,duplicate_of_document_id FROM person_documents WHERE person_id=? ORDER BY id",
                (person_id,),
            ).fetchall()
            self.assertEqual(rows[0]["status"], "human_review_required")
            self.assertEqual(rows[1]["status"], "duplicate")
            self.assertEqual(rows[1]["version_no"], 2)
            self.assertIsNotNone(rows[1]["duplicate_of_document_id"])
            self.assertIsNotNone(db.execute(
                "SELECT 1 FROM conflict_queue WHERE conflict_type='multiple_case_match'"
            ).fetchone())

            bad = save_person_document_batch(
                db, app.config["UPLOAD_FOLDER"], person_id,
                [make_upload("bad.exe", b"bad")], "contract", person_case_id=active_one,
            )
            self.assertEqual(bad["skipped"], 1)
            self.assertIsNotNone(db.execute(
                "SELECT 1 FROM retry_queue WHERE filename='bad.exe'"
            ).fetchone())

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
