import tempfile
import unittest
from pathlib import Path

from app import app, get_db, init_db
from app.services.dispatch_engine import (
    CONTRACT_LIFECYCLE,
    create_replacement_cycle,
    transition_contract,
    trigger_worker_departure,
)


class DispatchEngineTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        app.config.update(
            TESTING=True,
            DATABASE=Path(self.temp_dir.name) / "dispatch.db",
            UPLOAD_FOLDER=Path(self.temp_dir.name),
        )
        self.context = app.app_context()
        self.context.push()
        init_db()
        self.db = get_db()

    def tearDown(self):
        self.context.pop()
        self.temp_dir.cleanup()

    def add_person(self, name):
        cursor = self.db.execute(
            "INSERT INTO people(name,gender) VALUES(?,'男')", (name,)
        )
        return cursor.lastrowid

    def add_quota(self, quota_type, person_id, suffix):
        cursor = self.db.execute(
            """INSERT INTO quotas
               (quota_number,company_name,quota_type,person_id,
                max_replacement_count,status)
               VALUES(?, '调度测试公司', ?, ?, ?, 'active')""",
            (f"{quota_type}-{suffix}", quota_type, person_id, 1 if quota_type == "SWD" else 2),
        )
        return cursor.lastrowid

    def add_contract(self, contract_id, person_id, quota_id):
        name = self.db.execute(
            "SELECT name FROM people WHERE id=?", (person_id,)
        ).fetchone()["name"]
        self.db.execute(
            """INSERT INTO contracts
               (contract_id,person_name,company,status,person_id,quota_id,cycle_index)
               VALUES(?,?,?,'制作合同',?,?,1)""",
            (contract_id, name, "调度测试公司", person_id, quota_id),
        )
        self.db.commit()

    def advance_to_entry(self, contract_id, entry_date):
        for status in CONTRACT_LIFECYCLE[1:5]:
            transition_contract(self.db, contract_id, status, entry_date)

    def test_swd_entry_replacement_cycle_and_limit(self):
        first = self.add_person("SWD首位工人")
        second = self.add_person("SWD替补工人")
        third = self.add_person("SWD超限工人")
        quota_id = self.add_quota("SWD", first, "001")
        self.add_contract("SWD-CYCLE", first, quota_id)

        self.advance_to_entry("SWD-CYCLE", "2026-01-10")
        contract = self.db.execute(
            "SELECT * FROM contracts WHERE contract_id='SWD-CYCLE'"
        ).fetchone()
        quota = self.db.execute("SELECT * FROM quotas WHERE id=?", (quota_id,)).fetchone()
        self.assertEqual(contract["entry_date"], "2026-01-10")
        self.assertEqual(contract["start_date"], "2026-01-10")
        self.assertEqual(contract["status"], "工人入境")
        self.assertEqual(quota["usage_count"], 1)
        self.assertEqual(quota["status"], "in_use")

        departure = trigger_worker_departure(self.db, first, "2026-06-01")
        self.assertTrue(departure["allowed"])
        self.assertEqual(departure["replacement_count"], 1)
        cycle = create_replacement_cycle(self.db, quota_id, second, "2026-06-01")
        self.assertEqual(cycle["cycle_index"], 2)
        self.assertEqual(cycle["parent_contract_id"], "SWD-CYCLE")

        self.advance_to_entry(cycle["contract_id"], "2026-07-01")
        quota = self.db.execute("SELECT * FROM quotas WHERE id=?", (quota_id,)).fetchone()
        self.assertEqual(quota["usage_count"], 2)
        over_limit = trigger_worker_departure(self.db, second, "2026-12-01")
        self.assertFalse(over_limit["allowed"])
        self.assertEqual(over_limit["replacement_count"], 2)
        quota = self.db.execute("SELECT * FROM quotas WHERE id=?", (quota_id,)).fetchone()
        self.assertEqual(quota["status"], "invalid")
        with self.assertRaisesRegex(ValueError, "失效"):
            create_replacement_cycle(self.db, quota_id, third, "2026-12-01")

    def test_ld_allows_two_replacements_and_preserves_chain(self):
        workers = [self.add_person(f"LD工人{i}") for i in range(1, 5)]
        quota_id = self.add_quota("LD", workers[0], "001")
        self.add_contract("LD-CYCLE", workers[0], quota_id)
        current_contract = "LD-CYCLE"

        for index in range(3):
            self.advance_to_entry(current_contract, f"2026-0{index + 1}-10")
            result = trigger_worker_departure(
                self.db, workers[index], f"2026-0{index + 1}-20"
            )
            if index < 2:
                self.assertTrue(result["allowed"])
                cycle = create_replacement_cycle(
                    self.db, quota_id, workers[index + 1], f"2026-0{index + 1}-20"
                )
                current_contract = cycle["contract_id"]
            else:
                self.assertFalse(result["allowed"])

        rows = self.db.execute(
            """SELECT contract_id,cycle_index,parent_contract_id,is_replaced
               FROM contracts WHERE quota_id=? ORDER BY cycle_index""",
            (quota_id,),
        ).fetchall()
        self.assertEqual([row["cycle_index"] for row in rows], [1, 2, 3])
        self.assertEqual(rows[1]["parent_contract_id"], rows[0]["contract_id"])
        self.assertEqual(rows[2]["parent_contract_id"], rows[1]["contract_id"])
        self.assertTrue(all(row["is_replaced"] == 1 for row in rows))
        quota = self.db.execute("SELECT * FROM quotas WHERE id=?", (quota_id,)).fetchone()
        self.assertEqual(quota["replacement_count"], 3)
        self.assertEqual(quota["max_replacement_count"], 2)
        self.assertEqual(quota["status"], "invalid")

    def test_quota_assignment_route_creates_linked_cycle(self):
        first = self.add_person("路由首位工人")
        second = self.add_person("路由替补工人")
        quota_id = self.add_quota("SWD", first, "ROUTE")
        self.add_contract("ROUTE-CYCLE", first, quota_id)
        self.advance_to_entry("ROUTE-CYCLE", "2026-03-01")
        self.db.commit()

        response = app.test_client().post(
            f"/quotas/{quota_id}/assign",
            data={"person_id": second, "start_date": "2026-08-01"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        child = self.db.execute(
            "SELECT * FROM contracts WHERE parent_contract_id='ROUTE-CYCLE'"
        ).fetchone()
        self.assertIsNotNone(child)
        self.assertEqual(child["cycle_index"], 2)
        self.assertEqual(child["status"], "制作合同")
        self.assertEqual(child["person_id"], second)
        self.assertIsNotNone(
            self.db.execute(
                "SELECT id FROM contract WHERE contract_no=?", (child["contract_id"],)
            ).fetchone()
        )


if __name__ == "__main__":
    unittest.main()
