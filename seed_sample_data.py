#!/usr/bin/env python3
"""Seed a few weeks of realistic sample data so the weekly report can be tried out.

    python seed_sample_data.py            # seed
    python seed_sample_data.py --reset    # wipe logs first, then seed

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
from datetime import date, datetime, time, timedelta

from sqlalchemy import text

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


def seed(engine, week_start: date) -> None:
    # Sessions land in the three weeks ending with `week_start`'s week.
    session_days = [week_start - timedelta(weeks=2), week_start - timedelta(weeks=1), week_start]

    with engine.begin() as conn:
        known = pipeline.load_exercise_names(conn)

        for name, muscle_group, warmup_kg, sessions, pain_sessions in PLAN:
            exercise_id, _, _ = pipeline.get_or_create_exercise(conn, name, muscle_group, known)
            # Backfill the muscle group when the row already existed without one.
            conn.execute(
                text("UPDATE exercises SET muscle_group = :mg WHERE exercise_id = :id "
                     "AND muscle_group IS NULL"),
                {"mg": muscle_group, "id": exercise_id},
            )

            for index, (weight, reps_list) in enumerate(sessions):
                day = session_days[index]
                logged_at = pipeline.local_to_utc(datetime.combine(day, time(hour=18)))
                pain = index in pain_sessions
                raw = f"[seed] {name} session {index + 1} on {day.isoformat()}"

                rows = [
                    {
                        "exercise_id": exercise_id, "logged_at": logged_at,
                        "weight_kg": warmup_kg, "reps": 12, "set_number": 1,
                        "is_warmup": True, "is_dropset": False, "pain_flag": False,
                        "notes": "warm up", "raw_source": raw, "extraction_confidence": 1.0,
                    }
                ]
                for set_number, reps in enumerate(reps_list, start=2):
                    rows.append({
                        "exercise_id": exercise_id, "logged_at": logged_at,
                        "weight_kg": weight, "reps": reps, "set_number": set_number,
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
                    "logged_at": pipeline.local_to_utc(datetime.combine(day, time(hour=7))),
                    "weight_kg": weight, "body_fat_pct": None, "notes": None,
                    "raw_source": f"[seed] bodyweight {day.isoformat()}",
                    "extraction_confidence": 1.0,
                },
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true",
                        help="Delete all workout/bodyweight/report rows first")
    parser.add_argument("--week-start", help="Monday of the most recent seeded week (YYYY-MM-DD)")
    args = parser.parse_args()

    engine = pipeline.get_engine()
    if args.reset:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM workout_logs"))
            conn.execute(text("DELETE FROM bodyweight_logs"))
            conn.execute(text("DELETE FROM weekly_reports"))
        print("Cleared workout_logs, bodyweight_logs and weekly_reports.")

    week_start = (
        datetime.strptime(args.week_start, "%Y-%m-%d").date()
        if args.week_start
        else insights.week_start_for(date.today())
    )
    seed(engine, insights.week_start_for(week_start))
    print(f"Seeded 3 weeks of sample data ending the week of "
          f"{insights.week_start_for(week_start).isoformat()}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
