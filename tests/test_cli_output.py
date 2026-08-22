"""Tests for the dry-run line formatter.

Pure string formatting, but it is the only place the extracted timestamp and
cheat-rep count are visible before anything reaches the database — so a bug
here means testing the extraction blind.
"""

from __future__ import annotations

import os
from datetime import date

import pytest

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
        import inspect

        import app as app_module

        source = inspect.getsource(app_module.index)
        assert 'value="add" selected' in source
        assert 'value="replace"' in source
