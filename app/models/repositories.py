import json


class UserRepository:
    def __init__(self, database):
        self.database = database

    def find_by_username(self, username):
        with self.database.connect() as connection:
            return connection.execute(
                "SELECT * FROM users WHERE username=?", (username,)
            ).fetchone()

    def create(self, username, password_hash, role="viewer"):
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO users(username,password_hash,role) VALUES (?,?,?)",
                (username, password_hash, role),
            )
            return cursor.lastrowid

    def update_password(self, user_id, password_hash):
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE users SET password_hash=?, active=1 WHERE id=?",
                (password_hash, user_id),
            )


class ImportLogRepository:
    def __init__(self, database):
        self.database = database

    def create(self, import_type, filename, user_id, result):
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """INSERT INTO import_logs
                   (import_type,filename,user_id,success_count,skipped_count,
                    failed_count,errors_json,result_json) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    import_type, filename, user_id, result["success"],
                    result["skipped"], result["failed"],
                    json.dumps(result["errors"], ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False),
                ),
            )
            return cursor.lastrowid

    def list_recent(self, limit=100):
        with self.database.connect() as connection:
            return connection.execute(
                """SELECT l.*,u.username FROM import_logs l
                   LEFT JOIN users u ON u.id=l.user_id
                   ORDER BY l.id DESC LIMIT ?""",
                (int(limit),),
            ).fetchall()


class LegacyBusinessRepository:
    def __init__(self, database):
        self.database = database

    def person_matches(self, connection, name):
        return connection.execute(
            "SELECT id FROM people WHERE name=? AND is_deleted=0 ORDER BY id LIMIT 2", (name,)
        ).fetchall()

    def person_duplicate(self, connection, name, gender, company, id_last4, permit_last4):
        if id_last4:
            sql, values = "SELECT 1 FROM people WHERE name=? AND id_last4=? AND is_deleted=0", (name, id_last4)
        elif permit_last4:
            sql, values = "SELECT 1 FROM people WHERE name=? AND permit_last4=? AND is_deleted=0", (name, permit_last4)
        else:
            sql = """SELECT 1 FROM people WHERE name=? AND COALESCE(gender,'')=COALESCE(?,'') AND is_deleted=0
                     AND COALESCE(company_name,'')=COALESCE(?,'')"""
            values = (name, gender, company)
        return connection.execute(sql + " LIMIT 1", values).fetchone() is not None

    def insert_person(self, connection, values):
        cursor = connection.execute(
            """INSERT INTO people
               (name,gender,company_name,introducer,id_last4,permit_last4)
               VALUES (?,?,?,?,?,?)""",
            values,
        )
        connection.execute(
            """INSERT INTO events(person_id,event_type,note,created_at)
               VALUES (?,'登记','生产版Excel导入',datetime('now','localtime'))""",
            (cursor.lastrowid,),
        )
        return cursor.lastrowid

    def quota_duplicate(self, connection, quota_type, company, approval_no, quota_no):
        if quota_no:
            row = connection.execute(
                "SELECT 1 FROM quotas WHERE is_deleted=0 AND (quota_serial=? OR quota_number=?) LIMIT 1",
                (quota_no, quota_no),
            ).fetchone()
        elif approval_no:
            row = connection.execute(
                """SELECT 1 FROM quotas WHERE is_deleted=0 AND approval_number=?
                   AND quota_type=? AND company_name=? LIMIT 1""",
                (approval_no, quota_type, company),
            ).fetchone()
        else:
            row = None
        return row is not None

    def insert_quota(self, connection, values):
        cursor = connection.execute(
            """INSERT INTO quotas
               (quota_number,company_name,quota_type,approval_number,
                quota_serial,person_id,start_date,expiry_date)
               VALUES (?,?,?,?,?,?,?,?)""",
            values,
        )
        connection.execute(
            """UPDATE quotas SET max_replacement_count=CASE quota_type
                      WHEN 'LD' THEN 2 ELSE 1 END,
                      status=CASE WHEN person_id IS NULL THEN 'active' ELSE 'in_use' END
               WHERE id=?""",
            (cursor.lastrowid,),
        )
        return cursor.lastrowid

    def insert_quota_usage(self, connection, quota_id, person_id, start_date):
        connection.execute(
            "INSERT INTO quota_usages(quota_id,person_id,start_date) VALUES (?,?,?)",
            (quota_id, person_id, start_date),
        )
        connection.execute(
            "UPDATE quotas SET usage_count=usage_count+1,status='in_use' WHERE id=?",
            (quota_id,),
        )

    def quota_id_by_number(self, connection, quota_no):
        return connection.execute(
            "SELECT id FROM quota WHERE quota_no=? AND is_deleted=0 LIMIT 1", (quota_no,)
        ).fetchone()

    def contract_duplicate(self, connection, contract_no):
        if not contract_no:
            return False
        return connection.execute(
            "SELECT 1 FROM contract WHERE contract_no=? AND is_deleted=0 LIMIT 1", (contract_no,)
        ).fetchone() is not None

    def insert_contract(self, connection, values):
        (
            contract_no, company, person_id, quota_id, status,
        ) = values
        person = connection.execute(
            "SELECT name FROM people WHERE id=? AND is_deleted=0", (person_id,)
        ).fetchone() if person_id else None
        person_name = person["name"] if person else "待关联人员"
        cursor = connection.execute(
            """INSERT INTO contracts
               (contract_id,person_name,company,status,person_id,quota_id,cycle_index)
               VALUES (?,?,?,?,?,?,1)""",
            (contract_no, person_name, company, status, person_id, quota_id),
        )
        return cursor.lastrowid
