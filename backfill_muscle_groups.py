#!/usr/bin/env python3
"""Fill in `exercises.muscle_group` for rows created before it was resolved.

    python backfill_muscle_groups.py --dry-run       # show what would change
    python backfill_muscle_groups.py                 # apply, every account
    python backfill_muscle_groups.py --user alice    # apply, one account

Every exercise logged before the static lookup existed was inserted with a NULL
muscle group, which `insights.volume_by_muscle_group` reports as "Unassigned" -
so the whole training history collapses into one bar. This walks those rows
through `pipeline.resolve_muscle_group` and writes back the ones it recognizes.

Only NULL rows are touched. A group already stored - whether it came from the
table, from `seed_sample_data.py`, or from a hand-written correction - is left
exactly as it is, so re-running this can never overwrite a deliberate value.
Names the table does not recognize stay NULL and are listed at the end, which is
the signal for what to add to `muscle_groups.EXERCISE_MUSCLE_GROUPS`.

Without `--user` this runs across every account. That is safe in a way most
cross-account operations are not: the value written comes from a static lookup
table, not from anybody's data, and only NULLs are filled. `--user` narrows it
anyway, for when you only want to fix up one person's history.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import text

import auth
import pipeline


def backfill(engine, dry_run: bool = False, user_id: int | None = None) -> tuple[int, list[str]]:
    """Resolve NULL muscle groups. Returns (updated_count, unresolved_names)."""
    scope = "" if user_id is None else " AND user_id = :user_id"
    params = {} if user_id is None else {"user_id": user_id}

    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT exercise_id, name FROM exercises "
                f"WHERE muscle_group IS NULL{scope} ORDER BY name"
            ),
            params,
        ).fetchall()

        updated = 0
        unresolved: list[str] = []
        for exercise_id, name in rows:
            group = pipeline.resolve_muscle_group(name)
            if group is None:
                unresolved.append(name)
                continue

            print(f"  {name}  ->  {group}")
            if not dry_run:
                conn.execute(
                    text("UPDATE exercises SET muscle_group = :group "
                         "WHERE exercise_id = :id AND muscle_group IS NULL"),
                    {"group": group, "id": exercise_id},
                )
            updated += 1

    return updated, unresolved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the resolutions without writing them")
    parser.add_argument("--user", help="Limit to one account (default: all accounts)")
    args = parser.parse_args()

    engine = pipeline.get_engine()

    user_id = None
    if args.user:
        with engine.connect() as conn:
            user = auth.get_user_by_username(conn, args.user)
        if user is None:
            print(f"No account named {args.user!r}.", file=sys.stderr)
            return 2
        user_id = user.user_id

    updated, unresolved = backfill(engine, dry_run=args.dry_run, user_id=user_id)

    verb = "Would update" if args.dry_run else "Updated"
    print(f"\n{verb} {updated} exercise(s).")

    if unresolved:
        # Deduplicated: the same unrecognized name can sit in several accounts,
        # and the fix - one entry in the lookup table - is the same for all.
        distinct = sorted(set(unresolved))
        print(f"\n{len(distinct)} name(s) the table does not recognize; these stay "
              f"NULL and show as 'Unassigned':")
        for name in distinct:
            print(f"  {name}")
        print("\nAdd them to muscle_groups.EXERCISE_MUSCLE_GROUPS and re-run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
