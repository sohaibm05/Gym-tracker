#!/usr/bin/env python3
"""Seed a few weeks of realistic sample data so the weekly report can be tried out.

    python seed_sample_data.py --user alice            # seed
    python seed_sample_data.py --user alice --reset    # wipe alice's logs, then seed

The data belongs to one account. `--reset` clears only that account's rows -
never the whole table - so seeding a demo user cannot wipe real training.

The generated pattern deliberately exercises every recommendation branch:
  Chest Bench Press  - progressing, tops out the rep range  -> increase
  Dumbbell Bench Press - flat e1RM for 3 sessions           -> deload or swap
  Lat Pulldown       - flat, and carries a recent pain flag -> hold (pain safeguard)
  Barbell Row        - flat                                 -> deload or swap
  Leg Press          - flat                                 -> deload or swap
With 5 regularly-trained exercises and 4 of them plateaued (80% >= 60%), this
also trips the program-level stagnation flag.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, time, timedelta

from sqlalchemy import text

import auth
import insights
import pipeline

# (exercise, muscle_group, warmup_kg, [(weight, [reps...]) per session], pain_on_sessions)
PLAN = [
    ("Chest Bench Press", "Chest", 20.0,
     [(24.0, [10, 9, 8]), (24.0, [11, 10, 10]), (24.0, [12, 12, 12])], set()),
    ("Dumbbell Bench Press", "Chest", 10.0,
     [(15.0, [10, 9, 8]), (15.0, [10, 9, 8]), (15.0, [10, 9, 8])], set()),
    ("Lat Pulldown", "Back", 25.0,
     [(45.0, [10, 10, 9]), (45.0, [10, 9, 9]), (45.0, [10, 9, 9])], {2}),
    ("Barbell Row", "Back", 30.0,
     [(50.0, [9, 8, 8]), (50.0, [9, 8, 8]), (50.0, [9, 8, 8])], set()),
    ("Leg Press", "Legs", 60.0,
     [(120.0, [10, 10, 9]), (120.0, [10, 9, 9]), (120.0, [10, 9, 9])], set()),
]

BODYWEIGHTS = [83.1, 82.8, 82.4]


def seed(engine, week_start: date, user_id: int, timezone_name: str | None = None) -> None:
    # Sessions land in the three weeks ending with `week_start`'s week.
    session_days = [week_start - timedelta(weeks=2), week_start - timedelta(weeks=1), week_start]

    with engine.begin() as conn:
        known = pipeline.load_exercise_names(conn, user_id)

        for name, muscle_group, warmup_kg, sessions, pain_sessions in PLAN:
            exercise_id, _, _ = pipeline.get_or_create_exercise(
                conn, user_id, name, muscle_group, known
            )
            # Backfill the muscle group when the row already existed without one.
            conn.execute(
                text("UPDATE exercises SET muscle_group = :mg WHERE exercise_id = :id "
                     "AND user_id = :user_id AND muscle_group IS NULL"),
                {"mg": muscle_group, "id": exercise_id, "user_id": user_id},
            )

            for index, (weight, reps_list) in enumerate(sessions):
                day = session_days[index]
                logged_at = pipeline.local_to_utc(
                    datetime.combine(day, time(hour=18)), timezone_name
                )
                pain = index in pain_sessions
                raw = f"[seed] {name} session {index + 1} on {day.isoformat()}"

                rows = [
                    {
                        "user_id": user_id,
                        "exercise_id": exercise_id, "logged_at": logged_at,
                        "weight_kg": warmup_kg, "reps": 12, "cheat_reps": 0, "set_number": 1,
                        "is_warmup": True, "is_dropset": False, "pain_flag": False,
                        "notes": "warm up", "raw_source": raw, "extraction_confidence": 1.0,
                    }
                ]
                for set_number, reps in enumerate(reps_list, start=2):
                    # A couple of cheated reps on the last set of the isolation
                    # lift, so the clean-rep path is exercised by seeded data too.
                    cheat = 2 if (name == "Lateral Raise" and set_number == len(reps_list) + 1) else 0
                    rows.append({
                        "user_id": user_id,
                        "exercise_id": exercise_id, "logged_at": logged_at,
                        "weight_kg": weight, "reps": reps, "cheat_reps": cheat,
                        "set_number": set_number,
                        "is_warmup": False, "is_dropset": False, "pain_flag": pain,
                        "notes": "shoulder discomfort noted" if pain else None,
                        "raw_source": raw, "extraction_confidence": 1.0,
                    })
                for row in rows:
                    conn.execute(pipeline._INSERT_WORKOUT_SET, row)

        for index, weight in enumerate(BODYWEIGHTS):
            day = session_days[index]
            conn.execute(
                pipeline._INSERT_BODYWEIGHT,
                {
                    "user_id": user_id,
                    "logged_at": pipeline.local_to_utc(
                        datetime.combine(day, time(hour=7)), timezone_name
                    ),
                    "weight_kg": weight, "body_fat_pct": None, "notes": None,
                    "raw_source": f"[seed] bodyweight {day.isoformat()}",
                    "extraction_confidence": 1.0,
                },
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", help="Account to seed (default: $APP_USERNAME)")
    parser.add_argument("--reset", action="store_true",
                        help="Delete that user's workout/bodyweight/report rows first")
    parser.add_argument("--week-start", help="Monday of the most recent seeded week (YYYY-MM-DD)")
    args = parser.parse_args()

    username = args.user or os.getenv("APP_USERNAME") or ""
    if not username:
        print("Which user? Pass --user <username>, or set APP_USERNAME.", file=sys.stderr)
        return 2

    engine = pipeline.get_engine()
    with engine.connect() as conn:
        user = auth.get_user_by_username(conn, username)
    if user is None:
        print(f"No account named {username!r}. Create one with: "
              f"python manage_users.py create {username}", file=sys.stderr)
        return 2

    if args.reset:
        # Scoped to this user. An unscoped DELETE here would clear every
        # account's training to make room for one account's demo data.
        with engine.begin() as conn:
            for table in ("workout_logs", "bodyweight_logs", "weekly_reports"):
                conn.execute(
                    text(f"DELETE FROM {table} WHERE user_id = :user_id"),
                    {"user_id": user.user_id},
                )
        print(f"Cleared {user.username}'s workout_logs, bodyweight_logs and weekly_reports.")

    week_start = (
        datetime.strptime(args.week_start, "%Y-%m-%d").date()
        if args.week_start
        else insights.week_start_for(date.today())
    )
    seed(engine, insights.week_start_for(week_start), user.user_id, user.timezone)
    print(f"Seeded 3 weeks of sample data for {user.username} ending the week of "
          f"{insights.week_start_for(week_start).isoformat()}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
