import json
import logging
import uuid

from app.errors import AppError, NOT_FOUND, VALIDATION_ERROR, service_result
from app.services.person_system_service import find_duplicate_people
from app.utils.safe_execution import safe_execute


LOGGER = logging.getLogger("labour_os.event")


class MergeService:
    LINKED_TABLES = {
        "person_documents": "id", "contracts": "contract_id", "events": "id",
        "tasks": "id", "risks": "id", "person_cases": "id",
    }

    def __init__(self, database, audit_log=None):
        self.database = database
        self.audit_log = audit_log

    def duplicate_candidates_list(self, person_id, threshold=0.85):
        def operation():
            with self.database.connect() as connection:
                person = connection.execute(
                    "SELECT * FROM people WHERE id=? AND is_deleted=0", (person_id,)
                ).fetchone()
                if not person:
                    return service_result(error=AppError(NOT_FOUND, "人员不存在", {"person_id": person_id}))
                candidates = find_duplicate_people(
                    connection, dict(person), threshold, exclude_person_id=person_id,
                )
            return {"person": {"id": person_id, "name": person["name"]}, "candidates": candidates}
        return safe_execute(operation, context={"person_id": person_id}, logger=LOGGER)

    def create_workflow(self, source_person_id, target_person_id, user_id=None, trace_id=None):
        def operation():
            if source_person_id == target_person_id:
                return service_result(error=AppError(VALIDATION_ERROR, "不能合并同一人员", {}))
            with self.database.transaction() as connection:
                people = connection.execute(
                    "SELECT id,name FROM people WHERE id IN (?,?) AND is_deleted=0",
                    (source_person_id, target_person_id),
                ).fetchall()
                if len(people) != 2:
                    return service_result(error=AppError(NOT_FOUND, "合并人员不存在", {}))
                workflow_id = str(uuid.uuid4())
                connection.execute(
                    """INSERT INTO person_merge_workflows
                       (workflow_id,source_person_id,target_person_id,status,requested_by,trace_id)
                       VALUES (?,?,?,'candidate',?,?)""",
                    (workflow_id, source_person_id, target_person_id, user_id,
                     trace_id or str(uuid.uuid4())),
                )
            return {"workflow_id": workflow_id, "status": "candidate"}
        return safe_execute(operation, context={"source_person_id": source_person_id}, logger=LOGGER)

    def confirm_merge(self, workflow_id, user_id=None):
        def operation():
            with self.database.transaction() as connection:
                workflow = connection.execute(
                    "SELECT * FROM person_merge_workflows WHERE workflow_id=?", (workflow_id,)
                ).fetchone()
                if not workflow:
                    return service_result(error=AppError(NOT_FOUND, "合并流程不存在", {}))
                if workflow["status"] != "candidate":
                    return service_result(error=AppError(VALIDATION_ERROR, "合并流程状态不可执行", {}))
                source_id, target_id = workflow["source_person_id"], workflow["target_person_id"]
                source = connection.execute("SELECT * FROM people WHERE id=?", (source_id,)).fetchone()
                target = connection.execute("SELECT * FROM people WHERE id=?", (target_id,)).fetchone()
                if not source or not target or source["is_deleted"] or target["is_deleted"]:
                    return service_result(error=AppError(NOT_FOUND, "源人员或目标人员不可用", {}))
                snapshot = {"source_is_deleted": source["is_deleted"], "tables": {}}
                for table, primary_key in self.LINKED_TABLES.items():
                    rows = connection.execute(
                        f"SELECT {primary_key} AS record_key FROM {table} WHERE person_id=?", (source_id,)
                    ).fetchall()
                    snapshot["tables"][table] = [row["record_key"] for row in rows]
                    connection.execute(
                        f"UPDATE {table} SET person_id=? WHERE person_id=?", (target_id, source_id)
                    )
                event_refs = connection.execute(
                    "SELECT id FROM person_events WHERE person_global_key=?",
                    (source["person_global_key"],),
                ).fetchall()
                snapshot["person_event_ids"] = [row["id"] for row in event_refs]
                connection.execute(
                    "UPDATE person_events SET person_global_key=? WHERE person_global_key=?",
                    (target["person_global_key"], source["person_global_key"]),
                )
                connection.execute(
                    "UPDATE people SET is_deleted=1,deleted_at=CURRENT_TIMESTAMP,deleted_by=? WHERE id=?",
                    (user_id, source_id),
                )
                connection.execute(
                    """UPDATE person_merge_workflows SET status='confirmed',confirmed_by=?,
                              snapshot_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (user_id, json.dumps(snapshot, ensure_ascii=False), workflow["id"]),
                )
                connection.execute(
                    """INSERT INTO person_change_log
                       (person_global_key,action,entity_type,entity_id,new_data,source,rule_version)
                       VALUES (?,'merge','person',?,?,'manual_override','merge-workflow-v1')""",
                    (target["person_global_key"], str(target_id), json.dumps({
                        "workflow_id": workflow_id, "source_person_id": source_id,
                        "target_person_id": target_id, "confirmed_by": user_id,
                    }, ensure_ascii=False)),
                )
            if self.audit_log:
                try:
                    self.audit_log.append(
                        "person_merge", trace_id=workflow["trace_id"], user_id=user_id,
                        event_type="PERSON_MERGED", context={"workflow_id": workflow_id},
                    )
                except Exception:
                    LOGGER.exception("merge_file_audit_failed workflow_id=%s", workflow_id)
            return {"workflow_id": workflow_id, "status": "confirmed"}
        return safe_execute(operation, context={"workflow_id": workflow_id}, logger=LOGGER)

    def rollback_merge(self, workflow_id, user_id=None):
        def operation():
            with self.database.transaction() as connection:
                workflow = connection.execute(
                    "SELECT * FROM person_merge_workflows WHERE workflow_id=?", (workflow_id,)
                ).fetchone()
                if not workflow or workflow["status"] != "confirmed":
                    return service_result(error=AppError(VALIDATION_ERROR, "没有可回滚的合并", {}))
                snapshot = json.loads(workflow["snapshot_json"] or "{}")
                source_id = workflow["source_person_id"]
                source = connection.execute("SELECT * FROM people WHERE id=?", (source_id,)).fetchone()
                for table, ids in snapshot.get("tables", {}).items():
                    if not ids:
                        continue
                    placeholders = ",".join("?" for _ in ids)
                    primary_key = self.LINKED_TABLES[table]
                    connection.execute(
                        f"UPDATE {table} SET person_id=? WHERE {primary_key} IN ({placeholders})",
                        (source_id, *ids),
                    )
                event_ids = snapshot.get("person_event_ids", [])
                if event_ids:
                    placeholders = ",".join("?" for _ in event_ids)
                    connection.execute(
                        f"UPDATE person_events SET person_global_key=? WHERE id IN ({placeholders})",
                        (source["person_global_key"], *event_ids),
                    )
                connection.execute(
                    "UPDATE people SET is_deleted=0,deleted_at=NULL,deleted_by=NULL WHERE id=?",
                    (source_id,),
                )
                connection.execute(
                    """UPDATE person_merge_workflows SET status='rolled_back',rolled_back_by=?,
                              updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (user_id, workflow["id"]),
                )
            if self.audit_log:
                try:
                    self.audit_log.append(
                        "person_merge_rollback", trace_id=workflow["trace_id"], user_id=user_id,
                        event_type="PERSON_MERGE_ROLLED_BACK", context={"workflow_id": workflow_id},
                    )
                except Exception:
                    LOGGER.exception("rollback_file_audit_failed workflow_id=%s", workflow_id)
            return {"workflow_id": workflow_id, "status": "rolled_back"}
        return safe_execute(operation, context={"workflow_id": workflow_id}, logger=LOGGER)
