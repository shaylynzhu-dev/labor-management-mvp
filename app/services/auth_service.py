import os

from werkzeug.security import check_password_hash, generate_password_hash

PASSWORD_HASH_METHOD = "pbkdf2:sha256:600000"


def hash_password(password):
    return generate_password_hash(password, method=PASSWORD_HASH_METHOD)


class AuthService:
    def __init__(self, repository):
        self.repository = repository

    def ensure_admin(self):
        username = os.environ.get("LABOUR_OS_ADMIN_USERNAME", "admin")
        password = os.environ.get("LABOUR_OS_ADMIN_PASSWORD", "admin123")
        existing = self.repository.find_by_username(username)
        if not existing:
            self.repository.create(username, hash_password(password), "admin")
        elif os.environ.get("LABOUR_OS_ADMIN_PASSWORD"):
            self.repository.update_password(existing["id"], hash_password(password))

    def authenticate(self, username, password):
        user = self.repository.find_by_username(username)
        if not user or not user["active"] or not check_password_hash(user["password_hash"], password):
            return None
        return dict(user)

    def create_user(self, username, password, role="viewer"):
        username = (username or "").strip()
        if not username or len(password or "") < 8:
            raise ValueError("用户名必填，密码至少8位")
        if role not in {"admin", "hr", "manager", "viewer"}:
            raise ValueError("角色必须为 admin、hr、manager 或 viewer")
        if self.repository.find_by_username(username):
            raise ValueError("用户名已存在")
        return self.repository.create(username, hash_password(password), role)
