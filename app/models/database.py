import sqlite3
from contextlib import contextmanager
import time

from app.services.person_system_service import sqlite_person_global_key, sqlite_person_global_key_v3
from app.persistence.reliability_schema import initialize_reliability_schema
from app.persistence.safe_sqlite import connect_reliably


class Database:
    def __init__(self, path):
        self.path = str(path)

    def connect(self):
        connection = connect_reliably(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.create_function("person_global_key", 5, sqlite_person_global_key)
        connection.create_function("person_global_key_v3", 3, sqlite_person_global_key_v3)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def transaction(self):
        connection = self.connect()
        try:
            for attempt in range(6):
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError as error:
                    if "locked" not in str(error).casefold() or attempt == 5:
                        raise
                    time.sleep(0.05 * (2 ** attempt))
            yield connection
            for attempt in range(6):
                try:
                    connection.commit()
                    break
                except sqlite3.OperationalError as error:
                    if "locked" not in str(error).casefold() or attempt == 5:
                        raise
                    time.sleep(0.05 * (2 ** attempt))
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self):
        connection = self.connect()
        try:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'viewer'
                        CHECK(role IN ('admin','hr','manager','viewer')),
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS import_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    import_type TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    user_id INTEGER,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    skipped_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    errors_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS idx_import_logs_created
                    ON import_logs(created_at DESC);
                """
            )
            initialize_reliability_schema(connection)
            import_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(import_logs)").fetchall()
            }
            if "result_json" not in import_columns:
                connection.execute(
                    "ALTER TABLE import_logs ADD COLUMN result_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "batch_version" not in import_columns:
                connection.execute("ALTER TABLE import_logs ADD COLUMN batch_version TEXT")
            users_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
            ).fetchone()["sql"]
            if "'hr'" not in users_sql:
                connection.executescript(
                    """
                    CREATE TABLE users_rbac_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username TEXT NOT NULL UNIQUE,
                        password_hash TEXT NOT NULL,
                        role TEXT NOT NULL DEFAULT 'viewer'
                            CHECK(role IN ('admin','hr','manager','viewer')),
                        active INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    INSERT INTO users_rbac_new
                        (id,username,password_hash,role,active,created_at)
                    SELECT id,username,password_hash,
                           CASE role WHEN 'admin' THEN 'admin' ELSE 'viewer' END,
                           active,created_at FROM users;
                    DROP TABLE users;
                    ALTER TABLE users_rbac_new RENAME TO users;
                    """
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT,
                    timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_logs_timestamp
                    ON audit_logs(timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_audit_logs_entity
                    ON audit_logs(entity_type,entity_id);
                """
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.close()
