from contextlib import contextmanager
import logging
import sqlite3
import time


LOGGER = logging.getLogger("labour_os.persistence")


def _is_locked(error):
    return "locked" in str(error).casefold() or "busy" in str(error).casefold()


class RetryingSQLiteConnection(sqlite3.Connection):
    """Drop-in sqlite connection with bounded lock retry for legacy write paths."""
    retries = 5

    def execute(self, sql, parameters=(), /):
        for attempt in range(self.retries + 1):
            try:
                return super().execute(sql, parameters)
            except sqlite3.OperationalError as error:
                if not _is_locked(error) or attempt >= self.retries:
                    raise
                time.sleep(0.05 * (2 ** attempt))

    def commit(self):
        return safe_commit(self, self.retries)


def connect_reliably(path, timeout=30):
    return sqlite3.connect(path, timeout=timeout, factory=RetryingSQLiteConnection)


def safe_commit(connection, retries=5, base_delay=0.05):
    for attempt in range(retries + 1):
        try:
            sqlite3.Connection.commit(connection)
            return
        except sqlite3.OperationalError as error:
            if not _is_locked(error) or attempt >= retries:
                connection.rollback()
                raise
            time.sleep(base_delay * (2 ** attempt))


class SafeSQLite:
    def __init__(self, path, retries=5):
        self.path = str(path)
        self.retries = retries

    def connect(self):
        connection = connect_reliably(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def transaction(self):
        connection = self.connect()
        try:
            for attempt in range(self.retries + 1):
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError as error:
                    if not _is_locked(error) or attempt >= self.retries:
                        raise
                    time.sleep(0.05 * (2 ** attempt))
            yield connection
            safe_commit(connection, self.retries)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
