"""Accounts, passwords and login sessions.

The app was single-user: one username and password in env vars, checked with
HTTP Basic on every route. This module replaces that with real accounts stored
in Postgres, so several people can use the same deployment without seeing each
other's training.

Three pieces:

  * **Passwords** are stored as PBKDF2-HMAC-SHA256 with a per-user salt. No new
    dependency — `hashlib` ships with Python, and the cost factor is stored
    inside the hash string so it can be raised later without locking anyone out.
  * **Sessions** are opaque random tokens. Only the SHA-256 of a token is
    stored, so a leaked database dump does not hand out live logins. The raw
    token lives in an HttpOnly cookie and nowhere else.
  * **Users** are looked up by a lowercased username, so "Sohaib" and "sohaib"
    are the same account and cannot both be registered.

Nothing here renders HTML or touches FastAPI; app.py owns that. Nothing here
opens a connection either — every function takes one, so the caller decides the
transaction boundary.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

# --------------------------------------------------------------------------
# Tuning
# --------------------------------------------------------------------------


def _int_env(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


# Django's current default is in this range. Verification always uses the count
# stored in the hash, so raising this only affects passwords set from now on;
# existing users keep working and are re-hashed on their next successful login.
PBKDF2_ITERATIONS = _int_env("PBKDF2_ITERATIONS", 600_000)

# How long a login lasts without re-entering the password. Phone users should
# not be asked again every week, and there is nothing high-value behind it.
SESSION_TTL_DAYS = _int_env("SESSION_TTL_DAYS", 30)

SESSION_COOKIE = "gt_session"

USERNAME_MIN = 3
USERNAME_MAX = 32
PASSWORD_MIN = 8
# Not a security bound — a bound on how much text we are willing to run 600k
# rounds of PBKDF2 over, since that work happens before authentication.
PASSWORD_MAX = 200

_USERNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")

# Names that would collide with a route or read as official.
_RESERVED_USERNAMES = frozenset(
    {"admin", "root", "login", "logout", "signup", "account", "healthz",
     "progress", "log", "save", "static", "api", "me", "system"}
)


class AuthError(ValueError):
    """A registration or login input the user can fix, with a message for them."""


@dataclass(frozen=True)
class User:
    user_id: int
    username: str
    display_name: str
    timezone: Optional[str] = None
    is_active: bool = True

    @property
    def label(self) -> str:
        """What to show in the page header."""
        return self.display_name or self.username


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    """`pbkdf2_sha256$<iterations>$<salt>$<hash>`, all base64 without padding.

    The iteration count travels with the hash so `verify_password` never has to
    guess it, which is what makes the cost factor safe to raise later.
    """
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations, _b64(salt), _b64(digest)
    )


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of a password against a stored hash.

    A malformed or unrecognised hash is a failed login, not an exception: a row
    written by some future scheme must never authenticate by accident.
    """
    try:
        scheme, iterations_text, salt_text, digest_text = (stored or "").split("$")
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        salt = _unb64(salt_text)
        expected = _unb64(digest_text)
    except (ValueError, TypeError):
        return False
    if iterations < 1:
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(candidate, expected)


def needs_rehash(stored: str) -> bool:
    """True when a stored hash uses fewer rounds than we now ask for."""
    try:
        scheme, iterations_text, _, _ = (stored or "").split("$")
    except ValueError:
        return True
    return scheme != "pbkdf2_sha256" or int(iterations_text) < PBKDF2_ITERATIONS


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii").rstrip("=")


def _unb64(encoded: str) -> bytes:
    return base64.b64decode(encoded + "=" * (-len(encoded) % 4))


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def normalize_username(raw: str) -> str:
    """Fold a typed username to its stored form, or explain why it cannot be one."""
    candidate = (raw or "").strip().lower()
    if not candidate:
        raise AuthError("Pick a username.")
    if len(candidate) < USERNAME_MIN or len(candidate) > USERNAME_MAX:
        raise AuthError(
            f"Username must be {USERNAME_MIN}-{USERNAME_MAX} characters."
        )
    if not _USERNAME_RE.match(candidate):
        raise AuthError(
            "Username can use letters, numbers, dots, dashes and underscores, "
            "and must start and end with a letter or number."
        )
    if candidate in _RESERVED_USERNAMES:
        raise AuthError("That username is reserved. Pick another.")
    return candidate


def validate_password(password: str) -> str:
    """Check a new password meets the floor. Returns it unchanged."""
    password = password or ""
    if len(password) < PASSWORD_MIN:
        raise AuthError(f"Password must be at least {PASSWORD_MIN} characters.")
    if len(password) > PASSWORD_MAX:
        raise AuthError(f"Password must be at most {PASSWORD_MAX} characters.")
    return password


def validate_timezone(name: Optional[str]) -> Optional[str]:
    """An IANA zone the host can actually load, or None to use the app default."""
    candidate = (name or "").strip()
    if not candidate:
        return None
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise AuthError(f"{candidate!r} is not an IANA timezone name (e.g. Asia/Karachi).")
    return candidate


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------

_USER_COLUMNS = "user_id, username, display_name, timezone, is_active"


def _row_to_user(row) -> User:
    return User(
        user_id=int(row[0]),
        username=row[1],
        display_name=row[2],
        timezone=row[3],
        is_active=bool(row[4]),
    )


def create_user(
    conn: Connection,
    username: str,
    password: str,
    timezone_name: Optional[str] = None,
) -> User:
    """Register an account. Raises AuthError if the name is taken or invalid."""
    display_name = (username or "").strip()
    normalized = normalize_username(display_name)
    validate_password(password)
    zone = validate_timezone(timezone_name)

    try:
        row = conn.execute(
            text(
                f"""
                INSERT INTO users (username, display_name, password_hash, timezone)
                VALUES (:username, :display_name, :password_hash, :timezone)
                RETURNING {_USER_COLUMNS}
                """
            ),
            {
                "username": normalized,
                "display_name": display_name,
                "password_hash": hash_password(password),
                "timezone": zone,
            },
        ).fetchone()
    except IntegrityError as exc:
        # The unique index is the authority, not a prior SELECT: two people can
        # submit the same name in the same instant and only one row can win.
        raise AuthError("That username is already taken.") from exc
    return _row_to_user(row)


def get_user_by_username(conn: Connection, username: str) -> Optional[User]:
    try:
        normalized = normalize_username(username)
    except AuthError:
        return None
    row = conn.execute(
        text(f"SELECT {_USER_COLUMNS} FROM users WHERE username = :username"),
        {"username": normalized},
    ).fetchone()
    return _row_to_user(row) if row else None


def get_user(conn: Connection, user_id: int) -> Optional[User]:
    row = conn.execute(
        text(f"SELECT {_USER_COLUMNS} FROM users WHERE user_id = :user_id"),
        {"user_id": user_id},
    ).fetchone()
    return _row_to_user(row) if row else None


def authenticate(conn: Connection, username: str, password: str) -> Optional[User]:
    """Check a username/password pair. None means "no", with no detail as to why.

    A missing user still costs one PBKDF2 run, so the response time does not
    tell an attacker which usernames exist.
    """
    try:
        normalized = normalize_username(username)
    except AuthError:
        normalized = ""

    row = conn.execute(
        text(
            f"SELECT {_USER_COLUMNS}, password_hash FROM users "
            "WHERE username = :username"
        ),
        {"username": normalized},
    ).fetchone()

    if row is None:
        verify_password(password or "", _dummy_hash())
        return None

    stored = row[5]
    if not verify_password(password or "", stored):
        return None
    if not bool(row[4]):
        return None

    if needs_rehash(stored):
        conn.execute(
            text("UPDATE users SET password_hash = :hash WHERE user_id = :user_id"),
            {"hash": hash_password(password), "user_id": int(row[0])},
        )
    conn.execute(
        text("UPDATE users SET last_login_at = now() WHERE user_id = :user_id"),
        {"user_id": int(row[0])},
    )
    return _row_to_user(row)


def set_password(conn: Connection, user_id: int, password: str) -> None:
    """Replace a password. Every existing session for that user is revoked."""
    validate_password(password)
    conn.execute(
        text("UPDATE users SET password_hash = :hash WHERE user_id = :user_id"),
        {"hash": hash_password(password), "user_id": user_id},
    )
    delete_sessions_for_user(conn, user_id)


def set_timezone(conn: Connection, user_id: int, timezone_name: Optional[str]) -> Optional[str]:
    zone = validate_timezone(timezone_name)
    conn.execute(
        text("UPDATE users SET timezone = :timezone WHERE user_id = :user_id"),
        {"timezone": zone, "user_id": user_id},
    )
    return zone


def set_active(conn: Connection, user_id: int, active: bool) -> None:
    """Suspend or restore an account. Suspending also drops its sessions."""
    conn.execute(
        text("UPDATE users SET is_active = :active WHERE user_id = :user_id"),
        {"active": active, "user_id": user_id},
    )
    if not active:
        delete_sessions_for_user(conn, user_id)


def list_users(conn: Connection) -> list[User]:
    rows = conn.execute(
        text(f"SELECT {_USER_COLUMNS} FROM users ORDER BY user_id")
    ).fetchall()
    return [_row_to_user(row) for row in rows]


def user_count(conn: Connection) -> int:
    return int(conn.execute(text("SELECT count(*) FROM users")).scalar_one())


_DUMMY_HASH: Optional[str] = None


def _dummy_hash() -> str:
    """A real hash of a random secret, verified against when no user matches.

    It has to cost exactly what a genuine hash costs, or the response time
    would tell an attacker which usernames exist. Built on first use rather
    than at import so a cold start does not pay for 600k rounds before it can
    serve anything.
    """
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    return _DUMMY_HASH


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------


def _token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def create_session(conn: Connection, user_id: int, ttl_days: int = SESSION_TTL_DAYS) -> str:
    """Issue a login token. The raw token is returned once and never stored."""
    token = secrets.token_urlsafe(32)
    conn.execute(
        text(
            """
            INSERT INTO user_sessions (token_hash, user_id, expires_at)
            VALUES (:token_hash, :user_id, now() + CAST(:ttl AS interval))
            """
        ),
        {"token_hash": _token_hash(token), "user_id": user_id, "ttl": f"{int(ttl_days)} days"},
    )
    return token


def resolve_session(conn: Connection, token: Optional[str]) -> Optional[User]:
    """The live user behind a cookie, or None if it is unknown, expired or suspended.

    A session more than halfway through its life is extended, so someone who
    opens the app most weeks is never logged out; one nobody touches still
    expires on schedule.
    """
    if not token:
        return None
    token_hash = _token_hash(token)
    row = conn.execute(
        text(
            f"""
            SELECT {', '.join('u.' + c for c in _USER_COLUMNS.split(', '))},
                   s.expires_at, s.created_at
            FROM user_sessions s
            JOIN users u ON u.user_id = s.user_id
            WHERE s.token_hash = :token_hash AND s.expires_at > now()
            """
        ),
        {"token_hash": token_hash},
    ).fetchone()
    if row is None:
        return None
    if not bool(row[4]):
        # Suspended between requests: drop the session rather than serve it.
        delete_session(conn, token)
        return None

    expires_at, created_at = row[5], row[6]
    if _past_halfway(created_at, expires_at):
        conn.execute(
            text(
                """
                UPDATE user_sessions
                   SET expires_at = now() + CAST(:ttl AS interval),
                       last_seen_at = now()
                 WHERE token_hash = :token_hash
                """
            ),
            {"ttl": f"{int(SESSION_TTL_DAYS)} days", "token_hash": token_hash},
        )
    return _row_to_user(row)


def _past_halfway(created_at: datetime, expires_at: datetime) -> bool:
    """Whether a session has used more than half its life, so it is worth a write."""
    if created_at is None or expires_at is None:
        return True
    now = datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return now >= created_at + (expires_at - created_at) / 2


def delete_session(conn: Connection, token: Optional[str]) -> None:
    if not token:
        return
    conn.execute(
        text("DELETE FROM user_sessions WHERE token_hash = :token_hash"),
        {"token_hash": _token_hash(token)},
    )


def delete_sessions_for_user(conn: Connection, user_id: int) -> int:
    count = conn.execute(
        text("DELETE FROM user_sessions WHERE user_id = :user_id"),
        {"user_id": user_id},
    ).rowcount
    return int(count or 0)


def purge_expired_sessions(conn: Connection) -> int:
    count = conn.execute(
        text("DELETE FROM user_sessions WHERE expires_at <= now()")
    ).rowcount
    return int(count or 0)


def session_cookie_max_age(ttl_days: int = SESSION_TTL_DAYS) -> int:
    return int(timedelta(days=ttl_days).total_seconds())


# --------------------------------------------------------------------------
# Rate limiting
#
# In-process and therefore per-instance: on a serverless deploy several
# instances each keep their own counter. That is a weaker guarantee than a
# shared store, but it still turns an unattended password-guessing loop into a
# slow one, and it costs no round trips.
# --------------------------------------------------------------------------


class RateLimiter:
    """Fixed-window attempt counter keyed by whatever the caller considers a client."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[str, list[float]] = {}

    def check(self, key: str, now: Optional[float] = None) -> bool:
        """Record an attempt. False once the key is over its limit for the window."""
        import time as _time

        now = _time.monotonic() if now is None else now
        cutoff = now - self.window_seconds
        recent = [stamp for stamp in self._hits.get(key, []) if stamp > cutoff]
        recent.append(now)
        self._hits[key] = recent
        if len(self._hits) > 4096:
            self._prune(cutoff)
        return len(recent) <= self.limit

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)

    def _prune(self, cutoff: float) -> None:
        self._hits = {
            key: stamps
            for key, stamps in self._hits.items()
            if any(stamp > cutoff for stamp in stamps)
        }
