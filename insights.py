"""Weekly report generation, split strictly into two stages.

Stage A (this module's pure functions + SQL): every number in the report —
e1RM, volume, percentages, recommended weights, plateau and stagnation flags —
is computed here, in code.

Stage B (`narrate`): Groq is handed Stage A's finished numbers and writes prose
around them. It may not alter, invent, or re-round any of them, and it never
decides a recommendation.

The Stage A functions take plain dataclasses, not database rows, so they are
directly unit-testable with no database and no network.
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

import pipeline
from pipeline import env

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Tunables (env vars so they can be adjusted without a code change)
# --------------------------------------------------------------------------

# Program-level stagnation is meaningless on a handful of exercises: with 2-3
# logged, "60% plateaued" is noise. Require at least this many regularly-trained
# exercises before the check runs at all.
PROGRAM_STAGNATION_MIN_EXERCISES = int(env("PROGRAM_STAGNATION_MIN_EXERCISES", "4"))

# Fraction of regularly-trained exercises that must be simultaneously plateaued.
PROGRAM_STAGNATION_FRACTION = float(env("PROGRAM_STAGNATION_FRACTION", "0.6"))

# Pain safeguard defaults to ON. Only an explicit env var turns it off; no code
# path may skip the check on its own.
PAIN_SAFEGUARD_ENABLED = env("PAIN_SAFEGUARD_ENABLED", "true").lower() not in {
    "false", "0", "no", "off",
}

# Working rep range used by the progressive-overload rules.
WORKING_REP_RANGE_LOW = int(env("WORKING_REP_RANGE_LOW", "6"))
WORKING_REP_RANGE_HIGH = int(env("WORKING_REP_RANGE_HIGH", "12"))

# Smallest real load change available in the gym, in kg.
PLATE_INCREMENT_KG = float(env("PLATE_INCREMENT_KG", "2.5"))

# How much history each report considers.
ANALYSIS_WEEKS = int(env("ANALYSIS_WEEKS", "6"))

# Sessions of flat-or-declining e1RM that count as a per-exercise plateau.
PLATEAU_MIN_SESSIONS = int(env("PLATEAU_MIN_SESSIONS", "3"))

# Growth below this fraction over the window still counts as flat.
PLATEAU_TOLERANCE = float(env("PLATEAU_TOLERANCE", "0.01"))

GROQ_MODEL = pipeline.GROQ_MODEL


# --------------------------------------------------------------------------
# Stage A data types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SetRecord:
    """One logged set, already resolved to a local calendar date."""

    exercise_name: str
    session_date: date
    weight_kg: Optional[float] = None
    reps: Optional[int] = None
    cheat_reps: int = 0
    is_warmup: bool = False
    pain_flag: bool = False
    muscle_group: Optional[str] = None


@dataclass
class Recommendation:
    exercise: str
    action: str  # increase | hold | hold_pain | deload_or_swap | insufficient_data
    current_weight_kg: Optional[float] = None
    recommended_weight_kg: Optional[float] = None
    target_reps: Optional[str] = None
    reason: str = ""
    plateaued: bool = False
    pain_safeguard_applied: bool = False
    warmup_set_kg: Optional[float] = None
    pain_note: Optional[str] = None
    escalate_pain_to_professional: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# Stage A: pure computation
# --------------------------------------------------------------------------


def epley_1rm(weight_kg: Optional[float], reps: Optional[int]) -> Optional[float]:
    """Estimated 1RM: weight * (1 + reps / 30)."""
    if weight_kg is None or reps is None:
        return None
    if weight_kg <= 0 or reps <= 0:
        return None
    return float(weight_kg) * (1.0 + reps / 30.0)


def clean_reps(record: SetRecord) -> Optional[int]:
    """Reps performed without cheating or assistance.

    A cheated rep does not demonstrate strength at that load, so clean reps —
    not total reps — drive estimated 1RM and the rep-range rules. Volume still
    uses total reps, because the work was performed.
    """
    if record.reps is None:
        return None
    return max(0, record.reps - (record.cheat_reps or 0))


def is_working_set(record: SetRecord) -> bool:
    """A set that counts toward progression: not a warm-up, and fully logged."""
    return (
        not record.is_warmup
        and record.weight_kg is not None
        and record.reps is not None
        and record.weight_kg > 0
        and record.reps > 0
    )


def working_sets(records: Iterable[SetRecord]) -> list[SetRecord]:
    return [r for r in records if is_working_set(r)]


def group_by_exercise_session(
    records: Iterable[SetRecord],
) -> dict[str, dict[date, list[SetRecord]]]:
    """{exercise: {session_date: [sets]}} — the shape most Stage A rules need."""
    grouped: dict[str, dict[date, list[SetRecord]]] = defaultdict(lambda: defaultdict(list))
    for record in records:
        grouped[record.exercise_name][record.session_date].append(record)
    return {name: dict(sessions) for name, sessions in grouped.items()}


def e1rm_series(sessions: dict[date, list[SetRecord]]) -> list[tuple[date, float]]:
    """Best e1RM per session, oldest first. Sessions with no working set are skipped."""
    series: list[tuple[date, float]] = []
    for session_date in sorted(sessions):
        estimates = [
            value
            for value in (
                epley_1rm(r.weight_kg, clean_reps(r))
                for r in working_sets(sessions[session_date])
            )
            if value is not None
        ]
        if estimates:
            series.append((session_date, max(estimates)))
    return series


def detect_plateau(
    series: list[tuple[date, float]],
    min_sessions: int = PLATEAU_MIN_SESSIONS,
    tolerance: float = PLATEAU_TOLERANCE,
) -> bool:
    """True when e1RM is flat or declining across the trailing `min_sessions`.

    "Flat" allows `tolerance` of drift so a 0.5% wobble is not read as progress.
    """
    if min_sessions < 2 or len(series) < min_sessions:
        return False
    recent = series[-min_sessions:]
    baseline = recent[0][1]
    ceiling = baseline * (1.0 + tolerance)
    return all(value <= ceiling for _, value in recent[1:])


def rep_range_distribution(records: Iterable[SetRecord]) -> dict[str, int]:
    """Set counts by rep band. Raw input for the training-goal inference only.

    Bands: <=5 strength-biased, 6-12 hypertrophy-biased, >12 endurance-biased.
    (The spec's "12+" overlaps "6-12"; 12 is counted as hypertrophy.)
    """
    counts = {"strength": 0, "hypertrophy": 0, "endurance": 0}
    for record in working_sets(records):
        reps = record.reps or 0
        if reps <= 5:
            counts["strength"] += 1
        elif reps <= 12:
            counts["hypertrophy"] += 1
        else:
            counts["endurance"] += 1
    return counts


def set_volume(record: SetRecord) -> float:
    if not is_working_set(record):
        return 0.0
    return float(record.weight_kg) * float(record.reps)  # type: ignore[arg-type]


def total_volume(records: Iterable[SetRecord]) -> float:
    return round(sum(set_volume(r) for r in records), 1)


def volume_by_exercise(records: Iterable[SetRecord]) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for record in records:
        totals[record.exercise_name] += set_volume(record)
    return {name: round(value, 1) for name, value in sorted(totals.items())}


def volume_by_muscle_group(records: Iterable[SetRecord]) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for record in records:
        totals[record.muscle_group or "Unassigned"] += set_volume(record)
    return {name: round(value, 1) for name, value in sorted(totals.items())}


def week_start_for(day: date) -> date:
    """Monday of the week containing `day`."""
    return day - timedelta(days=day.weekday())


def find_regularly_trained(
    records: Iterable[SetRecord],
    reference_week_start: date,
    lookback_weeks: int = 3,
    min_weeks: int = 2,
) -> set[str]:
    """Exercises appearing in at least `min_weeks` of the last `lookback_weeks` weeks.

    Excludes one-off exercises, which would otherwise skew the program-level
    stagnation fraction.
    """
    windows = {reference_week_start - timedelta(weeks=offset) for offset in range(lookback_weeks)}
    weeks_seen: dict[str, set[date]] = defaultdict(set)
    for record in records:
        bucket = week_start_for(record.session_date)
        if bucket in windows:
            weeks_seen[record.exercise_name].add(bucket)
    return {name for name, weeks in weeks_seen.items() if len(weeks) >= min_weeks}


def detect_program_stagnation(
    regularly_trained: Iterable[str],
    plateaued: Iterable[str],
    min_exercises: int = PROGRAM_STAGNATION_MIN_EXERCISES,
    fraction_threshold: float = PROGRAM_STAGNATION_FRACTION,
) -> dict[str, Any]:
    """Roll per-exercise plateau flags up into a whole-program verdict.

    Deliberately built from the plateau flags rather than a total-volume trend:
    a volume drop can be an intentional deload or a dropped accessory lift, and
    reading that as staleness would flag a deliberate program change.
    """
    regular = sorted(set(regularly_trained))
    plateaued_regular = sorted(set(plateaued) & set(regular))
    count = len(regular)

    if count < min_exercises:
        return {
            "checked": False,
            "flagged": False,
            "regularly_trained_count": count,
            "regularly_trained": regular,
            "plateaued_count": len(plateaued_regular),
            "plateaued_exercises": plateaued_regular,
            "fraction": None,
            "fraction_threshold": fraction_threshold,
            "min_exercises": min_exercises,
            "reason": (
                f"Only {count} regularly-trained exercise(s); at least {min_exercises} "
                "are needed before a program-level judgement is meaningful."
            ),
        }

    fraction = len(plateaued_regular) / count
    flagged = fraction >= fraction_threshold
    return {
        "checked": True,
        "flagged": flagged,
        "regularly_trained_count": count,
        "regularly_trained": regular,
        "plateaued_count": len(plateaued_regular),
        "plateaued_exercises": plateaued_regular,
        "fraction": round(fraction, 3),
        "fraction_threshold": fraction_threshold,
        "min_exercises": min_exercises,
        "reason": (
            f"{len(plateaued_regular)} of {count} regularly-trained exercises are "
            f"individually plateaued ({fraction:.0%}), "
            + (
                f"at or above the {fraction_threshold:.0%} threshold."
                if flagged
                else f"below the {fraction_threshold:.0%} threshold."
            )
        ),
    }


def round_to_increment(weight: float, increment: float = PLATE_INCREMENT_KG) -> float:
    """Round to the nearest real plate change."""
    if increment <= 0:
        return round(weight, 2)
    return round(round(weight / increment) * increment, 2)


def next_load(current: float, increment: float = PLATE_INCREMENT_KG, pct: float = 0.05) -> float:
    """The next practical load up (~2.5-5%), never less than one increment.

    On light lifts a single increment is more than 5% — that is simply the
    smallest change the plates allow.
    """
    proposed = round_to_increment(current * (1.0 + pct), increment)
    if proposed <= current:
        proposed = round(current + increment, 2)
    return proposed


def deload_load(current: float, increment: float = PLATE_INCREMENT_KG, pct: float = 0.10) -> float:
    """~10% down for one session, rounded to a real increment and always lower."""
    proposed = round_to_increment(current * (1.0 - pct), increment)
    if proposed >= current:
        proposed = round(current - increment, 2)
    return proposed if proposed > 0 else round(current, 2)


def warmup_load(current: float, increment: float = PLATE_INCREMENT_KG, pct: float = 0.45) -> float:
    """One extra light warm-up set at ~40-50% of the working weight."""
    proposed = round_to_increment(current * pct, increment)
    return proposed if proposed > 0 else round(increment, 2)


def pain_sessions(sessions: dict[date, list[SetRecord]]) -> list[date]:
    """Session dates (oldest first) that carry at least one pain flag."""
    return sorted(d for d, sets in sessions.items() if any(s.pain_flag for s in sets))


def trailing_pain_streak(sessions: dict[date, list[SetRecord]]) -> int:
    """Consecutive most-recent sessions carrying a pain flag."""
    flagged = set(pain_sessions(sessions))
    streak = 0
    for session_date in sorted(sessions, reverse=True):
        if session_date in flagged:
            streak += 1
        else:
            break
    return streak


def recommend_for_exercise(
    exercise: str,
    sessions: dict[date, list[SetRecord]],
    rep_low: int = WORKING_REP_RANGE_LOW,
    rep_high: int = WORKING_REP_RANGE_HIGH,
    increment: float = PLATE_INCREMENT_KG,
    pain_safeguard_enabled: bool = PAIN_SAFEGUARD_ENABLED,
    plateau_min_sessions: int = PLATEAU_MIN_SESSIONS,
) -> Recommendation:
    """Rule-based progressive-overload recommendation for one exercise.

    Precedence (deliberate):
      1. Pain safeguard — holds the load regardless of performance.
      2. Hit the top of the rep range on every working set -> increase.
      3. Plateaued 3+ sessions -> deload or swap.
      4. Otherwise hold.

    Increase outranks the plateau branch on purpose: topping out the rep range
    at the same load for three sessions reads as a plateau by e1RM, but the
    right answer there is more weight, not a deload.
    """
    ordered_dates = sorted(sessions)
    if not ordered_dates:
        return Recommendation(exercise=exercise, action="insufficient_data",
                              reason="No sessions logged for this exercise.")

    last_date = ordered_dates[-1]
    last_working = working_sets(sessions[last_date])
    series = e1rm_series(sessions)
    plateaued = detect_plateau(series, min_sessions=plateau_min_sessions)

    if not last_working:
        return Recommendation(
            exercise=exercise,
            action="insufficient_data",
            plateaued=plateaued,
            reason=f"Last session on {last_date.isoformat()} had no fully-logged working sets.",
        )

    current_weight = max(float(s.weight_kg) for s in last_working)  # type: ignore[arg-type]
    target_reps = f"{rep_low}-{rep_high}"

    recent_two = ordered_dates[-2:]
    pain_recent = any(s.pain_flag for d in recent_two for s in sessions[d])
    pain_streak = trailing_pain_streak(sessions)

    # 1. Pain safeguard. Default on; only an explicit env var disables it.
    if pain_safeguard_enabled and pain_recent:
        note = (
            f"A pain flag was logged on this exercise within the last "
            f"{len(recent_two)} session(s), so the load is held rather than increased."
        )
        escalate = pain_streak >= 3
        if escalate:
            note += (
                f" Pain has now been flagged {pain_streak} sessions in a row — worth having "
                "it looked at by a qualified professional rather than continuing to "
                "self-adjust warm-ups."
            )
        reason = "Load held under the pain safeguard."
        if plateaued:
            reason += " (e1RM is also flat across recent sessions, but pain takes precedence.)"
        return Recommendation(
            exercise=exercise,
            action="hold_pain",
            current_weight_kg=current_weight,
            recommended_weight_kg=current_weight,
            target_reps=target_reps,
            reason=reason,
            plateaued=plateaued,
            pain_safeguard_applied=True,
            warmup_set_kg=warmup_load(current_weight, increment),
            pain_note=note,
            escalate_pain_to_professional=escalate,
        )

    # 2. Top of the rep range on every working set -> add load.
    if all((clean_reps(s) or 0) >= rep_high for s in last_working):
        return Recommendation(
            exercise=exercise,
            action="increase",
            current_weight_kg=current_weight,
            recommended_weight_kg=next_load(current_weight, increment),
            target_reps=target_reps,
            reason=(
                f"Every working set last session hit the top of the {target_reps} range "
                f"at {current_weight:g}kg."
            ),
            plateaued=plateaued,
        )

    # 3. Plateaued for 3+ sessions -> deload or swap (a suggestion, not an instruction).
    if plateaued:
        return Recommendation(
            exercise=exercise,
            action="deload_or_swap",
            current_weight_kg=current_weight,
            recommended_weight_kg=deload_load(current_weight, increment),
            target_reps=target_reps,
            reason=(
                f"Estimated 1RM has been flat or declining across the last "
                f"{plateau_min_sessions} sessions. Consider one session at roughly 10% "
                "lighter, or swapping this movement for a variation."
            ),
            plateaued=True,
        )

    # 4. Otherwise hold and repeat.
    missed_bottom = any((clean_reps(s) or 0) < rep_low for s in last_working)
    reason = (
        f"A working set fell below {rep_low} reps last session — repeat {current_weight:g}kg "
        "and build reps first."
        if missed_bottom
        else f"Still working through the {target_reps} range at {current_weight:g}kg."
    )
    return Recommendation(
        exercise=exercise,
        action="hold",
        current_weight_kg=current_weight,
        recommended_weight_kg=current_weight,
        target_reps=target_reps,
        reason=reason,
        plateaued=False,
    )


PROGRAM_STAGNATION_NOTE = (
    "Most of what you train regularly has stalled at the same time. That usually "
    "points at the program rather than any one lift — the categories of change "
    "worth considering are rotating your split, taking a full deload week, or "
    "starting a new mesocycle. Which one fits is your call; this tool doesn't "
    "prescribe a specific replacement program."
)


def build_stage_a(
    records: list[SetRecord],
    week_start: date,
    pain_safeguard_enabled: bool = PAIN_SAFEGUARD_ENABLED,
    min_exercises: int = PROGRAM_STAGNATION_MIN_EXERCISES,
    fraction_threshold: float = PROGRAM_STAGNATION_FRACTION,
    rep_low: int = WORKING_REP_RANGE_LOW,
    rep_high: int = WORKING_REP_RANGE_HIGH,
    increment: float = PLATE_INCREMENT_KG,
) -> dict[str, Any]:
    """Every number in the weekly report. No LLM involved anywhere in here."""
    week_end = week_start + timedelta(days=7)
    this_week = [r for r in records if week_start <= r.session_date < week_end]
    prior_4_weeks = [
        r for r in records if week_start - timedelta(weeks=4) <= r.session_date < week_start
    ]

    by_exercise = group_by_exercise_session(records)
    this_week_by_exercise = group_by_exercise_session(this_week)
    prior_by_exercise = group_by_exercise_session(prior_4_weeks)

    exercises: dict[str, Any] = {}
    plateaued: set[str] = set()
    recommendations: dict[str, Any] = {}

    for name, sessions in sorted(by_exercise.items()):
        series = e1rm_series(sessions)
        is_plateaued = detect_plateau(series)
        if is_plateaued:
            plateaued.add(name)

        week_series = e1rm_series(this_week_by_exercise.get(name, {}))
        best_this_week = round(max((v for _, v in week_series), default=0.0), 2) or None

        prior_series = e1rm_series(prior_by_exercise.get(name, {}))
        prior_average = (
            round(sum(v for _, v in prior_series) / len(prior_series), 2) if prior_series else None
        )
        delta_pct = (
            round((best_this_week - prior_average) / prior_average * 100, 1)
            if best_this_week and prior_average
            else None
        )

        recommendation = recommend_for_exercise(
            name,
            sessions,
            rep_low=rep_low,
            rep_high=rep_high,
            increment=increment,
            pain_safeguard_enabled=pain_safeguard_enabled,
        )
        recommendations[name] = recommendation.to_dict()

        exercises[name] = {
            "sessions_logged": len(sessions),
            "best_e1rm_this_week_kg": best_this_week,
            "trailing_4wk_avg_e1rm_kg": prior_average,
            "e1rm_delta_pct_vs_4wk_avg": delta_pct,
            "volume_this_week_kg": round(
                sum(set_volume(r) for r in this_week if r.exercise_name == name), 1
            ),
            "plateaued": is_plateaued,
            "pain_flag_streak_sessions": trailing_pain_streak(sessions),
            "e1rm_series": [[d.isoformat(), round(v, 2)] for d, v in series],
        }

    regularly_trained = find_regularly_trained(records, week_start)
    program_stagnation = detect_program_stagnation(
        regularly_trained, plateaued, min_exercises, fraction_threshold
    )

    program_note = {
        "stagnation_flagged": program_stagnation["flagged"],
        "detail": program_stagnation,
        "suggestion": PROGRAM_STAGNATION_NOTE if program_stagnation["flagged"] else None,
    }

    return {
        "week_start_date": week_start.isoformat(),
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "config": {
            "pain_safeguard_enabled": pain_safeguard_enabled,
            "working_rep_range": [rep_low, rep_high],
            "plate_increment_kg": increment,
            "program_stagnation_min_exercises": min_exercises,
            "program_stagnation_fraction": fraction_threshold,
        },
        "totals": {
            "sets_logged_this_week": len(working_sets(this_week)),
            "volume_this_week_kg": total_volume(this_week),
            "volume_prior_4wk_avg_kg": (
                round(total_volume(prior_4_weeks) / 4, 1) if prior_4_weeks else None
            ),
            "exercises_trained_this_week": len(this_week_by_exercise),
        },
        "volume_by_exercise_kg": volume_by_exercise(this_week),
        "volume_by_muscle_group_kg": volume_by_muscle_group(this_week),
        "rep_range_distribution": rep_range_distribution(records),
        "rep_range_distribution_this_week": rep_range_distribution(this_week),
        "exercises": exercises,
        "recommendations": recommendations,
        "program_note": program_note,
    }


# --------------------------------------------------------------------------
# Stage B: narrative only
# --------------------------------------------------------------------------

_NARRATIVE_SYSTEM_PROMPT = """\
You write a short weekly training report for one person, from a JSON object of
numbers that have ALREADY been computed.

Absolute rules:
- You may NOT alter, invent, recompute, or "round differently" ANY number in the
  input. Quote weights, percentages, volumes and e1RM values exactly as given.
- You may NOT create, change, or second-guess a recommendation. Each exercise
  already has an `action` and a `recommended_weight_kg`. Your job is to explain
  the decision that was made, not to make one.
- If a number you would like to cite is not in the input, leave it out.

Write these sections in plain markdown, no preamble:

## What improved
Two to four sentences on the exercises whose `e1rm_delta_pct_vs_4wk_avg` is
positive, and on this week's volume. Name real numbers from the input.

## Next week
One short line per exercise in `recommendations`, in this shape:
**<exercise>** — <what to do> (<recommended_weight_kg>kg, target <target_reps> reps). <one sentence of why, from `reason`>.
If `pain_safeguard_applied` is true, say the load is being held because pain was
logged, and mention the extra light warm-up set at `warmup_set_kg`kg. If
`escalate_pain_to_professional` is true, add that it is worth getting looked at
by a professional. Never suggest a cause for the pain and never prescribe
corrective, rehab or prehab exercises — a text log cannot tell a strain from a
tendon issue, and guessing is out of scope.

## Program note
Include this section ONLY if `program_note.stagnation_flagged` is true. Relay
`program_note.suggestion` in your own words. Name only the CATEGORY of change
(rotate the split, deload week, new mesocycle). Never invent a specific
replacement program, split, or exercise list.

## What you look like you're training for
Two or three sentences inferring the likely goal from
`volume_by_muscle_group_kg` and `rep_range_distribution` only. Describe the
pattern in the numbers ("your sets sit mostly in the 6-12 range and volume is
weighted toward X, which reads as hypertrophy-focused"). Frame it as a reading
of the data, not a fact about the person, and do not tell them to change it.

Keep the whole thing under 350 words. Be direct, no cheerleading.
"""


def narrate(stage_a: dict[str, Any], client: Any = None, model: str = GROQ_MODEL) -> str:
    """Stage B. Turns Stage A's numbers into prose. Computes nothing."""
    if client is None:
        client = pipeline.get_groq_client()

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _NARRATIVE_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(stage_a, default=str, indent=2)},
        ],
        temperature=0,
    )
    return (response.choices[0].message.content or "").strip()


def fallback_summary(stage_a: dict[str, Any]) -> str:
    """Deterministic summary used when Groq is unavailable.

    Same numbers, no prose polish — so a failed narration never blocks the
    report or tempts anyone into letting the LLM supply figures.
    """
    lines = [f"# Week of {stage_a['week_start_date']}", "", "## What improved", ""]
    improved = [
        (name, data["e1rm_delta_pct_vs_4wk_avg"])
        for name, data in stage_a["exercises"].items()
        if data["e1rm_delta_pct_vs_4wk_avg"] is not None and data["e1rm_delta_pct_vs_4wk_avg"] > 0
    ]
    if improved:
        for name, delta in sorted(improved, key=lambda pair: -pair[1]):
            lines.append(f"- {name}: estimated 1RM {delta:+.1f}% vs the trailing 4-week average.")
    else:
        lines.append("- No exercise beat its trailing 4-week average estimated 1RM this week.")
    totals = stage_a["totals"]
    lines += [
        "",
        f"Volume this week: {totals['volume_this_week_kg']:g} kg across "
        f"{totals['sets_logged_this_week']} working sets in "
        f"{totals['exercises_trained_this_week']} exercise(s).",
        "",
        "## Next week",
        "",
    ]
    for name, rec in stage_a["recommendations"].items():
        weight = rec["recommended_weight_kg"]
        weight_text = f"{weight:g}kg" if weight is not None else "n/a"
        lines.append(
            f"- **{name}** — {rec['action'].replace('_', ' ')} at {weight_text} "
            f"(target {rec['target_reps']} reps). {rec['reason']}"
        )
        if rec["pain_safeguard_applied"]:
            lines.append(
                f"  - Pain safeguard: hold the load and add one light warm-up set at "
                f"~{rec['warmup_set_kg']:g}kg. {rec['pain_note']}"
            )
    if stage_a["program_note"]["stagnation_flagged"]:
        lines += ["", "## Program note", "", stage_a["program_note"]["suggestion"]]
    distribution = stage_a["rep_range_distribution"]
    lines += [
        "",
        "## What you look like you're training for",
        "",
        f"Rep-range split across all logged working sets — strength (<=5): "
        f"{distribution['strength']}, hypertrophy (6-12): {distribution['hypertrophy']}, "
        f"endurance (>12): {distribution['endurance']}.",
        f"Volume by muscle group this week: {stage_a['volume_by_muscle_group_kg']}.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Database access + orchestration
# --------------------------------------------------------------------------


def load_set_records(engine: Engine, since: date, until: date) -> list[SetRecord]:
    """Load logged sets in [since, until), converting UTC timestamps to local dates."""
    query = text(
        """
        SELECT e.name, e.muscle_group, w.logged_at, w.weight_kg, w.reps,
               w.cheat_reps, w.is_warmup, w.pain_flag
        FROM workout_logs w
        JOIN exercises e ON e.exercise_id = w.exercise_id
        WHERE w.logged_at >= :since AND w.logged_at < :until
        ORDER BY w.logged_at
        """
    )
    since_utc = pipeline.local_to_utc(datetime.combine(since, datetime.min.time()))
    until_utc = pipeline.local_to_utc(datetime.combine(until, datetime.min.time()))

    from zoneinfo import ZoneInfo

    local_zone = ZoneInfo(pipeline.LOCAL_TIMEZONE)
    with engine.connect() as conn:
        rows = conn.execute(query, {"since": since_utc, "until": until_utc}).fetchall()

    records = []
    for (name, muscle_group, logged_at, weight_kg, reps, cheat_reps,
         is_warmup, pain_flag) in rows:
        records.append(
            SetRecord(
                exercise_name=name,
                muscle_group=muscle_group,
                session_date=logged_at.astimezone(local_zone).date(),
                weight_kg=float(weight_kg) if weight_kg is not None else None,
                reps=int(reps) if reps is not None else None,
                cheat_reps=int(cheat_reps or 0),
                is_warmup=bool(is_warmup),
                pain_flag=bool(pain_flag),
            )
        )
    return records


def save_report(
    engine: Engine,
    week_start: date,
    summary_text: str,
    recommendations: dict[str, Any],
) -> None:
    """Upsert on week_start_date so regenerating a week overwrites it."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO weekly_reports (week_start_date, summary_text, recommendations)
                VALUES (:week_start_date, :summary_text, CAST(:recommendations AS jsonb))
                ON CONFLICT (week_start_date) DO UPDATE
                    SET summary_text    = EXCLUDED.summary_text,
                        recommendations = EXCLUDED.recommendations,
                        created_at      = now()
                """
            ),
            {
                "week_start_date": week_start,
                "summary_text": summary_text,
                "recommendations": json.dumps(recommendations, default=str),
            },
        )


def generate_weekly_report(
    engine: Engine,
    week_start: Optional[date] = None,
    client: Any = None,
    use_llm: bool = True,
    persist: bool = True,
) -> dict[str, Any]:
    """Stage A, then Stage B, then upsert into weekly_reports."""
    if week_start is None:
        week_start = week_start_for(date.today())
    week_start = week_start_for(week_start)

    since = week_start - timedelta(weeks=ANALYSIS_WEEKS)
    until = week_start + timedelta(days=7)
    records = load_set_records(engine, since, until)

    stage_a = build_stage_a(records, week_start)

    summary_text = ""
    narration_error: Optional[str] = None
    if use_llm:
        try:
            summary_text = narrate(stage_a, client=client)
        except Exception as exc:  # noqa: BLE001 - never lose the report over narration
            narration_error = str(exc)
            logger.error("Stage B narration failed, using deterministic summary: %s", exc)
    if not summary_text:
        summary_text = fallback_summary(stage_a)

    payload = {
        "program_note": stage_a["program_note"],
        "exercises": stage_a["recommendations"],
        "stage_a": {
            key: stage_a[key]
            for key in (
                "config", "totals", "volume_by_exercise_kg", "volume_by_muscle_group_kg",
                "rep_range_distribution", "rep_range_distribution_this_week", "exercises",
            )
        },
    }

    if persist:
        save_report(engine, week_start, summary_text, payload)

    return {
        "week_start_date": week_start,
        "summary_text": summary_text,
        "recommendations": payload,
        "stage_a": stage_a,
        "narration_error": narration_error,
        "record_count": len(records),
    }
