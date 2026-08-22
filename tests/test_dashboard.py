"""Tests for the progress page: data assembly and safe rendering.

Exercise names reach this page straight from LLM output, so escaping is a
correctness concern here, not a formality.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

import pytest

import charts
import insights
from insights import (
    SetRecord,
    exercise_e1rm_series,
    pain_summary,
    weekly_volume_series,
    week_start_for,
)

MONDAY = date(2026, 8, 3)


def sets_for(exercise, day, weight, reps_list, pain=False, muscle="Chest"):
    return [SetRecord(exercise, day, weight, r, pain_flag=pain, muscle_group=muscle)
            for r in reps_list]


class TestWeeklyVolumeSeries:
    def test_zero_fills_weeks_with_no_training(self):
        weeks = [MONDAY - timedelta(weeks=n) for n in (2, 1, 0)]
        records = sets_for("Bench", MONDAY, 60, [10])
        series = weekly_volume_series(records, weeks)
        assert [v for _, v in series] == [0.0, 0.0, 600.0]

    def test_a_gap_week_stays_visible_as_zero(self):
        """Dropping empty weeks would make a lay-off look like continuous training."""
        weeks = [MONDAY - timedelta(weeks=1), MONDAY]
        series = weekly_volume_series(sets_for("Bench", MONDAY, 60, [10]), weeks)
        assert len(series) == 2 and series[0][1] == 0.0

    def test_sets_outside_the_window_are_ignored(self):
        records = sets_for("Bench", MONDAY - timedelta(weeks=9), 60, [10])
        assert [v for _, v in weekly_volume_series(records, [MONDAY])] == [0.0]


class TestExerciseSeries:
    def _records(self):
        records = []
        for offset in (0, 7, 14):
            day = MONDAY + timedelta(days=offset)
            records += sets_for("Bench", day, 60 + offset, [8])
            records += sets_for("Row", day, 50, [8])
        records += sets_for("Curl", MONDAY, 20, [10])       # one session only
        return records

    def test_single_session_exercises_are_dropped(self):
        names = [name for name, _ in exercise_e1rm_series(self._records())]
        assert "Curl" not in names
        assert {"Bench", "Row"} <= set(names)

    def test_most_trained_first(self):
        records = self._records() + sets_for("Row", MONDAY + timedelta(21), 50, [8])
        assert exercise_e1rm_series(records)[0][0] == "Row"

    def test_respects_the_limit(self):
        assert len(exercise_e1rm_series(self._records(), limit=1)) == 1

    def test_points_are_dated_and_rounded(self):
        points = dict(exercise_e1rm_series(self._records()))["Bench"]
        assert all(isinstance(day, date) for day, _ in points)
        assert all(round(value, 1) == value for _, value in points)


class TestPainSummary:
    def _sessions(self, pain_on):
        records = []
        for index, offset in enumerate((0, 7, 14)):
            day = MONDAY + timedelta(days=offset)
            records += sets_for("Lat Pulldown", day, 45, [10], pain=index in pain_on)
        return records

    def test_no_pain_is_omitted_entirely(self):
        assert pain_summary(self._sessions(set())) == []

    def test_recent_streak_is_warning(self):
        row = pain_summary(self._sessions({2}))[0]
        assert row["status"] == "warning" and row["streak"] == 1

    def test_three_in_a_row_escalates_to_serious(self):
        """Matches the safeguard's own threshold, so the view cannot disagree
        with the recommendation."""
        row = pain_summary(self._sessions({0, 1, 2}))[0]
        assert row["status"] == "serious" and row["streak"] == 3

    def test_old_pain_with_no_current_streak_still_listed(self):
        row = pain_summary(self._sessions({0}))[0]
        assert row["streak"] == 0 and row["sessions_flagged"] == 1

    def test_worst_streak_sorts_first(self):
        records = self._sessions({0, 1, 2})
        for index, offset in enumerate((0, 7, 14)):
            records += sets_for("Row", MONDAY + timedelta(days=offset), 50, [8],
                                pain=index == 0)
        assert pain_summary(records)[0]["exercise"] == "Lat Pulldown"


class TestRendering:
    def _data(self, exercise="Bench Press"):
        return {
            "week_start": MONDAY.isoformat(), "weeks": 12, "timezone": "Asia/Karachi",
            "kpis": {"volume_this_week": 7139.0, "volume_delta_pct": 1.0,
                     "sets_this_week": 15, "exercises_this_week": 5,
                     "bodyweight": 82.4, "bodyweight_delta": -0.7},
            "weekly_volume": [[MONDAY.isoformat(), 7139.0]],
            "exercise_e1rm": [{"exercise": exercise,
                               "points": [[MONDAY.isoformat(), 80.0],
                                          [(MONDAY + timedelta(7)).isoformat(), 82.0]]}],
            "bodyweight": [[MONDAY.isoformat(), 82.4]],
            "muscle_volume": [["Chest", 1269.0]],
            "pain": [{"exercise": exercise, "sessions_flagged": 1,
                      "last_flagged": MONDAY.isoformat(), "streak": 1, "status": "warning"}],
            "has_data": True,
        }

    def test_renders_the_expected_cards(self):
        body = charts.render_progress_body(self._data())
        for heading in ("Weekly volume", "Estimated 1RM by exercise", "Bodyweight",
                        "Volume by muscle group", "Pain flags"):
            assert heading in body

    def test_every_chart_has_a_table_twin(self):
        """No value may be reachable only by hovering."""
        body = charts.render_progress_body(self._data())
        assert body.count("<table>") >= 4

    def test_exercise_names_are_escaped(self):
        body = charts.render_progress_body(self._data(exercise="<script>alert(1)</script>"))
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;" in body

    def test_the_json_blob_cannot_close_its_own_script_tag(self):
        body = charts.render_progress_body(self._data(exercise="</script><script>alert(1)"))
        blob = re.search(r'id="viz-data">(.*?)</script>', body, re.S).group(1)
        assert "</script>" not in blob
        # and it still round-trips as valid JSON
        assert json.loads(blob)["exercise_e1rm"][0]["exercise"] == "</script><script>alert(1)"

    def test_empty_state_when_nothing_is_logged(self):
        data = self._data()
        data["has_data"] = False
        body = charts.render_progress_body(data)
        assert "Nothing logged yet" in body
        assert "Weekly volume" not in body

    def test_dark_theme_tokens_are_declared_for_both_scopes(self):
        """OS setting and an explicit theme stamp must each win."""
        assert "prefers-color-scheme: dark" in charts.PROGRESS_CSS
        assert ':root[data-theme="dark"] .viz-root' in charts.PROGRESS_CSS

    def test_labels_are_inserted_as_text_not_markup(self):
        """Series names come from LLM output; assigning them via innerHTML
        would make the tooltip an injection point."""
        assert not re.search(r"\.innerHTML\s*[=+]", charts.PROGRESS_JS)
        assert "textContent" in charts.PROGRESS_JS
