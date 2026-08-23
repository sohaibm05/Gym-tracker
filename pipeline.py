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
from sqlalchemy.pool import NullPool
from sqlalchemy.engine import Connection, Engine

import muscle_groups

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


def env(name: str, default: str) -> str:
    """Environment value, treating an empty or blank one as absent.

    os.getenv returns "" for a variable that exists but has no value, so the
    default never applies and float("") raises at import. Hosting dashboards
    make that easy to do by accident, and the resulting crash happens before
    any request is served, with no obvious link to the blank field.
    """
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


# Entries scoring below this are surfaced for manual review and NOT inserted.
CONFIDENCE_THRESHOLD = float(env("CONFIDENCE_THRESHOLD", "0.7"))

# rapidfuzz score (0-100) at or above which a proposed exercise name is treated
# as the same exercise as an existing row.
FUZZY_MATCH_THRESHOLD = float(env("FUZZY_MATCH_THRESHOLD", "85"))

# Groq's Llama chat models (llama-3.3-70b-versatile, llama-3.1-8b-instant) were
# deprecated for free/developer tiers on 2026-06-17. openai/gpt-oss-120b is the
# recommended general-purpose replacement. Re-check console.groq.com/docs/models
# before assuming this is still current.
GROQ_MODEL = env("GROQ_MODEL", "openai/gpt-oss-120b")

# gpt-oss models reason before answering, at "medium" effort by default. Reasoning
# shares the completion budget with the answer, so a long deliberation can leave
# nothing for the JSON - which the server rejects as json_validate_failed with an
# empty failed_generation. Low effort keeps room for both; this is extraction
# from text, not a task needing deep deliberation.
GROQ_REASONING_EFFORT = env("GROQ_REASONING_EFFORT", "low")

# Groq counts a request against the per-minute token limit as the prompt PLUS
# the whole completion budget reserved - spent or not. A flat 8000-token
# reservation on a ~1300-token prompt is therefore a 9337-token request against
# an 8000 TPM ceiling: rejected with a 413 before a single token is generated,
# on an entry whose answer needs well under 2000. So the reservation is sized
# per entry (see completion_budget) and held under this ceiling.
GROQ_TPM_LIMIT = int(env("GROQ_TPM_LIMIT", "8000"))

# Upper and lower bounds on that per-entry reservation. The floor exists because
# a budget too small to hold reasoning plus the JSON truncates the answer, which
# surfaces as a confusing parse failure rather than an honest "too big".
GROQ_MAX_COMPLETION_TOKENS = int(env("GROQ_MAX_COMPLETION_TOKENS", "8000"))
GROQ_MIN_COMPLETION_TOKENS = int(env("GROQ_MIN_COMPLETION_TOKENS", "1200"))

# Longest pause allowed before the retry when the server reports a rate limit.
# Bounded because a web request cannot sit blocked indefinitely - Vercel cuts
# the function off at 60s regardless.
GROQ_MAX_RETRY_WAIT_SECONDS = float(env("GROQ_MAX_RETRY_WAIT_SECONDS", "20"))

# Single fixed timezone: this is a single-user personal tool, so local wall-clock
# time in the journal is always interpreted in this zone and stored as UTC.
LOCAL_TIMEZONE = env("LOCAL_TIMEZONE", "UTC")

# Wall-clock time assumed for a session when the text carries no time marker.
DEFAULT_SESSION_HOUR = int(env("DEFAULT_SESSION_HOUR", "18"))

# Window used by the /log duplicate-submission guard.
DUPLICATE_WINDOW_MINUTES = int(env("DUPLICATE_WINDOW_MINUTES", "5"))

# Serverless platforms hand each request its own short-lived process, so a
# connection pool held between requests is never reused and only consumes
# Postgres connection slots. Vercel sets VERCEL=1 itself; SERVERLESS is the
# manual override for anywhere else.
SERVERLESS = bool(os.getenv("VERCEL")) or os.getenv("SERVERLESS", "").strip().lower() in {
    "1", "true", "yes", "on",
}

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
You extract structured training data from messy free-text gym journal entries:
voice-to-text style, with typos, missing punctuation, informal times and personal
commentary mixed in with the numbers.

Return ONLY this JSON object:

{"sets": [{"exercise_name": "Chest Bench Press", "weight_kg": 24.0, "reps": 12,
"cheat_reps": 0, "set_number": 2, "is_warmup": false, "is_dropset": false,
"pain_flag": false, "notes": "almost died", "logged_at_local":
"2026-08-14T18:20:00", "raw_span": "Chest bench press 24kg 12 reps"}],
"bodyweight": {"weight_kg": 82.4, "body_fat_pct": null, "notes": null,
"logged_at_local": "2026-08-14T07:00:00", "raw_span": "was 82.4kg this morning"}}

"bodyweight" is null when the entry gives no bodyweight.

Rules:
- One object per SET performed. "3 sets of 10 at 40kg" is three set objects.
- exercise_name: title case, typos fixed ("ches bent prees" -> "Chest Bench
  Press"). Use the same name for the same movement throughout the entry.
- weight_kg: per-side load as written. "12.5kg each hand" -> 12.5. "each hand" /
  "per hand" / "each side", written once, still applies to every LATER set of
  that exercise - keep reporting the per-hand number.
- set_number: counts within that exercise from 1, in the order written.
- cheat_reps: reps in THIS set completed with cheating, momentum or assistance
  ("10 reps 3 were cheat" -> reps 10, cheat_reps 3; also "last 2 were assisted",
  "2 forced reps", "final rep was a grinder with help"). Default 0, never more
  than reps.
- is_warmup: true when the text says warm up / warmup / "warm up ish" / similar.
- is_dropset: true only when a drop set is explicitly described.
- pain_flag: true on ANY mention of pain, discomfort, injury, tweak, strain,
  soreness that reads as a problem, or "felt something". Effort is NOT pain -
  "almost died", "brutal", "killed me", "barely got it", "struggled", "tough",
  "burned" describe difficulty, so leave those false. If the mention names an
  exercise, flag every working set of it. If it names none ("shoulder pain
  noticed toward the end"), flag the exercise described most recently BEFORE the
  mention - a body part is not an exercise, so "shoulder pain" does not mean the
  shoulder press. Never spread one mention across every exercise in the entry.
- logged_at_local: SESSION_DATE combined with any time marker in the text,
  formatted "YYYY-MM-DDTHH:MM:SS", no timezone. A bare time with no am/pm inside
  a workout sequence is afternoon or evening: "4:35" -> 16:35, "6:20ish" ->
  18:20. Read a time as morning only when the text says so ("this morning",
  "am", "before work"). null when no marker applies to a set.
- raw_span: the exact substring of the input this set was read from, copied
  verbatim, never paraphrased. It is used to verify the extraction.
- NEVER invent data. A weight, rep count or exercise name that is not in the
  text is null - never a plausible guess. Missing fields are handled
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
    # Which guard suppressed the write: "recent_submission" when the same text
    # was already saved inside the window, "already_committed" when this exact
    # preview was confirmed twice. They need different wording, because only the
    # first one is something the user might want to override.
    duplicate_reason: str = ""
    replaced: Optional[dict[str, int]] = None
    error: Optional[str] = None

    @property
    def total_inserted(self) -> int:
        return self.inserted_sets + self.inserted_bodyweight


@dataclass
class PreparedEntry:
    """A parsed, scored entry with nothing yet written.

    The web app shows this to the user and only writes once they confirm, so
    `prepare_entry` must stay free of inserts. It round-trips through the
    browser between those two requests via `to_dict`/`from_dict`, which is what
    keeps the confirm step from paying for a second extraction.
    """

    raw_text: str
    session_date: date
    accepted_sets: list[tuple[WorkoutSet, float]] = field(default_factory=list)
    accepted_bodyweight: Optional[tuple[BodyweightEntry, float]] = None
    review_items: list[ReviewItem] = field(default_factory=list)
    error: Optional[str] = None
    # Set when an identical entry was saved inside the duplicate window. The
    # extraction is skipped in that case, so the accepted lists stay empty.
    duplicate_of_recent: bool = False
    prior_submission: Optional[dict[str, int]] = None
    # Per-exercise breakdown of what that earlier submission saved, so the
    # duplicate claim can be checked rather than taken on trust.
    prior_detail: list[dict[str, Any]] = field(default_factory=list)
    # What the date already holds, for the preview to show before a replace.
    existing_on_date: Optional[dict[str, int]] = None
    # Database clock at prepare time. The confirm step looks for rows from this
    # text inserted *after* it, which is how a double-tapped confirm is caught
    # even when the user has deliberately overridden the duplicate guard.
    prepared_at: Optional[datetime] = None
    allow_duplicate: bool = False

    @property
    def has_insertable(self) -> bool:
        """True when confirming would actually write something."""
        return bool(self.accepted_sets) or self.accepted_bodyweight is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": 1,
            "raw_text": self.raw_text,
            "session_date": self.session_date.isoformat(),
            "sets": [
                {"set": workout_set.model_dump(), "confidence": confidence}
                for workout_set, confidence in self.accepted_sets
            ],
            "bodyweight": (
                {
                    "entry": self.accepted_bodyweight[0].model_dump(),
                    "confidence": self.accepted_bodyweight[1],
                }
                if self.accepted_bodyweight is not None
                else None
            ),
            "review": [
                {
                    "kind": item.kind,
                    "reason": item.reason,
                    "confidence": item.confidence,
                    "payload": item.payload,
                }
                for item in self.review_items
            ],
            "prepared_at": self.prepared_at.isoformat() if self.prepared_at else None,
            "allow_duplicate": self.allow_duplicate,
            "existing_on_date": self.existing_on_date,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PreparedEntry":
        """Rebuild from `to_dict`, re-validating every row through the models.

        The dict has been through the browser, so nothing in it is trusted:
        each set is re-validated rather than assigned, and each confidence is
        clamped. A payload that no longer validates raises, which the caller
        turns into "start again" rather than an insert of junk.
        """
        if not isinstance(data, dict) or data.get("v") != 1:
            raise ValueError("unrecognised prepared-entry payload")

        def _confidence(value: Any) -> float:
            return round(max(0.0, min(1.0, float(value))), 3)

        accepted_sets = [
            (WorkoutSet.model_validate(row["set"]), _confidence(row["confidence"]))
            for row in data.get("sets") or []
        ]
        raw_bodyweight = data.get("bodyweight")
        accepted_bodyweight = (
            (
                BodyweightEntry.model_validate(raw_bodyweight["entry"]),
                _confidence(raw_bodyweight["confidence"]),
            )
            if raw_bodyweight
            else None
        )
        review_items = [
            ReviewItem(
                kind=str(item.get("kind", "")),
                reason=str(item.get("reason", "")),
                confidence=None if item.get("confidence") is None else _confidence(item["confidence"]),
                payload=item.get("payload") or {},
            )
            for item in data.get("review") or []
        ]
        prepared_at = data.get("prepared_at")
        return cls(
            raw_text=str(data.get("raw_text") or ""),
            session_date=date.fromisoformat(str(data["session_date"])),
            accepted_sets=accepted_sets,
            accepted_bodyweight=accepted_bodyweight,
            review_items=review_items,
            prepared_at=datetime.fromisoformat(prepared_at) if prepared_at else None,
            allow_duplicate=bool(data.get("allow_duplicate")),
            existing_on_date=data.get("existing_on_date"),
        )


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


def resolve_muscle_group(name: str) -> Optional[str]:
    """Primary muscle group for an exercise name, or None when unrecognized.

    Table lookup only - see `muscle_groups` for why this is not asked of the
    LLM. Normalization lives here so `muscle_groups` stays a leaf module with no
    import back into this one.
    """
    return muscle_groups.group_for_tokens(normalize_tokens(name))


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


def local_to_utc(local_dt: datetime, timezone_name: Optional[str] = None) -> datetime:
    """Attach the fixed local timezone to a naive datetime and convert to UTC.

    The zone is resolved at call time, not bound as a default argument. A
    default would freeze whatever LOCAL_TIMEZONE held at import, so a later
    change to it would silently not apply - which matters most for the delete
    window in `_local_day_bounds`, where a stale zone would remove the wrong
    day's rows.
    """
    from datetime import timezone as _tz

    if ZoneInfo is None:  # pragma: no cover
        raise RuntimeError("zoneinfo unavailable; Python 3.9+ required")
    tz = ZoneInfo(timezone_name or LOCAL_TIMEZONE)
    aware = local_dt.replace(tzinfo=tz) if local_dt.tzinfo is None else local_dt
    return aware.astimezone(_tz.utc)


def local_today(timezone_name: Optional[str] = None) -> date:
    """Today's date in the configured local zone, not the server's.

    Every host this deploys to runs its containers on UTC, so `date.today()`
    there is the UTC date - which is still yesterday for part of every local day
    east of Greenwich. Writes and reads already route through LOCAL_TIMEZONE
    (`local_to_utc`, `_local_day_bounds`), so the day boundary has to as well:
    otherwise a late-night session is dated a day early, and at a Sunday
    boundary that files it under the previous week entirely.

    Resolved at call time for the same reason as `local_to_utc`.
    """
    from datetime import timezone as _tz

    if ZoneInfo is None:  # pragma: no cover
        raise RuntimeError("zoneinfo unavailable; Python 3.9+ required")
    tz = ZoneInfo(timezone_name or LOCAL_TIMEZONE)
    return datetime.now(_tz.utc).astimezone(tz).date()


def resolve_logged_at(
    logged_at_local: Optional[str],
    session_date: date,
    timezone_name: Optional[str] = None,
    default_hour: Optional[int] = None,
) -> datetime:
    """Turn the model's local-time guess into a UTC timestamp.

    The model's value is only trusted when it parses AND lands on the session
    date the user supplied. Anything else falls back to the session date at
    `default_hour` — the model does not get to move a workout to another day.
    """
    timezone_name = timezone_name or LOCAL_TIMEZONE
    default_hour = DEFAULT_SESSION_HOUR if default_hour is None else default_hour

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


def local_time_label(
    logged_at_local: Optional[str],
    session_date: date,
    timezone_name: Optional[str] = None,
) -> str:
    """Resolved wall-clock time for a set, in LOCAL_TIMEZONE.

    Prefixed with "~" when the text carried no usable time marker and the
    default session hour was applied, so a missed "4:35" is visible. Shared by
    the CLI dry run and the web preview so the two can never disagree about
    what time a set will be stored at.
    """
    if ZoneInfo is None:  # pragma: no cover
        raise RuntimeError("zoneinfo unavailable; Python 3.9+ required")
    zone_name = timezone_name or LOCAL_TIMEZONE
    local = resolve_logged_at(logged_at_local, session_date, zone_name).astimezone(
        ZoneInfo(zone_name)
    )
    prefix = "" if (logged_at_local or "").strip() else "~"
    return f"{prefix}{local:%H:%M}"


# --------------------------------------------------------------------------
# Extraction (Groq)
# --------------------------------------------------------------------------


class ExtractionError(RuntimeError):
    """Raised when extraction fails after the retry."""


# Characters per token, for sizing only. Prose runs about 4; the JSON coming
# back is punctuation-dense and runs nearer 3. Deliberately pessimistic - an
# over-estimate costs a little headroom, an under-estimate truncates an answer.
_CHARS_PER_TOKEN = 3.0

# Fixed cost of an answer: reasoning at low effort, plus the JSON scaffolding
# around the sets.
_ANSWER_FIXED_TOKENS = 700

# Answer tokens per character of journal text. One terse line ("Chest bench
# press 24kg 12 reps", 30 chars) expands into a set object of roughly 110
# tokens, so the answer is several times the size of the text it came from.
_ANSWER_TOKENS_PER_CHAR = 2.5

# Per-request overhead of the chat template itself (role markers, and for
# gpt-oss the harmony channel scaffolding).
_CHAT_OVERHEAD_TOKENS = 80


def estimate_tokens(text: str) -> int:
    """Rough token count for a string. Budgeting only, never billing."""
    return int(len(text) / _CHARS_PER_TOKEN) + 1


def estimate_prompt_tokens(raw_text: str, session_date: date) -> int:
    """What the request will cost before the completion budget is added."""
    messages = _build_messages(raw_text, session_date)
    return sum(estimate_tokens(m["content"]) for m in messages) + _CHAT_OVERHEAD_TOKENS


def completion_budget(raw_text: str, prompt_tokens: int) -> int:
    """Completion tokens to reserve for one extraction call.

    Sized to the entry rather than fixed flat, then held under the per-minute
    ceiling, because Groq counts the reservation toward TPM whether or not it is
    spent - so an oversized one is refused before generation starts.

    Only one call is fitted into the window, not the call plus its retry. Making
    room for both would mean halving the reservation on exactly the long entries
    that need it most, and a truncated answer costs more than a retry that has
    to wait for the window to roll over - which extract_entities now does.
    """
    ceiling = GROQ_TPM_LIMIT - prompt_tokens
    if ceiling < GROQ_MIN_COMPLETION_TOKENS:
        # The prompt alone leaves no room for a usable answer. Reserving less
        # than the floor would truncate it, which reads downstream as a parse
        # failure and hides the real cause, so send the floor and let the server
        # say plainly that the request is too large. entry_fits_in_window() is
        # what stops extract_entities getting this far in the first place.
        logger.warning(
            "A ~%d-token prompt leaves only %d of the %d TPM limit for the answer, "
            "under the %d floor; sending the floor anyway.",
            prompt_tokens, max(0, ceiling), GROQ_TPM_LIMIT, GROQ_MIN_COMPLETION_TOKENS,
        )
        return GROQ_MIN_COMPLETION_TOKENS

    needed = _ANSWER_FIXED_TOKENS + int(len(raw_text) * _ANSWER_TOKENS_PER_CHAR)
    return max(
        GROQ_MIN_COMPLETION_TOKENS,
        min(needed, GROQ_MAX_COMPLETION_TOKENS, ceiling),
    )


def entry_fits_in_window(prompt_tokens: int) -> bool:
    """Whether an entry this long can be extracted within the TPM limit at all.

    An entry can be long enough that its prompt plus the smallest usable answer
    exceeds the whole per-minute allowance. No reservation size rescues that, so
    it is worth saying before spending a call to be told.
    """
    return prompt_tokens + GROQ_MIN_COMPLETION_TOKENS <= GROQ_TPM_LIMIT


def _build_messages(raw_text: str, session_date: date) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"SESSION_DATE: {session_date.isoformat()}\n\nJOURNAL ENTRY:\n{raw_text}",
        },
    ]


def extract_json_object(content: Optional[str]) -> dict[str, Any]:
    """Parse a JSON object from a model response, tolerating surrounding text.

    Used by the fallback attempt, which runs without JSON mode and so may get
    prose or a fenced block around the object. Scans for the first balanced
    top-level {...}, ignoring braces inside strings.
    """
    if not content or not content.strip():
        raise ValueError("model returned an empty response")
    text_value = content.strip()
    try:
        payload = json.loads(text_value)
    except json.JSONDecodeError:
        start = text_value.find("{")
        if start == -1:
            raise ValueError("no JSON object in the response")
        depth, in_string, escaped, end = 0, False, False, -1
        for index in range(start, len(text_value)):
            char = text_value[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
        if end == -1:
            raise ValueError("unterminated JSON object in the response")
        payload = json.loads(text_value[start:end])
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object, got {type(payload).__name__}")
    return payload


def _extraction_attempts(model: str, budget: Optional[int] = None) -> list[dict[str, Any]]:
    """The two attempts, deliberately different from each other.

    At temperature 0 an identical retry reproduces an identical failure, so a
    repeat of the first call buys nothing against a deterministic error - which
    is exactly what json_validate_failed is. The second attempt therefore drops
    JSON mode and parses the object out of the text instead, so a failure caused
    by the server-side JSON validator has a way through.

    Both carry the same completion budget. The retry is not shrunk up front:
    without JSON mode the model may wrap the object in prose, so it needs no
    less room than the first. extract_entities shrinks it only after a failure
    that says the reservation itself was too large.
    """
    budget = GROQ_MAX_COMPLETION_TOKENS if budget is None else budget
    extra: dict[str, Any] = {"max_completion_tokens": budget}
    # Sent through extra_body rather than a named argument so it works whatever
    # groq SDK version is installed, and is simply ignored by models without it.
    if "gpt-oss" in model and GROQ_REASONING_EFFORT:
        extra["reasoning_effort"] = GROQ_REASONING_EFFORT
    return [
        {"response_format": {"type": "json_object"}, "extra_body": dict(extra)},
        {"extra_body": dict(extra)},
    ]


_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}

# Substrings Groq uses for the per-minute ceiling, matched when the exception
# carries no usable status code - which is what a bare RuntimeError from an
# older SDK version looks like.
_RATE_LIMIT_MARKERS = (
    "rate_limit_exceeded", "rate limit reached", "request too large",
    "tokens per minute", "reduce your message size",
)

# Pause before the retry when the server reports a limit without saying for how
# long, as the 413 "request too large" does.
_DEFAULT_RETRY_WAIT_SECONDS = 5.0


def _sleep(seconds: float) -> None:
    """Indirection so tests can wait for nothing."""
    import time as clock

    clock.sleep(seconds)


def _status_code(exc: Exception) -> Optional[int]:
    """HTTP status off a Groq SDK exception, wherever that version keeps it."""
    for attribute in ("status_code", "code"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int):
            return value
    value = getattr(getattr(exc, "response", None), "status_code", None)
    return value if isinstance(value, int) else None


def is_rate_limit_error(exc: Exception) -> bool:
    """Whether this failure is the token ceiling rather than a bad request.

    413 and 429 both mean it here: 429 is "too many tokens this minute", 413 is
    "this one request reserved more than the whole minute allows".
    """
    if _status_code(exc) in {413, 429}:
        return True
    message = str(exc).lower()
    return any(marker in message for marker in _RATE_LIMIT_MARKERS)


def parse_duration(value: str) -> Optional[float]:
    """Seconds from a Groq duration: "7.66s", "2m59.56s", "1500ms", or bare."""
    text_value = value.strip().lower()
    if not text_value:
        return None
    try:
        return float(text_value)  # a plain Retry-After, already in seconds
    except ValueError:
        pass
    total, matched = 0.0, False
    for amount, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(ms|m|s|h)", text_value):
        total += float(amount) * _UNIT_SECONDS[unit]
        matched = True
    return total if matched else None


def retry_after_seconds(exc: Exception) -> Optional[float]:
    """How long the server asked us to wait, if it said at all."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is not None:
        for name in ("retry-after", "x-ratelimit-reset-tokens"):
            raw = None
            try:
                raw = headers.get(name)
            except (AttributeError, TypeError):
                pass
            if raw:
                parsed = parse_duration(str(raw))
                if parsed is not None:
                    return parsed

    # Falls back to the message, which carries the delay for a 429 ("try again
    # in 7.66s") but not for a 413.
    match = re.search(r"try again in ([\d.]+\s*(?:ms|m|s|h)?)", str(exc), re.I)
    return parse_duration(match.group(1)) if match else None


def reported_tpm_limit(exc: Exception) -> Optional[int]:
    """The limit the server named, if it named one.

    GROQ_TPM_LIMIT is what we believe the tier allows; this is what the tier
    actually enforced. When they disagree the server is right, and saying so
    beats repeating a configured number that is the reason for the failure.
    """
    match = re.search(r"limit\s+(\d[\d,]*)", str(exc), re.I)
    return int(match.group(1).replace(",", "")) if match else None


def _wait_out_rate_limit(exc: Exception) -> float:
    """Pause before the retry, for as long as the server advised. Returns it."""
    advised = retry_after_seconds(exc)
    wait = _DEFAULT_RETRY_WAIT_SECONDS if advised is None else advised
    wait = max(0.0, min(wait, GROQ_MAX_RETRY_WAIT_SECONDS))
    logger.warning("Rate limited by Groq; waiting %.1fs before the retry.", wait)
    _sleep(wait)
    return wait


def extract_entities(
    raw_text: str,
    session_date: date,
    client: Any = None,
    model: str = GROQ_MODEL,
) -> dict[str, Any]:
    """Call Groq and return the parsed object.

    The first attempt uses JSON mode (`response_format={"type": "json_object"}`)
    rather than trusting the prompt alone. The second drops it and parses the
    object out of the text, because the failure JSON mode produces most often -
    the server rejecting an empty generation - repeats identically at
    temperature 0. After both, the caller routes the entry to manual review.
    """
    if client is None:
        client = get_groq_client()

    prompt_tokens = estimate_prompt_tokens(raw_text, session_date)
    if not entry_fits_in_window(prompt_tokens):
        raise ExtractionError(
            f"This entry is too long to extract in one request: about "
            f"{prompt_tokens} prompt tokens, and the per-minute limit is "
            f"{GROQ_TPM_LIMIT} with room needed for the answer on top. Split the "
            f"session into two entries and log them separately."
        )

    budget = completion_budget(raw_text, prompt_tokens)
    logger.debug(
        "Extraction request: ~%d prompt tokens + %d reserved = ~%d against a %d TPM limit.",
        prompt_tokens, budget, prompt_tokens + budget, GROQ_TPM_LIMIT,
    )

    last_error: Optional[Exception] = None
    attempts = _extraction_attempts(model, budget)
    for number, options in enumerate(attempts, start=1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=_build_messages(raw_text, session_date),
                temperature=0,
                **options,
            )
            return extract_json_object(response.choices[0].message.content)
        except Exception as exc:  # noqa: BLE001 - fall through to the next attempt
            last_error = exc
            logger.warning(
                "Extraction attempt %d/%d failed (json_mode=%s): %s",
                number, len(attempts), "response_format" in options, exc,
            )
            if number < len(attempts) and is_rate_limit_error(exc):
                # A token ceiling is a condition of the clock, so an instant
                # retry reproduces it exactly. Wait the window out, and ask for
                # less as well: a 413 means this one reservation was itself over
                # the limit, and no amount of waiting shrinks it.
                _wait_out_rate_limit(exc)
                budget = max(GROQ_MIN_COMPLETION_TOKENS, budget // 2)
                attempts[number]["extra_body"]["max_completion_tokens"] = budget

    if last_error is not None and is_rate_limit_error(last_error):
        served = reported_tpm_limit(last_error)
        advice = (
            f"Set GROQ_TPM_LIMIT to {served}, which is what your tier actually "
            f"enforced, then resubmit."
            if served is not None and served != GROQ_TPM_LIMIT
            else "Wait a minute and resubmit, or raise GROQ_TPM_LIMIT if your "
                 "Groq tier allows more."
        )
        raise ExtractionError(
            f"Groq's per-minute token limit was hit on both attempts. The last one "
            f"asked for about {prompt_tokens} prompt tokens plus a {budget}-token "
            f"reservation, against a configured limit of {GROQ_TPM_LIMIT}. {advice}"
        ) from last_error

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


def get_engine(database_url: Optional[str] = None, serverless: Optional[bool] = None) -> Engine:
    url = database_url or os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")

    if SERVERLESS if serverless is None else serverless:
        # One connection per request, closed on release. Pooling across
        # invocations is impossible when the process does not outlive them, and
        # a retained pool would just hold Postgres slots open for nothing.
        return create_engine(url, poolclass=NullPool, future=True)

    # Long-running process: pool, and pre-ping because free-tier Postgres drops
    # idle connections and a stale handle would otherwise surface as an error.
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

    # An explicit group from the caller wins; otherwise look the name up in the
    # static table. Either may be None, which stores NULL and reports as
    # "Unassigned" rather than a guess.
    if not muscle_group:
        muscle_group = resolve_muscle_group(proposed_name)

    row = conn.execute(
        text(
            """
            INSERT INTO exercises (name, muscle_group)
            VALUES (:name, :muscle_group)
            ON CONFLICT (name) DO UPDATE
                SET muscle_group = COALESCE(exercises.muscle_group,
                                            EXCLUDED.muscle_group)
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


def _local_day_bounds(session_date: date) -> tuple[datetime, datetime]:
    """The UTC window covering one local calendar day."""
    start = local_to_utc(datetime.combine(session_date, time.min))
    end = local_to_utc(datetime.combine(session_date + timedelta(days=1), time.min))
    return start, end


def _count_entries_on(conn: Connection, session_date: date) -> dict[str, int]:
    """How much is already logged against a local date, on a caller's connection."""
    start, end = _local_day_bounds(session_date)
    params = {"start": start, "end": end}
    sets = conn.execute(
        text("SELECT count(*) FROM workout_logs WHERE logged_at >= :start AND logged_at < :end"),
        params,
    ).scalar_one()
    bodyweight = conn.execute(
        text("SELECT count(*) FROM bodyweight_logs "
             "WHERE logged_at >= :start AND logged_at < :end"),
        params,
    ).scalar_one()
    return {"sets": int(sets), "bodyweight": int(bodyweight)}


def count_entries_for_date(engine: Engine, session_date: date) -> dict[str, int]:
    """How much is already logged against a local date."""
    with engine.connect() as conn:
        return _count_entries_on(conn, session_date)


def delete_entries_for_date(conn: Connection, session_date: date) -> dict[str, int]:
    """Remove every log row on a local date. Caller owns the transaction.

    Takes a Connection rather than an Engine so the delete and the insert that
    replaces it commit together: a failure mid-way must never leave the day
    emptied with nothing put back.
    """
    start, end = _local_day_bounds(session_date)
    params = {"start": start, "end": end}
    sets = conn.execute(
        text("DELETE FROM workout_logs WHERE logged_at >= :start AND logged_at < :end"), params
    ).rowcount
    bodyweight = conn.execute(
        text("DELETE FROM bodyweight_logs WHERE logged_at >= :start AND logged_at < :end"), params
    ).rowcount
    return {"sets": int(sets or 0), "bodyweight": int(bodyweight or 0)}


def _submission_clauses(
    raw_text: str,
    window_minutes: int,
    since: Optional[datetime],
    session_date: Optional[date],
) -> tuple[str, dict[str, Any]]:
    """The WHERE fragment identifying rows written from one journal entry.

    Matching is exact text equality - `raw_source` is TEXT, so nothing is
    truncated and two different entries cannot collide here.
    """
    params: dict[str, Any] = {"raw_source": raw_text}
    clauses = ["raw_source = :raw_source"]

    if since is not None:
        clauses.append("created_at >= :since")
        params["since"] = since
    else:
        clauses.append("created_at >= now() - CAST(:window AS interval)")
        params["window"] = f"{int(window_minutes)} minutes"

    if session_date is not None:
        # Scoped to the day being logged. Without this, pasting the same short
        # entry ("rest day, weighed 82.4") against two dates in one sitting
        # reads as a double-tap and the second date is silently dropped.
        start, end = _local_day_bounds(session_date)
        clauses.append("logged_at >= :day_start AND logged_at < :day_end")
        params["day_start"] = start
        params["day_end"] = end

    return " AND ".join(clauses), params


def _find_submission_on(
    conn: Connection,
    raw_text: str,
    window_minutes: int = DUPLICATE_WINDOW_MINUTES,
    since: Optional[datetime] = None,
    session_date: Optional[date] = None,
) -> Optional[dict[str, int]]:
    """Counts for an identical entry already inserted, else None.

    `since` narrows the search to rows created after a specific instant instead
    of the rolling window. The confirm step passes the database clock read at
    preview time, which identifies rows written *by this preview* and nothing
    older - so a double-tapped confirm is caught even when the user has
    deliberately overridden the rolling-window guard.
    """
    where, params = _submission_clauses(raw_text, window_minutes, since, session_date)
    sets = conn.execute(
        text(f"SELECT count(*) FROM workout_logs WHERE {where}"), params
    ).scalar_one()
    bodyweight = conn.execute(
        text(f"SELECT count(*) FROM bodyweight_logs WHERE {where}"), params
    ).scalar_one()

    if not sets and not bodyweight:
        return None
    return {"inserted_sets": int(sets), "inserted_bodyweight": int(bodyweight)}


def _describe_submission_on(
    conn: Connection,
    raw_text: str,
    session_date: Optional[date] = None,
    window_minutes: int = DUPLICATE_WINDOW_MINUTES,
) -> list[dict[str, Any]]:
    """What the matched entry actually saved, per exercise.

    A guard that says "duplicate" and shows nothing is impossible to argue
    with - and impossible to trust when the sets it claims to have saved are
    not the ones you meant to log. This is the evidence for the claim.
    """
    where, params = _submission_clauses(raw_text, window_minutes, None, session_date)
    rows = conn.execute(
        text(
            f"""
            SELECT e.name AS exercise, count(*) AS sets, min(w.logged_at) AS first_logged
            FROM workout_logs w
            JOIN exercises e ON e.exercise_id = w.exercise_id
            WHERE {where}
            GROUP BY e.name
            ORDER BY min(w.logged_at), e.name
            """
        ),
        params,
    ).all()
    return [
        {"exercise": row.exercise, "sets": int(row.sets), "first_logged": row.first_logged}
        for row in rows
    ]


def find_recent_submission(
    engine: Engine,
    raw_text: str,
    window_minutes: int = DUPLICATE_WINDOW_MINUTES,
    since: Optional[datetime] = None,
    session_date: Optional[date] = None,
) -> Optional[dict[str, int]]:
    """Return counts for an identical entry already inserted, else None.

    Backs the /log duplicate guard: Render's free tier cold-starts for 30-50s,
    which is exactly when a user double-taps submit.
    """
    with engine.connect() as conn:
        return _find_submission_on(conn, raw_text, window_minutes, since, session_date)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def prepare_entry(
    raw_text: str,
    session_date: date,
    engine: Optional[Engine] = None,
    client: Any = None,
    check_duplicates: bool = False,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
    allow_duplicate: bool = False,
) -> PreparedEntry:
    """Extract, validate and score one entry. Writes nothing.

    This is the half of the pipeline the web app runs first, so the user can
    see what was read out of their text before any of it reaches the database.
    `commit_entry` does the writing. Keep the two apart: an insert added here
    would silently defeat the preview.

    Passing `engine` (with `check_duplicates`) also collects what the database
    already holds - a recent identical submission, and the row counts on the
    session date - so the preview can say what a replace would discard.
    """
    raw_text = (raw_text or "").strip()
    if not raw_text:
        return PreparedEntry(
            raw_text="", session_date=session_date, error="Empty entry — nothing to parse."
        )

    prepared = PreparedEntry(
        raw_text=raw_text, session_date=session_date, allow_duplicate=allow_duplicate
    )

    if engine is not None:
        # One connection for every read the preview needs. Serverless runs with
        # NullPool, so each of these would otherwise be a fresh Postgres
        # connection.
        with engine.connect() as conn:
            prepared.prepared_at = conn.execute(text("SELECT now()")).scalar_one()
            prepared.existing_on_date = _count_entries_on(conn, session_date)
            if check_duplicates and not allow_duplicate:
                prior = _find_submission_on(conn, raw_text, session_date=session_date)
                if prior is not None:
                    logger.info("Duplicate submission held for confirmation (%s)", prior)
                    prepared.duplicate_of_recent = True
                    prepared.prior_submission = prior
                    prepared.prior_detail = _describe_submission_on(
                        conn, raw_text, session_date=session_date
                    )
                    # Deliberately before the extraction: the whole point of the
                    # guard is to not pay for a second model call.
                    return prepared

    try:
        payload = extract_entities(raw_text, session_date, client=client)
    except ExtractionError as exc:
        logger.error("Extraction failed: %s", exc)
        prepared.error = "Extraction failed after one retry — nothing was parsed."
        prepared.review_items.append(
            ReviewItem("extraction", str(exc), None, {"raw_text": raw_text})
        )
        return prepared

    scored_sets, scored_bodyweight, review = validate_extraction(payload, raw_text)
    prepared.review_items.extend(review)

    for workout_set, confidence in scored_sets:
        if confidence < confidence_threshold:
            prepared.review_items.append(
                ReviewItem(
                    "workout_set",
                    f"confidence {confidence:.2f} below threshold {confidence_threshold:.2f}",
                    confidence,
                    workout_set.model_dump(),
                )
            )
        else:
            prepared.accepted_sets.append((workout_set, confidence))

    if scored_bodyweight is not None:
        entry, confidence = scored_bodyweight
        if confidence < confidence_threshold:
            prepared.review_items.append(
                ReviewItem(
                    "bodyweight",
                    f"confidence {confidence:.2f} below threshold {confidence_threshold:.2f}",
                    confidence,
                    entry.model_dump(),
                )
            )
        else:
            prepared.accepted_bodyweight = (entry, confidence)

    return prepared


def commit_entry(
    prepared: PreparedEntry,
    engine: Optional[Engine] = None,
    replace_existing: bool = False,
) -> PipelineResult:
    """Write an already-prepared entry. The only half that touches the data.

    Everything the preview showed the user is carried in `prepared`, so this
    never re-runs the extraction and can never write something the preview did
    not display.
    """
    result = PipelineResult(review_items=list(prepared.review_items))
    if prepared.error:
        result.error = prepared.error
        return result
    if prepared.duplicate_of_recent:
        prior = prepared.prior_submission or {}
        result.duplicate_of_recent = True
        result.duplicate_reason = "recent_submission"
        result.inserted_sets = int(prior.get("inserted_sets", 0))
        result.inserted_bodyweight = int(prior.get("inserted_bodyweight", 0))
        return result
    if not prepared.has_insertable:
        return result

    if engine is None:
        engine = get_engine()

    with engine.begin() as conn:
        if prepared.prepared_at is not None:
            # Rows from this text written since the preview was built can only
            # have come from confirming this same preview, so a second confirm
            # is a double-tap however it was reached.
            already = _find_submission_on(
                conn,
                prepared.raw_text,
                since=prepared.prepared_at,
                session_date=prepared.session_date,
            )
            if already is not None:
                logger.info("Confirm replayed; nothing re-inserted (%s)", already)
                result.duplicate_of_recent = True
                result.duplicate_reason = "already_committed"
                result.inserted_sets = int(already.get("inserted_sets", 0))
                result.inserted_bodyweight = int(already.get("inserted_bodyweight", 0))
                return result

        if replace_existing:
            # Only reached when there is something to put back - the
            # has_insertable check above means a failed extraction or an
            # all-review entry never empties the day.
            result.replaced = delete_entries_for_date(conn, prepared.session_date)
            logger.info("Replaced %s on %s", result.replaced, prepared.session_date)

        known = load_exercise_names(conn)
        for workout_set, confidence in prepared.accepted_sets:
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
                    "logged_at": resolve_logged_at(
                        workout_set.logged_at_local, prepared.session_date
                    ),
                    "weight_kg": workout_set.weight_kg,
                    "reps": workout_set.reps,
                    "cheat_reps": workout_set.cheat_reps,
                    "set_number": workout_set.set_number,
                    "is_warmup": workout_set.is_warmup,
                    "is_dropset": workout_set.is_dropset,
                    "pain_flag": workout_set.pain_flag,
                    "notes": workout_set.notes,
                    "raw_source": prepared.raw_text,
                    "extraction_confidence": confidence,
                },
            )
            result.inserted_sets += 1

        if prepared.accepted_bodyweight is not None:
            entry, confidence = prepared.accepted_bodyweight
            conn.execute(
                _INSERT_BODYWEIGHT,
                {
                    "logged_at": resolve_logged_at(entry.logged_at_local, prepared.session_date),
                    "weight_kg": entry.weight_kg,
                    "body_fat_pct": entry.body_fat_pct,
                    "notes": entry.notes,
                    "raw_source": prepared.raw_text,
                    "extraction_confidence": confidence,
                },
            )
            result.inserted_bodyweight += 1

    return result


def process_entry(
    raw_text: str,
    session_date: date,
    engine: Optional[Engine] = None,
    client: Any = None,
    check_duplicates: bool = False,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
    replace_existing: bool = False,
) -> PipelineResult:
    """Prepare and commit one journal entry in a single call.

    What the CLI uses, and what the web app used before the preview step split
    the two halves apart.
    """
    # Resolved lazily on both sides: an empty or unparseable entry has to fail
    # with its own message, not with "DATABASE_URL is not set".
    if engine is None and check_duplicates:
        engine = get_engine()

    prepared = prepare_entry(
        raw_text,
        session_date,
        engine=engine if check_duplicates else None,
        client=client,
        check_duplicates=check_duplicates,
        confidence_threshold=confidence_threshold,
    )
    return commit_entry(prepared, engine=engine, replace_existing=replace_existing)
