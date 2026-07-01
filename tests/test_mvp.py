import io
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import (
    app, collect_push_reminders, db_insert, db_query, db_update, get_db,
    init_db, refresh_standard_risks, shift_months,
)


class LabourOSMVPTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.test_database = root / "test.db"
        upload_dir = root / "uploads"
        upload_dir.mkdir()
        app.config.update(
            TESTING=True,
            DATABASE=self.test_database,
            UPLOAD_FOLDER=upload_dir,
        )
        with app.app_context():
            init_db()
        self.client = app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def db_row(self, sql, parameters=()):
        with app.app_context():
            return get_db().execute(sql, parameters).fetchone()

    def test_clean_start_health_and_schema(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()["data"]
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["foreign_key_issues"], 0)
        with app.app_context():
            tables = {
                row[0]
                for row in get_db().execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        required = {
            "people", "quotas", "contracts", "workflow_instances", "workflow_steps",
            "documents", "risks", "tasks", "renewals", "lifecycle_nodes",
            "person", "quota", "contract", "event", "risk",
        }
        self.assertTrue(required.issubset(tables))
        with app.app_context():
            columns = {
                row["name"]: row
                for row in get_db().execute("PRAGMA table_info(people)").fetchall()
            }
        self.assertTrue({"gender", "company_name", "introducer"}.issubset(columns))
        self.assertEqual(columns["company_name"]["notnull"], 0)
        self.assertEqual(columns["introducer"]["notnull"], 0)
        with app.app_context():
            quota_columns = {
                row["name"]: row
                for row in get_db().execute("PRAGMA table_info(quotas)").fetchall()
            }
        self.assertTrue(
            {"approval_number", "quota_serial", "start_date", "expiry_date"}.issubset(
                quota_columns
            )
        )
        self.assertEqual(quota_columns["quota_number"]["notnull"], 0)
        self.assertEqual(quota_columns["approval_number"]["notnull"], 0)
        self.assertEqual(quota_columns["quota_serial"]["notnull"], 0)

    def test_jinja_templates_are_rendered_by_flask(self):
        template_dir = Path(app.root_path) / app.template_folder
        self.assertTrue((template_dir / "index.html").is_file())
        self.assertTrue((template_dir / "login.html").is_file())
        self.assertTrue((template_dir / "error.html").is_file())
        for template_path in template_dir.glob("*.html"):
            source = template_path.read_text(encoding="utf-8")
            self.assertNotIn("location.protocol === 'file:'", source)
            self.assertNotIn("http://127.0.0.1:5001/", source)
        self.assertTrue((template_dir / "contract_detail.html").is_file())
        self.assertTrue((template_dir / "quota_detail.html").is_file())
        self.assertTrue((template_dir / "quota" / "detail.html").is_file())
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.content_type)
        html = response.get_data(as_text=True)
        self.assertIn("Labour OS", html)
        self.assertNotIn("{{", html)
        self.assertNotIn("{%", html)
        self.assertIn("no-store", response.headers["Cache-Control"])
        contract_form = html.split('<dialog id="contract-modal">', 1)[1].split(
            "</dialog>", 1
        )[0]
        self.assertNotIn('name="arrival_date"', contract_form)
        self.assertNotIn('name="start_date"', contract_form)
        self.assertNotIn('name="end_date"', contract_form)

        login = self.client.get("/login")
        self.assertEqual(login.status_code, 200)
        self.assertNotIn("{{", login.get_data(as_text=True))
        self.assertNotIn("{%", login.get_data(as_text=True))

        alias = self.client.get("/index.html")
        self.assertEqual(alias.status_code, 200)
        self.assertIn("Labour OS", alias.get_data(as_text=True))

        for page_path in ("/people", "/contracts", "/quotas", "/risks", "/tasks"):
            page = self.client.get(page_path)
            self.assertEqual(page.status_code, 200)
            self.assertIn("text/html", page.content_type)
            self.assertNotIn("{{", page.get_data(as_text=True))
            self.assertNotIn("{%", page.get_data(as_text=True))

        missing = self.client.get("/not-a-real-page")
        self.assertEqual(missing.status_code, 404)
        self.assertIn("Labour OS", missing.get_data(as_text=True))
        self.assertNotIn("{{", missing.get_data(as_text=True))

    def test_standard_schema_crud_events_and_expiry_risk(self):
        with app.app_context():
            person_id = db_insert("person", {"name": "标准人员", "gender": "女"})
            quota_id = db_insert(
                "quota",
                {
                    "quota_type": "SWD", "company_name": "标准公司",
                    "user_id": person_id,
                    "end_date": (date.today() - timedelta(days=1)).isoformat(),
                },
            )
            contract_id = db_insert(
                "contract",
                {
                    "contract_no": "STD-001", "company_name": "标准公司",
                    "person_id": person_id, "quota_id": quota_id,
                },
            )
            standard_contract = db_query("contract", {"id": contract_id})[0]
            self.assertIsNone(standard_contract["arrival_date"])
            arrival_event_id = db_insert(
                "event",
                {
                    "event_type": "入境", "event_date": "2026-01-15",
                    "person_id": person_id, "description": "实际入境",
                },
            )
            standard_contract = db_query("contract", {"id": contract_id})[0]
            self.assertEqual(standard_contract["arrival_date"], "2026-01-15")
            self.assertEqual(standard_contract["contract_start_date"], "2026-01-15")
            self.assertEqual(standard_contract["contract_end_date"], "2028-01-15")
            self.assertEqual(
                db_query("event", {"id": arrival_event_id})[0]["contract_id"], contract_id
            )
            event_id = db_insert(
                "event",
                {
                    "event_type": "manual", "person_id": person_id,
                    "description": "手工事件",
                },
            )
            risk_id = db_insert(
                "risk",
                {
                    "person_id": person_id, "contract_id": contract_id,
                    "risk_type": "manual", "description": "手工风险",
                },
            )

            self.assertEqual(
                db_update("person", {"company_name": "更新公司"}, {"id": person_id}), 1
            )
            self.assertEqual(
                db_update("quota", {"approval_no": "APP-STD"}, {"id": quota_id}), 1
            )
            self.assertEqual(
                db_update("contract", {"status": "完成"}, {"id": contract_id}), 1
            )
            self.assertEqual(
                db_update("event", {"severity": "high"}, {"id": event_id}), 1
            )
            self.assertEqual(
                db_update("risk", {"status": "resolved"}, {"id": risk_id}), 1
            )

            self.assertEqual(db_query("person", {"id": person_id})[0]["company_name"], "更新公司")
            self.assertEqual(db_query("quota", {"id": quota_id})[0]["approval_no"], "APP-STD")
            self.assertEqual(db_query("contract", {"id": contract_id})[0]["status"], "完成合约")
            self.assertEqual(db_query("event", {"id": event_id})[0]["severity"], "high")
            self.assertEqual(db_query("risk", {"id": risk_id})[0]["status"], "resolved")

            refresh_standard_risks()
            expiry_risk = db_query(
                "risk", {"quota_id": quota_id, "risk_type": "quota_expired"}
            )
            self.assertEqual(len(expiry_risk), 1)
            self.assertEqual(expiry_risk[0]["status"], "open")
            automatic_events = get_db().execute(
                "SELECT COUNT(*) FROM event WHERE event_type LIKE '%_created'"
            ).fetchone()[0]
            self.assertGreaterEqual(automatic_events, 3)

    def test_excel_person_and_quota_import(self):
        import pandas as pd

        page = self.client.get("/?view=people").get_data(as_text=True)
        self.assertIn("导入人员Excel", page)
        self.assertIn("导入配额Excel", page)
        self.assertIn('/upload/person_excel', page)
        self.assertIn('/upload/quota_excel', page)

        people_file = io.BytesIO()
        pd.DataFrame(
            [
                {
                    "姓名": "批量人员甲", "性别": "男", "公司名": "批量公司",
                    "介绍人": "介绍甲", "身份证后四位": "0123",
                    "港澳通行证后四位": "0456",
                },
                {"姓名": "批量人员乙", "性别": "女"},
            ]
        ).to_excel(people_file, index=False, engine="openpyxl")
        people_bytes = people_file.getvalue()
        people_file.seek(0)
        response = self.client.post(
            "/upload/person_excel",
            data={"file": (people_file, "people.xlsx")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("人员 Excel 导入完成：成功 2，跳过 0，失败 0", response.get_data(as_text=True))
        imported_person = self.db_row("SELECT * FROM people WHERE name='批量人员甲'")
        self.assertEqual(imported_person["id_last4"], "0123")
        self.assertEqual(imported_person["permit_last4"], "0456")
        self.assertEqual(
            self.db_row("SELECT COUNT(*) AS count FROM person")["count"], 2
        )
        duplicate_response = self.client.post(
            "/upload/person_excel?format=json",
            data={"file": (io.BytesIO(people_bytes), "people.xlsx")},
            content_type="multipart/form-data",
        )
        self.assertEqual(
            duplicate_response.get_json(),
            {
                "code": 0, "message": "导入完成",
                "data": {
                    "type": "人员 Excel", "success": 0,
                    "skipped": 2, "failed": 0, "errors": [],
                },
            },
        )

        quota_file = io.BytesIO()
        pd.DataFrame(
            [
                {
                    "配额类型": "SWD", "公司名": "批量公司", "批文号": "APP-XLSX",
                    "配额序号": "SWD-XLSX-01", "使用人": "批量人员甲",
                    "开始日期": date.today().isoformat(),
                    "结束日期": (date.today() - timedelta(days=1)).isoformat(),
                },
                {"配额类型": "LD", "公司名": "批量公司二"},
            ]
        ).to_excel(quota_file, index=False, engine="openpyxl")
        quota_bytes = quota_file.getvalue()
        quota_file.seek(0)
        response = self.client.post(
            "/upload/quota_excel",
            data={"file": (quota_file, "quotas.xlsx")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("配额 Excel 导入完成：成功 2，跳过 0，失败 0", response.get_data(as_text=True))
        imported_quota = self.db_row(
            "SELECT * FROM quotas WHERE quota_serial='SWD-XLSX-01'"
        )
        self.assertEqual(imported_quota["person_id"], imported_person["id"])
        self.assertEqual(
            self.db_row("SELECT approval_no FROM quota WHERE id=?", (imported_quota["id"],))[
                "approval_no"
            ],
            "APP-XLSX",
        )
        self.assertEqual(
            self.db_row(
                "SELECT COUNT(*) AS count FROM risk WHERE quota_id=? AND risk_type='quota_expired'",
                (imported_quota["id"],),
            )["count"],
            1,
        )
        duplicate_response = self.client.post(
            "/upload/quota_excel?format=json",
            data={"file": (io.BytesIO(quota_bytes), "quotas.xlsx")},
            content_type="multipart/form-data",
        )
        self.assertEqual(
            duplicate_response.get_json(),
            {
                "code": 0, "message": "导入完成",
                "data": {
                    "type": "配额 Excel", "success": 0,
                    "skipped": 2, "failed": 0, "errors": [],
                },
            },
        )

        invalid_file = io.BytesIO()
        pd.DataFrame(
            [
                {"姓名": "应回滚人员", "性别": "男"},
                {"姓名": "错误人员", "性别": "未知"},
            ]
        ).to_excel(invalid_file, index=False, engine="openpyxl")
        invalid_file.seek(0)
        response = self.client.post(
            "/upload/person_excel?format=json",
            data={"file": (invalid_file, "invalid.xlsx")},
            content_type="multipart/form-data",
        )
        payload = response.get_json()["data"]
        self.assertEqual(payload["success"], 1)
        self.assertEqual(payload["skipped"], 0)
        self.assertEqual(payload["failed"], 1)
        self.assertIn("性别", payload["errors"][0])
        self.assertIsNotNone(self.db_row("SELECT id FROM people WHERE name='应回滚人员'"))

    def test_production_stability_guards(self):
        self.assertFalse(app.debug)
        self.assertTrue((Path(app.config["LOG_DIR"]) / "application.log").is_file())

        for endpoint in ("/health", "/api/dashboard/status", "/api/reminders"):
            response = self.client.get(endpoint)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(set(response.get_json()), {"code", "message", "data"})

        oversized = io.BytesIO(b"x" * (17 * 1024 * 1024))
        response = self.client.post(
            "/upload/person_excel?format=json",
            data={"file": (oversized, "oversized.xlsx")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(set(response.get_json()), {"code", "message", "data"})
        self.assertEqual(response.get_json()["code"], 413)

    def test_add_incomplete_quota_and_supplement_later(self):
        page = self.client.get("/").get_data(as_text=True)
        quota_form = page.split('<dialog id="quota-modal">', 1)[1].split("</dialog>", 1)[0]
        self.assertEqual(quota_form.count(" required"), 2)
        self.assertIn('name="quota_type" required', quota_form)
        self.assertIn('name="company_name" required', quota_form)
        for optional_field in (
            "approval_number", "quota_serial", "assigned_user", "expiry_date"
        ):
            self.assertIn(f'name="{optional_field}"', quota_form)
            self.assertNotIn(f'name="{optional_field}" required', quota_form)
        self.assertNotIn('name="start_date"', quota_form)
        response = self.client.post(
            "/quotas",
            data={"quota_type": "LD", "company_name": "半数据公司"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("已添加配额：未编号", response.get_data(as_text=True))
        quota = self.db_row("SELECT * FROM quotas WHERE company_name='半数据公司'")
        self.assertEqual(quota["quota_type"], "LD")
        for field in (
            "approval_number", "quota_serial", "person_id", "start_date", "expiry_date"
        ):
            self.assertIsNone(quota[field])
        self.assertIsNone(quota["quota_number"])

        # A half-complete quota is already referenceable by existing business flows.
        self.client.post(
            "/people",
            data={"person_name": "配额引用人员", "gender": "男"},
            follow_redirects=True,
        )
        person = self.db_row("SELECT * FROM people WHERE name='配额引用人员'")
        self.client.post(
            "/contracts",
            data={
                "contract_id": "HALF-Q-CONTRACT",
                "person_id": person["id"],
                "quota_id": quota["id"],
                "company": "半数据公司",
            },
            follow_redirects=True,
        )
        self.client.post(
            "/workflows",
            data={
                "person_id": person["id"], "quota_id": quota["id"],
                "workflow_name": "半数据配额流程",
            },
            follow_redirects=True,
        )
        self.assertEqual(
            self.db_row("SELECT quota_id FROM workflow_instances")["quota_id"], quota["id"]
        )

        response = self.client.post(
            f"/quotas/{quota['id']}/details",
            data={
                "quota_type": "LD", "company_name": "半数据公司",
                "approval_number": "APP-001", "quota_serial": "LD-009",
                "assigned_user": "", "expiry_date": "2027-12-31",
            },
            follow_redirects=True,
        )
        self.assertIn("配额资料已更新", response.get_data(as_text=True))
        updated = self.db_row("SELECT * FROM quotas WHERE id=?", (quota["id"],))
        self.assertEqual(updated["approval_number"], "APP-001")
        self.assertEqual(updated["quota_serial"], "LD-009")
        self.assertEqual(updated["quota_number"], "LD-009")
        self.assertEqual(updated["expiry_date"], "2027-12-31")

    def test_add_person_with_required_fields_only(self):
        response = self.client.post(
            "/people",
            data={"person_name": "最简人员", "gender": "女"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("已添加人员：最简人员", response.get_data(as_text=True))
        person = self.db_row("SELECT * FROM people WHERE name='最简人员'")
        self.assertEqual(person["gender"], "女")
        self.assertIsNone(person["company_name"])
        self.assertIsNone(person["introducer"])
        self.assertIsNone(person["id_last4"])
        self.assertIsNone(person["permit_last4"])
        people_page = self.client.get("/people", follow_redirects=True).get_data(as_text=True)
        self.assertIn("最简人员", people_page)
        self.assertIn("公司", people_page)
        self.assertIn("介绍人", people_page)

    def test_legacy_person_schema_migrates_without_data_loss(self):
        legacy_database = Path(self.temp_dir.name) / "legacy.db"
        connection = sqlite3.connect(legacy_database)
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE people (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                id_last4 TEXT NOT NULL CHECK(length(id_last4) = 4),
                permit_last4 TEXT NOT NULL CHECK(length(permit_last4) = 4),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE CASCADE
            );
            INSERT INTO people (name, id_last4, permit_last4)
            VALUES ('历史人员', '1234', '5678');
            INSERT INTO events (person_id, event_type, note, created_at)
            VALUES (1, '登记', '历史事件', '2025-01-01 09:00');
            CREATE TABLE quotas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quota_number TEXT NOT NULL UNIQUE,
                company_name TEXT NOT NULL,
                quota_type TEXT NOT NULL DEFAULT 'SWD',
                person_id INTEGER UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE SET NULL
            );
            CREATE TABLE quota_usages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quota_id INTEGER NOT NULL,
                person_id INTEGER NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (quota_id) REFERENCES quotas(id) ON DELETE CASCADE,
                FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE RESTRICT
            );
            INSERT INTO quotas
                (quota_number, company_name, quota_type, person_id)
            VALUES ('LEGACY-SWD-01', '历史公司', 'SWD', 1);
            INSERT INTO quota_usages (quota_id, person_id, start_date)
            VALUES (1, 1, '2025-01-01');
            """
        )
        connection.commit()
        connection.close()

        app.config["DATABASE"] = legacy_database
        try:
            with app.app_context():
                init_db()
                database = get_db()
                person = database.execute(
                    "SELECT * FROM people WHERE name='历史人员'"
                ).fetchone()
                event = database.execute(
                    "SELECT * FROM events WHERE person_id=1"
                ).fetchone()
                quota = database.execute(
                    "SELECT * FROM quotas WHERE quota_number='LEGACY-SWD-01'"
                ).fetchone()
                usage = database.execute(
                    "SELECT * FROM quota_usages WHERE quota_id=1"
                ).fetchone()
                foreign_key_issues = database.execute("PRAGMA foreign_key_check").fetchall()
                standard_person = database.execute(
                    "SELECT * FROM person WHERE id=1"
                ).fetchone()
                standard_quota = database.execute(
                    "SELECT * FROM quota WHERE id=1"
                ).fetchone()
            self.assertEqual(person["id_last4"], "1234")
            self.assertEqual(person["permit_last4"], "5678")
            self.assertIsNone(person["gender"])
            self.assertEqual(event["note"], "历史事件")
            self.assertEqual(quota["company_name"], "历史公司")
            self.assertIsNone(quota["approval_number"])
            self.assertEqual(usage["start_date"], "2025-01-01")
            self.assertEqual(standard_person["hk_macao_last4"], "5678")
            self.assertEqual(standard_quota["quota_no"], "LEGACY-SWD-01")
            self.assertEqual(foreign_key_issues, [])
        finally:
            app.config["DATABASE"] = self.test_database

    def test_complete_data_flow(self):
        # Person
        response = self.client.post(
            "/people",
            data={
                "person_name": "测试人员", "gender": "男",
                "company_name": "", "introducer": "",
            },
            follow_redirects=True,
        )
        self.assertIn("测试人员", response.get_data(as_text=True))
        person = self.db_row("SELECT * FROM people WHERE name='测试人员'")

        # Quota
        response = self.client.post(
            "/quotas",
            data={
                "quota_number": "SWD-MVP-001", "company_name": "测试机构",
                "quota_type": "SWD", "person_id": person["id"],
                "start_date": date.today().isoformat(),
            },
            follow_redirects=True,
        )
        self.assertIn("SWD-MVP-001", response.get_data(as_text=True))
        quota = self.db_row("SELECT * FROM quotas WHERE quota_number='SWD-MVP-001'")

        # Contract and query
        contract_end = date.today() + timedelta(days=10)
        arrival_date = shift_months(contract_end, -24)
        response = self.client.post(
            "/contracts",
            data={
                "contract_id": "CON-MVP-001", "person_id": person["id"],
                "person_name": "", "company": "测试机构", "status": "制作合同",
                "quota_id": quota["id"],
            },
            follow_redirects=True,
        )
        self.assertIn("CON-MVP-001", response.get_data(as_text=True))
        pending_contract = self.db_row(
            "SELECT * FROM contracts WHERE contract_id='CON-MVP-001'"
        )
        self.assertIsNone(pending_contract["start_date"])
        for lifecycle_status in (
            "交接香港同事", "交表香港入境处", "批出入境签证", "工人入境",
        ):
            self.client.post(
                "/events",
                data={
                    "person_id": person["id"], "event_type": lifecycle_status,
                    "event_date": arrival_date.isoformat(), "note": "生命周期验收",
                },
                follow_redirects=True,
            )
        created_contract = self.db_row(
            "SELECT * FROM contracts WHERE contract_id='CON-MVP-001'"
        )
        self.assertEqual(created_contract["arrival_date"], arrival_date.isoformat())
        self.assertEqual(created_contract["contract_start_date"], arrival_date.isoformat())
        self.assertEqual(created_contract["contract_end_date"], contract_end.isoformat())
        self.assertEqual(created_contract["start_date"], arrival_date.isoformat())
        self.assertEqual(created_contract["end_date"], contract_end.isoformat())
        self.assertIn(
            "CON-MVP-001",
            self.client.get("/?q=CON-MVP-001").get_data(as_text=True),
        )
        detail = self.client.get("/contracts/CON-MVP-001")
        self.assertEqual(detail.status_code, 200)
        self.assertNotIn("{{", detail.get_data(as_text=True))
        self.assertNotIn("{%", detail.get_data(as_text=True))
        self.assertIn("更新状态", detail.get_data(as_text=True))
        self.client.post(
            "/contracts/CON-MVP-001/status", data={"status": "制作合同"},
            follow_redirects=True,
        )
        self.assertEqual(
            self.db_row("SELECT status FROM contracts WHERE contract_id='CON-MVP-001'")["status"],
            "工人入境",
        )
        quota_detail = self.client.get(f"/quotas/{quota['id']}")
        self.assertEqual(quota_detail.status_code, 200)
        self.assertIn("使用情况", quota_detail.get_data(as_text=True))
        self.assertNotIn("{{", quota_detail.get_data(as_text=True))
        self.assertNotIn("{%", quota_detail.get_data(as_text=True))

        # Dashboard is the business entry point; a name query returns all related data.
        dashboard = self.client.get("/?q=测试人员")
        dashboard_text = dashboard.get_data(as_text=True)
        self.assertIn("My Work Queue", dashboard_text)
        self.assertIn("我的待办队列", dashboard_text)
        self.assertIn("系统建议动作", dashboard_text)
        self.assertIn("创建续约任务", dashboard_text)
        self.assertIn("当前状态", dashboard_text)
        self.assertIn("下一步建议操作", dashboard_text)
        self.assertIn("是否需要处理", dashboard_text)
        self.assertIn("快速办理", dashboard_text)
        self.assertIn("当前名额", dashboard_text)
        self.assertIn("SWD-MVP-001", dashboard_text)
        self.assertIn("CON-MVP-001", dashboard_text)
        self.assertIn("Yes", dashboard_text)
        self.assertNotIn("90天内合同到期", dashboard_text)
        self.assertNotIn("名额使用情况", dashboard_text)
        self.assertIn("事件", dashboard_text)
        live_status = self.client.get("/api/dashboard/status")
        self.assertEqual(live_status.status_code, 200)
        self.assertIn("risks", live_status.get_json()["data"])
        self.assertIn("tasks", live_status.get_json()["data"])
        self.assertEqual(live_status.get_json()["data"]["contracts_90"], 1)
        with app.app_context():
            pushed = collect_push_reminders()
        self.assertTrue(any(item["key"].startswith("contract:") for item in pushed))
        self.client.post(
            "/dashboard/actions/contract-renewal",
            data={"contract_id": "CON-MVP-001"}, follow_redirects=True,
        )
        self.assertEqual(
            self.db_row(
                "SELECT COUNT(*) AS count FROM tasks WHERE source_key LIKE 'DECISION:RENEWAL:%'"
            )["count"],
            1,
        )

        # Workflow
        response = self.client.post(
            "/workflows",
            data={
                "person_id": person["id"], "quota_id": quota["id"],
                "workflow_name": "MVP验收流程",
            },
            follow_redirects=True,
        )
        self.assertIn("流程已创建", response.get_data(as_text=True))
        workflow = self.db_row("SELECT * FROM workflow_instances")
        self.assertEqual(
            self.db_row("SELECT COUNT(*) AS count FROM workflow_steps")["count"], 6
        )

        # Document: binding, upload, extraction and query
        response = self.client.post(
            "/documents",
            data={
                "title": "护照文件", "person_id": str(person["id"]),
                "quota_id": str(quota["id"]), "workflow_id": str(workflow["id"]),
                "file": (io.BytesIO(b"MVP searchable passport content"), "passport.txt"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn("已提取", response.get_data(as_text=True))
        self.assertIn(
            "护照文件",
            self.client.get("/?view=documents&doc_q=searchable").get_data(as_text=True),
        )

        # Renewal -> lifecycle -> risk
        renewal_end = shift_months(date.today(), 6)
        self.client.post(
            "/renewals",
            data={
                "person_id": person["id"], "quota_id": quota["id"],
                "current_contract_end_date": renewal_end.isoformat(),
                "passport_expiry_check": "通过", "id_card_expiry_check": "待检查",
            },
            follow_redirects=True,
        )
        self.client.get("/")
        self.assertEqual(
            self.db_row("SELECT COUNT(*) AS count FROM lifecycle_nodes")["count"], 3
        )
        self.assertGreaterEqual(
            self.db_row("SELECT COUNT(*) AS count FROM risks")["count"], 2
        )
        self.assertIn(
            "风险中心", self.client.get("/risks", follow_redirects=True).get_data(as_text=True)
        )
        risk = self.db_row("SELECT id, source_key FROM risks WHERE status='开放' LIMIT 1")
        self.client.post(f"/risks/{risk['id']}/resolve", follow_redirects=True)
        self.client.get("/")
        self.assertEqual(
            self.db_row("SELECT status FROM risks WHERE source_key=?", (risk["source_key"],))["status"],
            "已解决",
        )

        # Full approval flow; final entry step generates tasks.
        while True:
            step = self.db_row(
                """SELECT id FROM workflow_steps WHERE workflow_id=? AND status='待处理'
                   ORDER BY step_order LIMIT 1""",
                (workflow["id"],),
            )
            if not step:
                break
            self.client.post(
                f"/workflow-steps/{step['id']}/action",
                data={"action": "approve"},
                follow_redirects=True,
            )
        self.assertEqual(
            self.db_row("SELECT status FROM workflow_instances")["status"], "已完成"
        )
        self.assertEqual(self.db_row("SELECT COUNT(*) AS count FROM tasks")["count"], 3)
        self.assertIn("已完成", self.client.get("/tasks", follow_redirects=True).get_data(as_text=True))
        boss_text = self.client.get("/").get_data(as_text=True)
        self.assertIn("入境后30天", boss_text)
        self.assertIn("入境后45天", boss_text)
        self.assertIn("入境后60天", boss_text)

        entry = self.db_row(
            "SELECT id FROM events WHERE person_id=? AND event_type='工人入境' ORDER BY id DESC LIMIT 1",
            (person["id"],),
        )
        self.client.post(
            "/dashboard/actions/reminder",
            data={"event_id": entry["id"], "days": "45"}, follow_redirects=True,
        )
        self.assertEqual(
            self.db_row(
                "SELECT COUNT(*) AS count FROM tasks WHERE source_key LIKE 'DECISION:EVENT:%'"
            )["count"],
            1,
        )

        with app.app_context():
            db = get_db()
            cursor = db.execute(
                """INSERT INTO risks
                   (person_id, quota_id, risk_type, risk_level, reason, source_key)
                   VALUES (?, ?, '名额', '中', '名额使用不足', 'TEST:QUOTA:ACTION')""",
                (person["id"], quota["id"]),
            )
            risk_id = cursor.lastrowid
            db.commit()
        decision_page = self.client.get("/").get_data(as_text=True)
        self.assertIn("建议释放或替补", decision_page)
        self.assertIn("缺少关联人员，暂不可执行", decision_page)
        self.assertEqual(
            self.db_row(
                "SELECT COUNT(*) AS count FROM workflow_instances WHERE workflow_name='名额替补流程'"
            )["count"],
            0,
        )

        task = self.db_row("SELECT id FROM tasks ORDER BY id LIMIT 1")
        self.client.post(f"/tasks/{task['id']}/status", data={"status": "已完成"})
        self.assertEqual(
            self.db_row("SELECT status FROM tasks WHERE id=?", (task["id"],))["status"],
            "已完成",
        )

        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.get_json()["data"]["foreign_key_issues"], 0)


if __name__ == "__main__":
    unittest.main()
