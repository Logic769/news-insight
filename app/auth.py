"""Local account authentication for news-insight.

No third-party crypto dependency: passwords use stdlib PBKDF2-HMAC-SHA256 with a
per-user random salt, and browser sessions are opaque random tokens whose SHA-256
hash is the only thing persisted. A leaked database therefore cannot be used to
log in without also guessing the 256-bit token.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from app import accounts, db
from app.settings import Settings

PBKDF2_ITERATIONS = 120_000
SALT_BYTES = 16
TOKEN_BYTES = 32


def hash_password(
    password: str, *, salt: str | None = None, iterations: int = PBKDF2_ITERATIONS
) -> tuple[str, str, int]:
    """Return (hex digest, hex salt, iterations)."""
    salt_hex = salt or os.urandom(SALT_BYTES).hex()
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), iterations
    )
    return digest.hex(), salt_hex, iterations


def verify_password(password: str, *, salt: str, expected_hash: str, iterations: int) -> bool:
    actual, _, _ = hash_password(password, salt=salt, iterations=iterations)
    return hmac.compare_digest(actual, expected_hash)


def password_policy_error(password: str) -> str | None:
    """规范书要求：新密码至少 8 位，且包含大小写字母、数字、特殊字符。"""
    if len(password) < 8:
        return "密码长度不能少于 8 位"
    if not any(ch.islower() for ch in password):
        return "密码必须包含小写字母"
    if not any(ch.isupper() for ch in password):
        return "密码必须包含大写字母"
    if not any(ch.isdigit() for ch in password):
        return "密码必须包含数字"
    if not any(not ch.isalnum() for ch in password):
        return "密码必须包含特殊字符"
    return None


def create_user(
    conn,
    *,
    username: str,
    password: str,
    display_name: str = "",
    department: str = "",
    role: str = "user",
    must_change_password: bool = False,
) -> dict[str, Any]:
    digest, salt, iterations = hash_password(password)
    return accounts.create_user(
        conn,
        username=username,
        display_name=display_name,
        department=department,
        role=role,
        password_hash=digest,
        password_salt=salt,
        password_iterations=iterations,
        must_change_password=must_change_password,
    )


def bootstrap_admin(settings: Settings) -> dict[str, Any] | None:
    """Create the initial admin only when the users table is empty.

    The credential comes exclusively from environment variables. If the password
    is absent we leave the database untouched and log a warning, so an existing
    deployment never gets a surprise account.
    """
    if not settings.admin_password:
        return None
    conn = db.connect(settings.db_path)
    try:
        if accounts.count_users(conn) > 0:
            return None
        user = create_user(
            conn,
            username=settings.admin_username,
            password=settings.admin_password,
            display_name="系统管理员",
            department="信息中心",
            role="admin",
            must_change_password=True,
        )
        return user
    finally:
        conn.close()


def issue_session(settings: Settings, conn, user_id: int) -> tuple[str, str]:
    token = os.urandom(TOKEN_BYTES).hex()
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=settings.session_ttl_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    accounts.create_session(conn, token_hash=token_hash, user_id=user_id, expires_at=expires_at)
    return token, expires_at


def token_from_headers(headers: Any) -> str:
    raw = headers.get("authorization") or ""
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return ""


def resolve_session(settings: Settings, token: str) -> dict[str, Any] | None:
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    conn = db.connect(settings.db_path)
    try:
        return accounts.get_session_user(conn, token_hash)
    finally:
        conn.close()


def revoke_token(settings: Settings, token: str) -> None:
    if not token:
        return
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    conn = db.connect(settings.db_path)
    try:
        accounts.revoke_session(conn, token_hash)
    finally:
        conn.close()


def generate_temp_password() -> str:
    """A one-time password that satisfies the same policy as user-chosen ones."""
    return f"Tmp@{os.urandom(9).hex()}"
