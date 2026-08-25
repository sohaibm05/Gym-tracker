#!/usr/bin/env python3
"""CLI entry point: parse a journal text file into Postgres.

    python parse_workout_log.py <file> <date> --user <username>

`<date>` is the session date in YYYY-MM-DD. `--user` says whose training this
is; the database is multi-user, so there is no default account and every row
written here belongs to exactly one of them. Times are resolved in that user's
timezone, falling back to LOCAL_TIMEZONE.

All extraction, validation and insert logic lives in pipeline.py — this script
only handles argument parsing and printing.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

import pipeline


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Parse a free-text gym journal entry into Postgres.",
    )
    parser.add_argument("file", nargs="?", type=Path,
                        help="Text file containing the journal entry")
    parser.add_argument("date", nargs="?", help="Session date, YYYY-MM-DD")
    parser.add_argument(
        "--user",
        help="Username to log against (default: $APP_USERNAME). "
             "List accounts with: python manage_users.py list",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Report where each setting comes from, test the connections, and exit",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Discard everything already logged on that date before inserting",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Extract and score without inserting anything",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    return parser.parse_args(argv)


def _local_time_label(workout_set, session_date: date, zone_name: str = "") -> str:
    """Resolved wall-clock time for a set, in the user's zone.

    Prefixed with "~" when the text carried no usable time marker and the
    default session hour was applied, so a missed "4:35" is visible.
    """
    from zoneinfo import ZoneInfo

    # Read the zone once and use it for both the resolve and the display, so the
    # two can never disagree.
    zone_name = zone_name or pipeline.LOCAL_TIMEZONE
    resolved = pipeline.resolve_logged_at(
        workout_set.logged_at_local, session_date, zone_name
    )
    local = resolved.astimezone(ZoneInfo(zone_name))
    prefix = "" if (workout_set.logged_at_local or "").strip() else "~"
    return f"{prefix}{local:%H:%M}"


def format_set_line(
    workout_set, confidence: float, session_date: date, zone_name: str = ""
) -> str:
    """One dry-run line: verdict, confidence, resolved time, load, reps, flags."""
    verdict = "INSERT" if confidence >= pipeline.CONFIDENCE_THRESHOLD else "REVIEW"
    weight = f"{workout_set.weight_kg:g}kg" if workout_set.weight_kg is not None else "?kg"
    reps = str(workout_set.reps) if workout_set.reps is not None else "?"

    detail = f"{weight} x {reps}"
    if workout_set.cheat_reps and workout_set.reps is not None:
        clean = max(0, workout_set.reps - workout_set.cheat_reps)
        detail += f" ({workout_set.cheat_reps} cheat -> {clean} clean)"

    flags = ""
    if workout_set.is_warmup:
        flags += "  (warmup)"
    if workout_set.is_dropset:
        flags += "  (dropset)"
    if workout_set.pain_flag:
        flags += "  (PAIN)"

    return (
        f"  [{verdict}] {confidence:.2f}  {_local_time_label(workout_set, session_date, zone_name):>6}  "
        f"{workout_set.exercise_name:<28} {detail}{flags}"
    )


def _parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        print(f"Invalid date {value!r} — expected YYYY-MM-DD", file=sys.stderr)
        raise SystemExit(2)


# APP_USERNAME / APP_PASSWORD are still listed because the migration and the
# --user default read them, but nobody logs into the web app with them any more.
SETTINGS = [
    ("DATABASE_URL", True),
    ("GROQ_API_KEY", True),
    ("APP_PASSWORD", True),
    ("LOCAL_TIMEZONE", False),
    ("GROQ_MODEL", False),
    ("APP_USERNAME", False),
]


def _mask(key: str, value: str) -> str:
    """Render a value without exposing the secret part."""
    if key == "DATABASE_URL":
        try:
            from urllib.parse import urlsplit

            parsed = urlsplit(value)
        except ValueError:
            return "<unparseable>"
        return value.replace(parsed.password, "***", 1) if parsed.password else value
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-2:]}  ({len(value)} chars)"


def check_config() -> int:
    """Report where each setting came from, then test that it works.

    Exists for one failure in particular: a stale shell variable silently
    overriding .env. The effective value looks plausible wherever you inspect
    it, and nothing points at the shell as the culprit.
    """
    from pathlib import Path as _Path

    env_path = _Path(pipeline.__file__).resolve().parent / ".env"
    print("Configuration")
    print()
    print(f"  .env: {env_path}  ({'found' if env_path.is_file() else 'not present'})")
    print()

    file_values: dict = {}
    if env_path.is_file():
        try:
            from dotenv import dotenv_values

            file_values = dict(dotenv_values(env_path))
        except ImportError:
            print("  python-dotenv is not installed, so .env is being ignored.")
            print("  Fix with:  pip install python-dotenv")
            print()

    shadowed: list = []
    for key, secret in SETTINGS:
        active = os.getenv(key)
        in_file = file_values.get(key)
        if active is None:
            print(f"  {key:16} not set")
            continue
        if in_file is not None and in_file != active:
            source = "SHELL (overriding .env)"
            shadowed.append(key)
        elif in_file is not None:
            source = ".env"
        else:
            source = "environment"
        print(f"  {key:16} {source:24} {_mask(key, active) if secret else active}")

    print()
    print("Checks")
    print()
    ok = True

    def report(label, passed, detail=""):
        nonlocal ok
        ok = ok and passed
        print(f"  [{'ok  ' if passed else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        report("DATABASE_URL set", False, "not found in .env or the environment")
    elif "[" in database_url or "]" in database_url:
        report("DATABASE_URL placeholder replaced", False,
               "still contains [...] - replace it, square brackets included")
    else:
        report("DATABASE_URL placeholder replaced", True)
        try:
            from urllib.parse import urlsplit

            parsed = urlsplit(database_url)
            report("DATABASE_URL parses", True,
                   f"user={parsed.username} host={parsed.hostname} port={parsed.port}")
            if not parsed.password:
                report("password present", False, "no password in the URL")
            if parsed.hostname and "supabase" in parsed.hostname \
                    and "pooler" not in parsed.hostname:
                report("Supabase pooler host", False,
                       "this is the direct host - IPv6 only, unreachable from Render")
        except ValueError as exc:
            report("DATABASE_URL parses", False, str(exc))

        try:
            from sqlalchemy import create_engine as _create_engine, text as _text

            # Short timeout: a diagnostic that hangs is worse than no diagnostic.
            engine = _create_engine(database_url, connect_args={"connect_timeout": 8})
            with engine.connect() as conn:
                rows = conn.execute(_text("SELECT count(*) FROM workout_logs")).scalar_one()
            report("database connection", True, f"{rows} rows in workout_logs")
        except Exception as exc:  # noqa: BLE001 - surface whatever failed
            report("database connection", False, str(exc).strip().splitlines()[0])

        # Accounts are the thing most likely to be missing on a database that
        # was set up before this app had them.
        try:
            import auth as _auth

            with pipeline.get_engine().connect() as conn:
                accounts = _auth.user_count(conn)
            report("accounts table", True, f"{accounts} account(s)")
            if accounts == 0:
                print("         note: nobody has registered yet - open /signup, or "
                      "run: python manage_users.py create <username>")
        except Exception as exc:  # noqa: BLE001
            report("accounts table", False,
                   f"{str(exc).strip().splitlines()[0]} "
                   "- run: python migrate_multi_user.py")

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        report("GROQ_API_KEY set", False, "not set")
    elif api_key.startswith("xai-"):
        report("GROQ_API_KEY is a Groq key", False,
               "starts with 'xai-', which is an xAI Grok key - different service")
    else:
        report("GROQ_API_KEY is a Groq key", api_key.startswith("gsk_"),
               "" if api_key.startswith("gsk_") else "expected it to start with 'gsk_'")

    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(pipeline.LOCAL_TIMEZONE)
        report("LOCAL_TIMEZONE valid", True, pipeline.LOCAL_TIMEZONE)
        if pipeline.LOCAL_TIMEZONE == "UTC":
            print("         note: this is still the default - set your own zone or "
                  "session times will be stored wrong")
    except Exception:  # noqa: BLE001
        report("LOCAL_TIMEZONE valid", False, f"unknown zone {pipeline.LOCAL_TIMEZONE!r}")

    for key in shadowed:
        print()
        print(f"  !  {key} is set BOTH in your shell and in .env, with different values.")
        print(f"     The shell wins. Clear it with:  Remove-Item Env:\\{key}")

    print()
    return 0 if ok else 1


def _resolve_user(username: str | None):
    """Look up the account to log against, or explain what to do instead.

    There is no implicit account: writing a set without saying whose it is would
    have to pick one, and picking the wrong one files somebody else's training
    under your name.
    """
    username = username or os.getenv("APP_USERNAME") or ""
    if not username:
        print(
            "Which user? Pass --user <username>, or set APP_USERNAME.\n"
            "List accounts with: python manage_users.py list",
            file=sys.stderr,
        )
        return None

    import auth

    with pipeline.get_engine().connect() as conn:
        user = auth.get_user_by_username(conn, username)

    if user is None:
        print(
            f"No account named {username!r}. "
            "List accounts with: python manage_users.py list",
            file=sys.stderr,
        )
        return None
    if not user.is_active:
        print(f"Account {user.username!r} is suspended.", file=sys.stderr)
        return None
    return user


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.check_config:
        return check_config()

    if args.file is None or args.date is None:
        print("file and date are required unless --check-config is given", file=sys.stderr)
        return 2

    if not args.file.is_file():
        print(f"No such file: {args.file}", file=sys.stderr)
        return 2

    user = _resolve_user(args.user)
    if user is None:
        return 2

    session_date = _parse_date(args.date)
    raw_text = args.file.read_text(encoding="utf-8").strip()
    if not raw_text:
        print(f"{args.file} is empty — nothing to parse.", file=sys.stderr)
        return 2

    zone = user.timezone or pipeline.LOCAL_TIMEZONE

    if args.dry_run:
        payload = pipeline.extract_entities(raw_text, session_date)
        scored_sets, scored_bodyweight, review = pipeline.validate_extraction(payload, raw_text)
        print(f"Dry run — nothing inserted. Session date: {session_date}")
        print(f"User: {user.username}")
        print(f"Times shown in {zone}; \"~\" means no time marker "
              f"in the text, so the default hour was used.\n")
        for workout_set, confidence in scored_sets:
            print(format_set_line(workout_set, confidence, session_date, zone))
        if scored_bodyweight:
            entry, confidence = scored_bodyweight
            verdict = "INSERT" if confidence >= pipeline.CONFIDENCE_THRESHOLD else "REVIEW"
            print(f"  [{verdict}] {confidence:.2f}  bodyweight {entry.weight_kg}kg")
        for item in review:
            print(f"  [REVIEW] {item.kind}: {item.reason}")
        return 0

    result = pipeline.process_entry(
        raw_text,
        session_date,
        user.user_id,
        replace_existing=args.replace,
        timezone_name=user.timezone,
    )

    if result.error:
        print(f"Error: {result.error}", file=sys.stderr)

    print(f"User         : {user.username}")
    print(f"Session date : {session_date}")
    if result.replaced:
        print(f"Replaced     : removed {result.replaced['sets']} set(s), "
              f"{result.replaced['bodyweight']} bodyweight entry/entries")
    print(f"Inserted     : {result.inserted_sets} set(s), "
          f"{result.inserted_bodyweight} bodyweight entry/entries")

    if result.exercises_created:
        print(f"New exercises: {', '.join(result.exercises_created)}")
    for proposed, matched in result.exercises_matched:
        print(f"Fuzzy match  : {proposed!r} -> existing {matched!r}")

    if result.name_flags:
        print(f"\nCheck these names ({len(result.name_flags)}) — saved anyway:")
        for flag in result.name_flags:
            print(f"  ! [{flag.reason}] {flag.exercise_name!r}: {flag.detail}")

    if result.review_items:
        print(f"\nNeeds manual review ({len(result.review_items)}) — NOT inserted:")
        for item in result.review_items:
            confidence = f"{item.confidence:.2f}" if item.confidence is not None else "n/a"
            detail = item.payload.get("exercise_name") or item.payload.get("weight_kg") or ""
            print(f"  - [{item.kind}] confidence={confidence}  {item.reason}"
                  + (f"  ({detail})" if detail else ""))
    else:
        print("\nNothing needing review.")

    return 1 if result.error else 0


if __name__ == "__main__":
    raise SystemExit(main())
