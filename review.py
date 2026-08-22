"""The review screen: what the extraction produced, before any of it is saved.

The web app never inserts straight from an extraction. It builds a draft
(`pipeline.build_draft`), renders it here as an editable form showing every
prospective database row, and only writes what comes back from that form
(`pipeline.commit_draft`). Anything missing, ungrounded or outright invalid is
marked in red, so a bad reading is corrected in place rather than discovered
weeks later in a chart.

This module owns the two halves of that round trip:
    render_review_body()  draft  -> HTML form
    draft_from_form()     posted -> draft, rescored against the edited values

Nothing here talks to the database or the model. Every dynamic value is escaped
before it reaches the page: exercise names come from LLM output, so escaping is
a correctness concern here, not a formality.
"""

from __future__ import annotations

import html
import json
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

import pipeline
from pipeline import CONFIDENCE_THRESHOLD, DraftRow, EntryDraft

# Column -> (label, css class, input mode). The order is the order the fields
# are laid out in; the grid classes decide how many columns each one spans.
SET_FIELDS: Sequence[tuple[str, str, str, str]] = (
    ("exercise_name", "Exercise", "n2 w2", "text"),
    ("weight_kg", "Weight (kg)", "", "decimal"),
    ("reps", "Reps", "", "numeric"),
    ("cheat_reps", "Cheat reps", "", "numeric"),
    ("set_number", "Set #", "", "numeric"),
    ("logged_at_local", "Time", "", "text"),
    ("muscle_group", "Muscle group", "w2", "text"),
    ("notes", "Notes", "n2 w3", "text"),
)

BODYWEIGHT_FIELDS: Sequence[tuple[str, str, str, str]] = (
    ("weight_kg", "Weight (kg)", "", "decimal"),
    ("body_fat_pct", "Body fat %", "", "decimal"),
    ("logged_at_local", "Time", "", "text"),
    ("notes", "Notes", "n2 w3", "text"),
)

SET_CHECKBOXES: Sequence[tuple[str, str]] = (
    ("is_warmup", "Warm-up"),
    ("is_dropset", "Drop set"),
    ("pain_flag", "Pain"),
)

REVIEW_CSS = """
/* The review grid needs more room than the entry form, which is sized for a
   phone held in one hand. Overrides the base rule by coming later in the sheet. */
body { max-width: 60rem; }
.draft { border: 1px solid #8884; border-left: 4px solid #8884; border-radius: .6rem;
         padding: .6rem .8rem .8rem; margin: .7rem 0; }
.draft.flagged { border-left-color: #dc2626; }
.draft.dropped { opacity: .55; }
.draft.blank { border-style: dashed; }
.draft-head { display: flex; align-items: center; gap: .6rem; flex-wrap: wrap;
              margin-bottom: .5rem; }
.draft-head .name { font-weight: 600; }
.save-toggle { display: inline-flex; align-items: center; gap: .35rem; font-weight: 600;
               margin: 0; white-space: nowrap; }
.save-toggle input { width: 1.05rem; height: 1.05rem; margin: 0; accent-color: #2563eb; }
.chip { font-size: .78rem; padding: .1rem .45rem; border-radius: 1rem;
        border: 1px solid #8886; opacity: .85; }
.chip.chip-bad { border-color: #dc2626; color: #dc2626; opacity: 1; }
.chip.bad { border-color: #dc2626; color: #dc2626; opacity: 1; }
.chip.edited { border-color: #2563eb; color: #2563eb; opacity: 1; }
.draft-grid { display: grid; gap: .5rem .6rem; grid-template-columns: repeat(2, minmax(0, 1fr)); }
.field { min-width: 0; }
.field.n2 { grid-column: span 2; }
.field label { font-size: .78rem; font-weight: 600; opacity: .8; margin: 0 0 .15rem; }
.field input[type=text] { width: 100%; font-size: 1rem; padding: .45rem .5rem;
        border-radius: .4rem; border: 1px solid #8884; font-family: inherit;
        background: transparent; color: inherit; }
.field input.bad { border-color: #dc2626; background: rgba(220, 38, 38, .1); }
.field input:focus { outline: 2px solid #2563eb; outline-offset: -1px; }
@media (min-width: 34rem) {
  .draft-grid { grid-template-columns: repeat(6, minmax(0, 1fr)); }
  .field.n2 { grid-column: span 1; }
  .field.w2 { grid-column: span 2; }
  .field.w3 { grid-column: span 3; }
}
.flags { display: flex; gap: 1rem; flex-wrap: wrap; margin-top: .55rem; }
.flags label { display: inline-flex; align-items: center; gap: .35rem; font-weight: 400;
               font-size: .9rem; margin: 0; }
.flags input { width: 1rem; height: 1rem; margin: 0; }
.problems { margin: .55rem 0 0; padding-left: 1.1rem; color: #dc2626; font-size: .88rem; }
.problems li { margin: .15rem 0; }
.evidence { margin: .5rem 0 0; font-size: .82rem; opacity: .65; }
.actions { display: flex; align-items: center; gap: 1rem; flex-wrap: wrap; margin-top: 1.2rem; }
.actions button { margin-top: 0; width: auto; padding: .7rem 1.4rem; }
.actions button.secondary { background: transparent; color: inherit; border: 1px solid #8886; }
.actions button.secondary:active { background: #8882; }
details.entry summary { cursor: pointer; font-weight: 600; }
details.entry pre { margin: .6rem 0 0; font-size: .9rem; opacity: .85; }
"""


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def display_value(value: Any) -> str:
    """A field value as it should appear in a text input.

    `%g` on floats so a weight the model returned as 28.0 shows as "28" — the
    round trip has to look like what the user wrote, or every row reads as edited.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else ""
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _origin_blob(row: DraftRow, columns: Sequence[str]) -> str:
    """The values as extracted, in submitted form, so edits can be detected later."""
    snapshot: dict[str, Any] = {}
    for column in columns:
        value = row.values.get(column)
        snapshot[column] = value if isinstance(value, bool) else display_value(value)
    return json.dumps(snapshot, separators=(",", ":"), sort_keys=True)


PLACEHOLDERS = {"logged_at_local": "HH:MM"}


def _text_field(prefix: str, column: str, label: str, css: str, mode: str, row: DraftRow) -> str:
    field_id = f"{prefix}.{column}"
    classes = "bad" if row.issues_for(column) else ""
    placeholder = PLACEHOLDERS.get(column, "")
    return (
        f'<div class="field {css}">'
        f'<label for="{html.escape(field_id)}">{html.escape(label)}</label>'
        f'<input type="text" inputmode="{mode}" id="{html.escape(field_id)}" '
        f'name="{html.escape(field_id)}" class="{classes}" autocomplete="off" '
        f'placeholder="{html.escape(placeholder)}" '
        f'value="{html.escape(display_value(row.values.get(column)))}">'
        f"</div>"
    )


def _checkbox(name: str, label: str, checked: bool, css: str = "") -> str:
    mark = " checked" if checked else ""
    return (
        f'<label class="{css}"><input type="checkbox" name="{html.escape(name)}"'
        f"{mark}> {html.escape(label)}</label>"
    )


def _row_card(
    row: DraftRow,
    prefix: str,
    title: str,
    fields: Sequence[tuple[str, str, str, str]],
    columns: Sequence[str],
    checkboxes: Sequence[tuple[str, str]] = (),
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
) -> str:
    chips = []
    if row.validation_error:
        # "confidence 0.00" on a row that never validated says nothing useful.
        chips.append('<span class="chip bad">cannot be saved yet</span>')
    elif row.confidence is not None:
        bad = " chip-bad" if row.confidence < confidence_threshold else ""
        chips.append(f'<span class="chip{bad}">confidence {row.confidence:.2f}</span>')
    if row.added:
        chips.append('<span class="chip edited">typed in by you</span>')
    elif row.edited:
        chips.append('<span class="chip edited">edited</span>')

    grid = "".join(
        _text_field(prefix, column, label, css, mode, row)
        for column, label, css, mode in fields
    )

    flags = ""
    if checkboxes:
        flags = '<div class="flags">' + "".join(
            _checkbox(f"{prefix}.{column}", label, bool(row.values.get(column)))
            for column, label in checkboxes
        ) + "</div>"

    problems = ""
    messages = [issue.message for issue in row.issues]
    if messages:
        problems = '<ul class="problems">' + "".join(
            f"<li>{html.escape(message)}</li>" for message in messages
        ) + "</ul>"

    evidence = ""
    span = (row.values.get("raw_span") or "").strip()
    if span:
        evidence = f'<p class="evidence">Read from: &ldquo;{html.escape(span)}&rdquo;</p>'

    if row.blank:
        problems = (
            '<p class="evidence">Fill this in to add something the parser missed. '
            "Left empty, it is ignored.</p>"
        )

    hidden = (
        f'<input type="hidden" name="{html.escape(prefix)}.raw_span" '
        f'value="{html.escape(display_value(row.values.get("raw_span")))}">'
        f'<input type="hidden" name="{html.escape(prefix)}.origin" '
        f'value="{html.escape(_origin_blob(row, columns))}">'
        + (f'<input type="hidden" name="{html.escape(prefix)}.added" value="1">'
           if row.added else "")
    )

    classes = (
        "draft"
        + (" flagged" if row.flagged else "")
        + (" blank" if row.blank else "")
        + ("" if row.include else " dropped")
    )
    return (
        f'<div class="{classes}">'
        f'<div class="draft-head">'
        f'{_checkbox(f"{prefix}.include", "Save", row.include, css="save-toggle")}'
        f'<span class="name">{html.escape(title)}</span>{"".join(chips)}'
        f"</div>"
        f'<div class="draft-grid">{grid}</div>'
        f"{flags}{problems}{evidence}{hidden}"
        f"</div>"
    )


def _add_buttons(draft: EntryDraft, standalone: bool = True) -> str:
    """Submit buttons that re-render the form with one more empty slot.

    Adding a row is a round trip rather than a script: the form already carries
    the whole draft, so the server can hand back the same state plus a slot, and
    the page stays JavaScript-free like the rest of the app.

    Save comes first in the markup on the full form, because pressing Enter in a
    text field submits via the first button — and that should save, not add.
    """
    buttons = [
        '<button type="submit" class="secondary" name="action" value="add_set">'
        "Add a set</button>"
    ]
    if draft.bodyweight is None:
        buttons.append(
            '<button type="submit" class="secondary" name="action" value="add_bodyweight">'
            "Add bodyweight</button>"
        )
    joined = "".join(buttons)
    return f'<div class="actions">{joined}</div>' if standalone else joined


def _banner(draft: EntryDraft, existing: Optional[Mapping[str, int]]) -> str:
    parts: list[str] = []

    flagged, blocked = draft.flagged_count, draft.blocked_count
    if flagged:
        detail = (
            f" {blocked} of them cannot be saved until the red fields are fixed "
            "or the row is unticked."
            if blocked
            else " Nothing stops you saving them — check them against your entry first."
        )
        parts.append(
            f'<div class="card err"><strong>{flagged} row(s) marked in red.</strong>'
            f"{html.escape(detail)}</div>"
        )

    if draft.replace_existing:
        if existing and (existing["sets"] or existing["bodyweight"]):
            parts.append(
                '<div class="card warn"><strong>Replace mode.</strong> Saving first '
                f"deletes the {existing['sets']} set(s) and {existing['bodyweight']} "
                "bodyweight entry/entries already logged on "
                f"{html.escape(draft.session_date.isoformat())}.</div>"
            )
        else:
            parts.append(
                '<div class="card warn"><strong>Replace mode.</strong> Nothing is '
                f"logged on {html.escape(draft.session_date.isoformat())} yet, so "
                "there is nothing to replace.</div>"
            )
    elif existing and (existing["sets"] or existing["bodyweight"]):
        parts.append(
            f'<div class="card muted">{existing["sets"]} set(s) are already logged on '
            f"{html.escape(draft.session_date.isoformat())}. These will be added to them.</div>"
        )

    for item in draft.review_items:
        parts.append(
            '<div class="card err"><strong>Could not be read as a row.</strong> '
            f"{html.escape(item.reason)} &mdash; this part of the entry cannot be "
            "edited here and will not be saved.</div>"
        )

    return "".join(parts)


def render_review_body(
    draft: EntryDraft,
    existing: Optional[Mapping[str, int]] = None,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
) -> str:
    """The review form: every prospective row, editable, before anything is written."""
    hidden = (
        f'<input type="hidden" name="raw_text" value="{html.escape(draft.raw_text)}">'
        f'<input type="hidden" name="session_date" '
        f'value="{html.escape(draft.session_date.isoformat())}">'
        f'<input type="hidden" name="mode" '
        f'value="{"replace" if draft.replace_existing else "add"}">'
        f'<input type="hidden" name="set_count" value="{len(draft.sets)}">'
        f'<input type="hidden" name="has_bodyweight" '
        f'value="{"1" if draft.bodyweight is not None else ""}">'
    )

    entry = (
        '<details class="card entry"><summary>Your entry</summary>'
        f"<pre>{html.escape(draft.raw_text)}</pre></details>"
    )

    if draft.is_empty:
        # A dead end otherwise: the parser found nothing, but the workout still
        # happened, so the same add buttons are offered without any rows above them.
        return (
            "<h1>Nothing to save</h1>"
            '<div class="card err">No sets or bodyweight readings came back from '
            "that entry. You can still enter them by hand.</div>"
            f"{_banner(draft, existing)}{entry}"
            f'<form method="post" action="/save">{hidden}{_add_buttons(draft)}</form>'
        )

    cards = "".join(
        _row_card(
            row,
            f"s{index}",
            "New set" if row.blank else f"Set {index + 1}",
            SET_FIELDS,
            pipeline.SET_COLUMNS,
            SET_CHECKBOXES,
            confidence_threshold,
        )
        for index, row in enumerate(draft.sets)
    )

    bodyweight = ""
    if draft.bodyweight is not None:
        bodyweight = "<h2>Bodyweight</h2>" + _row_card(
            draft.bodyweight,
            "bw",
            "New bodyweight reading" if draft.bodyweight.blank else "Bodyweight",
            BODYWEIGHT_FIELDS,
            pipeline.BODYWEIGHT_COLUMNS,
            (),
            confidence_threshold,
        )

    # The button label stays static: the page carries no JavaScript, so a count
    # baked in at render time would go stale the moment a box is unticked.
    total = len(draft.included)

    return f"""
<h1>Check before saving</h1>
<p class="muted">This is exactly what will be written to the database for
{html.escape(draft.session_date.isoformat())}. Correct anything that is wrong,
untick anything that should not be saved, then save. Nothing has been stored yet.</p>
{_banner(draft, existing)}
{entry}
<form method="post" action="/save">
{hidden}
<h2>Sets ({sum(1 for row in draft.sets if not row.blank)})</h2>
{cards}
{bodyweight}
<div class="actions">
  <button type="submit" name="action" value="save">Save to database</button>
  {_add_buttons(draft, standalone=False)}
  <a href="/">Discard and start over</a>
</div>
<p class="muted">{total} row(s) drafted. Only the ticked ones are saved; an empty
slot is ignored.</p>
</form>
"""


# --------------------------------------------------------------------------
# Reading the form back
# --------------------------------------------------------------------------


def _coerce(value: str) -> Optional[str]:
    """Form strings back into field values, leaving bad input for the model to reject.

    Nothing is repaired here: "twelve" in the reps box stays "twelve" so Pydantic
    can fail on it and the field comes back red, rather than being silently
    dropped to null. A blank box becomes None, which `pipeline._pick` resolves to
    that column's default.
    """
    return (value or "").strip() or None


def _row_values(
    form: Mapping[str, Any],
    prefix: str,
    columns: Sequence[str],
    checkboxes: Sequence[str],
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for column in columns:
        if column in checkboxes:
            # An unticked checkbox is simply absent from the submission.
            values[column] = f"{prefix}.{column}" in form
        else:
            values[column] = _coerce(str(form.get(f"{prefix}.{column}", "")))
    return values


def _was_edited(form: Mapping[str, Any], prefix: str, values: Mapping[str, Any]) -> bool:
    """True when a submitted value differs from what the extraction proposed.

    Compared in submitted form — display strings and booleans — so a float that
    round-tripped through the input does not read as a change.
    """
    try:
        origin = json.loads(str(form.get(f"{prefix}.origin", "")))
    except (TypeError, ValueError):
        return False
    if not isinstance(origin, dict):
        return False
    return any(
        origin.get(column) != (value if isinstance(value, bool) else display_value(value))
        for column, value in values.items()
    )


def draft_from_form(
    form: Mapping[str, Any],
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
) -> EntryDraft:
    """Rebuild a draft from a submitted review form, rescored against the edits.

    Every row is validated again from scratch, so a field the user fixed loses
    its red mark and a field they broke gains one. Raises ValueError if the
    round-tripped session date is unusable.
    """
    raw_text = str(form.get("raw_text", "")).strip()
    session_date = datetime.strptime(str(form.get("session_date", "")).strip(), "%Y-%m-%d").date()

    try:
        set_count = int(str(form.get("set_count", "0")).strip() or 0)
    except ValueError:
        set_count = 0

    draft = EntryDraft(
        raw_text=raw_text,
        session_date=session_date,
        replace_existing=str(form.get("mode", "add")) == "replace",
    )

    checkbox_columns = [column for column, _ in SET_CHECKBOXES]
    for index in range(max(0, set_count)):
        prefix = f"s{index}"
        values = _row_values(form, prefix, pipeline.SET_COLUMNS, checkbox_columns)
        row = pipeline.draft_set_row(
            values,
            raw_text,
            confidence_threshold,
            edited=_was_edited(form, prefix, values),
            added=f"{prefix}.added" in form,
        )
        row.include = f"{prefix}.include" in form
        draft.sets.append(row)

    if str(form.get("has_bodyweight", "")).strip():
        values = _row_values(form, "bw", pipeline.BODYWEIGHT_COLUMNS, ())
        row = pipeline.draft_bodyweight_row(
            values,
            raw_text,
            confidence_threshold,
            edited=_was_edited(form, "bw", values),
            added="bw.added" in form,
        )
        row.include = "bw.include" in form
        draft.bodyweight = row

    return draft


def add_row(draft: EntryDraft, action: str) -> bool:
    """Give the draft one more empty slot. True when `action` asked for one.

    Adding never validates or saves — the user may well be part way through
    fixing something else on the same form.
    """
    if action == "add_set":
        draft.sets.append(pipeline.blank_set_row())
        return True
    if action == "add_bodyweight" and draft.bodyweight is None:
        draft.bodyweight = pipeline.blank_bodyweight_row()
        return True
    return False
