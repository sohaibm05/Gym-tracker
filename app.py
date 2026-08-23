"""FastAPI web app — the phone-facing front end for the gym tracker.

Routes:
    GET  /               mobile-friendly entry form
    POST /log            parse a pasted entry and show it back — writes nothing
    POST /log/confirm    write the previewed entry to the database
    POST /log/edit       reopen the form with the same text, to fix and re-parse
    GET  /weekly-report  report page with a "Generate weekly report" button
    POST /weekly-report  run Stage A + Stage B and upsert the report
    GET  /healthz        unauthenticated liveness probe

Nothing reaches the database on POST /log. Extraction and scoring happen there,
the result is shown for checking, and only POST /log/confirm inserts — carrying
the parse forward in a signed hidden field so the model is not called twice.

All extraction/validation/insert logic is imported from pipeline.py; all report
logic from insights.py. Nothing is reimplemented here.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import sys
import time
import zlib
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

# Serverless loaders import this file by path rather than as part of a package,
# and do not necessarily put its own directory on sys.path. Without this the
# sibling imports below raise ModuleNotFoundError at cold start. No effect when
# running normally, where the directory is already there.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# These imports are the ones that break on a misconfigured serverless deploy,
# either because the sibling files were not bundled or because the loader did
# not put this directory on the path. Left unguarded the whole module fails to
# import and the platform reports an opaque "function crashed" with the real
# reason buried in provider logs. Capturing it here lets the running app serve
# the reason instead.
_STARTUP_ERROR: Optional[str] = None
try:
    import charts  # noqa: E402 - must follow the sys.path bootstrap above
    import insights  # noqa: E402
    import pipeline  # noqa: E402
except Exception:  # noqa: BLE001 - report anything that stops the app booting
    import traceback

    _STARTUP_ERROR = traceback.format_exc()
    charts = insights = pipeline = None  # type: ignore[assignment]

logging.basicConfig(
    level=pipeline.env("LOG_LEVEL", "INFO").upper() if pipeline else "INFO",
    format='%(asctime)s level=%(levelname)s logger=%(name)s %(message)s',
)
logger = logging.getLogger("gym_tracker.app")

app = FastAPI(title="Gym Tracker", docs_url=None, redoc_url=None)
security = HTTPBasic()

_engine = None


def get_engine():
    """Lazily build the SQLAlchemy engine so import never needs a database."""
    global _engine
    if _engine is None:
        _engine = pipeline.get_engine()
    return _engine


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


def require_auth(request: Request, credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """HTTP Basic Auth against env vars, compared in constant time.

    Basic Auth credentials are base64, not encrypted, so this is only safe over
    HTTPS. Render terminates TLS by default; the check below logs loudly if a
    request ever arrives over plain HTTP so a misconfigured deployment is not
    silently insecure.
    """
    expected_user = os.getenv("APP_USERNAME")
    expected_password = os.getenv("APP_PASSWORD")
    if not expected_user or not expected_password:
        logger.error("event=auth_misconfigured msg='APP_USERNAME/APP_PASSWORD not set'")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server auth is not configured.",
        )

    forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    if forwarded_proto != "https" and request.client and request.client.host not in {"127.0.0.1", "::1"}:
        logger.warning(
            "event=insecure_transport proto=%s msg='Basic Auth credentials sent without TLS'",
            forwarded_proto,
        )

    user_ok = secrets.compare_digest(credentials.username, expected_user)
    password_ok = secrets.compare_digest(credentials.password, expected_password)
    if not (user_ok and password_ok):
        logger.warning("event=auth_failed path=%s", request.url.path)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# --------------------------------------------------------------------------
# Structured request logging
# --------------------------------------------------------------------------


@app.middleware("http")
async def log_requests(request: Request, call_next):
    if _STARTUP_ERROR is not None:
        # Every route depends on the modules that failed to import, so serve the
        # reason rather than a stack-less 500 from the platform.
        logger.error("event=startup_failed path=%s", request.url.path)
        return PlainTextResponse(
            "The application failed to start.\n\n"
            "This is an import error, not a configuration one - no secrets are "
            "shown below.\n\n" + _STARTUP_ERROR,
            status_code=500,
        )

    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = (time.perf_counter() - started) * 1000
        logger.exception(
            "event=request method=%s path=%s status=500 duration_ms=%.1f",
            request.method,
            request.url.path,
            duration_ms,
        )
        raise
    duration_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "event=request method=%s path=%s status=%d duration_ms=%.1f",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


# --------------------------------------------------------------------------
# Rendering helpers (everything dynamic is escaped before it reaches the page)
# --------------------------------------------------------------------------

_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       margin: 0; padding: 1rem; max-width: 46rem; margin-inline: auto; line-height: 1.5; }
h1 { font-size: 1.35rem; margin: 0 0 1rem; }
h2 { font-size: 1.1rem; margin: 1.4rem 0 .5rem; }
label { display: block; font-weight: 600; margin: .9rem 0 .3rem; }
input[type=date], textarea, button, select { width: 100%; font-size: 1rem; padding: .7rem;
       border-radius: .5rem; border: 1px solid #8884; font-family: inherit; }
textarea { min-height: 11rem; resize: vertical; }
button { margin-top: 1rem; font-weight: 600; border: 0; background: #2563eb; color: #fff; }
button:active { background: #1d4ed8; }
.card { border: 1px solid #8884; border-radius: .6rem; padding: .8rem 1rem; margin: .8rem 0; }
.ok { border-left: 4px solid #16a34a; }
.warn { border-left: 4px solid #d97706; }
.err { border-left: 4px solid #dc2626; }
.muted { opacity: .75; font-size: .9rem; }
ul { padding-left: 1.2rem; }
nav a { display: inline-block; margin-right: 1rem; }
pre { white-space: pre-wrap; word-wrap: break-word; }
button.secondary { background: transparent; color: inherit; border: 1px solid #8886; }
button.danger { background: #b91c1c; }
button.danger:active { background: #991b1b; }
.preview-table { width: 100%; border-collapse: collapse; margin-top: .4rem; font-size: .95rem; }
.preview-table th, .preview-table td { text-align: left; padding: .35rem .5rem;
       border-bottom: 1px solid #8883; vertical-align: top; }
.preview-table th { font-size: .8rem; text-transform: uppercase; letter-spacing: .03em;
       opacity: .7; font-weight: 600; }
.preview-table td.num { text-align: right; white-space: nowrap; }
.scroll-x { overflow-x: auto; }
.flag { font-size: .78rem; padding: .05rem .35rem; border-radius: .3rem;
        border: 1px solid #8886; margin-right: .25rem; white-space: nowrap; }
.flag-pain { border-color: #dc2626; color: #dc2626; }
.actions { display: flex; gap: .6rem; flex-wrap: wrap; }
.actions form { flex: 1 1 12rem; margin: 0; }
"""


def _page(title: str, body: str, extra_css: str = "", extra_js: str = "") -> HTMLResponse:
    css = f"<style>{_STYLE}{extra_css}</style>"
    # Deferred so the markup the charts measure exists before the script runs.
    js = f"<script>{extra_js}</script>" if extra_js else ""
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
{css}
</head><body>
<nav><a href="/">Log entry</a><a href="/progress">Progress</a><a href="/weekly-report">Weekly report</a></nav>
{body}
{js}
</body></html>"""
    )


def _render_markdown(markdown_text: str) -> str:
    """Minimal markdown -> HTML. Escapes FIRST, so no user text can inject tags."""
    escaped = html.escape(markdown_text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    lines_out: list[str] = []
    in_list = False
    for line in escaped.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            if in_list:
                lines_out.append("</ul>")
                in_list = False
            lines_out.append(f"<h2>{stripped[3:]}</h2>")
        elif stripped.startswith("# "):
            if in_list:
                lines_out.append("</ul>")
                in_list = False
            lines_out.append(f"<h2>{stripped[2:]}</h2>")
        elif stripped.startswith("- "):
            if not in_list:
                lines_out.append("<ul>")
                in_list = True
            lines_out.append(f"<li>{stripped[2:]}</li>")
        elif not stripped:
            if in_list:
                lines_out.append("</ul>")
                in_list = False
        else:
            if in_list:
                lines_out.append("</ul>")
                in_list = False
            lines_out.append(f"<p>{stripped}</p>")
    if in_list:
        lines_out.append("</ul>")
    return "\n".join(lines_out)


# --------------------------------------------------------------------------
# Preview token
#
# The parse has to survive the round trip from the preview page to the confirm
# post without being re-extracted (a second model call per save) and without a
# server-side store (each serverless request is a fresh process). So it travels
# in a hidden field, signed: the browser holds it, but only this server can
# produce one it will accept. Everything inside is still re-validated through
# the Pydantic models on the way back in - the signature proves origin, not
# correctness.
# --------------------------------------------------------------------------

# Anything longer than this was not produced here; refuse before decompressing.
_MAX_TOKEN_CHARS = 512_000


def _token_key() -> bytes:
    """Signing key. APP_PASSWORD is already required for the app to serve at all.

    A per-process random key would be simpler but cannot work: the preview and
    the confirm are two requests, and serverless gives each its own process, so
    the second one would never recognise the first one's signature.
    """
    secret = os.getenv("APP_PREVIEW_SECRET") or os.getenv("APP_PASSWORD") or ""
    if not secret:
        raise RuntimeError("APP_PASSWORD is not set, so preview tokens cannot be signed")
    return hashlib.sha256(secret.encode("utf-8")).digest()


def _sign_preview(prepared: pipeline.PreparedEntry, mode: str) -> str:
    body = json.dumps(
        {"prepared": prepared.to_dict(), "mode": mode},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    packed = base64.urlsafe_b64encode(zlib.compress(body, 6)).decode("ascii")
    signature = hmac.new(_token_key(), packed.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{packed}.{signature}"


def _unsign_preview(token: str) -> tuple[pipeline.PreparedEntry, str]:
    """Recover a signed preview, or raise ValueError.

    Callers turn the ValueError into "parse it again" rather than guessing at
    what the user meant - a token that fails here is either tampered with or
    from an older deploy, and neither is worth writing rows for.
    """
    token = (token or "").strip()
    if not token or len(token) > _MAX_TOKEN_CHARS:
        raise ValueError("missing or oversized preview token")
    packed, separator, signature = token.partition(".")
    if not separator:
        raise ValueError("malformed preview token")
    expected = hmac.new(_token_key(), packed.encode("ascii", "ignore"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("preview token signature does not match")
    try:
        data = json.loads(zlib.decompress(base64.urlsafe_b64decode(packed)))
    except (binascii.Error, zlib.error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"unreadable preview token: {exc}") from exc
    prepared = pipeline.PreparedEntry.from_dict(data.get("prepared") or {})
    return prepared, _clean_mode(data.get("mode"))


def _clean_mode(value: Optional[str]) -> str:
    """Replace only ever comes from an exact match, so a stray value cannot delete a day."""
    return "replace" if value == "replace" else "add"


# --------------------------------------------------------------------------
# Forms and previews
# --------------------------------------------------------------------------


def _hidden_text(name: str, value: str) -> str:
    """Carry multi-line text between forms.

    A journal entry has newlines in it, and an attribute value is not a
    dependable place to keep them - a hidden <input> is subject to value
    sanitization and attribute-value normalization, either of which can flatten
    the entry into one line. A hidden <textarea> submits its content verbatim.
    No newline after the opening tag: the parser eats a leading one.
    """
    return f'<textarea name="{html.escape(name)}" hidden>{html.escape(value)}</textarea>'


def _log_form(session_date: str, raw_text: str = "", mode: str = "add") -> str:
    """The entry form, also used to reopen an entry for editing after a parse."""
    add_selected = " selected" if mode != "replace" else ""
    replace_selected = " selected" if mode == "replace" else ""
    return f"""
<form method="post" action="/log">
  <label for="session_date">Session date</label>
  <input type="date" id="session_date" name="session_date"
    value="{html.escape(session_date)}" required>
  <label for="raw_text">Journal entry</label>
  <textarea id="raw_text" name="raw_text" required
    placeholder="Paste straight from Notes. Messy is fine."
    >{html.escape(raw_text)}</textarea>
  <label for="mode">If that day already has entries</label>
  <select id="mode" name="mode">
    <option value="add"{add_selected}>Add to them &mdash; a second session, or more sets</option>
    <option value="replace"{replace_selected}>Replace them &mdash; discard that day and use this instead</option>
  </select>
  <button type="submit">Parse &amp; preview</button>
</form>
<p class="muted">Parsing shows you what was read out of the text.
Nothing is saved until you confirm it on the next screen. Sets below the
confidence threshold ({pipeline.CONFIDENCE_THRESHOLD:.0%}) are listed for review
instead of being saved. Bodyweight mentions in the same entry are picked up
automatically. <strong>Replace</strong> deletes everything already logged on
that date, so use it to correct a bad entry &mdash; not to add an evening
session.</p>
"""


def _flag_labels(workout_set) -> str:
    flags = []
    if workout_set.is_warmup:
        flags.append('<span class="flag">warm-up</span>')
    if workout_set.is_dropset:
        flags.append('<span class="flag">drop set</span>')
    if workout_set.pain_flag:
        flags.append('<span class="flag flag-pain">pain</span>')
    return "".join(flags)


def _load_label(workout_set) -> str:
    """Weight x reps, with the clean count spelled out when reps were cheated."""
    weight = f"{workout_set.weight_kg:g}kg" if workout_set.weight_kg is not None else "?kg"
    reps = str(workout_set.reps) if workout_set.reps is not None else "?"
    detail = f"{html.escape(weight)} &times; {html.escape(reps)}"
    if workout_set.cheat_reps and workout_set.reps is not None:
        clean = max(0, workout_set.reps - workout_set.cheat_reps)
        detail += (
            f' <span class="muted">({workout_set.cheat_reps} cheat &rarr; {clean} clean)</span>'
        )
    return detail


def _render_set_rows(prepared: pipeline.PreparedEntry) -> str:
    rows = []
    for workout_set, confidence in prepared.accepted_sets:
        time_label = pipeline.local_time_label(
            workout_set.logged_at_local, prepared.session_date
        )
        rows.append(
            "<tr>"
            f'<td class="num muted">{html.escape(time_label)}</td>'
            f"<td>{html.escape(workout_set.exercise_name)}"
            + (
                f'<br><span class="muted">{html.escape(workout_set.muscle_group)}</span>'
                if workout_set.muscle_group
                else ""
            )
            + "</td>"
            f'<td class="num">{_load_label(workout_set)}</td>'
            f"<td>{_flag_labels(workout_set)}</td>"
            f'<td class="num muted">{confidence:.2f}</td>'
            "</tr>"
        )
    return "".join(rows)


def _render_review_card(review_items) -> str:
    if not review_items:
        return ""
    rows = []
    for item in review_items:
        confidence = f"{item.confidence:.2f}" if item.confidence is not None else "n/a"
        label = html.escape(str(item.payload.get("exercise_name") or item.kind))
        weight = item.payload.get("weight_kg")
        reps = item.payload.get("reps")
        detail = ""
        if weight is not None or reps is not None:
            detail = f" ({html.escape(str(weight))}kg &times; {html.escape(str(reps))})"
        rows.append(
            f"<li><strong>{label}</strong>{detail} &mdash; confidence {confidence}. "
            f"{html.escape(item.reason)}</li>"
        )
    return (
        '<div class="card warn"><strong>Not being saved '
        f"({len(review_items)})</strong><ul>{''.join(rows)}</ul>"
        "<p class=\"muted\">Fix the wording in the entry and parse again if any of "
        "these should have been read.</p></div>"
    )


def _render_preview(prepared: pipeline.PreparedEntry, mode: str, token: str) -> str:
    """The screen between parsing and writing. Nothing here has been saved yet."""
    session_date = html.escape(prepared.session_date.isoformat())
    existing = prepared.existing_on_date or {}
    existing = {"sets": int(existing.get("sets") or 0),
                "bodyweight": int(existing.get("bodyweight") or 0)}
    set_count = len(prepared.accepted_sets)
    bodyweight_count = 1 if prepared.accepted_bodyweight is not None else 0

    parts = [
        (
            f'<div class="card ok"><strong>Nothing saved yet.</strong> This is what was '
            f"read out of your entry for {session_date} &mdash; {set_count} set(s) and "
            f"{bodyweight_count} bodyweight entry/entries ready to save.</div>"
        )
    ]

    if mode == "replace" and (existing["sets"] or existing["bodyweight"]):
        parts.append(
            '<div class="card err"><strong>Replace will delete first.</strong> '
            f"{session_date} currently holds {existing['sets']} set(s) and "
            f"{existing['bodyweight']} bodyweight entry/entries. Saving discards all "
            "of them and stores the rows below instead. This cannot be undone.</div>"
        )
    elif mode == "replace":
        parts.append(
            '<div class="card muted">Replace is selected, but nothing is logged on '
            f"{session_date} yet, so there is nothing to discard.</div>"
        )
    elif existing["sets"] or existing["bodyweight"]:
        parts.append(
            f'<div class="card muted">{session_date} already holds {existing["sets"]} '
            f"set(s) and {existing['bodyweight']} bodyweight entry/entries. These will "
            "be added alongside them &mdash; go back and choose Replace if you meant to "
            "overwrite the day.</div>"
        )

    if prepared.accepted_sets:
        parts.append(
            '<h2>Sets to save</h2><div class="scroll-x"><table class="preview-table">'
            "<thead><tr><th>Time</th><th>Exercise</th><th>Load</th><th>Flags</th>"
            "<th>Conf.</th></tr></thead>"
            f"<tbody>{_render_set_rows(prepared)}</tbody></table></div>"
            f'<p class="muted">Times are {html.escape(pipeline.LOCAL_TIMEZONE)}; '
            "&ldquo;~&rdquo; means the text carried no time marker, so the default hour "
            f"({pipeline.DEFAULT_SESSION_HOUR}:00) was used.</p>"
        )

    if prepared.accepted_bodyweight is not None:
        entry, confidence = prepared.accepted_bodyweight
        body_fat = (
            f", {entry.body_fat_pct:g}% body fat" if entry.body_fat_pct is not None else ""
        )
        parts.append(
            f'<div class="card"><strong>Bodyweight</strong> {entry.weight_kg:g}kg'
            f'{html.escape(body_fat)} <span class="muted">(confidence '
            f"{confidence:.2f})</span></div>"
        )

    parts.append(_render_review_card(prepared.review_items))

    save_label = (
        f"Replace {session_date} with these {set_count} set(s)"
        if mode == "replace"
        else f"Save {set_count} set(s)"
    )
    button_class = "danger" if mode == "replace" else ""
    parts.append(
        f"""
<div class="actions">
  <form method="post" action="/log/confirm" onsubmit="this.querySelector('button').disabled=true">
    <input type="hidden" name="token" value="{html.escape(token)}">
    <button type="submit" class="{button_class}">{save_label}</button>
  </form>
  <form method="post" action="/log/edit">
    {_hidden_text("raw_text", prepared.raw_text)}
    <input type="hidden" name="session_date" value="{session_date}">
    <input type="hidden" name="mode" value="{html.escape(mode)}">
    <button type="submit" class="secondary">Back &mdash; edit the text</button>
  </form>
</div>
<p class="muted">Leaving this page without saving discards the parse; the entry
is not stored anywhere until you press save.</p>
<details><summary class="muted">The text this was read from</summary>
<pre>{html.escape(prepared.raw_text)}</pre></details>
"""
    )
    return "".join(parts)


def _render_prior_detail(detail: list) -> str:
    """What the matched submission actually saved, exercise by exercise.

    Without this the guard just asserts "duplicate" and gives you no way to tell
    whether it is right - which is exactly the moment you need to know, because
    the sets it saved may not be the ones you meant to log.
    """
    if not detail:
        return (
            '<p class="muted">Those rows are on this date with the same text, but the '
            "exercises behind them could not be listed.</p>"
        )
    rows = "".join(
        f"<tr><td>{html.escape(str(row['exercise']))}</td>"
        f'<td class="num">{int(row["sets"])} set(s)</td></tr>'
        for row in detail
    )
    return (
        '<p class="muted">That earlier save produced these rows. If this is not what '
        "your entry says, it was parsed wrong &mdash; replace the day.</p>"
        '<div class="scroll-x"><table class="preview-table">'
        "<thead><tr><th>Exercise</th><th>Saved</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
    )


def _render_duplicate_choice(
    raw_text: str, session_date: date, prior: dict, detail: list = ()
) -> str:
    """Shown instead of parsing when this exact text was saved moments ago.

    The guard exists for a double-tapped submit, but re-pasting the same text on
    purpose - to replace a bad parse - looks identical to one. So it offers both
    ways out rather than refusing, which is what made a deliberate replace
    silently do nothing.
    """
    escaped_date = html.escape(session_date.isoformat())
    return f"""
<div class="card warn"><strong>You already saved this text.</strong>
Character for character the same entry went in against {escaped_date} within the
last {pipeline.DUPLICATE_WINDOW_MINUTES} minutes &mdash;
{prior.get('inserted_sets', 0)} set(s) and
{prior.get('inserted_bodyweight', 0)} bodyweight entry/entries. Nothing has been
parsed or saved this time.</div>
{_render_prior_detail(list(detail))}
<p>If you double-tapped save, you are done &mdash; the sets are in. Otherwise pick one:</p>
<div class="actions">
  <form method="post" action="/log">
    {_hidden_text("raw_text", raw_text)}
    <input type="hidden" name="session_date" value="{escaped_date}">
    <input type="hidden" name="mode" value="replace">
    <input type="hidden" name="force" value="1">
    <button type="submit" class="danger">Replace that day with this entry</button>
  </form>
  <form method="post" action="/log">
    {_hidden_text("raw_text", raw_text)}
    <input type="hidden" name="session_date" value="{escaped_date}">
    <input type="hidden" name="mode" value="add">
    <input type="hidden" name="force" value="1">
    <button type="submit" class="secondary">Parse it again and add a second copy</button>
  </form>
</div>
<p class="muted">Both parse the text again and show it before anything is written.</p>
<p><a href="/">Back to the form</a></p>
"""


def _render_result(result: pipeline.PipelineResult, session_date: date) -> str:
    parts: list[str] = []

    if result.error:
        parts.append(f'<div class="card err"><strong>Error.</strong> {html.escape(result.error)}</div>')

    if result.duplicate_of_recent and result.duplicate_reason == "already_committed":
        parts.append(
            '<div class="card warn"><strong>Already saved.</strong> This preview had '
            f"already been saved, so nothing went in twice. It stored "
            f"{result.inserted_sets} set(s) and {result.inserted_bodyweight} "
            "bodyweight entry/entries.</div>"
        )
    elif result.duplicate_of_recent:
        parts.append(
            '<div class="card warn"><strong>Duplicate submission.</strong> '
            "This exact text was already saved within the last "
            f"{pipeline.DUPLICATE_WINDOW_MINUTES} minutes, so nothing was re-inserted. "
            f"The earlier run saved {result.inserted_sets} set(s) and "
            f"{result.inserted_bodyweight} bodyweight entry/entries. To overwrite that "
            'day instead, <a href="/">paste the entry again</a> and choose Replace.</div>'
        )
    else:
        parts.append(
            f'<div class="card ok"><strong>Inserted {result.inserted_sets} set(s)</strong> and '
            f"{result.inserted_bodyweight} bodyweight entry/entries for "
            f"{html.escape(session_date.isoformat())}.</div>"
        )

    if result.replaced:
        removed = result.replaced
        parts.append(
            '<div class="card warn"><strong>Replaced that day.</strong> Removed '
            f"{removed['sets']} set(s) and {removed['bodyweight']} bodyweight "
            "entry/entries logged earlier on this date before saving this one.</div>"
        )

    if result.exercises_created:
        names = ", ".join(html.escape(name) for name in result.exercises_created)
        parts.append(f'<div class="card muted">New exercises created: {names}</div>')

    if result.exercises_matched:
        rows = "".join(
            f"<li>{html.escape(proposed)} &rarr; matched existing <strong>{html.escape(matched)}</strong></li>"
            for proposed, matched in result.exercises_matched
        )
        parts.append(f'<div class="card muted"><strong>Fuzzy-matched names</strong><ul>{rows}</ul></div>')

    if result.review_items:
        parts.append(_render_review_card(result.review_items))
    elif not result.error and not result.duplicate_of_recent:
        parts.append('<div class="card muted">Nothing needed review.</div>')

    return "".join(parts)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/", response_class=HTMLResponse)
async def index(_user: str = Depends(require_auth)) -> HTMLResponse:
    return _page(
        "Log a workout",
        "<h1>Log a workout</h1>" + _log_form(pipeline.local_today().isoformat()),
    )


@app.post("/log/edit", response_class=HTMLResponse)
async def log_edit(
    raw_text: str = Form(""),
    session_date: str = Form(""),
    mode: str = Form("add"),
    _user: str = Depends(require_auth),
) -> HTMLResponse:
    """Reopen the form on the entry just previewed, so a bad parse can be reworded."""
    parsed_date = _parse_session_date(session_date) or pipeline.local_today()
    return _page(
        "Log a workout",
        "<h1>Edit and parse again</h1>"
        + _log_form(parsed_date.isoformat(), raw_text, _clean_mode(mode)),
    )


def _parse_session_date(value: str) -> Optional[date]:
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _bad_date_page(back: str) -> HTMLResponse:
    return _page(
        "Invalid date",
        '<h1>Result</h1><div class="card err">Session date must be YYYY-MM-DD.</div>'
        f'<p><a href="{back}">Back</a></p>',
    )


@app.post("/log", response_class=HTMLResponse)
async def log_entry(
    session_date: str = Form(...),
    raw_text: str = Form(...),
    mode: str = Form("add"),
    force: str = Form(""),
    _user: str = Depends(require_auth),
) -> HTMLResponse:
    """Parse and show. This route writes nothing - /log/confirm does that."""
    parsed_date = _parse_session_date(session_date)
    if parsed_date is None:
        return _bad_date_page("/")

    mode = _clean_mode(mode)
    allow_duplicate = force == "1"
    prepared = pipeline.prepare_entry(
        raw_text,
        parsed_date,
        engine=get_engine(),
        check_duplicates=True,
        allow_duplicate=allow_duplicate,
    )
    logger.info(
        "event=log_previewed date=%s mode=%s forced=%s sets=%d bodyweight=%d review=%d "
        "duplicate=%s error=%s",
        parsed_date,
        mode,
        allow_duplicate,
        len(prepared.accepted_sets),
        1 if prepared.accepted_bodyweight is not None else 0,
        len(prepared.review_items),
        prepared.duplicate_of_recent,
        bool(prepared.error),
    )

    if prepared.duplicate_of_recent:
        return _page(
            "Already logged",
            "<h1>Already logged</h1>"
            + _render_duplicate_choice(
                prepared.raw_text,
                parsed_date,
                prepared.prior_submission or {},
                prepared.prior_detail,
            ),
        )

    if prepared.error:
        return _page(
            "Could not parse",
            f'<h1>Could not parse</h1><div class="card err">{html.escape(prepared.error)}</div>'
            + _render_review_card(prepared.review_items)
            + '<p><a href="/">Back to the form</a></p>',
        )

    if not prepared.has_insertable:
        return _page(
            "Nothing to save",
            '<h1>Nothing to save</h1><div class="card warn">Nothing in that entry '
            "cleared the confidence threshold, so there is nothing to write.</div>"
            + _render_review_card(prepared.review_items)
            + '<p><a href="/">Back to the form</a></p>',
        )

    return _page(
        "Check before saving",
        "<h1>Check before saving</h1>"
        + _render_preview(prepared, mode, _sign_preview(prepared, mode)),
    )


@app.post("/log/confirm", response_class=HTMLResponse)
async def log_confirm(
    token: str = Form(...),
    _user: str = Depends(require_auth),
) -> HTMLResponse:
    """Write the entry the preview showed. The only route on this page that inserts."""
    try:
        prepared, mode = _unsign_preview(token)
    except (ValueError, KeyError) as exc:
        logger.warning("event=preview_token_rejected reason=%s", exc)
        return _page(
            "Preview expired",
            '<h1>Nothing saved</h1><div class="card err">That preview could not be '
            "read back, so nothing was written. Paste the entry again and re-parse "
            "it.</div><p><a href=\"/\">Back to the form</a></p>",
        )

    result = pipeline.commit_entry(
        prepared, engine=get_engine(), replace_existing=(mode == "replace")
    )
    logger.info(
        "event=log_committed date=%s mode=%s inserted_sets=%d inserted_bodyweight=%d "
        "review=%d duplicate=%s replaced=%s",
        prepared.session_date,
        mode,
        result.inserted_sets,
        result.inserted_bodyweight,
        len(result.review_items),
        result.duplicate_reason or result.duplicate_of_recent,
        result.replaced,
    )
    return _page(
        "Result",
        f"<h1>Result</h1>{_render_result(result, prepared.session_date)}"
        '<p><a href="/">Log another</a></p>',
    )


@app.get("/progress", response_class=HTMLResponse)
async def progress(_user: str = Depends(require_auth)) -> HTMLResponse:
    """Charts over the logged data. Every figure is computed in code, as in the
    weekly report - this page plots the same Stage A numbers."""
    weeks = insights.ANALYSIS_WEEKS if insights.ANALYSIS_WEEKS >= 8 else 12
    data = insights.build_dashboard(get_engine(), weeks=weeks)
    logger.info("event=progress_rendered weeks=%d has_data=%s", weeks, data["has_data"])
    return _page(
        "Progress",
        charts.render_progress_body(data),
        extra_css=charts.PROGRESS_CSS,
        extra_js=charts.PROGRESS_JS,
    )


@app.get("/weekly-report", response_class=HTMLResponse)
async def weekly_report_form(_user: str = Depends(require_auth)) -> HTMLResponse:
    current_week = insights.week_start_for(pipeline.local_today())
    return _page(
        "Weekly report",
        f"""
<h1>Weekly report</h1>
<p class="muted">All figures are computed in code. The language model only writes
the prose around them.</p>
<form method="post" action="/weekly-report">
  <label for="week_start">Week starting (Monday)</label>
  <input type="date" id="week_start" name="week_start" value="{current_week.isoformat()}">
  <button type="submit">Generate weekly report</button>
</form>
<p class="muted">Regenerating a week overwrites that week's stored report.
Pain safeguard is currently <strong>{'on' if insights.PAIN_SAFEGUARD_ENABLED else 'OFF'}</strong>.</p>
""",
    )


@app.post("/weekly-report", response_class=HTMLResponse)
async def weekly_report_generate(
    week_start: Optional[str] = Form(None),
    _user: str = Depends(require_auth),
) -> HTMLResponse:
    parsed_week: Optional[date] = None
    if week_start and week_start.strip():
        try:
            parsed_week = datetime.strptime(week_start.strip(), "%Y-%m-%d").date()
        except ValueError:
            return _page(
                "Invalid date",
                '<h1>Weekly report</h1><div class="card err">Week start must be YYYY-MM-DD.</div>'
                '<p><a href="/weekly-report">Back</a></p>',
            )

    report = insights.generate_weekly_report(get_engine(), week_start=parsed_week)
    logger.info(
        "event=report_generated week_start=%s records=%d narration_error=%s",
        report["week_start_date"],
        report["record_count"],
        bool(report["narration_error"]),
    )

    banner = ""
    if report["record_count"] == 0:
        banner = (
            '<div class="card warn">No sets logged in the analysis window, so the '
            "report has nothing to work from.</div>"
        )
    elif report["narration_error"]:
        banner = (
            '<div class="card warn">The narration step failed, so this is the '
            "deterministic summary. Every number is unaffected &mdash; they are all "
            "computed in code.</div>"
        )

    program_note = report["recommendations"]["program_note"]
    if program_note["stagnation_flagged"]:
        banner += (
            '<div class="card warn"><strong>Program-level stagnation flagged.</strong> '
            f"{html.escape(program_note['detail']['reason'])}</div>"
        )

    return _page(
        "Weekly report",
        f"""<h1>Week of {html.escape(str(report['week_start_date']))}</h1>
{banner}
<div class="card">{_render_markdown(report['summary_text'])}</div>
<p class="muted">Saved to <code>weekly_reports</code> ({report['record_count']} sets analysed).</p>
<p><a href="/weekly-report">Generate another</a></p>""",
    )
