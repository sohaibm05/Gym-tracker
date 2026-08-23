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
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable, Mapping, Optional, Sequence

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

# Band immediately below FUZZY_MATCH_THRESHOLD. A new name scoring in here is
# too far from an existing exercise to merge automatically, but close enough
# that a typo is the likeliest explanation - which is exactly the case that
# silently splits one lift's history across two rows.
NEAR_MISS_FLOOR = float(env("NEAR_MISS_FLOOR", "70"))

# Grounding similarity below which a name that WAS saved is still treated as
# weakly supported by the text it was read from. Confidence already penalizes
# this band by 0.15, which on its own does not drop a set with a weight and a
# rep count under CONFIDENCE_THRESHOLD - so without a flag it passes unseen.
WEAK_GROUNDING_SIMILARITY = float(env("WEAK_GROUNDING_SIMILARITY", "0.8"))

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
class NameFlag:
    """A saved exercise name that does not look right - niche, or just wrong.

    Distinct from `ReviewItem` in the one way that matters: the row this refers
    to WAS inserted. A flag is an amber "look at this", not a rejection, so it
    never withholds data. Raised only when the name creates a NEW exercise,
    because that is the moment a bad name becomes a permanent row; re-logging an
    exercise that already exists is not suspicious and must not nag.
    """

    exercise_name: str
    reason: str  # "near_miss" | "weak_grounding" | "unrecognized"
    detail: str
    nearest_name: Optional[str] = None
    score: Optional[float] = None


@dataclass
class PipelineResult:
    inserted_sets: int = 0
    inserted_bodyweight: int = 0
    review_items: list[ReviewItem] = field(default_factory=list)
    exercises_created: list[str] = field(default_factory=list)
    name_flags: list[NameFlag] = field(default_factory=list)
    exercises_matched: list[tuple[str, str]] = field(default_factory=list)
    duplicate_of_recent: bool = False
    # What the matched earlier save actually produced, per exercise. Empty
    # unless `duplicate_of_recent`; it is the evidence for that claim, so the
    # user can check a match that looks wrong rather than having to believe it.
    duplicate_evidence: list[dict[str, Any]] = field(default_factory=list)
    replaced: Optional[dict[str, int]] = None
    error: Optional[str] = None
    # Rows the user unticked on the review screen. Distinct from review_items,
    # which are rows the pipeline itself held back.
    skipped_sets: int = 0
    skipped_bodyweight: int = 0

    @property
    def total_inserted(self) -> int:
        return self.inserted_sets + self.inserted_bodyweight


# --------------------------------------------------------------------------
# Editable draft — what the review screen shows before anything is written
# --------------------------------------------------------------------------

# The editable columns of each row, in the order the review screen lays them
# out. `raw_span` is carried but never edited: it is the evidence the row was
# read from, and rewriting it would rewrite the audit trail.
SET_COLUMNS: tuple[str, ...] = (
    "exercise_name",
    "weight_kg",
    "reps",
    "cheat_reps",
    "set_number",
    "logged_at_local",
    "muscle_group",
    "is_warmup",
    "is_dropset",
    "pain_flag",
    "notes",
    "raw_span",
)

BODYWEIGHT_COLUMNS: tuple[str, ...] = (
    "weight_kg",
    "body_fat_pct",
    "logged_at_local",
    "notes",
    "raw_span",
)


@dataclass
class DraftIssue:
    """Something wrong with a drafted row, addressed to the person reviewing it.

    `blocking` separates the two kinds the review screen treats differently:
    a blocking issue is one the database would reject, so the row cannot be
    saved until it is fixed; a non-blocking one is missing or weakly grounded
    information, which is flagged but savable — a set with no recorded weight
    is still a set that happened.
    """

    message: str
    field: Optional[str] = None  # column it belongs to; None = the whole row
    blocking: bool = False


@dataclass
class DraftRow:
    """One prospective database row, as extracted and possibly since edited."""

    kind: str  # "workout_set" | "bodyweight"
    values: dict[str, Any] = field(default_factory=dict)
    confidence: Optional[float] = None
    issues: list[DraftIssue] = field(default_factory=list)
    include: bool = True
    edited: bool = False  # a person changed a value on the review screen
    # Which columns they changed. Muscle group needs this: a value the user
    # chose may overwrite what is on record for an exercise, a suggestion the
    # app filled in must not.
    edited_fields: frozenset[str] = frozenset()
    # Typed in on the review screen rather than read out of the entry. Such a row
    # has no source text behind it, so the grounding checks do not apply to it.
    added: bool = False
    # An added row nobody has filled in yet: not a row at all, so it is neither
    # saved nor complained about.
    blank: bool = False
    # The exact wording `process_entry` used before the review screen existed,
    # kept so the CLI and the unattended path still report failures the same way.
    validation_error: Optional[str] = None

    @property
    def blocking(self) -> bool:
        return any(issue.blocking for issue in self.issues)

    @property
    def flagged(self) -> bool:
        return bool(self.issues)

    def issues_for(self, column: str) -> list[DraftIssue]:
        return [issue for issue in self.issues if issue.field == column]

    @property
    def row_issues(self) -> list[DraftIssue]:
        """Issues that belong to no single column."""
        return [issue for issue in self.issues if issue.field is None]

    def legacy_reason(self, confidence_threshold: float) -> str:
        """Why an unattended run would have held this row back."""
        if self.validation_error:
            return self.validation_error
        return (
            f"confidence {self.confidence or 0.0:.2f} below "
            f"threshold {confidence_threshold:.2f}"
        )


@dataclass
class EntryDraft:
    """Everything one journal entry would write, before any of it is written."""

    raw_text: str
    session_date: date
    sets: list[DraftRow] = field(default_factory=list)
    bodyweight: Optional[DraftRow] = None
    # Fragments of the extraction that are not editable rows at all — a `sets`
    # key that came back as a string, say. Nothing can be done with these on the
    # review screen, so they pass straight through to the result.
    review_items: list[ReviewItem] = field(default_factory=list)
    error: Optional[str] = None
    replace_existing: bool = False

    @property
    def rows(self) -> list[DraftRow]:
        return self.sets + ([self.bodyweight] if self.bodyweight is not None else [])

    @property
    def included(self) -> list[DraftRow]:
        return [row for row in self.rows if row.include and not row.blank]

    @property
    def flagged_count(self) -> int:
        return sum(1 for row in self.rows if row.flagged)

    @property
    def blocked_count(self) -> int:
        return sum(1 for row in self.included if row.blocking)

    @property
    def is_empty(self) -> bool:
        return not self.rows

    @property
    def ready(self) -> bool:
        """True when saving would not be rejected by the database."""
        return self.error is None and self.blocked_count == 0


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


def nearest_existing_exercise(
    proposed: str, existing_names: Iterable[str]
) -> tuple[Optional[str], float]:
    """The closest existing name and its score, whatever that score is.

    Split out from `find_matching_exercise` because the near-miss flag needs the
    score precisely when it is too low to merge on.
    """
    best_name: Optional[str] = None
    best_score = 0.0
    for candidate in existing_names:
        score = name_match_score(proposed, candidate)
        if score > best_score:
            best_name, best_score = candidate, score
    return best_name, best_score


def find_matching_exercise(
    proposed: str,
    existing_names: Iterable[str],
    threshold: float = FUZZY_MATCH_THRESHOLD,
) -> Optional[str]:
    """Return the best existing name at/above `threshold`, else None."""
    best_name, best_score = nearest_existing_exercise(proposed, existing_names)
    return best_name if best_score >= threshold else None


def _content_tokens(name: str) -> list[str]:
    """Sorted name tokens with equipment/muscle qualifiers removed.

    Qualifiers are exactly what `name_match_score` already forgives, so they
    must not be what makes a name look suspicious.
    """
    return sorted(
        token for token in normalize_tokens(name)
        if token not in _QUALIFIER_TOKENS_SINGULAR
    )


def _is_probable_typo(proposed: str, existing: str, per_token_ratio: float = 80.0) -> bool:
    """True when two names differ by misspelling rather than by movement.

    The distinction the near-miss flag lives or dies on. "Bech Press" against
    "Chest Bench Press" is a typo splitting one lift in two. "Incline Bench
    Press" against "Chest Bench Press" is two different lifts that SHOULD be
    separate rows - `QUALIFIER_TOKENS` already documents `incline` as marking a
    genuinely different movement, so flagging it would contradict the matcher.

    Tokens shared exactly are paired off, then what is left is matched up by
    character similarity - that pairing is the misspelling. Only tokens that
    find no partner at all are allowed to be qualifiers, which is what lets
    "Bech Press" pair against "Chest Bench Press" (the spare "chest" is an
    equipment/muscle word) while "Incline Bench Press" does not (the spare
    "incline" is not). Qualifiers are deliberately NOT stripped up front: doing
    so deletes the partner a typo inside one ("Shoulderr Press") needs to be
    compared against.
    """
    shared = Counter(normalize_tokens(proposed)) & Counter(normalize_tokens(existing))
    leftover_left = sorted((Counter(normalize_tokens(proposed)) - shared).elements())
    unpaired_right = sorted((Counter(normalize_tokens(existing)) - shared).elements())

    unpaired_left: list[str] = []
    paired = 0
    for token in leftover_left:
        best_index, best_score = None, per_token_ratio
        for index, candidate in enumerate(unpaired_right):
            score = fuzz.ratio(token, candidate)
            if score >= best_score:
                best_index, best_score = index, score
        if best_index is None:
            unpaired_left.append(token)
        else:
            unpaired_right.pop(best_index)
            paired += 1

    if paired == 0:
        return False
    # A leftover with no partner is only forgivable as an equipment/muscle word;
    # anything else is a genuine variation and must not read as a misspelling.
    return all(token in _QUALIFIER_TOKENS_SINGULAR
               for token in unpaired_left + unpaired_right)


def flag_exercise_name(
    proposed: str,
    existing_names: Iterable[str],
    raw_span: Optional[str] = None,
    raw_text: str = "",
    near_miss_floor: float = NEAR_MISS_FLOOR,
    merge_threshold: float = FUZZY_MATCH_THRESHOLD,
    weak_grounding: float = WEAK_GROUNDING_SIMILARITY,
) -> Optional["NameFlag"]:
    """Amber-flag a newly created exercise name that does not look right.

    Three checks, in descending order of how much damage the case does, and at
    most one flag is returned so the report stays readable:

      1. `near_miss`      - close to an existing exercise but under the merge
                            threshold. A typo here splits one lift's history in
                            two, and every later chart inherits the split.
      2. `weak_grounding` - the name is poorly supported by the text it was
                            read from, i.e. the model may have tidied it into
                            something that was never written.
      3. `unrecognized`   - no muscle-group table, muscle word or movement verb
                            knows this name. Either genuinely niche, or wrong.

    Returns None when the name looks ordinary. Pure: no database, no network.
    """
    nearest, score = nearest_existing_exercise(proposed, existing_names)
    if (nearest is not None
            and near_miss_floor <= score < merge_threshold
            and _is_probable_typo(proposed, nearest)):
        return NameFlag(
            exercise_name=proposed,
            reason="near_miss",
            detail=(f"close to existing {nearest!r} (score {score:.0f}, merge needs "
                    f"{merge_threshold:.0f}) - if these are the same lift, the history "
                    f"is now split across two exercises"),
            nearest_name=nearest,
            score=score,
        )

    # Same haystack rule `compute_confidence` uses: the span if the model gave
    # one, otherwise the whole entry.
    haystack = (raw_span or "").strip() or (raw_text or "")
    if haystack:
        # Take the better of the full name and the name stripped of qualifiers:
        # the model routinely adds "Barbell"/"Dumbbell" to a name the text wrote
        # bare, and that is normalization, not invention.
        similarity = max(
            _grounding_similarity(proposed, haystack),
            _grounding_similarity(" ".join(_content_tokens(proposed)), haystack),
        )
        if similarity < weak_grounding:
            return NameFlag(
                exercise_name=proposed,
                reason="weak_grounding",
                detail=(f"only {similarity:.0%} similar to the text it was read from "
                        f"({haystack.strip()[:60]!r}) - check the name was actually "
                        f"written, not inferred"),
                score=similarity,
            )

    if resolve_muscle_group(proposed) is None:
        return NameFlag(
            exercise_name=proposed,
            reason="unrecognized",
            detail=("no muscle group, muscle word or movement verb recognized - either "
                    "a niche movement worth adding to muscle_groups.py, or not an "
                    "exercise name at all"),
        )

    return None


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


def score_set(
    item: dict[str, Any],
    raw_text: str,
) -> tuple[Optional[WorkoutSet], float, Optional[ValidationError]]:
    """Validate and score one raw set object.

    The single place a set is turned into a model and given a confidence, so the
    review screen, the CLI dry run and the insert path can never disagree about
    what is wrong with a row. Returns (model, confidence, error); exactly one of
    model and error is None.
    """
    try:
        workout_set = WorkoutSet.model_validate(item)
    except ValidationError as exc:
        return None, compute_confidence(None, None, None, None, raw_text, validation_ok=False), exc

    confidence = compute_confidence(
        workout_set.exercise_name,
        workout_set.weight_kg,
        workout_set.reps,
        workout_set.raw_span,
        raw_text,
    )
    return workout_set, confidence, None


def score_bodyweight(
    item: dict[str, Any],
    raw_text: str,
) -> tuple[Optional[BodyweightEntry], float, Optional[ValidationError]]:
    """`score_set`, for the bodyweight reading."""
    try:
        entry = BodyweightEntry.model_validate(item)
    except ValidationError as exc:
        return None, 0.0, exc
    return entry, compute_bodyweight_confidence(entry, raw_text), None


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
        workout_set, confidence, exc = score_set(item, raw_text)
        if exc is not None:
            review.append(
                ReviewItem(
                    "workout_set",
                    f"failed validation: {_short_errors(exc)}",
                    confidence,
                    item,
                )
            )
            continue
        scored_sets.append((workout_set, confidence))

    scored_bodyweight: Optional[tuple[BodyweightEntry, float]] = None
    raw_bodyweight = payload.get("bodyweight")
    if isinstance(raw_bodyweight, dict):
        entry, bodyweight_confidence, exc = score_bodyweight(raw_bodyweight, raw_text)
        if exc is not None:
            review.append(
                ReviewItem("bodyweight", f"failed validation: {_short_errors(exc)}", 0.0, raw_bodyweight)
            )
        else:
            scored_bodyweight = (entry, bodyweight_confidence)
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
# Draft building (extraction -> editable rows, still nothing written)
# --------------------------------------------------------------------------


def _clean_message(message: str) -> str:
    """Pydantic prefixes custom validator failures; the prefix means nothing here."""
    return re.sub(r"^(Value|Assertion) error, ", "", message).strip()


def _named_column(message: str, columns: Sequence[str]) -> Optional[str]:
    """The column a whole-model validation message is about, if it names one.

    Pydantic reports a `model_validator` failure against no field at all, which
    would leave the form saying a row is unsaveable without marking anything on
    it. These messages are ours, so the column they name is reliable.
    """
    lowered = message.lower()
    # Longest first, so "cheat_reps exceeds reps" points at cheat_reps, the
    # column that is actually out of range, rather than the one it is compared to.
    matches = sorted((c for c in columns if c in lowered), key=len, reverse=True)
    return matches[0] if matches else None


def _field_issues(exc: ValidationError, columns: Sequence[str]) -> list[DraftIssue]:
    """Turn a validation failure into per-column issues the form can highlight."""
    issues: list[DraftIssue] = []
    for error in exc.errors():
        message = _clean_message(error["msg"])
        location = str(error["loc"][0]) if error["loc"] else None
        column = location if location in columns else _named_column(message, columns)
        issues.append(DraftIssue(message, column, blocking=True))
    return issues


def _column_defaults(model: type[BaseModel], columns: Sequence[str]) -> dict[str, Any]:
    """What each column falls back to when the extraction leaves it out.

    Read off the models rather than restated, so the review screen shows the
    value the database would actually store — `cheat_reps` absent means 0, not
    null, and null is what the column rejects.
    """
    defaults: dict[str, Any] = {}
    for column in columns:
        info = model.model_fields.get(column)
        defaults[column] = (
            None if info is None or info.is_required()
            else info.get_default(call_default_factory=True)
        )
    return defaults


SET_DEFAULTS = _column_defaults(WorkoutSet, SET_COLUMNS)
BODYWEIGHT_DEFAULTS = _column_defaults(BodyweightEntry, BODYWEIGHT_COLUMNS)


def _pick(
    values: dict[str, Any], columns: Sequence[str], defaults: dict[str, Any]
) -> dict[str, Any]:
    """Keep only the editable columns, filling omissions with the column default.

    A missing key and an explicit null are treated alike: models routinely emit
    one or the other for a flag they had nothing to say about, and neither means
    "store null in a NOT NULL column".
    """
    picked: dict[str, Any] = {}
    for column in columns:
        value = values.get(column)
        picked[column] = defaults[column] if value is None else value
    return picked


def _is_blank(values: dict[str, Any], defaults: dict[str, Any]) -> bool:
    """True when nothing has been entered — every column still holds its default.

    Compared as text as well as by value, because anything that has been through
    a form arrives as a string: an untouched cheat-rep box submits "0", not 0,
    and an empty slot that failed this check would fail validation on its blank
    exercise name instead of being ignored.
    """
    return all(
        value == defaults[column] or str(value).strip() == str(defaults[column])
        for column, value in values.items()
    )


def suggest_muscle_group(
    exercise_name: str,
    known_groups: Optional[Mapping[str, Optional[str]]] = None,
) -> Optional[str]:
    """The muscle group to offer for an exercise name, or None if nothing fits.

    What is already on record wins over the table. `get_or_create_exercise`
    never revises a stored group on its own, so offering a table answer that
    disagrees with one would show the user a value that saving quietly ignores.
    The name is fuzzy-matched the same way the insert path matches it, so a
    reworded name still finds its own row.
    """
    if known_groups:
        matched = find_matching_exercise(exercise_name, known_groups.keys())
        if matched is not None and known_groups[matched]:
            return known_groups[matched]
    return resolve_muscle_group(exercise_name)


def draft_set_row(
    values: dict[str, Any],
    raw_text: str,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
    edited: bool = False,
    added: bool = False,
    edited_fields: frozenset[str] = frozenset(),
    known_groups: Optional[Mapping[str, Optional[str]]] = None,
    resuggest_muscle_group: bool = False,
) -> DraftRow:
    """Score one set and describe everything a person should look at before saving.

    `added` marks a row typed in on the review screen rather than read out of the
    entry. There is no source text behind such a row, so the grounding checks —
    which ask how well a reading is supported by what was written — would be
    measuring nothing. Missing weights and reps are still worth pointing out.
    """
    row = DraftRow(
        "workout_set",
        _pick(values, SET_COLUMNS, SET_DEFAULTS),
        edited=edited,
        added=added,
        edited_fields=edited_fields,
    )
    if _is_blank(row.values, SET_DEFAULTS):
        # An empty slot the user has not filled in. Not a row, so it is neither
        # saved nor complained about.
        row.blank = True
        return row

    workout_set, confidence, exc = score_set(row.values, raw_text)
    row.confidence = 1.0 if added else confidence

    if exc is not None:
        row.validation_error = f"failed validation: {_short_errors(exc)}"
        row.issues = _field_issues(exc, SET_COLUMNS)
        return row

    # Show the coerced values, so what the screen displays is what gets stored.
    row.values = _pick(workout_set.model_dump(), SET_COLUMNS, SET_DEFAULTS)

    if workout_set.weight_kg is None:
        row.issues.append(
            DraftIssue("no weight recorded — saved blank unless you fill it in", "weight_kg")
        )
    if workout_set.reps is None:
        row.issues.append(
            DraftIssue("no rep count recorded — saved blank unless you fill it in", "reps")
        )

    # Suggested rather than read: the extraction is never asked for a muscle
    # group. A value already there is kept on the way in, so the rare one a model
    # does volunteer still counts; on the way back from the review screen it is
    # recomputed unless the user took the field over, because by then the value
    # sitting in the box is this function's own last answer — and renaming the
    # exercise has to re-answer it rather than leave the old name's group behind.
    if "muscle_group" not in row.edited_fields:
        current = None if resuggest_muscle_group else row.values.get("muscle_group")
        row.values["muscle_group"] = current or suggest_muscle_group(
            workout_set.exercise_name, known_groups
        )
    _flag_muscle_group(row)

    if added:
        return row

    if _grounding_similarity(workout_set.exercise_name, workout_set.raw_span or raw_text) < 0.5:
        row.issues.append(
            DraftIssue("this name does not closely match the text it was read from",
                       "exercise_name")
        )

    # Only worth saying when nothing more specific already explains the score.
    if confidence < confidence_threshold and not row.issues and not edited:
        row.issues.append(
            DraftIssue(
                f"confidence {confidence:.2f}, below the {confidence_threshold:.2f} "
                "threshold — check it against your entry"
            )
        )
    return row


def _flag_muscle_group(row: DraftRow) -> None:
    """Mark a muscle group that nothing can be done with downstream.

    `insights.volume_by_muscle_group` buckets on this column exactly as stored,
    so a blank one silently becomes "Unassigned" and a typo becomes its own
    bucket. Neither is an error the database would catch, which is why both are
    flagged here rather than left to be discovered in a chart.
    """
    group = (row.values.get("muscle_group") or "").strip()
    if not group:
        row.issues.append(
            DraftIssue(
                "no muscle group matched this name — volume by muscle group will "
                "count it as Unassigned",
                "muscle_group",
            )
        )
    elif group not in muscle_groups.CANONICAL_GROUPS:
        row.issues.append(
            DraftIssue(
                f"“{group}” is not one of the standard groups "
                f"({', '.join(muscle_groups.CANONICAL_GROUPS)}) — it will be "
                "counted as its own bucket",
                "muscle_group",
            )
        )


def draft_bodyweight_row(
    values: dict[str, Any],
    raw_text: str,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
    edited: bool = False,
    added: bool = False,
) -> DraftRow:
    """`draft_set_row`, for the bodyweight reading."""
    row = DraftRow(
        "bodyweight",
        _pick(values, BODYWEIGHT_COLUMNS, BODYWEIGHT_DEFAULTS),
        edited=edited,
        added=added,
    )
    if _is_blank(row.values, BODYWEIGHT_DEFAULTS):
        row.blank = True
        return row

    entry, confidence, exc = score_bodyweight(row.values, raw_text)
    row.confidence = 1.0 if added else confidence

    if exc is not None:
        row.validation_error = f"failed validation: {_short_errors(exc)}"
        row.issues = _field_issues(exc, BODYWEIGHT_COLUMNS)
        return row

    row.values = _pick(entry.model_dump(), BODYWEIGHT_COLUMNS, BODYWEIGHT_DEFAULTS)
    if added:
        return row

    haystack = _normalize_numeric_text(entry.raw_span or raw_text)
    if not _number_appears_in(float(entry.weight_kg), haystack):
        row.issues.append(
            DraftIssue("this number does not appear in your entry", "weight_kg")
        )
    if confidence < confidence_threshold and not row.issues and not edited:
        row.issues.append(
            DraftIssue(
                f"confidence {confidence:.2f}, below the {confidence_threshold:.2f} "
                "threshold — check it against your entry"
            )
        )
    return row


def load_exercise_groups(conn: Connection) -> dict[str, Optional[str]]:
    """Every known exercise name and the muscle group it is filed under."""
    rows = conn.execute(text("SELECT name, muscle_group FROM exercises")).fetchall()
    return {row[0]: row[1] for row in rows}


def blank_set_row() -> DraftRow:
    """An empty slot for a set the extraction missed entirely."""
    return DraftRow("workout_set", dict(SET_DEFAULTS), added=True, blank=True)


def blank_bodyweight_row() -> DraftRow:
    """An empty slot for a bodyweight reading the entry never mentioned."""
    return DraftRow("bodyweight", dict(BODYWEIGHT_DEFAULTS), added=True, blank=True)


def draft_from_payload(
    payload: dict[str, Any],
    raw_text: str,
    session_date: date,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
    known_groups: Optional[Mapping[str, Optional[str]]] = None,
) -> EntryDraft:
    """Turn a raw extraction into editable rows, keeping the ones that failed.

    `validate_extraction` drops invalid sets into a review list; here they are
    kept as rows, because a set the model got half right is exactly the one a
    person wants to correct rather than retype.
    """
    draft = EntryDraft(raw_text=raw_text, session_date=session_date)

    raw_sets = payload.get("sets") or []
    if not isinstance(raw_sets, list):
        draft.review_items.append(
            ReviewItem("extraction", "'sets' was not a list", None, {"sets": str(raw_sets)[:500]})
        )
        raw_sets = []

    for index, item in enumerate(raw_sets):
        if not isinstance(item, dict):
            draft.review_items.append(
                ReviewItem("workout_set", f"set {index} was not an object", 0.0,
                           {"raw": str(item)[:500]})
            )
            continue
        draft.sets.append(
            draft_set_row(item, raw_text, confidence_threshold, known_groups=known_groups)
        )

    raw_bodyweight = payload.get("bodyweight")
    if isinstance(raw_bodyweight, dict):
        draft.bodyweight = draft_bodyweight_row(raw_bodyweight, raw_text, confidence_threshold)
    elif raw_bodyweight not in (None, ""):
        draft.review_items.append(
            ReviewItem("bodyweight", "'bodyweight' was neither null nor an object", 0.0,
                       {"raw": str(raw_bodyweight)[:500]})
        )

    return draft


def build_draft(
    raw_text: str,
    session_date: date,
    client: Any = None,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
    known_groups: Optional[Mapping[str, Optional[str]]] = None,
) -> EntryDraft:
    """Extract one journal entry into a draft. Touches the model, not the database.

    `known_groups` is what the `exercises` table already records, so the muscle
    group offered for an exercise you have logged before is the one it is
    actually filed under.
    """
    raw_text = (raw_text or "").strip()
    if not raw_text:
        return EntryDraft(
            raw_text=raw_text,
            session_date=session_date,
            error="Empty entry — nothing to parse.",
        )

    try:
        payload = extract_entities(raw_text, session_date, client=client)
    except ExtractionError as exc:
        logger.error("Extraction failed: %s", exc)
        return EntryDraft(
            raw_text=raw_text,
            session_date=session_date,
            error="Extraction failed after one retry — entry not inserted.",
            review_items=[ReviewItem("extraction", str(exc), None, {"raw_text": raw_text})],
        )

    return draft_from_payload(
        payload, raw_text, session_date, confidence_threshold, known_groups
    )


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
    chosen_group: bool = False,
) -> tuple[int, Optional[str], bool]:
    """Resolve an exercise name to an id, fuzzy-matching before inserting.

    Returns (exercise_id, matched_existing_name_or_None, created).

    `chosen_group` says the muscle group came from a person on the review
    screen rather than from the table. Only then is an existing exercise's group
    revised: a suggestion must never quietly overwrite a filing decision that
    has already been made, but a correction the user typed has to stick, or the
    review screen would be offering an edit that does nothing.
    """
    matched = find_matching_exercise(proposed_name, known.keys())
    if matched is not None:
        exercise_id = known[matched]
        if chosen_group:
            conn.execute(
                text("UPDATE exercises SET muscle_group = :muscle_group "
                     "WHERE exercise_id = :exercise_id "
                     "AND muscle_group IS DISTINCT FROM :muscle_group"),
                {"muscle_group": muscle_group, "exercise_id": exercise_id},
            )
        return exercise_id, matched, False

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


def count_entries_for_date(engine: Engine, session_date: date) -> dict[str, int]:
    """How much is already logged against a local date."""
    start, end = _local_day_bounds(session_date)
    params = {"start": start, "end": end}
    with engine.connect() as conn:
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
    session_date: Optional[date],
) -> tuple[str, dict[str, Any]]:
    """The WHERE fragment identifying rows written from one journal entry.

    Matching is exact text equality — `raw_source` is TEXT, so nothing is
    truncated and two different entries cannot collide here.
    """
    params: dict[str, Any] = {
        "raw_source": raw_text,
        "window": f"{int(window_minutes)} minutes",
    }
    clauses = [
        "raw_source = :raw_source",
        "created_at >= now() - CAST(:window AS interval)",
    ]

    if session_date is not None:
        # Scoped to the day being logged. Without this, pasting the same short
        # entry ("rest day, weighed 82.4") against two dates in one sitting
        # reads as a double-tap and the second date is silently dropped.
        start, end = _local_day_bounds(session_date)
        clauses.append("logged_at >= :day_start AND logged_at < :day_end")
        params["day_start"] = start
        params["day_end"] = end

    return " AND ".join(clauses), params


def find_recent_submission(
    engine: Engine,
    raw_text: str,
    window_minutes: int = DUPLICATE_WINDOW_MINUTES,
    session_date: Optional[date] = None,
) -> Optional[dict[str, int]]:
    """Return counts for an identical entry inserted within the window, else None.

    Backs the /log duplicate-submission guard: Render's free tier cold-starts for
    30-50s, which is exactly when a user double-taps submit.

    `session_date` scopes the match to the day being logged. It is optional only
    so the older two-argument call still works; every caller that knows the date
    should pass it, or logging one short entry against two dates in one sitting
    reads as a double-tap and the second date is dropped.
    """
    where, params = _submission_clauses(raw_text, window_minutes, session_date)
    with engine.connect() as conn:
        sets = conn.execute(
            text(f"SELECT count(*) FROM workout_logs WHERE {where}"), params
        ).scalar_one()
        bodyweight = conn.execute(
            text(f"SELECT count(*) FROM bodyweight_logs WHERE {where}"), params
        ).scalar_one()

    if not sets and not bodyweight:
        return None
    return {"inserted_sets": int(sets), "inserted_bodyweight": int(bodyweight)}


def describe_recent_submission(
    engine: Engine,
    raw_text: str,
    window_minutes: int = DUPLICATE_WINDOW_MINUTES,
    session_date: Optional[date] = None,
) -> list[dict[str, Any]]:
    """What the matched entry actually saved, per exercise.

    A guard that says "duplicate" and shows nothing is impossible to argue with,
    and impossible to trust when the sets it claims to have saved are not the
    ones you meant to log. This is the evidence for the claim.
    """
    where, params = _submission_clauses(raw_text, window_minutes, session_date)
    with engine.connect() as conn:
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
        {"exercise": row[0], "sets": int(row[1]), "first_logged": row[2]}
        for row in rows
    ]


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def commit_draft(
    draft: EntryDraft,
    engine: Optional[Engine] = None,
    check_duplicates: bool = False,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
    review_excluded: bool = False,
    override_duplicate: bool = False,
) -> PipelineResult:
    """Write the ticked rows of a draft. The only function that inserts.

    A row that reaches here has either been looked at on the review screen or
    cleared the threshold unattended, so the threshold is not applied again —
    `include` is the decision. `review_excluded` makes the rows left out come
    back as review items, which is what the unattended path reports.

    Confidence is recomputed from the values as they stand. A row a person
    edited or typed in themselves is stored at 1.0: the score measures how well
    an extraction is grounded in the source text, and a human correction is
    better evidence than any grounding heuristic — for a hand-entered row there
    is no extraction to score at all.

    Blank slots are skipped in silence. They are offers to add a row that nobody
    took up, so they are neither saved nor reported as left out.

    `override_duplicate` is the answer to the duplicate guard rather than a way
    around it. Re-pasting an entry to correct a bad parse is indistinguishable
    from a double-tapped submit, so the guard asks instead of refusing, and this
    carries the decision back. It is also what makes Replace work on a matched
    entry: without it the delete-and-reinsert is stopped by the same guard and
    the save appears to succeed while changing nothing.
    """
    if engine is None:
        engine = get_engine()

    result = PipelineResult(review_items=list(draft.review_items))
    if draft.error:
        result.error = draft.error
        return result

    if check_duplicates and not override_duplicate:
        prior = find_recent_submission(
            engine, draft.raw_text, session_date=draft.session_date
        )
        if prior is not None:
            logger.info("Duplicate submission held for a decision (%s)", prior)
            return PipelineResult(
                inserted_sets=prior["inserted_sets"],
                inserted_bodyweight=prior["inserted_bodyweight"],
                duplicate_of_recent=True,
                duplicate_evidence=describe_recent_submission(
                    engine, draft.raw_text, session_date=draft.session_date
                ),
            )

    # (model, confidence, the muscle group was chosen by the user not suggested,
    #  the exercise name was typed by the user rather than extracted)
    accepted_sets: list[tuple[WorkoutSet, float, bool, bool]] = []
    for row in draft.sets:
        if row.blank:
            continue
        if not row.include:
            result.skipped_sets += 1
            if review_excluded:
                result.review_items.append(
                    ReviewItem(
                        "workout_set",
                        row.legacy_reason(confidence_threshold),
                        row.confidence,
                        dict(row.values),
                    )
                )
            continue
        workout_set, confidence, exc = score_set(row.values, draft.raw_text)
        if exc is not None:
            # Callers check `draft.ready` first, so this is a guard rather than a
            # path: a row the database would reject is never silently dropped.
            result.review_items.append(
                ReviewItem("workout_set", f"failed validation: {_short_errors(exc)}",
                           row.confidence, dict(row.values))
            )
            continue
        accepted_sets.append((
            workout_set,
            1.0 if row.edited or row.added else confidence,
            "muscle_group" in row.edited_fields,
            row.added or "exercise_name" in row.edited_fields,
        ))

    accepted_bodyweight: Optional[tuple[BodyweightEntry, float]] = None
    if draft.bodyweight is not None and not draft.bodyweight.blank:
        row = draft.bodyweight
        if not row.include:
            result.skipped_bodyweight += 1
            if review_excluded:
                result.review_items.append(
                    ReviewItem("bodyweight", row.legacy_reason(confidence_threshold),
                               row.confidence, dict(row.values))
                )
        else:
            entry, confidence, exc = score_bodyweight(row.values, draft.raw_text)
            if exc is not None:
                result.review_items.append(
                    ReviewItem("bodyweight", f"failed validation: {_short_errors(exc)}",
                               row.confidence, dict(row.values))
                )
            else:
                accepted_bodyweight = (entry, 1.0 if row.edited or row.added else confidence)

    if not accepted_sets and accepted_bodyweight is None:
        return result

    with engine.begin() as conn:
        if draft.replace_existing:
            # Only reached when there is something to put back - the early
            # return above means a failed extraction never empties the day.
            result.replaced = delete_entries_for_date(conn, draft.session_date)
            logger.info("Replaced %s on %s", result.replaced, draft.session_date)
        known = load_exercise_names(conn)
        for workout_set, confidence, chosen_group, name_typed in accepted_sets:
            exercise_id, matched_name, created = get_or_create_exercise(
                conn, workout_set.exercise_name, workout_set.muscle_group, known,
                chosen_group=chosen_group,
            )
            if created:
                result.exercises_created.append(workout_set.exercise_name)
                # Flag against everything known EXCEPT the row just inserted -
                # `get_or_create_exercise` has already added it to `known`, and a
                # name always scores 100 against itself. Names created earlier in
                # this same entry stay in scope, so a typo of a lift first logged
                # two sets ago is still caught.
                #
                # A name the user typed on the review screen is flagged with no
                # source text, which skips the grounding check while leaving the
                # near-miss and unrecognized ones in force. Grounding asks
                # whether the model read the name or invented it; for a name a
                # person wrote there is no extraction to score, the same reason
                # `confidence` above is pinned to 1.0 for an edited row. Asking
                # it anyway would flag every hand-typed name, and a flag that
                # fires on ordinary lifts hides the real ones.
                flag = flag_exercise_name(
                    workout_set.exercise_name,
                    [name for name in known if name != workout_set.exercise_name],
                    None if name_typed else workout_set.raw_span,
                    "" if name_typed else draft.raw_text,
                )
                if flag is not None:
                    result.name_flags.append(flag)
                    logger.info(
                        "event=name_flagged exercise=%r reason=%s",
                        workout_set.exercise_name, flag.reason,
                    )
            elif matched_name and matched_name != workout_set.exercise_name:
                result.exercises_matched.append((workout_set.exercise_name, matched_name))

            conn.execute(
                _INSERT_WORKOUT_SET,
                {
                    "exercise_id": exercise_id,
                    "logged_at": resolve_logged_at(workout_set.logged_at_local, draft.session_date),
                    "weight_kg": workout_set.weight_kg,
                    "reps": workout_set.reps,
                    "cheat_reps": workout_set.cheat_reps,
                    "set_number": workout_set.set_number,
                    "is_warmup": workout_set.is_warmup,
                    "is_dropset": workout_set.is_dropset,
                    "pain_flag": workout_set.pain_flag,
                    "notes": workout_set.notes,
                    "raw_source": draft.raw_text,
                    "extraction_confidence": confidence,
                },
            )
            result.inserted_sets += 1

        if accepted_bodyweight is not None:
            entry, confidence = accepted_bodyweight
            conn.execute(
                _INSERT_BODYWEIGHT,
                {
                    "logged_at": resolve_logged_at(entry.logged_at_local, draft.session_date),
                    "weight_kg": entry.weight_kg,
                    "body_fat_pct": entry.body_fat_pct,
                    "notes": entry.notes,
                    "raw_source": draft.raw_text,
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
    """Extract and insert one journal entry with nobody watching.

    The CLI path: no review screen, so the confidence threshold does the
    deciding and anything under it is reported rather than saved. The web app
    goes through `build_draft` and `commit_draft` instead, and lets the person
    who wrote the entry make that call.
    """
    raw_text = (raw_text or "").strip()
    if not raw_text:
        return PipelineResult(error="Empty entry — nothing to parse.")

    if engine is None:
        engine = get_engine()

    if check_duplicates:
        # The unattended path has nobody to ask, so it still refuses outright.
        prior = find_recent_submission(engine, raw_text, session_date=session_date)
        if prior is not None:
            logger.info("Duplicate submission suppressed (%s)", prior)
            return PipelineResult(
                inserted_sets=prior["inserted_sets"],
                inserted_bodyweight=prior["inserted_bodyweight"],
                duplicate_of_recent=True,
            )

    draft = build_draft(raw_text, session_date, client=client,
                        confidence_threshold=confidence_threshold)
    if draft.error:
        return PipelineResult(error=draft.error, review_items=list(draft.review_items))

    draft.replace_existing = replace_existing
    for row in draft.rows:
        row.include = not row.blocking and (row.confidence or 0.0) >= confidence_threshold

    return commit_draft(
        draft,
        engine=engine,
        confidence_threshold=confidence_threshold,
        review_excluded=True,
    )
