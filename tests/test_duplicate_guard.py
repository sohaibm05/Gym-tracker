"""Tests for the duplicate-submission guard.

The guard exists because Render's free tier cold-starts for 30-50s, which is
exactly when a user double-taps submit. But a double-tap and a deliberate
re-paste to correct a bad parse are the same bytes arriving twice, so a guard
that decides on its own is wrong about half the time it fires. These cover the
three ways it used to be wrong: it ignored which day was being logged, it
refused instead of asking, and it asserted a match without showing one.
"""

from __future__ import annotations

from datetime import date
from html.parser import HTMLParser
from typing import Optional

import pipeline
import review

SESSION = date(2026, 8, 22)
RAW = "Chest bench press 28kg 11 reps. Was 82.4kg this morning."
USER_ID = 7


# --------------------------------------------------------------------------
# Which rows count as "the same entry"
# --------------------------------------------------------------------------


class TestSubmissionClauses:
    """The WHERE fragment that decides what a duplicate is."""

    def test_matches_on_exact_text_and_a_time_window(self):
        where, params = pipeline._submission_clauses(RAW, USER_ID, 5, None)
        assert "raw_source = :raw_source" in where
        assert "created_at >= now() - CAST(:window AS interval)" in where
        assert params["raw_source"] == RAW and params["window"] == "5 minutes"

    def test_without_a_date_it_is_not_scoped_to_a_day(self):
        where, params = pipeline._submission_clauses(RAW, USER_ID, 5, None)
        assert "logged_at" not in where
        assert "day_start" not in params

    def test_a_date_scopes_the_match_to_that_local_day(self):
        where, params = pipeline._submission_clauses(RAW, USER_ID, 5, SESSION)
        assert "logged_at >= :day_start AND logged_at < :day_end" in where
        assert params["day_start"], params["day_end"]

    def test_two_dates_produce_disjoint_windows(self):
        """The defect: one short entry logged against two dates in one sitting.

        "rest day, weighed 82.4" pasted for Saturday and again for Sunday is not
        a double-tap, but without day scoping the second read as one and was
        silently dropped.
        """
        _, saturday = pipeline._submission_clauses(RAW, USER_ID, 5, date(2026, 8, 22))
        _, sunday = pipeline._submission_clauses(RAW, USER_ID, 5, date(2026, 8, 23))
        assert saturday["day_start"] != sunday["day_start"]
        # Disjoint, not merely different: Saturday ends where Sunday begins.
        assert saturday["day_end"] == sunday["day_start"]


# --------------------------------------------------------------------------
# A fake engine that can be told what the database already holds
# --------------------------------------------------------------------------


class _Result:
    def __init__(self, rows=(), scalar=0, rowcount=0):
        self._rows, self._scalar = list(rows), scalar
        self.rowcount = rowcount

    def fetchall(self):
        return self._rows

    def all(self):
        return self._rows

    def fetchone(self):
        return (1,)

    def scalar_one(self):
        return self._scalar


class _Connection:
    def __init__(self, engine):
        self.engine = engine

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        self.engine.statements.append((sql, params or {}))

        # Order matters: the evidence query also selects count(*) over
        # raw_source, so it has to be matched before the counting branch.
        if "GROUP BY e.name" in sql:
            return _Result(rows=self.engine.evidence_rows)
        if "count(*)" in sql and "raw_source" in sql:
            return _Result(scalar=self.engine.prior_count)
        if "FROM exercises" in sql:
            return _Result(rows=[])
        if sql.startswith("DELETE"):
            self.engine.deletes.append(sql)
            return _Result(scalar=0)
        return _Result()


class _Engine:
    """Enough of an Engine for commit_draft, with a settable prior save."""

    def __init__(self, prior_count=0, evidence_rows=()):
        self.prior_count = prior_count
        self.evidence_rows = list(evidence_rows)
        self.statements: list = []
        self.deletes: list = []

    def begin(self):
        return _Connection(self)

    def connect(self):
        return _Connection(self)

    @property
    def inserted(self):
        return [p for _, p in self.statements if "extraction_confidence" in p]


def draft_for(replace=False):
    draft = pipeline.draft_from_payload(
        {"sets": [{"exercise_name": "Chest Bench Press", "weight_kg": 28.0,
                   "reps": 11, "raw_span": RAW}]},
        RAW,
        SESSION,
    )
    for row in draft.sets:
        row.include = True
    draft.replace_existing = replace
    return draft


# --------------------------------------------------------------------------
# Asking instead of refusing
# --------------------------------------------------------------------------


class TestGuardHoldsForADecision:
    def test_a_match_writes_nothing_and_reports_the_prior_save(self):
        engine = _Engine(prior_count=3)
        result = pipeline.commit_draft(
            draft_for(), USER_ID, engine=engine, check_duplicates=True
        )
        assert result.duplicate_of_recent is True
        assert result.inserted_sets == 3
        assert engine.inserted == []

    def test_no_match_saves_normally(self):
        engine = _Engine(prior_count=0)
        result = pipeline.commit_draft(
            draft_for(), USER_ID, engine=engine, check_duplicates=True
        )
        assert result.duplicate_of_recent is False
        assert result.inserted_sets == 1

    def test_the_guard_is_scoped_to_the_day_being_logged(self):
        engine = _Engine(prior_count=1)
        pipeline.commit_draft(
            draft_for(), USER_ID, engine=engine, check_duplicates=True
        )
        counts = [(sql, p) for sql, p in engine.statements if "count(*)" in sql]
        assert counts, "the guard never ran"
        assert all("logged_at" in sql for sql, _ in counts)
        assert all("day_start" in p for _, p in counts)

    def test_the_match_comes_with_evidence(self):
        """A guard that shows nothing cannot be argued with, or trusted."""
        engine = _Engine(prior_count=2, evidence_rows=[("Chest Bench Press", 2, None)])
        result = pipeline.commit_draft(
            draft_for(), USER_ID, engine=engine, check_duplicates=True
        )
        assert result.duplicate_evidence == [
            {"exercise": "Chest Bench Press", "sets": 2, "first_logged": None}
        ]

    def test_evidence_is_empty_when_nothing_is_matched(self):
        engine = _Engine(prior_count=0)
        result = pipeline.commit_draft(
            draft_for(), USER_ID, engine=engine, check_duplicates=True
        )
        assert result.duplicate_evidence == []


class TestOverridingTheGuard:
    def test_override_saves_despite_a_match(self):
        engine = _Engine(prior_count=3)
        result = pipeline.commit_draft(
            draft_for(),
            USER_ID,
            engine=engine,
            check_duplicates=True,
            override_duplicate=True,
        )
        assert result.duplicate_of_recent is False
        assert result.inserted_sets == 1

    def test_replace_on_a_matched_entry_actually_replaces(self):
        """The defect: selecting Replace and re-submitting did nothing at all.

        The delete-and-reinsert was stopped by the same guard, so the save
        appeared to succeed while changing nothing.
        """
        engine = _Engine(prior_count=3)
        result = pipeline.commit_draft(
            draft_for(replace=True),
            USER_ID,
            engine=engine,
            check_duplicates=True,
            override_duplicate=True,
        )
        assert engine.deletes, "replace never deleted the day"
        assert result.inserted_sets == 1

    def test_override_does_not_skip_the_guard_when_not_asked(self):
        engine = _Engine(prior_count=3)
        result = pipeline.commit_draft(
            draft_for(),
            USER_ID,
            engine=engine,
            check_duplicates=True,
            override_duplicate=False,
        )
        assert result.duplicate_of_recent is True


class TestGuardIsScopedToTheAccount:
    """Per-day and per-account scoping arrived from different branches and meet
    here. Either one dropped leaves a guard that looks like it works."""

    def test_the_counts_read_only_this_account(self):
        engine = _Engine(prior_count=1)
        pipeline.commit_draft(
            draft_for(), USER_ID, engine=engine, check_duplicates=True
        )
        counts = [(sql, p) for sql, p in engine.statements if "count(*)" in sql]
        assert counts, "the guard never ran"
        assert all("user_id = :user_id" in sql for sql, _ in counts)
        assert all(p["user_id"] == USER_ID for _, p in counts)

    def test_the_day_scope_survived_alongside_it(self):
        """Both predicates, in the same WHERE, not one replacing the other."""
        where, params = pipeline._submission_clauses(RAW, USER_ID, 5, SESSION)
        assert "user_id = :user_id" in where and "logged_at >= :day_start" in where
        assert params["user_id"] == USER_ID and params["day_start"]

    def test_the_owner_is_bound_not_interpolated(self):
        """The fragment is interpolated into the SQL, so the id must not be."""
        where, params = pipeline._submission_clauses(RAW, USER_ID, 5, None)
        assert "user_id = :user_id" in where and str(USER_ID) not in where
        assert params["user_id"] == USER_ID

    def test_the_evidence_join_is_scoped_on_both_sides(self):
        """`user_id` is on `exercises` too, so an unqualified predicate would be
        ambiguous — and an unscoped join could name another account's lift."""
        engine = _Engine(prior_count=2, evidence_rows=[("Chest Bench Press", 2, None)])
        pipeline.commit_draft(
            draft_for(), USER_ID, engine=engine, check_duplicates=True
        )
        sql = next(sql for sql, _ in engine.statements if "GROUP BY e.name" in sql)
        assert "e.user_id = w.user_id" in sql and "w.user_id = :user_id" in sql


# --------------------------------------------------------------------------
# What the page offers
# --------------------------------------------------------------------------


DUPLICATE = {
    "inserted_sets": 2,
    "inserted_bodyweight": 1,
    "evidence": [{"exercise": "Chest Bench Press", "sets": 2, "first_logged": None}],
}


class TestDuplicatePage:
    def test_it_offers_both_ways_forward(self):
        body = review.render_review_body(draft_for(), duplicate=DUPLICATE)
        assert 'name="mode" value="replace"' in body
        assert 'name="mode" value="add"' in body

    def test_it_carries_the_override_back(self):
        body = review.render_review_body(draft_for(), duplicate=DUPLICATE)
        assert 'name="override_duplicate" value="1"' in body

    def test_it_does_not_also_submit_a_hidden_mode(self):
        """Both would post, and the form would carry two values for one field."""
        body = review.render_review_body(draft_for(), duplicate=DUPLICATE)
        assert '<input type="hidden" name="mode"' not in body

    def test_it_shows_what_the_earlier_save_produced(self):
        body = review.render_review_body(draft_for(), duplicate=DUPLICATE)
        assert "Chest Bench Press" in body and "2 set(s)" in body

    def test_a_bodyweight_only_match_says_so_instead_of_an_empty_list(self):
        body = review.render_review_body(
            draft_for(), duplicate={**DUPLICATE, "evidence": []}
        )
        assert "bodyweight reading only" in body
        assert '<ul class="dup-evidence">' not in body

    def test_the_rows_are_still_there_and_still_editable(self):
        """Deciding must not cost the user the corrections they just made."""
        body = review.render_review_body(draft_for(), duplicate=DUPLICATE)
        assert 'name="s0.exercise_name"' in body

    def test_replace_is_marked_destructive(self):
        body = review.render_review_body(draft_for(), duplicate=DUPLICATE)
        assert 'value="replace" class="danger"' in body

    def test_the_first_button_is_the_one_that_cannot_lose_data(self):
        """The rows on this page are still editable, and pressing Enter in a
        text field submits via the first button. Replace deletes the whole day,
        so it must not be the one a stray keystroke reaches."""
        body = review.render_review_body(draft_for(), duplicate=DUPLICATE)
        assert body.index('value="add"') < body.index('value="replace"')


class TestOrdinaryPageIsUnchanged:
    def test_it_still_carries_a_hidden_mode(self):
        body = review.render_review_body(draft_for())
        assert '<input type="hidden" name="mode"' in body

    def test_it_does_not_offer_an_override(self):
        body = review.render_review_body(draft_for())
        assert "override_duplicate" not in body

    def test_a_plain_save_button_is_not_destructive(self):
        body = review.render_review_body(draft_for())
        assert "Save to database" in body
        assert 'class="danger"' not in body

    def test_replace_mode_marks_its_save_button_destructive(self):
        """It deletes a day before it writes; it should not look like Add."""
        body = review.render_review_body(draft_for(replace=True))
        assert 'class="danger"' in body
        assert "Replace 2026-08-22 and save" in body


# --------------------------------------------------------------------------
# The decision travelling back
# --------------------------------------------------------------------------


class _FormScraper(HTMLParser):
    """Read the rendered form back as the browser would submit it."""

    def __init__(self) -> None:
        super().__init__()
        self.data: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if "name" not in attributes:
            return
        if tag == "input" and attributes.get("type") == "checkbox":
            if "checked" in attributes:
                self.data[attributes["name"]] = "on"
        elif tag == "input":
            self.data[attributes["name"]] = attributes.get("value", "")


def _submit(body: str, pressed: Optional[tuple[str, str]] = None) -> dict[str, str]:
    """The fields a browser would post, plus whichever button was pressed."""
    scraper = _FormScraper()
    scraper.feed(body)
    data = scraper.data
    if pressed:
        data[pressed[0]] = pressed[1]
    return data


class TestDecisionRoundTrip:
    """Pressing a button has to survive the trip back through the form.

    The buttons carry `mode` because the hidden copy is dropped; if either end
    of that arrangement broke, the save would silently fall back to Add.
    """

    def test_pressing_replace_comes_back_as_replace(self):
        body = review.render_review_body(draft_for(), duplicate=DUPLICATE)
        form = _submit(body, pressed=("mode", "replace"))
        assert review.draft_from_form(form).replace_existing is True

    def test_pressing_add_comes_back_as_add(self):
        body = review.render_review_body(draft_for(), duplicate=DUPLICATE)
        form = _submit(body, pressed=("mode", "add"))
        assert review.draft_from_form(form).replace_existing is False

    def test_the_override_survives_the_round_trip(self):
        body = review.render_review_body(draft_for(), duplicate=DUPLICATE)
        assert _submit(body)["override_duplicate"] == "1"

    def test_the_rows_survive_the_round_trip(self):
        """Whatever the user corrected before the guard fired must still be there."""
        body = review.render_review_body(draft_for(), duplicate=DUPLICATE)
        rebuilt = review.draft_from_form(_submit(body, pressed=("mode", "add")))
        assert rebuilt.sets[0].values["exercise_name"] == "Chest Bench Press"
        assert rebuilt.sets[0].values["weight_kg"] == 28.0

    def test_an_ordinary_save_carries_no_override(self):
        assert "override_duplicate" not in _submit(review.render_review_body(draft_for()))
