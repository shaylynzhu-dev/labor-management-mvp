import calendar
from datetime import date, datetime


CONTRACT_LIFECYCLE = (
    "制作合同",
    "交接香港同事",
    "交表香港入境处",
    "批出入境签证",
    "工人入境",
    "完成合约",
)
QUOTA_STATUSES = ("active", "in_use", "exhausted", "invalid")
LEGACY_STATUS_MAP = {
    "登记": "制作合同",
    "付款": "制作合同",
    "签证中": "交表香港入境处",
    "已签": "批出入境签证",
    "入境": "工人入境",
    "完成": "完成合约",
    "active": "制作合同",
    "completed": "完成合约",
}


def normalize_contract_status(status):
    value = str(status or "").strip()
    return LEGACY_STATUS_MAP.get(value, value)


def quota_replacement_limit(quota_type):
    if quota_type == "SWD":
        return 1
    if quota_type == "LD":
        return 2
    raise ValueError("配额类型必须为 SWD 或 LD")


def shift_months(value, months):
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _iso_date(value, label):
    if isinstance(value, date):
        return value.isoformat()
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date().isoformat()
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label}格式无效") from error


def _quota_for_contract(connection, contract):
    if contract["quota_id"]:
        return connection.execute(
            "SELECT * FROM quotas WHERE id=?", (contract["quota_id"],)
        ).fetchone()
    quota = connection.execute(
        "SELECT * FROM quotas WHERE person_id=? ORDER BY id DESC LIMIT 1",
        (contract["person_id"],),
    ).fetchone()
    if quota:
        connection.execute(
            "UPDATE contracts SET quota_id=? WHERE contract_id=?",
            (quota["id"], contract["contract_id"]),
        )
    return quota


def transition_contract(connection, contract_id, target_status, event_date=None):
    target = normalize_contract_status(target_status)
    if target not in CONTRACT_LIFECYCLE:
        raise ValueError("合同生命周期状态无效")
    contract = connection.execute(
        "SELECT * FROM contracts WHERE contract_id=?", (contract_id,)
    ).fetchone()
    if not contract:
        raise ValueError("合同不存在")
    current = normalize_contract_status(contract["status"])
    if current == target:
        return {"contract_id": contract_id, "status": current, "changed": False}
    current_index = CONTRACT_LIFECYCLE.index(current)
    target_index = CONTRACT_LIFECYCLE.index(target)
    if current == "完成合约":
        raise ValueError("已完成合同禁止再次流转")
    if target_index != current_index + 1:
        raise ValueError(f"合同只能从“{current}”流转到“{CONTRACT_LIFECYCLE[current_index + 1]}”")
    if target == "工人入境":
        if not event_date:
            raise ValueError("工人入境必须提供实际入境日期")
        return register_worker_entry(connection, contract_id, event_date)
    connection.execute(
        "UPDATE contracts SET status=? WHERE contract_id=?", (target, contract_id)
    )
    if target == "完成合约":
        completed_on = _iso_date(event_date or date.today(), "完成日期")
        quota = _quota_for_contract(connection, contract)
        if quota:
            connection.execute(
                """UPDATE quota_usages SET end_date=COALESCE(end_date, ?)
                   WHERE quota_id=? AND person_id=? AND end_date IS NULL""",
                (completed_on, quota["id"], contract["person_id"]),
            )
            connection.execute(
                "UPDATE quotas SET status='active', person_id=NULL WHERE id=? AND status!='invalid'",
                (quota["id"],),
            )
    return {"contract_id": contract_id, "status": target, "changed": True}


def register_worker_entry(connection, contract_id, entry_date):
    entry = _iso_date(entry_date, "入境日期")
    contract = connection.execute(
        "SELECT * FROM contracts WHERE contract_id=?", (contract_id,)
    ).fetchone()
    if not contract:
        raise ValueError("合同不存在")
    current = normalize_contract_status(contract["status"])
    if current not in {"批出入境签证", "工人入境"}:
        raise ValueError("合同必须先完成“批出入境签证”阶段")
    quota = _quota_for_contract(connection, contract)
    if not quota:
        raise ValueError("合同未绑定配额，禁止进入工人入境阶段")
    if quota["status"] in {"invalid", "exhausted"}:
        raise ValueError("配额已失效或耗尽，禁止继续使用")
    contract_end = shift_months(datetime.strptime(entry, "%Y-%m-%d").date(), 24).isoformat()
    first_entry = not contract["entry_date"]
    connection.execute(
        """UPDATE contracts SET entry_date=?, arrival_date=?,
                  contract_start_date=?, contract_end_date=?, start_date=?, end_date=?,
                  status='工人入境' WHERE contract_id=?""",
        (entry, entry, entry, contract_end, entry, contract_end, contract_id),
    )
    if first_entry:
        connection.execute(
            """UPDATE quotas SET usage_count=usage_count+1, status='in_use',
                      person_id=?, start_date=COALESCE(start_date, ?)
               WHERE id=?""",
            (contract["person_id"], entry, quota["id"]),
        )
        active_usage = connection.execute(
            "SELECT id FROM quota_usages WHERE quota_id=? AND end_date IS NULL",
            (quota["id"],),
        ).fetchone()
        if not active_usage:
            connection.execute(
                "INSERT INTO quota_usages(quota_id,person_id,start_date) VALUES(?,?,?)",
                (quota["id"], contract["person_id"], entry),
            )
    return {
        "contract_id": contract_id,
        "quota_id": quota["id"],
        "status": "工人入境",
        "entry_date": entry,
        "changed": first_entry,
    }


def trigger_worker_departure(connection, person_id, departure_date):
    departure = _iso_date(departure_date, "离职日期")
    contract = connection.execute(
        """SELECT * FROM contracts WHERE person_id=? AND is_replaced=0
           AND status!='完成合约' ORDER BY cycle_index DESC, created_at DESC LIMIT 1""",
        (person_id,),
    ).fetchone()
    if not contract:
        raise ValueError("未找到该人员可替补的合同周期")
    quota = _quota_for_contract(connection, contract)
    if not quota:
        raise ValueError("合同未绑定配额")
    maximum = quota_replacement_limit(quota["quota_type"])
    new_count = int(quota["replacement_count"] or 0) + 1
    invalid = new_count > maximum
    connection.execute(
        """UPDATE quota_usages SET end_date=COALESCE(end_date, ?)
           WHERE quota_id=? AND person_id=? AND end_date IS NULL""",
        (departure, quota["id"], person_id),
    )
    connection.execute(
        """UPDATE contracts SET is_replaced=1, status='完成合约'
           WHERE contract_id=?""",
        (contract["contract_id"],),
    )
    connection.execute(
        """UPDATE quotas SET replacement_count=?, max_replacement_count=?,
                  status=?, person_id=NULL WHERE id=?""",
        (new_count, maximum, "invalid" if invalid else "active", quota["id"]),
    )
    return {
        "quota_id": quota["id"],
        "parent_contract_id": contract["contract_id"],
        "replacement_count": new_count,
        "max_replacement_count": maximum,
        "allowed": not invalid,
        "quota_status": "invalid" if invalid else "active",
    }


def create_replacement_cycle(connection, quota_id, new_person_id, replacement_date):
    start = _iso_date(replacement_date, "替补日期")
    quota = connection.execute("SELECT * FROM quotas WHERE id=?", (quota_id,)).fetchone()
    person = connection.execute(
        "SELECT id,name FROM people WHERE id=?", (new_person_id,)
    ).fetchone()
    if not quota or not person:
        raise ValueError("配额或替补人员不存在")
    if quota["status"] in {"invalid", "exhausted"}:
        raise ValueError("配额已失效或耗尽，禁止生成新周期")
    if int(quota["replacement_count"] or 0) > int(quota["max_replacement_count"]):
        connection.execute("UPDATE quotas SET status='invalid' WHERE id=?", (quota_id,))
        raise ValueError("配额替补次数已超过上限")
    parent = connection.execute(
        """SELECT * FROM contracts WHERE quota_id=? AND is_replaced=1
           ORDER BY cycle_index DESC, created_at DESC LIMIT 1""",
        (quota_id,),
    ).fetchone()
    if not parent:
        connection.execute(
            "UPDATE quotas SET person_id=?, status='active' WHERE id=?",
            (new_person_id, quota_id),
        )
        return {"contract_id": None, "cycle_index": None, "initial_assignment": True}
    existing_child = connection.execute(
        "SELECT contract_id FROM contracts WHERE parent_contract_id=?",
        (parent["contract_id"],),
    ).fetchone()
    if existing_child:
        raise ValueError("该合同周期已经生成替补合同")
    cycle_index = int(parent["cycle_index"] or 1) + 1
    base_id = parent["contract_id"].split("-R", 1)[0]
    contract_id = f"{base_id}-R{cycle_index}"
    while connection.execute(
        "SELECT 1 FROM contracts WHERE contract_id=?", (contract_id,)
    ).fetchone():
        cycle_index += 1
        contract_id = f"{base_id}-R{cycle_index}"
    connection.execute(
        """INSERT INTO contracts
           (contract_id,person_name,company,status,person_id,quota_id,
            cycle_index,parent_contract_id,is_replaced)
           VALUES(?,?,?,'制作合同',?,?,?,?,0)""",
        (
            contract_id, person["name"], quota["company_name"], new_person_id,
            quota_id, cycle_index, parent["contract_id"],
        ),
    )
    connection.execute(
        "UPDATE quotas SET person_id=?, status='active' WHERE id=?",
        (new_person_id, quota_id),
    )
    return {
        "contract_id": contract_id,
        "cycle_index": cycle_index,
        "parent_contract_id": parent["contract_id"],
        "replacement_date": start,
        "initial_assignment": False,
    }
