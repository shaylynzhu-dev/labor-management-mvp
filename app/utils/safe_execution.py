import logging
import sqlite3
import uuid

from app.errors import AppError, DB_ERROR, UNKNOWN_ERROR, service_result


LOGGER = logging.getLogger("labour_os.error")


def safe_execute(fn, fallback=None, *, context=None, logger=None):
    """Execute a service boundary and always return a UI-safe structured result."""
    try:
        value = fn()
        if isinstance(value, dict) and {"success", "data", "error"}.issubset(value):
            return value
        return service_result(value)
    except sqlite3.Error as exc:
        (logger or LOGGER).exception("safe_execute captured database failure context=%s", context or {})
        error = AppError(DB_ERROR, "数据暂时繁忙，请稍后重试", {
            "trace_id": str(uuid.uuid4()), **(context or {}), "exception": type(exc).__name__,
        })
    except Exception as exc:  # A service exception never crosses the boundary.
        (logger or LOGGER).exception("safe_execute captured failure context=%s", context or {})
        error = AppError(UNKNOWN_ERROR, "系统暂时无法完成操作", {
            "trace_id": str(uuid.uuid4()), **(context or {}), "exception": type(exc).__name__,
        })
    if callable(fallback):
        try:
            fallback_value = fallback(error)
            if isinstance(fallback_value, dict) and {"success", "data", "error"}.issubset(fallback_value):
                return fallback_value
        except Exception:
            (logger or LOGGER).exception("safe_execute fallback failed")
    return service_result(fallback if fallback is not None and not callable(fallback) else {}, error)
