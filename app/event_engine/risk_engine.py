from datetime import date, timedelta
import logging

from app.utils.safe_execution import safe_execute


LOGGER = logging.getLogger("labour_os.event")


class RiskEngine:
    def __init__(self, database):
        self.database = database

    def evaluate(self, event_type, payload, trace_id=None):
        def operation():
            generated = []
            person_id = payload.get("person_id")
            contract_id = payload.get("contract_id")
            with self.database.transaction() as connection:
                if event_type == "CONTRACT_RENEWED" and contract_id:
                    contract = connection.execute(
                        "SELECT * FROM contracts WHERE contract_id=?", (contract_id,)
                    ).fetchone()
                    if contract and contract["end_date"]:
                        due = date.fromisoformat(contract["end_date"][:10])
                        source_key = f"v3:contract-renewal:{contract_id}"
                        connection.execute(
                            """INSERT OR IGNORE INTO risks
                               (person_id,contract_id,risk_type,risk_level,reason,status,source_key,due_date)
                               VALUES (?,?,'contract_restart_due','中',?,'开放',?,?)""",
                            (contract["person_id"], contract_id,
                             "合同结束前30天需要启动续约", source_key,
                             (due - timedelta(days=30)).isoformat()),
                        )
                        generated.append(source_key)
                if event_type == "VISA_STATUS_UPDATED" and person_id:
                    generated.append(f"visa-reviewed:{person_id}")
            return {"generated": generated, "trace_id": trace_id}
        return safe_execute(operation, context={"event_type": event_type}, logger=LOGGER)

    def escalate_overdue_contracts(self):
        def operation():
            today = date.today().isoformat()
            with self.database.transaction() as connection:
                rows = connection.execute(
                    """SELECT contract_id,person_id,end_date FROM contracts
                       WHERE is_deleted=0 AND end_date<? AND status!='完成合约'""",
                    (today,),
                ).fetchall()
                for row in rows:
                    connection.execute(
                        """INSERT OR IGNORE INTO domain_events
                           (event_id,event_type,payload,status,retry_count,max_retries,trace_id,next_retry_at)
                           VALUES (lower(hex(randomblob(16))),'WORKER_TASK_FAILED',?,'pending',0,3,
                                   lower(hex(randomblob(16))),CURRENT_TIMESTAMP)""",
                        (f'{{"contract_id":"{row["contract_id"]}","reason":"contract_overdue"}}',),
                    )
            return {"escalated": len(rows)}
        return safe_execute(operation, context={"event_type": "CONTRACT_OVERDUE"}, logger=LOGGER)
