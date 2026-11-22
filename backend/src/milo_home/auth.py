import hashlib
import secrets
import sqlite3
import time
from collections.abc import Callable
from threading import BoundedSemaphore
from uuid import uuid4

from pwdlib import PasswordHash

from .config import Settings
from .database import Database
from .errors import ApiError


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class AuthService:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        clock: Callable[[], float] = time.time,
    ):
        self.database = database
        self.settings = settings
        self.clock = clock
        self.passwords = PasswordHash.recommended()
        self.dummy_hash = self.passwords.hash(secrets.token_urlsafe(32))
        self.hash_slots = BoundedSemaphore(2)

    def limit_attempt(self, client: str, action: str):
        now = int(self.clock())
        key = digest(f"{action}:{client}")
        with self.database.connect(write=True) as connection:
            connection.execute(
                "DELETE FROM auth_buckets WHERE window_start < ?",
                (now - self.settings.auth_window_seconds,),
            )
            bucket = connection.execute(
                "SELECT attempts FROM auth_buckets WHERE key_hash = ?", (key,)
            ).fetchone()
            if bucket and bucket["attempts"] >= self.settings.auth_attempts:
                raise ApiError(429, "auth_rate_limited", "操作太频繁，请稍后再试。")
            connection.execute(
                "INSERT INTO auth_buckets VALUES (?, ?, 1) "
                "ON CONFLICT(key_hash) DO UPDATE SET attempts = attempts + 1",
                (key, now),
            )

    def create_invitation(
        self, handle: str, ttl_seconds: int = 48 * 3600, *, token: str | None = None
    ) -> str:
        token = token or secrets.token_urlsafe(32)
        now = int(self.clock())
        with self.database.connect(write=True) as connection:
            if connection.execute("SELECT 1 FROM users WHERE handle = ?", (handle,)).fetchone():
                raise ApiError(409, "account_exists", "账号已经存在。")
            existing = connection.execute(
                "SELECT expires_at, consumed_at FROM invitations WHERE handle = ?", (handle,)
            ).fetchone()
            if existing and existing["consumed_at"] is None and existing["expires_at"] > now:
                raise ApiError(409, "invitation_exists", "该账号已有有效邀请。")
            connection.execute("DELETE FROM invitations WHERE handle = ?", (handle,))
            connection.execute(
                "INSERT INTO invitations VALUES (?, ?, ?, NULL)",
                (digest(token), handle, now + ttl_seconds),
            )
        return token

    def activate(self, token: str, password: str, display_name: str) -> tuple[dict, str]:
        now = int(self.clock())
        with self.database.connect() as connection:
            invitation = connection.execute(
                "SELECT * FROM invitations WHERE token_hash = ?", (digest(token),)
            ).fetchone()
        if (
            not invitation
            or invitation["consumed_at"] is not None
            or invitation["expires_at"] <= now
        ):
            raise ApiError(401, "invitation_invalid", "邀请无效或已过期。")
        password_hash = self._hash(password)
        user = {
            "id": str(uuid4()),
            "handle": invitation["handle"],
            "display_name": display_name,
        }
        session = secrets.token_urlsafe(32)
        with self.database.connect(write=True) as connection:
            consumed = connection.execute(
                "UPDATE invitations SET consumed_at = ? WHERE token_hash = ? "
                "AND consumed_at IS NULL AND expires_at > ?",
                (now, digest(token), int(self.clock())),
            )
            if consumed.rowcount != 1:
                raise ApiError(401, "invitation_invalid", "邀请无效或已过期。")
            connection.execute(
                "INSERT INTO users VALUES (?, ?, ?, ?, ?)",
                (user["id"], user["handle"], display_name, password_hash, now),
            )
            connection.execute(
                "INSERT INTO threads VALUES (?, ?, ?)", (str(uuid4()), user["id"], now)
            )
            self._save_session(connection, session, user["id"], now)
        return user, session

    def login(self, handle: str, password: str) -> tuple[dict, str]:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM users WHERE handle = ?", (handle,)).fetchone()
        valid, updated_hash = self._verify(
            password, row["password_hash"] if row else self.dummy_hash
        )
        if not valid or not row:
            raise ApiError(401, "login_invalid", "账号或密码不正确。")
        session = secrets.token_urlsafe(32)
        with self.database.connect(write=True) as connection:
            if updated_hash:
                connection.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?", (updated_hash, row["id"])
                )
            self._save_session(connection, session, row["id"], int(self.clock()))
        return dict(row), session

    def authenticate(self, token: str | None) -> dict:
        if not token or len(token) != 43:
            raise ApiError(401, "login_required", "请先登录。")
        with self.database.connect() as connection:
            user = connection.execute(
                "SELECT users.id, users.handle, users.display_name FROM sessions "
                "JOIN users ON users.id = sessions.user_id "
                "WHERE sessions.token_hash = ? AND sessions.expires_at > ?",
                (digest(token), int(self.clock())),
            ).fetchone()
        if not user:
            raise ApiError(401, "login_required", "请先登录。")
        return dict(user)

    def logout(self, token: str | None):
        if token:
            with self.database.connect(write=True) as connection:
                connection.execute("DELETE FROM sessions WHERE token_hash = ?", (digest(token),))

    def _save_session(self, connection: sqlite3.Connection, token: str, user_id: str, now: int):
        connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?)",
            (digest(token), user_id, now, now + self.settings.session_seconds),
        )

    def _hash(self, password: str) -> str:
        if not self.hash_slots.acquire(blocking=False):
            raise ApiError(429, "auth_busy", "登录服务繁忙，请稍后再试。")
        try:
            return self.passwords.hash(password)
        finally:
            self.hash_slots.release()

    def _verify(self, password: str, encoded: str) -> tuple[bool, str | None]:
        if not self.hash_slots.acquire(blocking=False):
            raise ApiError(429, "auth_busy", "登录服务繁忙，请稍后再试。")
        try:
            return self.passwords.verify_and_update(password, encoded)
        finally:
            self.hash_slots.release()
