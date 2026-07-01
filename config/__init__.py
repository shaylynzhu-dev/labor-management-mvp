import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_PATH = os.path.join(str(PROJECT_ROOT), "labor.db")


class BaseConfig:
    DEBUG = False
    SECRET_KEY = os.environ.get("LABOUR_OS_SECRET_KEY", "change-this-in-production")
    DATABASE = Path(
        os.environ.get("LABOUR_OS_DATABASE_PATH", DEFAULT_DATABASE_PATH)
    ).expanduser().resolve()
    UPLOAD_FOLDER = PROJECT_ROOT / "uploads"
    MAX_CONTENT_LENGTH = 1300 * 1024 * 1024
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = 8 * 60 * 60
    LOG_DIR = (PROJECT_ROOT / "logs").resolve()


class DevelopmentConfig(BaseConfig):
    DEBUG = False
    SESSION_COOKIE_SECURE = False


class ProductionConfig(BaseConfig):
    DEBUG = False
    SESSION_COOKIE_SECURE = os.environ.get("LABOUR_OS_HTTPS", "0") == "1"
    PREFERRED_URL_SCHEME = "https" if SESSION_COOKIE_SECURE else "http"


def get_config():
    return ProductionConfig if os.environ.get("LABOUR_OS_ENV", "production") == "production" else DevelopmentConfig
