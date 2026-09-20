#!/usr/bin/env python3
"""Save a handful of workout entries through the review form, without Groq.

The normal path is paste -> POST /log (Groq extracts) -> review -> POST /save.
Only the first step needs GROQ_API_KEY. POST /save builds its draft from the
submitted review form alone, so this script submits that form directly, in
exactly the shape the review page renders. Every business metric is recorded
by the app as usual: entries saved, sets written, sets per entry, commit time,
duplicate decisions, blocked saves.

It exists so the business dashboard has real data on a machine with no Groq
key. It is not a load test; see load_generator.py for that.

Usage:
    python scripts/submit_reviewed_entries.py --username you --password ...
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import sys
import urllib.parse
import urllib.request

SET_ORIGIN = json.dumps({
    "cheat_reps": "0", "exercise_name": "", "is_dropset": False, "is_warmup": False,
    "logged_at_local": "", "muscle_group": "", "notes": "", "pain_flag": False,
    "raw_span": "", "reps": "", "set_number": "", "weight_kg": "",
})
BW_ORIGIN = json.dumps({
    "body_fat_pct": "", "logged_at_local": "", "notes": "", "raw_span": "", "weight_kg": "",
})

# (session date, [(exercise, muscle group, weight kg, reps), ...], bodyweight kg or None)
ENTRIES = [
    ("2026-09-13", [("Bench Press", "Chest", 60, 8), ("Bench Press", "Chest", 62.5, 6),
                    ("Bench Press", "Chest", 62.5, 5), ("Pull Up", "Back", 0, 8),
                    ("Pull Up", "Back", 0, 7)], 78.2),
    ("2026-09-15", [("Squat", "Legs", 80, 5), ("Squat", "Legs", 85, 5),
                    ("Squat", "Legs", 85, 5), ("Squat", "Legs", 85, 4)], None),
    ("2026-09-17", [("Overhead Press", "Shoulders", 40, 8), ("Overhead Press", "Shoulders", 40, 7),
                    ("Barbell Row", "Back", 60, 10), ("Barbell Row", "Back", 60, 10),
                    ("Barbell Row", "Back", 60, 9), ("Bicep Curl", "Arms", 12.5, 12)], None),
    ("2026-09-19", [("Deadlift", "Back", 100, 5), ("Deadlift", "Back", 110, 3),
                    ("Deadlift", "Back", 110, 3)], 77.9),
]


def entry_form(date: str, sets: list, bodyweight: float | None, action: str = "save") -> dict:
    form = {
        "raw_text": f"reviewed entry for {date}", "session_date": date, "mode": "add",
        "set_count": str(len(sets)), "has_bodyweight": "1" if bodyweight else "",
        "action": action,
    }
    for i, (name, group, weight, reps) in enumerate(sets):
        p = f"s{i}"
        form.update({
            f"{p}.include": "on", f"{p}.exercise_name": name, f"{p}.weight_kg": str(weight),
            f"{p}.reps": str(reps), f"{p}.cheat_reps": "0", f"{p}.set_number": str(i + 1),
            f"{p}.logged_at_local": "", f"{p}.muscle_group": group, f"{p}.notes": "",
            f"{p}.raw_span": "", f"{p}.origin": SET_ORIGIN, f"{p}.added": "1",
        })
    if bodyweight:
        form.update({
            "bw.include": "on", "bw.weight_kg": str(bodyweight), "bw.body_fat_pct": "",
            "bw.logged_at_local": "", "bw.notes": "", "bw.raw_span": "",
            "bw.origin": BW_ORIGIN, "bw.added": "1",
        })
    return form


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    args = parser.parse_args()

    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )

    def post(path: str, data: dict) -> str:
        body = urllib.parse.urlencode(data).encode()
        with opener.open(args.base_url + path, body) as response:
            return response.read().decode("utf-8", "replace")

    post("/login", {"username": args.username, "password": args.password})

    submissions = [(d, s, b, "save") for d, s, b in ENTRIES]
    # The same entry again: the duplicate guard should hold it rather than save twice.
    submissions.append((*ENTRIES[-1], "save"))
    # A row the database would reject: the review form should block the save.
    submissions.append(("2026-09-18", [("Plank", "Core", 0, "eleven")], None, "save"))

    for date, sets, bodyweight, action in submissions:
        page = post("/save", entry_form(date, sets, bodyweight, action))
        if "Inserted" in page:
            outcome = "saved"
        elif "already" in page.lower() or "duplicate" in page.lower():
            outcome = "held as a duplicate"
        else:
            outcome = "blocked by review"
        print(f"{date}  {len(sets)} set(s)  -> {outcome}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
