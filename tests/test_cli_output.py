"""Tests for the dry-run line formatter.

Pure string formatting, but it is the only place the extracted timestamp and
cheat-rep count are visible before anything reaches the database — so a bug
here means testing the extraction blind.
"""

from __future__ import annotations

import os
import re
from datetime import date

import pytest

import app
import parse_workout_log
import pipeline
from parse_workout_log import format_set_line
from pipeline import WorkoutSet

SESSION = date(2026, 8, 19)


def line_for(**kwargs) -> str:
    defaults = dict(exercise_name="Lateral Raise", weight_kg=7.5, reps=10)
    defaults.update(kwargs)
    confidence = defaults.pop("confidence", 1.0)
    return format_set_line(WorkoutSet(**defaults), confidence, SESSION)


class TestVerdict:
    def test_above_threshold_is_insert(self):
        assert "[INSERT]" in line_for(confidence=1.0)

    def test_below_threshold_is_review(self):
        assert "[REVIEW]" in line_for(confidence=0.65)

    def test_confidence_is_shown(self):
        assert "0.65" in line_for(confidence=0.65)


class TestRepsAndWeight:
    def test_plain_set(self):
        assert "7.5kg x 10" in line_for()

    def test_missing_weight_and_reps_render_as_question_marks(self):
        assert "?kg x ?" in line_for(weight_kg=None, reps=None)

    def test_integer_weight_has_no_trailing_zero(self):
        assert "30kg" in line_for(weight_kg=30.0)


class TestCheatReps:
    def test_cheat_reps_show_the_clean_count(self):
        assert "(3 cheat -> 7 clean)" in line_for(reps=10, cheat_reps=3)

    def test_no_annotation_when_nothing_was_cheated(self):
        assert "cheat" not in line_for(reps=10, cheat_reps=0)

    def test_fully_cheated_set_shows_zero_clean(self):
        assert "(5 cheat -> 0 clean)" in line_for(reps=5, cheat_reps=5)


class TestFlags:
    def test_warmup(self):
        assert "(warmup)" in line_for(is_warmup=True)

    def test_dropset(self):
        assert "(dropset)" in line_for(is_dropset=True)

    def test_pain(self):
        assert "(PAIN)" in line_for(pain_flag=True)

    def test_no_flags_on_a_plain_working_set(self):
        line = line_for()
        assert "(warmup)" not in line and "(PAIN)" not in line


class TestTimeLabel:
    def test_extracted_afternoon_time_is_shown(self, monkeypatch):
        monkeypatch.setattr(pipeline, "LOCAL_TIMEZONE", "UTC")
        assert "16:35" in line_for(logged_at_local="2026-08-19T16:35:00")

    def test_missing_time_marker_is_flagged_with_a_tilde(self, monkeypatch):
        monkeypatch.setattr(pipeline, "LOCAL_TIMEZONE", "UTC")
        line = line_for(logged_at_local=None)
        assert "~" in line

    def test_supplied_time_is_not_flagged(self, monkeypatch):
        monkeypatch.setattr(pipeline, "LOCAL_TIMEZONE", "UTC")
        assert "~" not in line_for(logged_at_local="2026-08-19T16:35:00")

    def test_time_is_rendered_in_the_configured_local_zone(self, monkeypatch):
        monkeypatch.setattr(pipeline, "LOCAL_TIMEZONE", "Asia/Karachi")
        # 16:35 local is stored as 11:35Z, and must display as 16:35 again.
        assert "16:35" in line_for(logged_at_local="2026-08-19T16:35:00")

    def test_a_timestamp_on_the_wrong_day_falls_back_to_the_default_hour(self, monkeypatch):
        monkeypatch.setattr(pipeline, "LOCAL_TIMEZONE", "UTC")
        line = line_for(logged_at_local="2026-01-01T09:00:00")
        assert "09:00" not in line


# --------------------------------------------------------------------------
# --check-config value masking: a diagnostic must never print a secret
# --------------------------------------------------------------------------


class TestMask:
    def test_database_url_password_is_replaced(self):
        url = "postgresql://postgres.abc:SuperSecret@host.pooler.supabase.com:5432/postgres"
        masked = parse_workout_log._mask("DATABASE_URL", url)
        assert "SuperSecret" not in masked
        assert "***" in masked

    def test_database_url_keeps_the_diagnosable_parts(self):
        url = "postgresql://postgres.abc:SuperSecret@host.pooler.supabase.com:5432/postgres"
        masked = parse_workout_log._mask("DATABASE_URL", url)
        assert "postgres.abc" in masked
        assert "host.pooler.supabase.com" in masked
        assert "5432" in masked

    def test_database_url_without_a_password_is_unchanged(self):
        url = "postgresql://postgres@localhost:5432/postgres"
        assert parse_workout_log._mask("DATABASE_URL", url) == url

    def test_unparseable_database_url_does_not_leak_it(self):
        bad = "postgresql://user:[YOUR-PASSWORD]@host:5432/db"
        assert parse_workout_log._mask("DATABASE_URL", bad) == "<unparseable>"

    def test_api_key_shows_only_its_ends(self):
        masked = parse_workout_log._mask("GROQ_API_KEY", "gsk_abcdefghijklmnop")
        assert "abcdefghijklmn" not in masked
        assert masked.startswith("gsk_")
        assert "(20 chars)" in masked

    def test_short_secret_is_fully_starred(self):
        assert parse_workout_log._mask("APP_PASSWORD", "abc123") == "******"


# --------------------------------------------------------------------------
# Vercel entrypoint: api/index.py must expose the same app, importable from api/
# --------------------------------------------------------------------------


class TestVercelEntrypoint:
    """Regression tests for the serverless cold-start import failure.

    Vercel loads app.py by path as `__vc_module`, without putting its own
    directory on sys.path. That made `import insights` raise
    ModuleNotFoundError on every invocation while the build itself succeeded,
    so the only symptom was FUNCTION_INVOCATION_FAILED with no local repro.
    """

    def test_app_imports_when_loaded_by_path(self):
        """The exact thing Vercel does. Fails without the sys.path bootstrap."""
        import importlib.util
        from pathlib import Path

        entry = Path(__file__).resolve().parent.parent / "app.py"
        spec = importlib.util.spec_from_file_location("vc_module_probe", entry)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        assert type(module.app).__name__ == "FastAPI"
        paths = {r.path for r in module.app.routes if hasattr(r, "path")}
        assert {"/", "/log", "/weekly-report", "/healthz"} <= paths

    def test_vercel_json_declares_the_app_function(self):
        import json
        from pathlib import Path

        config = json.loads(
            (Path(__file__).resolve().parent.parent / "vercel.json").read_text()
        )
        assert "app.py" in config["functions"]
        assert config["functions"]["app.py"]["maxDuration"] == 60

    def test_probe_exists_for_bisecting_deploys(self):
        from pathlib import Path

        assert (Path(__file__).resolve().parent.parent / "api" / "ping.py").is_file()

    def test_tzdata_is_pinned(self):
        """zoneinfo reads the OS tz database; slim images often lack it."""
        from pathlib import Path

        requirements = (
            Path(__file__).resolve().parent.parent / "requirements.txt"
        ).read_text()
        assert "tzdata" in requirements


class TestSameDayMode:
    """Replace deletes a whole day, so it must never be the default anywhere."""

    def test_cli_replace_flag_defaults_off(self):
        args = parse_workout_log.parse_args(["entry.txt", "2026-08-20"])
        assert args.replace is False

    def test_cli_replace_flag_can_be_set(self):
        args = parse_workout_log.parse_args(["entry.txt", "2026-08-20", "--replace"])
        assert args.replace is True

    def test_form_offers_both_modes_with_add_selected(self):
        import app as app_module

        page = app_module._page("t", "").body.decode()
        assert 'name="mode"' not in page   # the nav page has no form

    def test_form_markup_defaults_to_add(self):
        import app as app_module

        form = app_module._log_form("2026-08-20")
        assert 'value="add" selected' in form
        assert 'value="replace"' in form
        assert 'value="replace" selected' not in form

    def test_form_keeps_replace_selected_when_reopened_for_editing(self):
        """Going back from the preview must not quietly downgrade a replace to an add."""
        import app as app_module

        form = app_module._log_form("2026-08-20", "bench 60x8", mode="replace")
        assert 'value="replace" selected' in form
        assert 'value="add" selected' not in form

    def test_an_unrecognised_mode_is_treated_as_add(self):
        import app as app_module

        assert app_module._clean_mode("REPLACE") == "add"
        assert app_module._clean_mode("") == "add"
        assert app_module._clean_mode(None) == "add"
        assert app_module._clean_mode("replace") == "replace"


# --------------------------------------------------------------------------
# The preview step: /log parses and shows, /log/confirm writes
# --------------------------------------------------------------------------


def _prepared(sets=None, bodyweight=None, review=(), existing=None, raw_text="bench 60kg x 8"):
    return pipeline.PreparedEntry(
        raw_text=raw_text,
        session_date=SESSION,
        accepted_sets=list(sets if sets is not None else [(WorkoutSet(
            exercise_name="Chest Bench Press", weight_kg=60, reps=8), 0.93)]),
        accepted_bodyweight=bodyweight,
        review_items=list(review),
        existing_on_date=existing or {"sets": 0, "bodyweight": 0},
    )


class TestPreviewPage:
    """What the user checks before anything is written."""

    def test_it_says_nothing_has_been_saved(self):
        page = app._render_preview(_prepared(), "add", "tok")
        assert "Nothing saved yet" in page

    def test_each_set_is_shown_with_its_load_and_confidence(self):
        page = app._render_preview(_prepared(), "add", "tok")
        assert "Chest Bench Press" in page
        assert "60kg" in page and "8" in page
        assert "0.93" in page

    def test_cheat_reps_show_the_clean_count(self):
        prepared = _prepared([(WorkoutSet(exercise_name="Row", weight_kg=40, reps=10,
                                          cheat_reps=3), 0.9)])
        assert "3 cheat &rarr; 7 clean" in app._render_preview(prepared, "add", "tok")

    def test_pain_is_flagged_where_it_can_be_seen_before_saving(self):
        prepared = _prepared([(WorkoutSet(exercise_name="Row", weight_kg=40, reps=10,
                                          pain_flag=True), 0.9)])
        assert "flag-pain" in app._render_preview(prepared, "add", "tok")

    def test_bodyweight_is_shown(self):
        prepared = _prepared(bodyweight=(pipeline.BodyweightEntry(weight_kg=82.4), 0.8))
        page = app._render_preview(prepared, "add", "tok")
        assert "82.4kg" in page

    def test_the_confirm_form_posts_the_token_to_the_write_route(self):
        page = app._render_preview(_prepared(), "add", "tok-123")
        assert 'action="/log/confirm"' in page
        assert 'name="token" value="tok-123"' in page

    def test_going_back_carries_the_text_so_it_can_be_reworded(self):
        page = app._render_preview(_prepared(raw_text="bench 60kg x 8"), "add", "tok")
        assert 'action="/log/edit"' in page
        assert 'hidden>bench 60kg x 8</textarea>' in page

    def test_a_multi_line_entry_is_not_flattened_into_an_attribute(self):
        """Journal entries have newlines; an attribute value cannot be trusted to keep them."""
        page = app._render_preview(_prepared(raw_text="line one\nline two"), "add", "tok")
        assert 'name="raw_text" value=' not in page
        assert "hidden>line one\nline two</textarea>" in page

    def test_an_exercise_name_from_the_model_is_escaped(self):
        """Names reach this page straight from LLM output."""
        prepared = _prepared([(WorkoutSet(exercise_name="<script>alert(1)</script>",
                                          weight_kg=40, reps=10), 0.9)])
        page = app._render_preview(prepared, "add", "tok")
        assert "<script>" not in page and "&lt;script&gt;" in page

    def test_the_raw_text_is_escaped_in_both_places_it_appears(self):
        prepared = _prepared(raw_text='</textarea><script>x</script>')
        page = app._render_preview(prepared, "add", "tok")
        assert "<script>" not in page

    def test_review_items_are_listed_as_not_being_saved(self):
        prepared = _prepared(review=[pipeline.ReviewItem(
            "workout_set", "confidence 0.40 below threshold 0.70", 0.4,
            {"exercise_name": "Mystery Lift", "weight_kg": 20, "reps": 5})])
        page = app._render_preview(prepared, "add", "tok")
        assert "Not being saved" in page and "Mystery Lift" in page


class TestPreviewWarnsBeforeOverwriting:
    """Replace deletes a whole day, so the preview has to say what goes."""

    def test_replace_names_the_counts_it_will_delete(self):
        prepared = _prepared(existing={"sets": 14, "bodyweight": 1})
        page = app._render_preview(prepared, "replace", "tok")
        assert "Replace will delete first" in page
        assert "14 set(s)" in page and "1 bodyweight" in page
        assert "cannot be undone" in page

    def test_replace_on_an_empty_day_says_there_is_nothing_to_discard(self):
        page = app._render_preview(_prepared(existing={"sets": 0, "bodyweight": 0}),
                                   "replace", "tok")
        assert "nothing to discard" in page
        assert "Replace will delete first" not in page

    def test_add_onto_an_occupied_day_says_so_without_a_delete_warning(self):
        page = app._render_preview(_prepared(existing={"sets": 14, "bodyweight": 0}),
                                   "add", "tok")
        assert "already holds 14 set(s)" in page
        assert "Replace will delete first" not in page

    def test_the_replace_button_is_marked_as_destructive(self):
        page = app._render_preview(_prepared(existing={"sets": 14, "bodyweight": 0}),
                                   "replace", "tok")
        assert 'class="danger"' in page

    def test_the_add_button_is_not(self):
        assert 'class="danger"' not in app._render_preview(_prepared(), "add", "tok")


class TestPreviewToken:
    """The parse round-trips through the browser, so it is signed on the way out."""

    @pytest.fixture(autouse=True)
    def _secret(self, monkeypatch):
        monkeypatch.setenv("APP_PASSWORD", "hunter2")
        monkeypatch.delenv("APP_PREVIEW_SECRET", raising=False)

    def test_a_signed_preview_comes_back_intact(self):
        prepared = _prepared()
        restored, mode = app._unsign_preview(app._sign_preview(prepared, "replace"))
        assert mode == "replace"
        assert restored.accepted_sets[0][0].exercise_name == "Chest Bench Press"
        assert restored.accepted_sets[0][1] == 0.93
        assert restored.session_date == SESSION

    def test_an_edited_payload_is_refused(self):
        signature = app._sign_preview(_prepared(), "add").partition(".")[2]
        tampered = app._sign_preview(_prepared(raw_text="something else"), "add").split(".")[0]
        with pytest.raises(ValueError):
            app._unsign_preview(f"{tampered}.{signature}")

    def test_a_token_signed_with_another_secret_is_refused(self, monkeypatch):
        token = app._sign_preview(_prepared(), "add")
        monkeypatch.setenv("APP_PASSWORD", "different")
        with pytest.raises(ValueError):
            app._unsign_preview(token)

    def test_a_mode_smuggled_into_the_token_still_has_to_be_exact(self):
        prepared = _prepared()
        _, mode = app._unsign_preview(app._sign_preview(prepared, "REPLACE"))
        assert mode == "add"

    def test_garbage_is_refused_rather_than_crashing(self):
        for value in ["", "no-dot", "abc.def", "." * 10]:
            with pytest.raises(ValueError):
                app._unsign_preview(value)

    def test_an_oversized_token_is_refused_before_it_is_decompressed(self):
        with pytest.raises(ValueError):
            app._unsign_preview("x" * (app._MAX_TOKEN_CHARS + 1))


class TestDuplicateOffersAWayOut:
    """The guard used to swallow a deliberate re-paste, replace included."""

    def test_it_reports_what_the_earlier_run_saved(self):
        page = app._render_duplicate_choice(
            "bench 60kg x 8", SESSION,
            {"inserted_sets": 14, "inserted_bodyweight": 0})
        assert "14 set(s)" in page

    def test_it_names_the_date_the_match_was_found_on(self):
        page = app._render_duplicate_choice("bench", SESSION, {})
        assert SESSION.isoformat() in page

    def test_it_lists_the_exercises_behind_the_claim(self):
        page = app._render_duplicate_choice(
            "bench", SESSION, {"inserted_sets": 7},
            [{"exercise": "Leg Press", "sets": 4}, {"exercise": "Calf Raise", "sets": 3}])
        assert "Leg Press" in page and "4 set(s)" in page
        assert "Calf Raise" in page and "3 set(s)" in page

    def test_an_exercise_name_in_the_evidence_is_escaped(self):
        page = app._render_duplicate_choice(
            "bench", SESSION, {}, [{"exercise": "<img src=x>", "sets": 1}])
        assert "<img" not in page and "&lt;img" in page

    def test_it_says_so_when_the_breakdown_is_unavailable(self):
        page = app._render_duplicate_choice("bench", SESSION, {"inserted_sets": 0}, [])
        assert "could not be listed" in page

    def test_it_offers_replacing_the_day(self):
        page = app._render_duplicate_choice("bench", SESSION, {"inserted_sets": 14})
        assert 'name="mode" value="replace"' in page
        assert 'name="force" value="1"' in page

    def test_it_offers_parsing_again_as_an_addition(self):
        page = app._render_duplicate_choice("bench", SESSION, {"inserted_sets": 14})
        assert 'name="mode" value="add"' in page

    def test_the_entry_text_is_carried_through_and_escaped(self):
        page = app._render_duplicate_choice('a "quoted" <b>', SESSION, {})
        assert "<b>" not in page and "&lt;b&gt;" in page

    def test_a_multi_line_entry_survives_the_offer(self):
        page = app._render_duplicate_choice("line one\nline two", SESSION, {})
        assert "hidden>line one\nline two</textarea>" in page

    def test_a_closing_textarea_tag_in_the_entry_cannot_break_out(self):
        page = app._render_duplicate_choice("</textarea><script>x</script>", SESSION, {})
        assert "<script>" not in page


class TestWriteRoutesAreSeparate:
    def test_the_routes_the_preview_flow_needs_all_exist(self):
        paths = {r.path for r in app.app.routes if hasattr(r, "path")}
        assert {"/", "/log", "/log/confirm", "/log/edit"} <= paths

    def test_only_the_confirm_route_commits(self):
        """A guard against quietly reinstating the write on /log."""
        import inspect

        assert "commit_entry" not in inspect.getsource(app.log_entry)
        assert "prepare_entry" in inspect.getsource(app.log_entry)
        assert "commit_entry" in inspect.getsource(app.log_confirm)

    def test_a_replayed_confirm_is_reported_as_already_saved(self):
        result = pipeline.PipelineResult(
            inserted_sets=14, duplicate_of_recent=True, duplicate_reason="already_committed")
        assert "Already saved" in app._render_result(result, SESSION)

    def test_a_recent_duplicate_points_at_replace_instead_of_dead_ending(self):
        result = pipeline.PipelineResult(
            inserted_sets=14, duplicate_of_recent=True, duplicate_reason="recent_submission")
        page = app._render_result(result, SESSION)
        assert "Duplicate submission" in page and "choose Replace" in page


# --------------------------------------------------------------------------
# End to end through the real routes: parse writes nothing, confirm writes
# --------------------------------------------------------------------------


class _CannedConnection:
    """Answers the preview's reads. Anything else is a test-visible failure."""

    def __init__(self, scalars):
        self._scalars = list(scalars)

    def execute(self, statement, params=None):
        if not self._scalars:
            raise AssertionError("more reads than the test staged")
        value = self._scalars.pop(0)
        return type("R", (), {"scalar_one": lambda self, v=value: v,
                              "all": lambda self, v=value: list(v)})()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _NoWriteEngine:
    def __init__(self, scalars):
        self._scalars = scalars
        self.begin_calls = 0

    def connect(self):
        return _CannedConnection(self._scalars)

    def begin(self):
        self.begin_calls += 1
        raise AssertionError("a write transaction was opened outside /log/confirm")


LOG_FLOW_PAYLOAD = {
    "sets": [{"exercise_name": "Chest Bench Press", "weight_kg": 60, "reps": 8,
              "set_number": 1, "raw_span": "bench 60kg x 8"}],
    "bodyweight": None,
}


class TestLogFlowEndToEnd:
    AUTH = ("me", "hunter2")

    @pytest.fixture
    def client(self, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.setenv("APP_USERNAME", "me")
        monkeypatch.setenv("APP_PASSWORD", "hunter2")
        monkeypatch.delenv("APP_PREVIEW_SECRET", raising=False)
        monkeypatch.setattr(
            pipeline, "extract_entities",
            lambda raw_text, session_date, client=None: LOG_FLOW_PAYLOAD,
        )
        return TestClient(app.app)

    def _engine(self, monkeypatch, existing_sets=0, duplicate_sets=0, detail=()):
        from datetime import datetime, timezone

        engine = _NoWriteEngine([
            datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc),  # SELECT now()
            existing_sets, 0,                                    # counts on the date
            duplicate_sets, 0,                                   # recent duplicate
            list(detail),                                        # what it saved
        ])
        monkeypatch.setattr(app, "_engine", engine)
        return engine

    def _post_log(self, client, **overrides):
        form = {"session_date": "2026-08-19", "raw_text": "bench 60kg x 8", "mode": "add"}
        form.update(overrides)
        return client.post("/log", data=form, auth=self.AUTH)

    def test_posting_the_form_previews_without_writing(self, client, monkeypatch):
        engine = self._engine(monkeypatch)
        response = self._post_log(client)
        assert response.status_code == 200
        assert "Nothing saved yet" in response.text
        assert "Chest Bench Press" in response.text
        assert engine.begin_calls == 0

    def test_the_preview_carries_a_token_the_confirm_route_accepts(self, client, monkeypatch):
        self._engine(monkeypatch)
        token = re.search(r'name="token" value="([^"]+)"', self._post_log(client).text)
        assert token, "the preview must carry a token forward"

        committed = {}

        def _fake_commit(prepared, engine=None, replace_existing=False):
            committed["prepared"] = prepared
            committed["replace"] = replace_existing
            return pipeline.PipelineResult(inserted_sets=len(prepared.accepted_sets))

        monkeypatch.setattr(pipeline, "commit_entry", _fake_commit)
        response = client.post("/log/confirm", data={"token": token.group(1)}, auth=self.AUTH)

        assert response.status_code == 200
        assert "Inserted 1 set(s)" in response.text
        assert committed["replace"] is False
        assert committed["prepared"].accepted_sets[0][0].exercise_name == "Chest Bench Press"
        assert committed["prepared"].session_date == date(2026, 8, 19)

    def test_replace_survives_the_round_trip_to_the_confirm_route(self, client, monkeypatch):
        self._engine(monkeypatch, existing_sets=14)
        page = self._post_log(client, mode="replace").text
        assert "Replace will delete first" in page

        committed = {}
        monkeypatch.setattr(
            pipeline, "commit_entry",
            lambda prepared, engine=None, replace_existing=False: committed.update(
                replace=replace_existing) or pipeline.PipelineResult(inserted_sets=1),
        )
        token = re.search(r'name="token" value="([^"]+)"', page).group(1)
        client.post("/log/confirm", data={"token": token}, auth=self.AUTH)
        assert committed["replace"] is True

    def test_a_recent_duplicate_offers_replace_instead_of_stopping(self, client, monkeypatch):
        """The reported bug: re-pasting to overwrite a bad parse did nothing at all."""
        self._engine(monkeypatch, existing_sets=14, duplicate_sets=14)
        response = self._post_log(client, mode="replace")
        assert "You already saved this text" in response.text
        assert 'name="force" value="1"' in response.text
        assert 'name="mode" value="replace"' in response.text

    def test_the_duplicate_page_shows_what_that_save_actually_produced(
        self, client, monkeypatch
    ):
        """So a wrong-looking 'duplicate' can be checked instead of just believed."""
        rows = [type("Row", (), {"exercise": "Leg Press", "sets": 4, "first_logged": None})()]
        self._engine(monkeypatch, existing_sets=4, duplicate_sets=4, detail=rows)
        response = self._post_log(client)
        assert "Leg Press" in response.text and "4 set(s)" in response.text
        assert "it was parsed wrong" in response.text

    def test_forcing_past_the_duplicate_guard_reaches_the_preview(self, client, monkeypatch):
        from datetime import datetime, timezone

        # Forced, so the duplicate lookup is skipped and its scalars are not read.
        monkeypatch.setattr(app, "_engine", _NoWriteEngine(
            [datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc), 14, 0]))
        response = self._post_log(client, mode="replace", force="1")
        assert "Replace will delete first" in response.text

    def test_a_rejected_token_writes_nothing(self, client, monkeypatch):
        engine = self._engine(monkeypatch)
        response = client.post("/log/confirm", data={"token": "bogus.token"}, auth=self.AUTH)
        assert response.status_code == 200
        assert "Nothing saved" in response.text
        assert engine.begin_calls == 0

    def test_going_back_reopens_the_form_with_the_text_intact(self, client, monkeypatch):
        response = client.post(
            "/log/edit",
            data={"raw_text": "bench 60kg x 8", "session_date": "2026-08-19",
                  "mode": "replace"},
            auth=self.AUTH,
        )
        assert "bench 60kg x 8" in response.text
        assert 'value="replace" selected' in response.text

    def test_an_all_review_entry_says_there_is_nothing_to_save(self, client, monkeypatch):
        self._engine(monkeypatch)
        monkeypatch.setattr(
            pipeline, "extract_entities",
            lambda raw_text, session_date, client=None: {
                "sets": [{"exercise_name": "Invented Lift", "weight_kg": 999, "reps": 3}]},
        )
        response = self._post_log(client)
        assert "Nothing to save" in response.text

    def test_a_bad_date_is_rejected_before_the_model_is_called(self, client, monkeypatch):
        monkeypatch.setattr(
            pipeline, "extract_entities",
            lambda *a, **k: pytest.fail("extraction ran on an invalid date"),
        )
        response = self._post_log(client, session_date="19-08-2026")
        assert "Session date must be YYYY-MM-DD" in response.text
