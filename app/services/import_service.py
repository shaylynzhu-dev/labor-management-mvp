import io
import json
from datetime import date, datetime

from app.utils.excel import validate_excel_shape, validate_excel_upload
from app.services.dispatch_engine import CONTRACT_LIFECYCLE, normalize_contract_status


DEFAULT_MAPPINGS = {
    "person": {
        "name": "姓名", "gender": "性别", "company_name": "公司名",
        "introducer": "介绍人", "id_last4": "身份证后四位",
        "permit_last4": "港澳通行证后四位",
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

    def _frame(self, kind, uploaded, mapping):
        pd = _pandas()
        validate_excel_upload(uploaded)
        frame = pd.read_excel(uploaded, engine="openpyxl", dtype=object).dropna(how="all")
        validate_excel_shape(frame)
        frame.columns = [str(column).strip() for column in frame.columns]
        required_keys = {
            "person": {"name", "gender"},
            "quota": {"quota_type", "company_name"},
            "contract": {"company_name"},
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
        with self.database.transaction() as connection:
            for index, row in frame.iterrows():
                line = int(index) + 2
                connection.execute("SAVEPOINT production_import_row")
                try:
                    outcome = getattr(self, f"_import_{kind}")(connection, row, columns)
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
        if not name or gender not in {"男", "女"}:
            raise ValueError("姓名必填，性别必须为男或女")
        company = self._text(self._value(row, columns, "company_name"))
        introducer = self._text(self._value(row, columns, "introducer"))
        id_last4 = self._last4(self._value(row, columns, "id_last4"))
        permit_last4 = self._last4(self._value(row, columns, "permit_last4"))
        if self.repository.person_duplicate(connection, name, gender, company, id_last4, permit_last4):
            return "skipped"
        self.repository.insert_person(
            connection, (name, gender, company, introducer, id_last4, permit_last4)
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
