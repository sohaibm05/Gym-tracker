#!/usr/bin/env python3
"""One-time upgrade of a single-user database to accounts.

    python migrate_multi_user.py                 # owner from APP_USERNAME/APP_PASSWORD
    python migrate_multi_user.py --username me --password '...'
    python migrate_multi_user.py --dry-run       # show what would run, change nothing

Everything already logged is assigned to one owner account, created here
because hashing a password needs Python and the SQL file cannot do it. After
that the owner logs in with the same credentials as before, and anyone else can
register their own account at /signup.

The statements themselves live in migrations/002_multi_user.sql, which is the
single source of truth for the shape of the change; this script only supplies
the owner id, runs each statement in its own transaction, and skips the ones
already applied so a re-run is a no-op.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent))

import auth  # noqa: E402
import pipeline  # noqa: E402

MIGRATION = Path(__file__).resolve().parent / "migrations" / "002_multi_user.sql"

_ADD_CONSTRAINT = re.compile(
    r"ALTER TABLE\s+(?P<table>\w+)\s+ADD CONSTRAINT\s+(?P<name>\w+)", re.IGNORECASE
)


def statements(sql: str) -> list[str]:
    """Split the migration into executable statements, comments removed."""
    without_comments = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    return [part.strip() for part in without_comments.split(";") if part.strip()]


def constraint_exists(conn, table: str, name: str) -> bool:
    return bool(
        conn.execute(
            text(
                """
                SELECT 1 FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                WHERE t.relname = :table AND c.conname = :name
                """
            ),
            {"table": table, "name": name},
        ).scalar()
    )


def table_exists(conn, table: str) -> bool:
    return bool(
        conn.execute(
            text("SELECT to_regclass(:qualified)"), {"qualified": f"public.{table}"}
        ).scalar()
    )


def ensure_owner(engine, username: str, password: str) -> auth.User:
    """The account everything existing is handed to. Re-running finds it again."""
    with engine.begin() as conn:
        existing = auth.get_user_by_username(conn, username)
        if existing is not None:
            print(f"Owner account {existing.username!r} already exists (id {existing.user_id}).")
            return existing
        user = auth.create_user(conn, username, password, os.getenv("LOCAL_TIMEZONE"))
        print(f"Created owner account {user.username!r} (id {user.user_id}).")
        return user


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--username", help="Owner username (default: $APP_USERNAME)")
    parser.add_argument("--password", help="Owner password (default: $APP_PASSWORD)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the statements that would run, change nothing")
    args = parser.parse_args(argv)

    username = args.username or os.getenv("APP_USERNAME") or ""
    password = args.password or os.getenv("APP_PASSWORD") or ""
    if not username or not password:
        print(
            "No owner credentials. Pass --username/--password, or set "
            "APP_USERNAME and APP_PASSWORD in the environment.",
            file=sys.stderr,
        )
        return 2

    try:
        auth.normalize_username(username)
        auth.validate_password(password)
    except auth.AuthError as exc:
        print(f"Owner credentials rejected: {exc}", file=sys.stderr)
        return 2

    sql = MIGRATION.read_text(encoding="utf-8")
    parts = statements(sql)

    if args.dry_run:
        print(f"{len(parts)} statement(s) from {MIGRATION.name}:\n")
        for part in parts:
            print(f"  {' '.join(part.split())[:110]}")
        print("\nNothing was executed (--dry-run).")
        return 0

    engine = pipeline.get_engine()

    with engine.connect() as conn:
        if not table_exists(conn, "workout_logs"):
            print(
                "This database has no workout_logs table, so there is nothing to "
                "migrate. Run schema.sql against a fresh database instead - it "
                "already has accounts.",
                file=sys.stderr,
            )
            return 2

    owner = None
    applied = skipped = 0
    for part in parts:
        match = _ADD_CONSTRAINT.search(part)
        if match:
            with engine.connect() as conn:
                if constraint_exists(conn, match["table"], match["name"]):
                    skipped += 1
                    continue

        # The owner is created the moment a statement first needs its id, which
        # is the backfill. It cannot be created any earlier: the CREATE TABLE
        # that gives it somewhere to live is one of the statements above it.
        if ":owner_id" in part and owner is None:
            owner = ensure_owner(engine, username, password)

        # One transaction per statement: a step that has already been applied
        # must not roll back the ones before it.
        with engine.begin() as conn:
            conn.execute(text(part), {"owner_id": owner.user_id if owner else None})
        applied += 1

    if owner is None:
        # Every backfill statement was a no-op, which means a previous run
        # already did them. Find the account so the summary can still report.
        owner = ensure_owner(engine, username, password)

    with engine.connect() as conn:
        counts = {
            table: conn.execute(
                text(f"SELECT count(*) FROM {table} WHERE user_id = :owner_id"),
                {"owner_id": owner.user_id},
            ).scalar_one()
            for table in ("exercises", "workout_logs", "bodyweight_logs", "weekly_reports")
        }

    print(f"\nApplied {applied} statement(s), skipped {skipped} already in place.")
    print(f"Owned by {owner.username!r}:")
    for table, count in counts.items():
        print(f"  {table:<16} {count}")
    print(
        "\nDone. Log in as that user; everyone else can register at /signup.\n"
        "APP_USERNAME / APP_PASSWORD are no longer used for login and can be "
        "removed from the environment."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
