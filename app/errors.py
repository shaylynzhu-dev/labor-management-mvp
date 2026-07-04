from dataclasses import dataclass, field
from typing import Any


VALIDATION_ERROR = "VALIDATION_ERROR"
NOT_FOUND = "NOT_FOUND"
DB_ERROR = "DB_ERROR"
EXTERNAL_ERROR = "EXTERNAL_ERROR"
UNKNOWN_ERROR = "UNKNOWN_ERROR"


@dataclass(frozen=True)
class AppError:
    code: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)

    def as_dict(self):
        return {"code": self.code, "message": self.message, "context": self.context}


def service_result(data=None, error=None):
    return {
        "success": error is None,
        "data": data if data is not None else {},
        "error": error.as_dict() if isinstance(error, AppError) else error,
    }
