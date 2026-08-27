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
single source of truth for the shape of the change and is idempotent on its
own; this script only creates the owner account, names it to the migration,
and runs each statement in its own transaction. A re-run is a no-op.

That file can also be pasted straight into psql or a SQL console - see its
header - which is why the owner is named through a setting rather than
substituted into the SQL here.
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

# The migration reads the owner's username from this setting, set for the
# transaction the statement that needs it runs in.
OWNER_SETTING = "gym_tracker.owner_username"

# $$ ... $$ or $tag$ ... $tag$, which is what a PL/pgSQL block in the migration
# is wrapped in.
_DOLLAR_QUOTE = re.compile(r"\$\w*\$")


def statements(sql: str) -> list[str]:
    """Split the migration into executable statements, comments removed.

    A semicolon inside a dollar-quoted body belongs to the PL/pgSQL block it is
    written in, not to the migration, so splitting has to skip over those
    bodies whole - otherwise a DO block arrives at the server in pieces.
    """
    without_comments = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )

    parts: list[str] = []
    start = position = 0
    tag: str | None = None
    while position < len(without_comments):
        if tag is None:
            opening = _DOLLAR_QUOTE.match(without_comments, position)
            if opening:
                tag = opening.group(0)
                position = opening.end()
            elif without_comments[position] == ";":
                parts.append(without_comments[start:position])
                position += 1
                start = position
            else:
                position += 1
        elif without_comments.startswith(tag, position):
            position += len(tag)
            tag = None
        else:
            position += 1
    parts.append(without_comments[start:])

    return [part.strip() for part in parts if part.strip()]


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
    for part in parts:
        # The owner is created the moment a statement first asks who it is,
        # which is the backfill. It cannot be created any earlier: the CREATE
        # TABLE that gives it somewhere to live is one of the statements above.
        names_owner = OWNER_SETTING in part
        if names_owner and owner is None:
            owner = ensure_owner(engine, username, password)

        # One transaction per statement: a step that has already been applied
        # must not roll back the ones before it. The setting is transaction
        # local, so it is set inside the same one the statement runs in.
        with engine.begin() as conn:
            if names_owner:
                conn.execute(
                    text("SELECT set_config(:name, :value, true)"),
                    {"name": OWNER_SETTING, "value": owner.username},
                )
            conn.execute(text(part))

    with engine.connect() as conn:
        counts = {
            table: conn.execute(
                text(f"SELECT count(*) FROM {table} WHERE user_id = :owner_id"),
                {"owner_id": owner.user_id},
            ).scalar_one()
            for table in ("exercises", "workout_logs", "bodyweight_logs", "weekly_reports")
        }

    print(f"\nRan {len(parts)} statement(s) from {MIGRATION.name}.")
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
