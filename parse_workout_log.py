#!/usr/bin/env python3
"""CLI entry point: parse a journal text file into Postgres.

    python parse_workout_log.py <file> <date>

`<date>` is the session date in YYYY-MM-DD. All extraction, validation and
insert logic lives in pipeline.py — this script only handles argument parsing
and printing.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

import pipeline


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Parse a free-text gym journal entry into Postgres.",
    )
    parser.add_argument("file", type=Path, help="Text file containing the journal entry")
    parser.add_argument("date", help="Session date, YYYY-MM-DD")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Extract and score without inserting anything",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    return parser.parse_args(argv)


def _parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        print(f"Invalid date {value!r} — expected YYYY-MM-DD", file=sys.stderr)
        raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not args.file.is_file():
        print(f"No such file: {args.file}", file=sys.stderr)
        return 2

    session_date = _parse_date(args.date)
    raw_text = args.file.read_text(encoding="utf-8").strip()
    if not raw_text:
        print(f"{args.file} is empty — nothing to parse.", file=sys.stderr)
        return 2

    if args.dry_run:
        payload = pipeline.extract_entities(raw_text, session_date)
        scored_sets, scored_bodyweight, review = pipeline.validate_extraction(payload, raw_text)
        print(f"Dry run — nothing inserted. Session date: {session_date}\n")
        for workout_set, confidence in scored_sets:
            verdict = "INSERT" if confidence >= pipeline.CONFIDENCE_THRESHOLD else "REVIEW"
            print(
                f"  [{verdict}] {confidence:.2f}  {workout_set.exercise_name} "
                f"{workout_set.weight_kg}kg x {workout_set.reps}"
                + ("  (warmup)" if workout_set.is_warmup else "")
                + ("  (PAIN)" if workout_set.pain_flag else "")
            )
        if scored_bodyweight:
            entry, confidence = scored_bodyweight
            verdict = "INSERT" if confidence >= pipeline.CONFIDENCE_THRESHOLD else "REVIEW"
            print(f"  [{verdict}] {confidence:.2f}  bodyweight {entry.weight_kg}kg")
        for item in review:
            print(f"  [REVIEW] {item.kind}: {item.reason}")
        return 0

    result = pipeline.process_entry(raw_text, session_date)

    if result.error:
        print(f"Error: {result.error}", file=sys.stderr)

    print(f"Session date : {session_date}")
    print(f"Inserted     : {result.inserted_sets} set(s), "
          f"{result.inserted_bodyweight} bodyweight entry/entries")

    if result.exercises_created:
        print(f"New exercises: {', '.join(result.exercises_created)}")
    for proposed, matched in result.exercises_matched:
        print(f"Fuzzy match  : {proposed!r} -> existing {matched!r}")

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
