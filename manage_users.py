#!/usr/bin/env python3
"""Account administration from the command line.

    python manage_users.py list
    python manage_users.py create alice [--timezone Asia/Karachi]
    python manage_users.py passwd alice
    python manage_users.py timezone alice Europe/London
    python manage_users.py suspend alice
    python manage_users.py restore alice
    python manage_users.py delete alice
    python manage_users.py purge-sessions

Anyone can register at /signup, so this is not how accounts are normally made.
It exists for the things a web form cannot do: resetting a password for someone
who has forgotten theirs, suspending an account, and deleting one along with its
training.

Passwords are read from a prompt, never from an argument — a password passed on
the command line ends up in the shell history and in the process list.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent))

import auth  # noqa: E402
import pipeline  # noqa: E402


def _prompt_password(label: str = "Password") -> str:
    """Ask twice and compare, so a typo does not become the new password."""
    first = getpass.getpass(f"{label}: ")
    second = getpass.getpass(f"{label} (again): ")
    if first != second:
        raise auth.AuthError("The two entries do not match.")
    return auth.validate_password(first)


def _require_user(conn, username: str) -> auth.User:
    user = auth.get_user_by_username(conn, username)
    if user is None:
        raise auth.AuthError(f"No account named {username!r}.")
    return user


def cmd_list(engine, args) -> int:
    with engine.connect() as conn:
        users = auth.list_users(conn)
        counts = dict(
            conn.execute(
                text(
                    "SELECT user_id, count(*) FROM workout_logs GROUP BY user_id"
                )
            ).fetchall()
        )
    if not users:
        print("No accounts yet. Anyone can register at /signup, or use "
              "`manage_users.py create <username>`.")
        return 0

    print(f"{'id':>4}  {'username':<24} {'sets':>7}  {'timezone':<20} status")
    for user in users:
        status = "active" if user.is_active else "SUSPENDED"
        print(
            f"{user.user_id:>4}  {user.username:<24} "
            f"{counts.get(user.user_id, 0):>7}  "
            f"{(user.timezone or '(default)'):<20} {status}"
        )
    return 0


def cmd_create(engine, args) -> int:
    # Validate everything that can be validated before asking for a password,
    # so a rejected username does not cost the operator two blind prompts.
    auth.normalize_username(args.username)
    auth.validate_timezone(args.timezone)

    password = _prompt_password()
    with engine.begin() as conn:
        user = auth.create_user(conn, args.username, password, args.timezone)
    print(f"Created {user.username!r} (id {user.user_id}).")
    return 0


def cmd_passwd(engine, args) -> int:
    # Look the account up first: prompting twice for a password and only then
    # reporting a typo in the username wastes the operator's time.
    with engine.connect() as conn:
        user = _require_user(conn, args.username)

    password = _prompt_password(f"New password for {user.username}")
    with engine.begin() as conn:
        auth.set_password(conn, user.user_id, password)
    print(f"Password changed for {user.username!r}. Existing sessions were signed out.")
    return 0


def cmd_timezone(engine, args) -> int:
    with engine.begin() as conn:
        user = _require_user(conn, args.username)
        zone = auth.set_timezone(conn, user.user_id, args.timezone)
    print(f"{user.username!r} timezone set to {zone or '(app default)'}.")
    return 0


def cmd_suspend(engine, args) -> int:
    with engine.begin() as conn:
        user = _require_user(conn, args.username)
        auth.set_active(conn, user.user_id, False)
    print(f"Suspended {user.username!r}. Their data is untouched; they cannot log in.")
    return 0


def cmd_restore(engine, args) -> int:
    with engine.begin() as conn:
        user = _require_user(conn, args.username)
        auth.set_active(conn, user.user_id, True)
    print(f"Restored {user.username!r}.")
    return 0


def cmd_delete(engine, args) -> int:
    with engine.connect() as conn:
        user = _require_user(conn, args.username)
        sets = conn.execute(
            text("SELECT count(*) FROM workout_logs WHERE user_id = :user_id"),
            {"user_id": user.user_id},
        ).scalar_one()

    print(
        f"This deletes {user.username!r} and {sets} logged set(s), permanently. "
        "Suspending instead keeps the data (`manage_users.py suspend`)."
    )
    if not args.yes:
        typed = input(f"Type the username to confirm [{user.username}]: ").strip()
        if typed != user.username:
            print("Not confirmed; nothing was deleted.")
            return 1

    # Every data table references users(user_id) ON DELETE CASCADE, so one
    # delete takes the exercises, logs, reports and sessions with it.
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM users WHERE user_id = :user_id"), {"user_id": user.user_id}
        )
    print(f"Deleted {user.username!r} and everything they logged.")
    return 0


def cmd_purge_sessions(engine, args) -> int:
    with engine.begin() as conn:
        removed = auth.purge_expired_sessions(conn)
    print(f"Removed {removed} expired session(s).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(__doc__.splitlines()[2:11]),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="Show every account and how much it has logged")

    create = sub.add_parser("create", help="Create an account (prompts for a password)")
    create.add_argument("username")
    create.add_argument("--timezone", help="IANA name, e.g. Asia/Karachi")

    passwd = sub.add_parser("passwd", help="Set a new password (prompts)")
    passwd.add_argument("username")

    zone = sub.add_parser("timezone", help="Set or clear an account's timezone")
    zone.add_argument("username")
    zone.add_argument("timezone", nargs="?", default="",
                      help="IANA name; omit to fall back to LOCAL_TIMEZONE")

    suspend = sub.add_parser("suspend", help="Block login, keep the data")
    suspend.add_argument("username")

    restore = sub.add_parser("restore", help="Undo a suspend")
    restore.add_argument("username")

    delete = sub.add_parser("delete", help="Delete an account AND all its training")
    delete.add_argument("username")
    delete.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")

    sub.add_parser("purge-sessions", help="Delete expired login sessions")
    return parser


_COMMANDS = {
    "list": cmd_list,
    "create": cmd_create,
    "passwd": cmd_passwd,
    "timezone": cmd_timezone,
    "suspend": cmd_suspend,
    "restore": cmd_restore,
    "delete": cmd_delete,
    "purge-sessions": cmd_purge_sessions,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _COMMANDS[args.command](pipeline.get_engine(), args)
    except auth.AuthError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        # EOFError is what a password prompt raises when stdin is not a
        # terminal, which is how this gets run by mistake from a script.
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
