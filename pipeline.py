"""Shared extraction / validation / insert logic for the gym tracker.

Both the CLI (`parse_workout_log.py`) and the web app (`app.py`) import from
this module. Neither reimplements the Groq call, the confidence heuristic, the
fuzzy exercise match, or the insert statements.

Pipeline order:
    raw journal text
      -> Groq extraction (JSON mode, one retry)
      -> Pydantic validation
      -> computed confidence heuristic
      -> fuzzy exercise-name match against existing `exercises` rows
      -> rows >= CONFIDENCE_THRESHOLD inserted; the rest returned for review
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable, Optional, Sequence

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from rapidfuzz import fuzz
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9 is unsupported anyway
    ZoneInfo = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Load a .env sitting next to this file, if there is one. Must happen before the
# configuration block below, which reads these variables at import time.
#
# override=False means a real environment variable always wins over the file, so
# Render's dashboard configuration is never shadowed by a stray .env in the repo.
try:
    from pathlib import Path

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
except ImportError:  # pragma: no cover - python-dotenv is optional
    pass

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

# Entries scoring below this are surfaced for manual review and NOT inserted.
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.7"))

# rapidfuzz score (0-100) at or above which a proposed exercise name is treated
# as the same exercise as an existing row.
FUZZY_MATCH_THRESHOLD = float(os.getenv("FUZZY_MATCH_THRESHOLD", "85"))

# Groq's Llama chat models (llama-3.3-70b-versatile, llama-3.1-8b-instant) were
# deprecated for free/developer tiers on 2026-06-17. openai/gpt-oss-120b is the
# recommended general-purpose replacement. Re-check console.groq.com/docs/models
# before assuming this is still current.
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

# Single fixed timezone: this is a single-user personal tool, so local wall-clock
# time in the journal is always interpreted in this zone and stored as UTC.
LOCAL_TIMEZONE = os.getenv("LOCAL_TIMEZONE", "UTC")

# Wall-clock time assumed for a session when the text carries no time marker.
DEFAULT_SESSION_HOUR = int(os.getenv("DEFAULT_SESSION_HOUR", "18"))

# Window used by the /log duplicate-submission guard.
DUPLICATE_WINDOW_MINUTES = int(os.getenv("DUPLICATE_WINDOW_MINUTES", "5"))

# Tokens that describe equipment or the muscle worked. When the ONLY difference
# between two multi-word exercise names is tokens from this set, the names are
# treated as the same exercise ("Barbell Chest Bench Press" == "Chest Bench
# Press"). Anything outside this set — "incline", "front", "close grip",
# "romanian" — marks a genuinely different movement and blocks the match. The
# set is an allowlist so unknown words fail closed, i.e. into a separate row.
QUALIFIER_TOKENS = frozenset(
    {
        "barbell", "dumbbell", "dumbell", "db", "bb", "machine", "cable",
        "smith", "ez", "bar", "weighted",
        "chest", "back", "shoulder", "shoulders", "leg", "legs", "arm", "arms",
        "bicep", "biceps", "tricep", "triceps", "glute", "glutes",
        "hamstring", "hamstrings", "quad", "quads", "calf", "calves",
        "lat", "lats", "core", "ab", "abs", "trap", "traps",
    }
)

_SYSTEM_PROMPT = """\
You extract structured training data from messy free-text gym journal entries.
The text is voice-to-text style: typos, missing punctuation, informal times, and
personal commentary mixed in with the actual numbers.

Return ONLY a JSON object with exactly this shape:

{
  "sets": [
    {
      "exercise_name": "Chest Bench Press",
      "weight_kg": 24.0,
      "reps": 12,
      "cheat_reps": 0,
      "set_number": 2,
      "is_warmup": false,
      "is_dropset": false,
      "pain_flag": false,
      "notes": "almost died",
      "logged_at_local": "2026-08-14T18:20:00",
      "raw_span": "Chest bench press 24kg 12 reps"
    }
  ],
  "bodyweight": null
}

"bodyweight" is either null or:
  {"weight_kg": 82.4, "body_fat_pct": null, "notes": null,
   "logged_at_local": "2026-08-14T07:00:00", "raw_span": "was 82.4kg this morning"}

Rules:
- One object per SET performed. "3 sets of 10 at 40kg" is three set objects.
- Normalize exercise names to title case and fix typos:
  "ches bent prees" -> "Chest Bench Press", "shoulder pres" -> "Shoulder Press".
  Use the same name for the same movement everywhere in the entry.
- weight_kg is per-side load as written. "12.5kg each hand" -> 12.5.
- cheat_reps: how many reps in THIS set were completed with cheating, momentum
  or assistance. "10 reps 3 were cheat" -> reps 10, cheat_reps 3. Also covers
  "3 cheat", "last 2 were assisted", "2 forced reps", "final rep was a grinder
  with help". Default 0. cheat_reps must never exceed reps.
- "each hand" / "per hand" / "each side" applies to every LATER set of the same
  exercise in the entry even when written only once. Keep reporting the per-hand
  number for those sets.
- set_number counts within that exercise, starting at 1, in the order written.
- is_warmup true when the text says warm up / warmup / "warm up ish" / similar.
- is_dropset true only when a drop set is explicitly described.
- pain_flag true on ANY mention of pain, discomfort, injury, tweak, strain,
  soreness that reads as a problem, or "felt something".
  - Effort is NOT pain. "almost died", "brutal", "killed me", "barely got it",
    "struggled", "tough", "burned" describe how hard a set was, not an injury.
    Leave pain_flag false for those.
  - If the mention names an exercise, apply it to every working set of that
    exercise.
  - If it names no exercise ("shoulder pain noticed toward the end"), apply it
    to the sets of the exercise described most recently BEFORE the mention in
    the text. A body part is not an exercise: "shoulder pain" does not mean the
    shoulder press.
  - Never spread a single pain mention across every exercise in the entry.
- logged_at_local: combine SESSION_DATE (given below) with any time marker in
  the text. A bare time with no am/pm that sits in a workout sequence is
  afternoon or evening: "4:35" -> 16:35, "6:20ish" -> 18:20. Read a time as
  morning only when the text says so ("this morning", "am", "before work").
  Format "YYYY-MM-DDTHH:MM:SS", no timezone. If no time marker applies to a
  set, use null.
- raw_span: the exact substring of the input this set was read from. Copy it
  verbatim; do not paraphrase. This is used to verify the extraction.
- NEVER invent data. If a weight, rep count, or exercise name is not in the
  text, use null. Do not guess a plausible value. Missing fields are handled
  downstream; fabricated ones are not.
- Do not report a confidence score. Confidence is computed separately.
"""


# --------------------------------------------------------------------------
# Pydantic models
# --------------------------------------------------------------------------


class WorkoutSet(BaseModel):
    """One performed set, mirroring the `workout_logs` columns."""

    exercise_name: str = Field(min_length=1, max_length=120)
    weight_kg: Optional[float] = Field(default=None, ge=0, le=1000)
    reps: Optional[int] = Field(default=None, gt=0, le=1000)
    cheat_reps: int = Field(default=0, ge=0, le=1000)
    set_number: Optional[int] = Field(default=None, gt=0, le=100)
    is_warmup: bool = False
    is_dropset: bool = False
    pain_flag: bool = False
    notes: Optional[str] = None
    logged_at_local: Optional[str] = None
    raw_span: Optional[str] = None
    muscle_group: Optional[str] = None

    @model_validator(mode="after")
    def _cheat_reps_within_reps(self) -> "WorkoutSet":
        """A set cannot contain more cheat reps than reps."""
        if self.reps is not None and self.cheat_reps > self.reps:
            raise ValueError(
                f"cheat_reps ({self.cheat_reps}) exceeds reps ({self.reps})"
            )
        return self

    @field_validator("exercise_name")
    @classmethod
    def _clean_name(cls, value: str) -> str:
        cleaned = re.sub(r"\s+", " ", value).strip()
        if not cleaned:
            raise ValueError("exercise_name must not be blank")
        return cleaned


class BodyweightEntry(BaseModel):
    """A bodyweight mention, mirroring the `bodyweight_logs` columns."""

    weight_kg: float = Field(gt=0, le=500)
    body_fat_pct: Optional[float] = Field(default=None, ge=0, le=100)
    notes: Optional[str] = None
    logged_at_local: Optional[str] = None
    raw_span: Optional[str] = None


@dataclass
class ReviewItem:
    """An extraction that did not clear CONFIDENCE_THRESHOLD, or failed validation."""

    kind: str  # "workout_set" | "bodyweight" | "extraction"
    reason: str
    confidence: Optional[float]
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineResult:
    inserted_sets: int = 0
    inserted_bodyweight: int = 0
    review_items: list[ReviewItem] = field(default_factory=list)
    exercises_created: list[str] = field(default_factory=list)
    exercises_matched: list[tuple[str, str]] = field(default_factory=list)
    duplicate_of_recent: bool = False
    error: Optional[str] = None

    @property
    def total_inserted(self) -> int:
        return self.inserted_sets + self.inserted_bodyweight


# --------------------------------------------------------------------------
# Pure helpers (no network, no database — these are what the unit tests cover)
# --------------------------------------------------------------------------


def normalize_name(name: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace."""
    lowered = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower().strip())
    return re.sub(r"\s+", " ", lowered).strip()


def _singularize(token: str) -> str:
    """Crude singularizer so "Curls" and "Curl" compare equal.

    Words ending in "ss" are left alone, which is what keeps "press" from
    becoming "pres".
    """
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def normalize_tokens(name: str) -> list[str]:
    """Normalized, singularized tokens of an exercise name."""
    return [_singularize(token) for token in normalize_name(name).split() if token]


# Compared against singularized tokens, so the qualifier set is singularized too.
_QUALIFIER_TOKENS_SINGULAR = frozenset(_singularize(t) for t in QUALIFIER_TOKENS)


def name_match_score(proposed: str, existing: str) -> float:
    """Similarity (0-100) between two exercise names.

    Base metric is `token_sort_ratio`, which is insensitive to word order but
    still penalizes extra words — so "Chest Bench Press" and "Bench Press
    (Chest)" score 100, while "Bench Press" and "Incline Bench Press" score 73.

    A subset bonus lifts the score to `token_set_ratio` only when one name's
    tokens are a strict subset of the other's AND every extra token is a known
    equipment/muscle qualifier. That merges "Barbell Chest Bench Press" into
    "Chest Bench Press" without merging "Squat" into "Front Squat". Both names
    must have at least two tokens, so "Curl" never absorbs "Leg Curl".
    """
    left_list, right_list = normalize_tokens(proposed), normalize_tokens(existing)
    if not left_list or not right_list:
        return 0.0
    left, right = " ".join(left_list), " ".join(right_list)

    score = float(fuzz.token_sort_ratio(left, right))

    # "Skullcrushers" vs "Skull Crushers" differ only in word boundaries, which
    # token-based scorers cannot see. Comparing with spaces stripped catches it,
    # and stays low for genuinely different names ("benchpress" vs
    # "inclinebenchpress" scores 74).
    score = max(score, float(fuzz.ratio(left.replace(" ", ""), right.replace(" ", ""))))

    left_tokens, right_tokens = set(left_list), set(right_list)
    is_strict_subset = left_tokens < right_tokens or right_tokens < left_tokens
    if (
        left_tokens != right_tokens
        and is_strict_subset
        and len(left_tokens) >= 2
        and len(right_tokens) >= 2
        and (left_tokens ^ right_tokens) <= _QUALIFIER_TOKENS_SINGULAR
    ):
        score = max(score, float(fuzz.token_set_ratio(left, right)))

    return score


def find_matching_exercise(
    proposed: str,
    existing_names: Iterable[str],
    threshold: float = FUZZY_MATCH_THRESHOLD,
) -> Optional[str]:
    """Return the best existing name at/above `threshold`, else None."""
    best_name: Optional[str] = None
    best_score = 0.0
    for candidate in existing_names:
        score = name_match_score(proposed, candidate)
        if score > best_score:
            best_name, best_score = candidate, score
    return best_name if best_score >= threshold else None


def compute_confidence(
    exercise_name: Optional[str],
    weight_kg: Optional[float],
    reps: Optional[int],
    raw_span: Optional[str],
    raw_text: str,
    validation_ok: bool = True,
) -> float:
    """Confidence from checkable signals only — never self-reported by the LLM.

    Signals:
      * Pydantic validation succeeded.
      * Required fields (exercise name, weight, reps) came back non-null.
      * String similarity between the normalized exercise name and the raw text
        span it was supposedly read from — the grounding check that catches a
        name the model invented rather than read.
    """
    if not validation_ok:
        return 0.0
    if not exercise_name or not exercise_name.strip():
        return 0.0

    score = 1.0
    if weight_kg is None:
        score -= 0.35
    if reps is None:
        score -= 0.35

    span = (raw_span or "").strip()
    if span:
        haystack = span
    else:
        # No span to check against: fall back to the whole entry, and penalize,
        # because grounding is that much weaker.
        haystack = raw_text or ""
        score -= 0.2

    similarity = _grounding_similarity(exercise_name, haystack)
    if similarity < 0.5:
        score -= 0.35
    elif similarity < 0.8:
        score -= 0.15

    return round(max(0.0, min(1.0, score)), 3)


def _grounding_similarity(exercise_name: str, haystack: str) -> float:
    """How strongly `exercise_name` is supported by the text it came from (0-1)."""
    name, text_norm = normalize_name(exercise_name), normalize_name(haystack)
    if not name or not text_norm:
        return 0.0
    return max(
        fuzz.partial_ratio(name, text_norm),
        fuzz.token_set_ratio(name, text_norm),
    ) / 100.0


def local_to_utc(local_dt: datetime, timezone_name: str = LOCAL_TIMEZONE) -> datetime:
    """Attach the fixed local timezone to a naive datetime and convert to UTC."""
    from datetime import timezone as _tz

    if ZoneInfo is None:  # pragma: no cover
        raise RuntimeError("zoneinfo unavailable; Python 3.9+ required")
    tz = ZoneInfo(timezone_name)
    aware = local_dt.replace(tzinfo=tz) if local_dt.tzinfo is None else local_dt
    return aware.astimezone(_tz.utc)


def resolve_logged_at(
    logged_at_local: Optional[str],
    session_date: date,
    timezone_name: str = LOCAL_TIMEZONE,
    default_hour: int = DEFAULT_SESSION_HOUR,
) -> datetime:
    """Turn the model's local-time guess into a UTC timestamp.

    The model's value is only trusted when it parses AND lands on the session
    date the user supplied. Anything else falls back to the session date at
    `default_hour` — the model does not get to move a workout to another day.
    """
    if logged_at_local:
        parsed: Optional[datetime] = None
        candidate = logged_at_local.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%H:%M:%S", "%H:%M"):
                try:
                    parsed = datetime.strptime(logged_at_local.strip(), fmt)
                    break
                except ValueError:
                    continue
        if parsed is not None:
            if parsed.year == 1900:  # time-only format
                parsed = datetime.combine(session_date, parsed.time())
            if parsed.tzinfo is not None:
                parsed = parsed.replace(tzinfo=None)
            if parsed.date() == session_date:
                return local_to_utc(parsed, timezone_name)
            logger.warning(
                "Discarding extracted timestamp %s: not on session date %s",
                logged_at_local,
                session_date,
            )

    return local_to_utc(datetime.combine(session_date, time(hour=default_hour)), timezone_name)


# --------------------------------------------------------------------------
# Extraction (Groq)
# --------------------------------------------------------------------------


class ExtractionError(RuntimeError):
    """Raised when extraction fails after the retry."""


def _build_messages(raw_text: str, session_date: date) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"SESSION_DATE: {session_date.isoformat()}\n\nJOURNAL ENTRY:\n{raw_text}",
        },
    ]


def extract_entities(
    raw_text: str,
    session_date: date,
    client: Any = None,
    model: str = GROQ_MODEL,
) -> dict[str, Any]:
    """Call Groq in JSON mode and return the parsed object.

    JSON mode (`response_format={"type": "json_object"}`) is used rather than
    trusting the prompt to produce valid JSON. One retry is attempted on an API
    error or a parse failure; after that the caller routes the entry to review.
    """
    if client is None:
        client = get_groq_client()

    last_error: Optional[Exception] = None
    for attempt in (1, 2):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=_build_messages(raw_text, session_date),
                response_format={"type": "json_object"},
                temperature=0,
            )
            content = response.choices[0].message.content
            payload = json.loads(content)
            if not isinstance(payload, dict):
                raise ValueError(f"expected a JSON object, got {type(payload).__name__}")
            return payload
        except Exception as exc:  # noqa: BLE001 - retry once on anything
            last_error = exc
            logger.warning("Extraction attempt %d/2 failed: %s", attempt, exc)

    raise ExtractionError(f"extraction failed after retry: {last_error}")


def get_groq_client() -> Any:
    """Build a Groq client from GROQ_API_KEY (env var only, never hardcoded)."""
    from groq import Groq

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is not set")
    return Groq(api_key=api_key)


def validate_extraction(
    payload: dict[str, Any],
    raw_text: str,
) -> tuple[list[tuple[WorkoutSet, float]], Optional[tuple[BodyweightEntry, float]], list[ReviewItem]]:
    """Validate the raw extraction into models plus computed confidences."""
    scored_sets: list[tuple[WorkoutSet, float]] = []
    review: list[ReviewItem] = []

    raw_sets = payload.get("sets") or []
    if not isinstance(raw_sets, list):
        review.append(
            ReviewItem("extraction", "'sets' was not a list", None, {"sets": str(raw_sets)[:500]})
        )
        raw_sets = []

    for index, item in enumerate(raw_sets):
        if not isinstance(item, dict):
            review.append(ReviewItem("workout_set", f"set {index} was not an object", 0.0,
                                     {"raw": str(item)[:500]}))
            continue
        try:
            workout_set = WorkoutSet.model_validate(item)
        except ValidationError as exc:
            review.append(
                ReviewItem(
                    "workout_set",
                    f"failed validation: {_short_errors(exc)}",
                    compute_confidence(None, None, None, None, raw_text, validation_ok=False),
                    item,
                )
            )
            continue

        confidence = compute_confidence(
            workout_set.exercise_name,
            workout_set.weight_kg,
            workout_set.reps,
            workout_set.raw_span,
            raw_text,
        )
        scored_sets.append((workout_set, confidence))

    scored_bodyweight: Optional[tuple[BodyweightEntry, float]] = None
    raw_bodyweight = payload.get("bodyweight")
    if isinstance(raw_bodyweight, dict):
        try:
            entry = BodyweightEntry.model_validate(raw_bodyweight)
        except ValidationError as exc:
            review.append(
                ReviewItem("bodyweight", f"failed validation: {_short_errors(exc)}", 0.0, raw_bodyweight)
            )
        else:
            scored_bodyweight = (entry, compute_bodyweight_confidence(entry, raw_text))
    elif raw_bodyweight not in (None, ""):
        review.append(
            ReviewItem("bodyweight", "'bodyweight' was neither null nor an object", 0.0,
                       {"raw": str(raw_bodyweight)[:500]})
        )

    return scored_sets, scored_bodyweight, review


def _normalize_numeric_text(value: str) -> str:
    """Lowercase and collapse whitespace while KEEPING digits and decimal points.

    `normalize_name` strips punctuation, which would turn "82.4kg" into "82 4kg"
    and make a correct reading look ungrounded. Number checks use this instead.
    """
    return re.sub(r"\s+", " ", (value or "").lower()).strip()


def _number_appears_in(number: float, haystack: str) -> bool:
    """True when `number` occurs in `haystack` as a standalone figure.

    Digit boundaries stop "82" from matching inside "182.5".
    """
    candidates = {f"{number:g}", f"{number:.1f}"}
    if float(number).is_integer():
        candidates.add(str(int(number)))
    return any(
        re.search(rf"(?<!\d){re.escape(token)}(?!\d)", haystack) for token in candidates if token
    )


def compute_bodyweight_confidence(entry: BodyweightEntry, raw_text: str) -> float:
    """Confidence for a bodyweight reading: does its number appear in the text?"""
    haystack = _normalize_numeric_text(entry.raw_span or raw_text)
    score = 1.0
    if not (entry.raw_span or "").strip():
        score -= 0.2
    # The weight itself must be traceable to the source text.
    if not _number_appears_in(float(entry.weight_kg), haystack):
        score -= 0.5
    return round(max(0.0, min(1.0, score)), 3)


def _short_errors(exc: ValidationError) -> str:
    parts = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:3]]
    return "; ".join(parts)


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------


def get_engine(database_url: Optional[str] = None) -> Engine:
    url = database_url or os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    # Free-tier Postgres drops idle connections; pre-ping avoids stale-handle errors.
    return create_engine(url, pool_pre_ping=True, future=True)


def load_exercise_names(conn: Connection) -> dict[str, int]:
    rows = conn.execute(text("SELECT name, exercise_id FROM exercises")).fetchall()
    return {row[0]: row[1] for row in rows}


def get_or_create_exercise(
    conn: Connection,
    proposed_name: str,
    muscle_group: Optional[str],
    known: dict[str, int],
) -> tuple[int, Optional[str], bool]:
    """Resolve an exercise name to an id, fuzzy-matching before inserting.

    Returns (exercise_id, matched_existing_name_or_None, created).
    """
    matched = find_matching_exercise(proposed_name, known.keys())
    if matched is not None:
        return known[matched], matched, False

    row = conn.execute(
        text(
            """
            INSERT INTO exercises (name, muscle_group)
            VALUES (:name, :muscle_group)
            ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
            RETURNING exercise_id
            """
        ),
        {"name": proposed_name, "muscle_group": muscle_group},
    ).fetchone()
    exercise_id = int(row[0])
    known[proposed_name] = exercise_id
    return exercise_id, None, True


_INSERT_WORKOUT_SET = text(
    """
    INSERT INTO workout_logs (
        exercise_id, logged_at, weight_kg, reps, cheat_reps, set_number,
        is_warmup, is_dropset, pain_flag, notes, raw_source, extraction_confidence
    ) VALUES (
        :exercise_id, :logged_at, :weight_kg, :reps, :cheat_reps, :set_number,
        :is_warmup, :is_dropset, :pain_flag, :notes, :raw_source, :extraction_confidence
    )
    """
)

_INSERT_BODYWEIGHT = text(
    """
    INSERT INTO bodyweight_logs (
        logged_at, weight_kg, body_fat_pct, notes, raw_source, extraction_confidence
    ) VALUES (
        :logged_at, :weight_kg, :body_fat_pct, :notes, :raw_source, :extraction_confidence
    )
    """
)


def find_recent_submission(
    engine: Engine,
    raw_text: str,
    window_minutes: int = DUPLICATE_WINDOW_MINUTES,
) -> Optional[dict[str, int]]:
    """Return counts for an identical entry inserted within the window, else None.

    Backs the /log duplicate-submission guard: Render's free tier cold-starts for
    30-50s, which is exactly when a user double-taps submit.
    """
    with engine.connect() as conn:
        params = {"raw_source": raw_text, "window": f"{int(window_minutes)} minutes"}
        sets = conn.execute(
            text(
                """
                SELECT count(*) FROM workout_logs
                WHERE raw_source = :raw_source
                  AND created_at >= now() - CAST(:window AS interval)
                """
            ),
            params,
        ).scalar_one()
        bodyweight = conn.execute(
            text(
                """
                SELECT count(*) FROM bodyweight_logs
                WHERE raw_source = :raw_source
                  AND created_at >= now() - CAST(:window AS interval)
                """
            ),
            params,
        ).scalar_one()

    if not sets and not bodyweight:
        return None
    return {"inserted_sets": int(sets), "inserted_bodyweight": int(bodyweight)}


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def process_entry(
    raw_text: str,
    session_date: date,
    engine: Optional[Engine] = None,
    client: Any = None,
    check_duplicates: bool = False,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
) -> PipelineResult:
    """Run the full pipeline for one journal entry.

    Called by both the CLI and the web app. `check_duplicates` is enabled by the
    web app, where a double-tapped submit is a real risk.
    """
    raw_text = (raw_text or "").strip()
    if not raw_text:
        return PipelineResult(error="Empty entry — nothing to parse.")

    if engine is None:
        engine = get_engine()

    if check_duplicates:
        prior = find_recent_submission(engine, raw_text)
        if prior is not None:
            logger.info("Duplicate submission suppressed (%s)", prior)
            return PipelineResult(
                inserted_sets=prior["inserted_sets"],
                inserted_bodyweight=prior["inserted_bodyweight"],
                duplicate_of_recent=True,
            )

    try:
        payload = extract_entities(raw_text, session_date, client=client)
    except ExtractionError as exc:
        logger.error("Extraction failed: %s", exc)
        return PipelineResult(
            error="Extraction failed after one retry — entry not inserted.",
            review_items=[ReviewItem("extraction", str(exc), None, {"raw_text": raw_text})],
        )

    scored_sets, scored_bodyweight, review = validate_extraction(payload, raw_text)
    result = PipelineResult(review_items=list(review))

    accepted_sets = []
    for workout_set, confidence in scored_sets:
        if confidence < confidence_threshold:
            result.review_items.append(
                ReviewItem(
                    "workout_set",
                    f"confidence {confidence:.2f} below threshold {confidence_threshold:.2f}",
                    confidence,
                    workout_set.model_dump(),
                )
            )
        else:
            accepted_sets.append((workout_set, confidence))

    accepted_bodyweight = None
    if scored_bodyweight is not None:
        entry, confidence = scored_bodyweight
        if confidence < confidence_threshold:
            result.review_items.append(
                ReviewItem(
                    "bodyweight",
                    f"confidence {confidence:.2f} below threshold {confidence_threshold:.2f}",
                    confidence,
                    entry.model_dump(),
                )
            )
        else:
            accepted_bodyweight = (entry, confidence)

    if not accepted_sets and accepted_bodyweight is None:
        return result

    with engine.begin() as conn:
        known = load_exercise_names(conn)
        for workout_set, confidence in accepted_sets:
            exercise_id, matched_name, created = get_or_create_exercise(
                conn, workout_set.exercise_name, workout_set.muscle_group, known
            )
            if created:
                result.exercises_created.append(workout_set.exercise_name)
            elif matched_name and matched_name != workout_set.exercise_name:
                result.exercises_matched.append((workout_set.exercise_name, matched_name))

            conn.execute(
                _INSERT_WORKOUT_SET,
                {
                    "exercise_id": exercise_id,
                    "logged_at": resolve_logged_at(workout_set.logged_at_local, session_date),
                    "weight_kg": workout_set.weight_kg,
                    "reps": workout_set.reps,
                    "cheat_reps": workout_set.cheat_reps,
                    "set_number": workout_set.set_number,
                    "is_warmup": workout_set.is_warmup,
                    "is_dropset": workout_set.is_dropset,
                    "pain_flag": workout_set.pain_flag,
                    "notes": workout_set.notes,
                    "raw_source": raw_text,
                    "extraction_confidence": confidence,
                },
            )
            result.inserted_sets += 1

        if accepted_bodyweight is not None:
            entry, confidence = accepted_bodyweight
            conn.execute(
                _INSERT_BODYWEIGHT,
                {
                    "logged_at": resolve_logged_at(entry.logged_at_local, session_date),
                    "weight_kg": entry.weight_kg,
                    "body_fat_pct": entry.body_fat_pct,
                    "notes": entry.notes,
                    "raw_source": raw_text,
                    "extraction_confidence": confidence,
                },
            )
            result.inserted_bodyweight += 1

    return result
