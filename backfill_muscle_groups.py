#!/usr/bin/env python3
"""Fill in `exercises.muscle_group` for rows created before it was resolved.

    python backfill_muscle_groups.py --dry-run   # show what would change
    python backfill_muscle_groups.py             # apply

Every exercise logged before the static lookup existed was inserted with a NULL
muscle group, which `insights.volume_by_muscle_group` reports as "Unassigned" -
so the whole training history collapses into one bar. This walks those rows
through `pipeline.resolve_muscle_group` and writes back the ones it recognizes.

Only NULL rows are touched. A group already stored - whether it came from the
table, from `seed_sample_data.py`, or from a hand-written correction - is left
exactly as it is, so re-running this can never overwrite a deliberate value.
Names the table does not recognize stay NULL and are listed at the end, which is
the signal for what to add to `muscle_groups.EXERCISE_MUSCLE_GROUPS`.
"""

from __future__ import annotations

import argparse

from sqlalchemy import text

import pipeline


def backfill(engine, dry_run: bool = False) -> tuple[int, list[str]]:
    """Resolve NULL muscle groups. Returns (updated_count, unresolved_names)."""
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT exercise_id, name FROM exercises "
                "WHERE muscle_group IS NULL ORDER BY name"
            )
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
    args = parser.parse_args()

    updated, unresolved = backfill(pipeline.get_engine(), dry_run=args.dry_run)

    verb = "Would update" if args.dry_run else "Updated"
    print(f"\n{verb} {updated} exercise(s).")

    if unresolved:
        print(f"\n{len(unresolved)} name(s) the table does not recognize; these stay "
              f"NULL and show as 'Unassigned':")
        for name in unresolved:
            print(f"  {name}")
        print("\nAdd them to muscle_groups.EXERCISE_MUSCLE_GROUPS and re-run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
