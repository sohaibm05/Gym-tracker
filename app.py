"""FastAPI web app — the phone-facing front end for the gym tracker.

Routes:
    GET  /               mobile-friendly entry form
    POST /log            parse a pasted journal entry into an editable review form
    POST /save           write the reviewed rows to the database
    GET  /weekly-report  report page with a "Generate weekly report" button
    POST /weekly-report  run Stage A + Stage B and upsert the report
    GET  /healthz        unauthenticated liveness probe

Saving is deliberately two steps. POST /log only extracts; it renders every
prospective database row for editing, with anything missing or invalid marked in
red. Nothing is written until POST /save, and what it writes is the reviewed
form, not the raw extraction.

All extraction/validation/insert logic is imported from pipeline.py; all report
logic from insights.py; the review form from review.py. Nothing is
reimplemented here.
"""

from __future__ import annotations

import html
import logging
import os
import re
import secrets
import sys
import time
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
    import review  # noqa: E402
except Exception:  # noqa: BLE001 - report anything that stops the app booting
    import traceback

    _STARTUP_ERROR = traceback.format_exc()
    charts = insights = pipeline = review = None  # type: ignore[assignment]

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


def _render_result(result: pipeline.PipelineResult, session_date: date) -> str:
    parts: list[str] = []

    if result.error:
        parts.append(f'<div class="card err"><strong>Error.</strong> {html.escape(result.error)}</div>')

    if result.duplicate_of_recent:
        parts.append(
            '<div class="card warn"><strong>Duplicate submission.</strong> '
            "This exact text was already processed within the last "
            f"{pipeline.DUPLICATE_WINDOW_MINUTES} minutes, so nothing was re-inserted. "
            f"The earlier run saved {result.inserted_sets} set(s) and "
            f"{result.inserted_bodyweight} bodyweight entry/entries.</div>"
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

    skipped = result.skipped_sets + result.skipped_bodyweight
    if skipped:
        parts.append(
            f'<div class="card muted">You unticked {skipped} row(s) on the review '
            "screen, so they were not saved.</div>"
        )

    if result.review_items:
        rows = []
        for item in result.review_items:
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
        parts.append(
            '<div class="card err"><strong>Could not be saved '
            f"({len(result.review_items)})</strong> &mdash; these parts of the entry "
            f"could not be turned into editable rows.<ul>{''.join(rows)}</ul></div>"
        )
    elif not result.error and not result.duplicate_of_recent and not skipped:
        parts.append('<div class="card muted">Everything you ticked was saved.</div>')

    return "".join(parts)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/", response_class=HTMLResponse)
async def index(_user: str = Depends(require_auth)) -> HTMLResponse:
    today = date.today().isoformat()
    return _page(
        "Log a workout",
        f"""
<h1>Log a workout</h1>
<form method="post" action="/log">
  <label for="session_date">Session date</label>
  <input type="date" id="session_date" name="session_date" value="{today}" required>
  <label for="raw_text">Journal entry</label>
  <textarea id="raw_text" name="raw_text" required
    placeholder="Paste straight from Notes. Messy is fine."></textarea>
  <label for="mode">If that day already has entries</label>
  <select id="mode" name="mode">
    <option value="add" selected>Add to them &mdash; a second session, or more sets</option>
    <option value="replace">Replace them &mdash; discard that day and use this instead</option>
  </select>
  <button type="submit">Parse &amp; review</button>
</form>
<p class="muted">Parsing shows you every row it is about to write, with anything
missing or invalid marked in red. Nothing is saved until you have looked at it
and pressed save. Bodyweight mentions in the same entry are picked up
automatically. <strong>Replace</strong> deletes everything already logged on that
date, so use it to correct a bad entry &mdash; not to add an evening session.</p>
""",
    )


def _parse_session_date(value: str) -> Optional[date]:
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _invalid_date_page() -> HTMLResponse:
    return _page(
        "Invalid date",
        '<h1>Result</h1><div class="card err">Session date must be YYYY-MM-DD.</div>'
        '<p><a href="/">Back</a></p>',
    )


def _existing_counts(session_date: date) -> Optional[dict[str, int]]:
    """What is already logged on that date, for the review banner.

    Best effort: this only decorates the page, so a database that is briefly
    unreachable should not cost the user the extraction they just paid for.
    """
    try:
        return pipeline.count_entries_for_date(get_engine(), session_date)
    except Exception:  # noqa: BLE001 - the save step will surface a real outage
        logger.warning("event=existing_counts_failed date=%s", session_date, exc_info=True)
        return None


def _review_page(draft: pipeline.EntryDraft) -> HTMLResponse:
    return _page(
        "Check before saving",
        review.render_review_body(
            draft, _existing_counts(draft.session_date), pipeline.CONFIDENCE_THRESHOLD
        ),
        extra_css=review.REVIEW_CSS,
    )


@app.post("/log", response_class=HTMLResponse)
async def log_entry(
    session_date: str = Form(...),
    raw_text: str = Form(...),
    mode: str = Form("add"),
    _user: str = Depends(require_auth),
) -> HTMLResponse:
    """Extract only. Nothing reaches the database until POST /save."""
    parsed_date = _parse_session_date(session_date)
    if parsed_date is None:
        return _invalid_date_page()

    draft = pipeline.build_draft(raw_text, parsed_date)
    draft.replace_existing = mode == "replace"
    logger.info(
        "event=entry_drafted date=%s mode=%s sets=%d bodyweight=%s flagged=%d blocked=%d error=%s",
        parsed_date,
        mode,
        len(draft.sets),
        draft.bodyweight is not None,
        draft.flagged_count,
        draft.blocked_count,
        bool(draft.error),
    )

    if draft.error:
        notes = "".join(
            f'<div class="card muted">{html.escape(item.reason)}</div>'
            for item in draft.review_items
        )
        return _page(
            "Could not parse",
            f'<h1>Could not parse that entry</h1>'
            f'<div class="card err">{html.escape(draft.error)}</div>{notes}'
            '<p class="muted">Nothing was saved. Go back and try again — the entry '
            "is unchanged.</p>"
            '<p><a href="/">Back</a></p>',
        )

    return _review_page(draft)


@app.post("/save", response_class=HTMLResponse)
async def save_reviewed(request: Request, _user: str = Depends(require_auth)) -> HTMLResponse:
    """Write the reviewed form, or hand it back with one more empty slot.

    The rows are rescored from the submitted values rather than from the
    extraction, so a corrected field is judged on what the user actually typed.
    A row the database would still reject sends the whole form back for another
    pass instead of being quietly dropped.
    """
    form = await request.form()
    try:
        draft = review.draft_from_form(form)
    except ValueError:
        return _invalid_date_page()

    # "Add a set" posts the same form; it hands back the draft plus an empty slot
    # rather than saving, so a half-corrected row is not judged in passing.
    if review.add_row(draft, str(form.get("action", "save"))):
        return _review_page(draft)

    if not draft.ready:
        logger.info("event=save_blocked date=%s blocked=%d", draft.session_date, draft.blocked_count)
        return _review_page(draft)

    result = pipeline.commit_draft(draft, engine=get_engine(), check_duplicates=True)
    logger.info(
        "event=entry_saved date=%s replace=%s inserted_sets=%d inserted_bodyweight=%d "
        "skipped=%d review=%d duplicate=%s replaced=%s",
        draft.session_date,
        draft.replace_existing,
        result.inserted_sets,
        result.inserted_bodyweight,
        result.skipped_sets + result.skipped_bodyweight,
        len(result.review_items),
        result.duplicate_of_recent,
        result.replaced,
    )
    return _page(
        "Saved",
        f'<h1>Saved</h1>{_render_result(result, draft.session_date)}'
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
    current_week = insights.week_start_for(date.today())
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
