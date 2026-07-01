import io
import json
from datetime import date, datetime

from app.utils.excel import validate_excel_shape, validate_excel_upload
from app.services.dispatch_engine import (
    CONTRACT_LIFECYCLE, normalize_contract_status, quota_replacement_limit,
    shift_months,
)


DEFAULT_MAPPINGS = {
    "person": {
        "company_name": "公司名称", "name": "劳工姓名",
        "worker_type": "人员类型", "entry_permit_no": "入境签证号码",
        "birth_date": "出生日期", "mainland_id_number": "身份证号码",
        "hkmo_permit_first4": "通行证前四位",
        "hkmo_permit_last6": "通行证后六位",
        "hk_submission_date": "HK入表日期",
        "visa_status": "VISA状态",
    },
    "quota": {
        "quota_type": "配额类型", "company_name": "公司名",
        "approval_no": "批文号", "quota_no": "配额序号",
        "user_name": "使用人", "start_date": "开始日期", "end_date": "结束日期",
    },
    "contract": {
        "contract_no": "合同编号", "company_name": "公司名",
        "person_name": "人员", "quota_no": "配额序号",
        "status": "状态",
    },
    "lifecycle": {
        "approval_no": "批文号", "facility": "院舍",
        "quota_no": "配额编号", "worker_name": "姓名",
        "contract_no": "合同编号", "visa_no": "签证号", "hkid": "HKID",
        "entry_date": "入境日期", "contract_end_date": "合同结束日期",
        "employment_status": "状态", "note": "备注",
    },
}

PERSON_COLUMN_ALIASES = {
    "name": ("劳工姓名", "姓名", "人员姓名"),
    "company_name": ("公司名称", "公司名"),
    "worker_type": ("人员类型",), "entry_permit_no": ("入境签证号码", "入境编号"),
    "birth_date": ("出生日期",), "birth_year_month": ("出生年月",),
    "mainland_id_number": ("身份证号码",),
    "mainland_id_first4": ("身份证前四位",), "mainland_id_last4": ("身份证后四位",),
    "hkmo_permit_first4": ("通行证前四位", "港澳通行证前四位"),
    "hkmo_permit_last6": ("通行证后六位", "港澳通行证后六位"),
    "hk_submission_date": ("HK入表日期",), "visa_status_date": ("出VISA情况日期",),
    "visa_status": ("VISA状态",), "remarks": ("备注",), "gender": ("性别",),
    "introducer": ("介绍人",), "permit_last4": ("港澳通行证后四位",),
}


def _pandas():
    try:
        import pandas as pd
    except ImportError as error:
        raise RuntimeError("缺少 pandas/openpyxl，请通过生产启动脚本安装依赖。") from error
    return pd


class ExcelImportService:
    def __init__(self, database, repository, log_repository):
        self.database = database
        self.repository = repository
        self.log_repository = log_repository

    @staticmethod
    def _text(value):
        pd = _pandas()
        if value is None or pd.isna(value):
            return None
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return str(value).strip() or None

    @staticmethod
    def _date(value):
        pd = _pandas()
        if value is None or pd.isna(value):
            return None
        return pd.to_datetime(value, errors="raise").date().isoformat()

    @staticmethod
    def _last4(value):
        text = ExcelImportService._text(value)
        if text is None:
            return None
        if text.isdigit() and len(text) < 4:
            text = text.zfill(4)
        if not text.isdigit() or len(text) != 4:
            raise ValueError("证件后四位必须为4位数字")
        return text

    @staticmethod
    def _digits(value, length, label):
        text = ExcelImportService._text(value)
        if text is None:
            return None
        if not text.isdigit() or len(text) != length:
            raise ValueError(f"{label}必须为{length}位数字")
        return text

    @staticmethod
    def _characters(value, length, label):
        text = ExcelImportService._text(value)
        if text is None:
            return None
        if len(text) != length:
            raise ValueError(f"{label}必须为{length}位字符")
        return text.upper()

    def _frame(self, kind, uploaded, mapping):
        pd = _pandas()
        validate_excel_upload(uploaded)
        frame = pd.read_excel(uploaded, engine="openpyxl", dtype=object).dropna(how="all")
        validate_excel_shape(frame)
        frame.columns = [str(column).strip() for column in frame.columns]
        if kind == "person":
            for key, aliases in PERSON_COLUMN_ALIASES.items():
                if mapping.get(key) not in frame.columns:
                    matched = next((column for column in aliases if column in frame.columns), None)
                    if matched:
                        mapping[key] = matched
        required_keys = {
            "person": {"name"},
            "quota": {"quota_type", "company_name"},
            "contract": {"company_name"},
            "lifecycle": set(DEFAULT_MAPPINGS["lifecycle"]),
        }[kind]
        required_missing = [
            mapping[key] for key in required_keys if mapping.get(key) not in frame.columns
        ]
        if required_missing:
            raise ValueError("缺少字段：" + "、".join(required_missing))
        return frame

    def import_file(self, kind, uploaded, user_id, mapping=None):
        if kind not in DEFAULT_MAPPINGS:
            raise ValueError("不支持的导入类型")
        columns = dict(DEFAULT_MAPPINGS[kind])
        if mapping:
            columns.update({key: value for key, value in mapping.items() if key in columns and value})
        frame = self._frame(kind, uploaded, columns)
        result = {"success": 0, "skipped": 0, "failed": 0, "errors": []}
        if kind == "lifecycle":
            result.update(initial=0, replacement=0, renewal=0, resignation=0, replacement_count=0)
        with self.database.transaction() as connection:
            for index, row in frame.iterrows():
                line = int(index) + 2
                connection.execute("SAVEPOINT production_import_row")
                try:
                    outcome = getattr(self, f"_import_{kind}")(connection, row, columns)
                    if isinstance(outcome, dict):
                        result[outcome["outcome"]] += 1
                        classification = outcome.get("classification")
                        if classification:
                            result[classification] += 1
                            if classification == "replacement":
                                result["replacement_count"] += 1
                    else:
                        result[outcome] += 1
                    connection.execute("RELEASE SAVEPOINT production_import_row")
                except Exception as error:
                    connection.execute("ROLLBACK TO SAVEPOINT production_import_row")
                    connection.execute("RELEASE SAVEPOINT production_import_row")
                    result["failed"] += 1
                    result["errors"].append({"row": line, "message": str(error)})
        result["mapping"] = columns
        log_id = self.log_repository.create(kind, uploaded.filename, user_id, result)
        result["log_id"] = log_id
        return result

    def _value(self, row, columns, key):
        column = columns.get(key)
        return row.get(column) if column in row else None

    def _import_person(self, connection, row, columns):
        name = self._text(self._value(row, columns, "name"))
        gender = self._text(self._value(row, columns, "gender"))
        if not name:
            raise ValueError("缺少劳工姓名")
        if gender not in {None, "男", "女"}:
            raise ValueError("性别如填写必须为男或女")
        company = self._text(self._value(row, columns, "company_name"))
        introducer = self._text(self._value(row, columns, "introducer"))
        worker_type_raw = self._text(self._value(row, columns, "worker_type")) or "new"
        worker_type = {"新人": "new", "续约": "renewal", "new": "new", "renewal": "renewal"}.get(worker_type_raw)
        if not worker_type:
            raise ValueError("人员类型必须为新人或续约")
        mainland_number = self._text(self._value(row, columns, "mainland_id_number"))
        mainland_number = mainland_number.replace(" ", "").upper() if mainland_number else None
        mainland_first4 = mainland_number[:4] if mainland_number and len(mainland_number) >= 4 else None
        mainland_last4 = mainland_number[-4:] if mainland_number and len(mainland_number) >= 4 else None
        if not mainland_number:
            mainland_first4 = self._digits(self._value(row, columns, "mainland_id_first4"), 4, "身份证前四位")
            mainland_last4 = self._digits(self._value(row, columns, "mainland_id_last4"), 4, "身份证后四位")
        permit_first4 = self._characters(self._value(row, columns, "hkmo_permit_first4"), 4, "通行证前四位")
        permit_last6 = self._digits(self._value(row, columns, "hkmo_permit_last6"), 6, "通行证后六位")
        legacy_permit_last4 = permit_last6[-4:] if permit_last6 else self._last4(self._value(row, columns, "permit_last4"))
        birth_date = self._date(self._value(row, columns, "birth_date"))
        birth_year_month = birth_date[:7] if birth_date else self._text(
            self._value(row, columns, "birth_year_month")
        )
        if birth_year_month and len(birth_year_month) >= 7:
            birth_year_month = birth_year_month[:7]
        visa_status_raw = self._text(self._value(row, columns, "visa_status"))
        visa_status = {
            None: None, "未出": "未出", "未提交": "未出", "处理中": "未出",
            "待缴费": "待缴费", "待补资料": "待缴费", "已出": "已出",
        }.get(visa_status_raw)
        if visa_status_raw is not None and visa_status is None:
            raise ValueError("VISA状态只能是：未出、待缴费、已出")
        if self.repository.person_duplicate(
            connection, name, gender, company, mainland_last4, legacy_permit_last4
        ):
            return "skipped"
        cursor = connection.execute(
            """INSERT INTO people
               (name,person_name,gender,company_name,introducer,id_last4,permit_last4,
                worker_type,birth_date,birth_year_month,mainland_id_first4,
                mainland_id_last4,hkmo_permit_first4,hkmo_permit_last6,
                entry_permit_no,hk_submission_date,visa_status_date,visa_status,remarks)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (name, name, gender, company, introducer, mainland_last4, legacy_permit_last4,
             worker_type, birth_date, birth_year_month, mainland_first4, mainland_last4,
             permit_first4, permit_last6,
             self._text(self._value(row, columns, "entry_permit_no")),
             self._date(self._value(row, columns, "hk_submission_date")),
             self._date(self._value(row, columns, "visa_status_date")),
             visa_status,
             self._text(self._value(row, columns, "remarks"))),
        )
        connection.execute(
            """INSERT INTO events(person_id,event_type,note,created_at)
               VALUES (?,'登记','生产版Excel导入',datetime('now','localtime'))""",
            (cursor.lastrowid,),
        )
        return "success"

    def _import_quota(self, connection, row, columns):
        quota_type = self._text(self._value(row, columns, "quota_type"))
        company = self._text(self._value(row, columns, "company_name"))
        if quota_type not in {"SWD", "LD"} or not company:
            raise ValueError("配额类型和公司名必填")
        approval = self._text(self._value(row, columns, "approval_no"))
        quota_no = self._text(self._value(row, columns, "quota_no"))
        user_name = self._text(self._value(row, columns, "user_name"))
        start = self._date(self._value(row, columns, "start_date"))
        end = self._date(self._value(row, columns, "end_date"))
        if self.repository.quota_duplicate(connection, quota_type, company, approval, quota_no):
            return "skipped"
        user_id = None
        if user_name:
            matches = self.repository.person_matches(connection, user_name)
            if len(matches) != 1:
                raise ValueError("使用人不存在或存在重名")
            user_id = matches[0]["id"]
        quota_id = self.repository.insert_quota(
            connection,
            (quota_no, company, quota_type, approval, quota_no, user_id, start, end),
        )
        if user_id and start:
            self.repository.insert_quota_usage(connection, quota_id, user_id, start)
        return "success"

    def _import_contract(self, connection, row, columns):
        contract_no = self._text(self._value(row, columns, "contract_no"))
        company = self._text(self._value(row, columns, "company_name"))
        if not company:
            raise ValueError("公司名必填")
        if self.repository.contract_duplicate(connection, contract_no):
            return "skipped"
        person_name = self._text(self._value(row, columns, "person_name"))
        quota_no = self._text(self._value(row, columns, "quota_no"))
        person_id = quota_id = None
        if person_name:
            matches = self.repository.person_matches(connection, person_name)
            if len(matches) != 1:
                raise ValueError("人员不存在或存在重名")
            person_id = matches[0]["id"]
        if quota_no:
            quota = self.repository.quota_id_by_number(connection, quota_no)
            if not quota:
                raise ValueError("配额序号不存在")
            quota_id = quota["id"]
        status = normalize_contract_status(
            self._text(self._value(row, columns, "status")) or "制作合同"
        )
        if status not in CONTRACT_LIFECYCLE:
            raise ValueError("合同状态无效")
        self.repository.insert_contract(
            connection,
            (
                contract_no, company, person_id, quota_id, status,
            ),
        )
        return "success"

    def _resolve_lifecycle_quota(self, connection, quota_no, approval_no, facility):
        if quota_no:
            rows = connection.execute(
                """SELECT * FROM quotas WHERE is_deleted=0
                   AND (quota_number=? OR quota_serial=?)""",
                (quota_no, quota_no),
            ).fetchall()
        elif approval_no and facility:
            rows = connection.execute(
                """SELECT * FROM quotas WHERE is_deleted=0
                   AND approval_number=? AND company_name=?""",
                (approval_no, facility),
            ).fetchall()
        else:
            raise ValueError("配额编号必填；或同时提供批文号和院舍")
        if len(rows) != 1:
            raise ValueError("配额不存在或匹配到多条配额")
        return rows[0]

    def _resolve_lifecycle_worker(self, connection, name, hkid, visa_no):
        if not name:
            raise ValueError("姓名不能为空")
        rows = []
        if hkid:
            rows = connection.execute(
                "SELECT * FROM people WHERE hkid=? AND is_deleted=0", (hkid,)
            ).fetchall()
        if not rows:
            rows = connection.execute(
                "SELECT * FROM people WHERE name=? AND is_deleted=0", (name,)
            ).fetchall()
        if len(rows) > 1:
            raise ValueError("人员姓名或HKID匹配到多条记录")
        if rows:
            worker_id = rows[0]["id"]
            connection.execute(
                """UPDATE people SET visa_number=COALESCE(?,visa_number),
                          hkid=COALESCE(?,hkid),
                          id_last4=COALESCE(id_last4,?) WHERE id=?""",
                (visa_no, hkid, hkid[-4:] if hkid and len(hkid) >= 4 else None, worker_id),
            )
            return worker_id, False
        cursor = connection.execute(
            """INSERT INTO people(name,gender,id_last4,visa_number,hkid)
               VALUES (?,NULL,?,?,?)""",
            (name, hkid[-4:] if hkid and len(hkid) >= 4 else None, visa_no, hkid),
        )
        return cursor.lastrowid, True

    def _append_history(
        self, connection, quota, worker_id, contract_no, start_date, end_date,
        status, event_type, replacement_round, note,
    ):
        connection.execute(
            """INSERT INTO quota_worker_history
               (quota_id,worker_id,contract_id,start_date,end_date,status,
                event_type,replacement_round,source_type,source_note)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                quota["id"], worker_id, contract_no, start_date, end_date,
                status, event_type, replacement_round, quota["quota_type"], note,
            ),
        )

    def _import_lifecycle(self, connection, row, columns):
        approval_no = self._text(self._value(row, columns, "approval_no"))
        facility = self._text(self._value(row, columns, "facility"))
        quota_no = self._text(self._value(row, columns, "quota_no"))
        worker_name = self._text(self._value(row, columns, "worker_name"))
        contract_no = self._text(self._value(row, columns, "contract_no"))
        visa_no = self._text(self._value(row, columns, "visa_no"))
        hkid = self._text(self._value(row, columns, "hkid"))
        entry_date = self._date(self._value(row, columns, "entry_date"))
        contract_end = self._date(self._value(row, columns, "contract_end_date"))
        employment_status = self._text(self._value(row, columns, "employment_status"))
        note = self._text(self._value(row, columns, "note")) or ""
        if employment_status not in {"在职", "离职"}:
            raise ValueError("状态必须为在职或离职")
        quota = self._resolve_lifecycle_quota(connection, quota_no, approval_no, facility)
        if quota["status"] in {"invalid", "exhausted"}:
            raise ValueError("配额已失效或耗尽")
        worker_id, _created = self._resolve_lifecycle_worker(
            connection, worker_name, hkid, visa_no
        )

        history = connection.execute(
            """SELECT * FROM quota_worker_history
               WHERE quota_id=? ORDER BY id""",
            (quota["id"],),
        ).fetchall()
        if not history and quota["person_id"]:
            self._append_history(
                connection, quota, quota["person_id"], None, quota["start_date"],
                None, "active", "initial", 0, "由既有配额数据建立初始链",
            )
            history = connection.execute(
                "SELECT * FROM quota_worker_history WHERE quota_id=? ORDER BY id",
                (quota["id"],),
            ).fetchall()
        current = None
        for item in history:
            if item["event_type"] in {"initial", "replacement", "renewal"}:
                current = item
            elif item["event_type"] == "resignation" and current:
                if item["worker_id"] == current["worker_id"]:
                    current = None
        max_round = max(
            [int(item["replacement_round"] or 0) for item in history]
            + [int(quota["replacement_count"] or 0)]
        )

        if employment_status == "离职":
            departure_date = contract_end or entry_date
            if not departure_date:
                raise ValueError("离职记录必须提供合同结束日期或入境日期作为离职日期")
            duplicate = connection.execute(
                """SELECT 1 FROM quota_worker_history
                   WHERE quota_id=? AND worker_id=? AND event_type='resignation'
                     AND end_date=? LIMIT 1""",
                (quota["id"], worker_id, departure_date),
            ).fetchone()
            if duplicate:
                return {"outcome": "skipped"}
            if not current or current["worker_id"] != worker_id:
                raise ValueError("该人员不是此配额的当前使用人，无法登记离职")
            self._append_history(
                connection, quota, worker_id, current["contract_id"],
                current["start_date"], departure_date, "closed", "resignation",
                current["replacement_round"], note,
            )
            connection.execute(
                """UPDATE quota_usages SET end_date=COALESCE(end_date,?)
                   WHERE quota_id=? AND person_id=? AND end_date IS NULL""",
                (departure_date, quota["id"], worker_id),
            )
            connection.execute(
                """UPDATE quotas SET person_id=NULL,status='active'
                   WHERE id=? AND person_id=?""",
                (quota["id"], worker_id),
            )
            return {"outcome": "success", "classification": "resignation"}

        if not entry_date:
            raise ValueError("在职记录必须提供入境日期")
        if not contract_no:
            raise ValueError("在职记录必须提供合同编号")
        exact = connection.execute(
            """SELECT 1 FROM quota_worker_history
               WHERE quota_id=? AND worker_id=? AND contract_id=?
                 AND start_date=? AND event_type IN ('initial','replacement','renewal')
               LIMIT 1""",
            (quota["id"], worker_id, contract_no, entry_date),
        ).fetchone()
        if exact:
            return {"outcome": "skipped"}

        if not history:
            classification, replacement_round = "initial", 0
        elif any(
            item["worker_id"] == worker_id
            and item["event_type"] in {"initial", "replacement", "renewal"}
            for item in history
        ):
            if "替补" in note:
                raise ValueError("备注为替补，但配额当前使用人未变化")
            classification, replacement_round = "renewal", int(current["replacement_round"])
        else:
            if "续约" in note:
                raise ValueError("备注为续约，但配额使用人发生变化")
            classification, replacement_round = "replacement", max_round + 1
            maximum = quota_replacement_limit(quota["quota_type"])
            if replacement_round > maximum:
                raise ValueError(
                    f"{quota['quota_type']} 配额替补第{replacement_round}次超过上限{maximum}次"
                )

        if current:
            if current["worker_id"] != worker_id:
                self._append_history(
                    connection, quota, current["worker_id"], current["contract_id"],
                    current["start_date"], entry_date, "closed", "resignation",
                    current["replacement_round"], "导入时根据新使用人自动关闭",
                )
            connection.execute(
                """UPDATE quota_usages SET end_date=COALESCE(end_date,?)
                   WHERE quota_id=? AND end_date IS NULL""",
                (entry_date, quota["id"]),
            )

        existing_contract = connection.execute(
            "SELECT * FROM contracts WHERE contract_id=? AND is_deleted=0",
            (contract_no,),
        ).fetchone()
        if existing_contract and (
            existing_contract["person_id"] != worker_id
            or existing_contract["quota_id"] != quota["id"]
        ):
            raise ValueError("合同编号已绑定其他人员或配额")
        if not contract_end:
            contract_end = shift_months(
                datetime.strptime(entry_date, "%Y-%m-%d").date(), 24
            ).isoformat()
        if not existing_contract:
            connection.execute(
                """INSERT INTO contracts
                   (contract_id,person_name,company,entry_date,arrival_date,
                    contract_start_date,contract_end_date,start_date,end_date,
                    status,person_id,quota_id,cycle_index)
                   VALUES (?,?,?,?,?,?,?,?,?,'工人入境',?,?,?)""",
                (
                    contract_no, worker_name, facility or quota["company_name"],
                    entry_date, entry_date, entry_date, contract_end, entry_date,
                    contract_end, worker_id, quota["id"], replacement_round + 1,
                ),
            )
        self._append_history(
            connection, quota, worker_id, contract_no, entry_date, contract_end,
            "active", classification, replacement_round, note,
        )
        connection.execute(
            """INSERT INTO quota_usages(quota_id,person_id,start_date)
               VALUES (?,?,?)""",
            (quota["id"], worker_id, entry_date),
        )
        connection.execute(
            """UPDATE quotas SET person_id=?,status='in_use',
                      usage_count=usage_count+1,
                      replacement_count=MAX(replacement_count,?),
                      max_replacement_count=? WHERE id=?""",
            (
                worker_id, replacement_round,
                quota_replacement_limit(quota["quota_type"]), quota["id"],
            ),
        )
        connection.execute(
            """INSERT INTO events(person_id,event_type,note,event_date,created_at)
               VALUES (?,?,?,?,CURRENT_TIMESTAMP)""",
            (
                worker_id, "工人入境",
                f"Excel自动识别：{classification}；{note}".rstrip("；"), entry_date,
            ),
        )
        return {"outcome": "success", "classification": classification}

    @staticmethod
    def template(kind):
        try:
            from openpyxl import Workbook
        except ImportError as error:
            raise RuntimeError("缺少 openpyxl，请通过生产启动脚本安装依赖。") from error
        if kind not in DEFAULT_MAPPINGS:
            raise ValueError("不支持的模板类型")
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = kind
        headers = list(dict.fromkeys(DEFAULT_MAPPINGS[kind].values()))
        sheet.append(headers)
        sheet.freeze_panes = "A2"
        for cell in sheet[1]:
            cell.font = cell.font.copy(bold=True)
        for index, header in enumerate(headers, 1):
            sheet.column_dimensions[chr(64 + index)].width = max(14, len(header) * 2 + 4)
        output = io.BytesIO()
        workbook.save(output)
        output.seek(0)
        return output


def parse_mapping(raw):
    if not raw:
        return None
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("字段映射必须为 JSON 对象")
    return parsed
