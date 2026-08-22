"""Tests for the review screen: draft, edit, save.

Nothing reaches the database from the web app without passing through here, so
these cover the two ways that guarantee could fail quietly. One is the round
trip: a row rendered into the form and posted straight back must come out
identical, or an untouched entry silently changes on save. The other is the red
marking: a row the database would reject has to block the save and say which
field is wrong, otherwise the screen tells the user their entry is fine while
dropping half of it.
"""

from __future__ import annotations

from datetime import date
from html.parser import HTMLParser

import pytest

import pipeline
import review
from pipeline import CONFIDENCE_THRESHOLD

SESSION = date(2026, 8, 22)

RAW_ENTRY = (
    "Chest bench press 28kg 11 reps almost died. Cable pulls 28kg. "
    "External rotation 15 reps each side. Tricep pushdown. Was 82.4kg this morning."
)

PAYLOAD = {
    "sets": [
        {"exercise_name": "Chest Bench Press", "weight_kg": 28, "reps": 11,
         "notes": "almost died", "raw_span": "Chest bench press 28kg 11 reps almost died"},
        {"exercise_name": "Cable Pulls", "weight_kg": 28, "raw_span": "Cable pulls 28kg"},
        {"exercise_name": "External Rotation", "reps": 15,
         "raw_span": "External rotation 15 reps each side"},
    ],
    "bodyweight": {"weight_kg": 82.4, "raw_span": "Was 82.4kg this morning"},
}


def draft_for(payload=None, raw_text=RAW_ENTRY) -> pipeline.EntryDraft:
    return pipeline.draft_from_payload(payload or PAYLOAD, raw_text, SESSION)


class FormScraper(HTMLParser):
    """Read a rendered review form back as the browser would submit it."""

    def __init__(self) -> None:
        super().__init__()
        self.data: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag != "input" or "name" not in attributes:
            return
        if attributes.get("type") == "checkbox":
            # An unticked box is simply absent from the submission.
            if "checked" in attributes:
                self.data[attributes["name"]] = "on"
        else:
            self.data[attributes["name"]] = attributes.get("value", "")


def submitted(draft: pipeline.EntryDraft) -> dict[str, str]:
    scraper = FormScraper()
    scraper.feed(review.render_review_body(draft))
    return scraper.data


# --------------------------------------------------------------------------
# Building the draft
# --------------------------------------------------------------------------


class TestDraftBuilding:
    def test_every_extracted_set_becomes_a_row(self):
        assert len(draft_for().sets) == 3

    def test_bodyweight_becomes_its_own_row(self):
        assert draft_for().bodyweight.values["weight_kg"] == 82.4

    def test_a_clean_row_is_not_flagged(self):
        assert draft_for().sets[0].issues == []

    def test_omitted_columns_fall_back_to_the_model_default(self):
        """A missing cheat_reps means zero, not null — the column is NOT NULL."""
        row = draft_for().sets[0]
        assert row.values["cheat_reps"] == 0
        assert row.values["is_warmup"] is False

    def test_an_explicit_null_is_treated_as_omitted(self):
        payload = {"sets": [dict(PAYLOAD["sets"][0], cheat_reps=None, pain_flag=None)]}
        row = draft_for(payload).sets[0]
        assert row.values["cheat_reps"] == 0 and row.values["pain_flag"] is False

    def test_values_are_coerced_to_what_will_be_stored(self):
        assert draft_for().sets[0].values["weight_kg"] == 28.0

    def test_nothing_extracted_is_reported_as_empty(self):
        assert draft_for({"sets": [], "bodyweight": None}).is_empty

    def test_a_fragment_that_is_not_a_row_stays_out_of_the_form(self):
        """It cannot be edited, so it is reported rather than rendered."""
        draft = draft_for({"sets": ["a bare string"], "bodyweight": None})
        assert draft.sets == []
        assert "was not an object" in draft.review_items[0].reason


class TestFlagging:
    def test_missing_weight_is_flagged_on_its_own_field(self):
        row = draft_for().sets[2]
        assert [issue.field for issue in row.issues] == ["weight_kg"]

    def test_missing_reps_is_flagged_on_its_own_field(self):
        row = draft_for().sets[1]
        assert [issue.field for issue in row.issues] == ["reps"]

    def test_missing_information_does_not_block_the_save(self):
        """A set with no recorded weight is still a set that happened."""
        draft = draft_for()
        assert draft.flagged_count == 2
        assert draft.blocked_count == 0 and draft.ready

    def test_a_name_the_model_invented_is_flagged(self):
        payload = {"sets": [{"exercise_name": "Bulgarian Split Squat", "weight_kg": 20,
                             "reps": 10, "raw_span": "Cable pulls 28kg"}]}
        assert draft_for(payload).sets[0].issues_for("exercise_name")

    def test_a_low_score_with_no_specific_cause_is_still_explained(self):
        """Nothing is missing here — the name is only loosely supported by the
        text and there is no span to check it against."""
        payload = {"sets": [{"exercise_name": "Overhead Press", "weight_kg": 40, "reps": 8}]}
        row = draft_for(payload).sets[0]
        assert row.confidence < CONFIDENCE_THRESHOLD
        assert [issue.field for issue in row.issues] == [None]

    def test_a_bodyweight_number_not_in_the_entry_is_flagged(self):
        payload = {"sets": [], "bodyweight": {"weight_kg": 91.0, "raw_span": "Was 82.4kg"}}
        assert draft_for(payload).bodyweight.issues_for("weight_kg")


class TestBlockingIssues:
    def test_an_invalid_value_blocks_the_save(self):
        payload = {"sets": [{"exercise_name": "Bench", "weight_kg": 28, "reps": "eleven"}]}
        draft = draft_for(payload)
        assert draft.sets[0].blocking and not draft.ready

    def test_the_blocked_field_is_named(self):
        payload = {"sets": [{"exercise_name": "Bench", "weight_kg": 28, "reps": "eleven"}]}
        assert draft_for(payload).sets[0].issues_for("reps")

    def test_a_whole_row_check_is_pinned_to_the_column_it_names(self):
        """Pydantic reports a model validator against no field at all."""
        payload = {"sets": [{"exercise_name": "Leg Press", "weight_kg": 100, "reps": 8,
                             "cheat_reps": 12}]}
        row = draft_for(payload).sets[0]
        assert [issue.field for issue in row.issues] == ["cheat_reps"]

    def test_an_unticked_row_cannot_block_the_save(self):
        payload = {"sets": [{"exercise_name": "Bench", "reps": "eleven"}]}
        draft = draft_for(payload)
        draft.sets[0].include = False
        assert draft.ready

    def test_a_blank_name_blocks_rather_than_inserting_an_empty_exercise(self):
        payload = {"sets": [{"exercise_name": "   ", "weight_kg": 20, "reps": 10}]}
        assert draft_for(payload).sets[0].blocking


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


class TestRendering:
    def test_every_editable_column_gets_a_field(self):
        page = review.render_review_body(draft_for())
        for column in pipeline.SET_COLUMNS:
            assert f'name="s0.{column}"' in page

    def test_a_flagged_field_is_marked(self):
        page = review.render_review_body(draft_for())
        assert 'name="s1.reps" class="bad"' in page

    def test_a_clean_field_is_not_marked(self):
        page = review.render_review_body(draft_for())
        assert 'name="s0.reps" class=""' in page

    def test_the_issue_is_spelled_out_next_to_the_row(self):
        assert "no rep count found" in review.render_review_body(draft_for())

    def test_a_row_that_cannot_be_saved_says_so_instead_of_showing_a_zero_score(self):
        payload = {"sets": [{"exercise_name": "Bench", "reps": "eleven"}]}
        page = review.render_review_body(draft_for(payload))
        assert "cannot be saved yet" in page and "confidence 0.00" not in page

    def test_exercise_names_are_escaped(self):
        """Names come straight from model output and land in an attribute."""
        payload = {"sets": [{"exercise_name": '"><script>alert(1)</script>',
                             "weight_kg": 20, "reps": 10}]}
        page = review.render_review_body(draft_for(payload))
        assert "<script>" not in page and "&lt;script&gt;" in page

    def test_the_entry_text_is_escaped_in_the_hidden_field(self):
        page = review.render_review_body(draft_for(raw_text='bench "20kg" <b>'))
        assert "<b>" not in page and "&lt;b&gt;" in page

    def test_the_original_entry_is_carried_forward_for_the_save(self):
        assert submitted(draft_for())["raw_text"] == RAW_ENTRY

    def test_replace_mode_survives_the_review_step(self):
        draft = draft_for()
        draft.replace_existing = True
        assert submitted(draft)["mode"] == "replace"

    def test_replace_mode_says_what_it_will_delete(self):
        draft = draft_for()
        draft.replace_existing = True
        page = review.render_review_body(draft, {"sets": 12, "bodyweight": 1})
        assert "deletes the 12 set(s)" in page

    def test_an_empty_draft_offers_no_form(self):
        page = review.render_review_body(draft_for({"sets": [], "bodyweight": None}))
        assert "Nothing to save" in page and "<form" not in page


# --------------------------------------------------------------------------
# Reading the form back
# --------------------------------------------------------------------------


class TestRoundTrip:
    def test_an_untouched_submission_reproduces_the_draft(self):
        original = draft_for()
        returned = review.draft_from_form(submitted(original))
        assert [row.values for row in returned.sets] == [row.values for row in original.sets]

    def test_an_untouched_submission_is_not_reported_as_edited(self):
        """Otherwise every row would be stored as human-confirmed."""
        returned = review.draft_from_form(submitted(draft_for()))
        assert not any(row.edited for row in returned.rows)

    def test_an_untouched_submission_keeps_its_scores(self):
        original = draft_for()
        returned = review.draft_from_form(submitted(original))
        assert [row.confidence for row in returned.sets] == [row.confidence for row in original.sets]

    def test_the_session_date_survives(self):
        assert review.draft_from_form(submitted(draft_for())).session_date == SESSION

    def test_an_unusable_session_date_is_refused(self):
        form = submitted(draft_for())
        form["session_date"] = "22/08/2026"
        with pytest.raises(ValueError):
            review.draft_from_form(form)

    def test_the_bodyweight_row_survives(self):
        assert review.draft_from_form(submitted(draft_for())).bodyweight is not None

    def test_no_bodyweight_stays_no_bodyweight(self):
        draft = draft_for({"sets": PAYLOAD["sets"], "bodyweight": None})
        assert review.draft_from_form(submitted(draft)).bodyweight is None


class TestEditing:
    def test_a_corrected_field_is_taken(self):
        form = submitted(draft_for())
        form["s1.reps"] = "9"
        assert review.draft_from_form(form).sets[1].values["reps"] == 9

    def test_correcting_a_field_clears_its_mark(self):
        form = submitted(draft_for())
        form["s1.reps"] = "9"
        assert review.draft_from_form(form).sets[1].issues == []

    def test_a_corrected_row_is_recorded_as_edited(self):
        form = submitted(draft_for())
        form["s1.reps"] = "9"
        returned = review.draft_from_form(form)
        assert returned.sets[1].edited and not returned.sets[0].edited

    def test_a_toggled_flag_counts_as_an_edit(self):
        form = submitted(draft_for())
        form["s0.pain_flag"] = "on"
        returned = review.draft_from_form(form)
        assert returned.sets[0].values["pain_flag"] is True and returned.sets[0].edited

    def test_clearing_a_flag_counts_as_an_edit(self):
        payload = {"sets": [dict(PAYLOAD["sets"][0], is_warmup=True)]}
        form = submitted(draft_for(payload))
        del form["s0.is_warmup"]
        returned = review.draft_from_form(form)
        assert returned.sets[0].values["is_warmup"] is False and returned.sets[0].edited

    def test_unticking_a_row_leaves_it_out(self):
        form = submitted(draft_for())
        del form["s1.include"]
        returned = review.draft_from_form(form)
        assert not returned.sets[1].include and len(returned.included) == 3

    def test_unticking_the_bodyweight_row_leaves_it_out(self):
        form = submitted(draft_for())
        del form["bw.include"]
        assert not review.draft_from_form(form).bodyweight.include

    def test_breaking_a_field_blocks_the_save(self):
        form = submitted(draft_for())
        form["s0.reps"] = "eleven"
        returned = review.draft_from_form(form)
        assert not returned.ready and returned.sets[0].issues_for("reps")

    def test_bad_input_is_kept_so_it_can_be_corrected(self):
        """Dropping it to null would hide the mistake and save the row anyway."""
        form = submitted(draft_for())
        form["s0.reps"] = "eleven"
        page = review.render_review_body(review.draft_from_form(form))
        assert 'value="eleven"' in page

    def test_clearing_a_field_empties_it_rather_than_failing(self):
        form = submitted(draft_for())
        form["s0.weight_kg"] = "  "
        returned = review.draft_from_form(form)
        assert returned.sets[0].values["weight_kg"] is None and returned.ready

    def test_a_cleared_cheat_rep_count_becomes_zero(self):
        form = submitted(draft_for())
        form["s0.cheat_reps"] = ""
        assert review.draft_from_form(form).sets[0].values["cheat_reps"] == 0

    def test_a_second_pass_over_a_corrected_form_is_stable(self):
        """The re-rendered page has to submit back to the same values."""
        form = submitted(draft_for())
        form["s1.reps"] = "9"
        corrected = review.draft_from_form(form)
        again = review.draft_from_form(submitted(corrected))
        assert [row.values for row in again.sets] == [row.values for row in corrected.sets]


# --------------------------------------------------------------------------
# Saving
# --------------------------------------------------------------------------


class FakeConnection:
    def __init__(self, log: list) -> None:
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params=None):
        self.log.append(params or {})

        class Result:
            @staticmethod
            def fetchall():
                return []

            @staticmethod
            def fetchone():
                return (1,)

            @staticmethod
            def scalar_one():
                return 0

        return Result()


class FakeEngine:
    """Enough of an Engine for commit_draft; every statement is recorded."""

    def __init__(self) -> None:
        self.log: list = []

    def begin(self):
        return FakeConnection(self.log)

    def connect(self):
        return FakeConnection(self.log)

    @property
    def inserted(self) -> list:
        return [params for params in self.log if "extraction_confidence" in params]


def commit(draft, engine=None, **kwargs):
    engine = engine or FakeEngine()
    result = pipeline.commit_draft(draft, engine=engine, **kwargs)
    return result, engine


class TestCommitting:
    def test_ticked_rows_are_written(self):
        result, engine = commit(draft_for())
        assert result.inserted_sets == 3 and result.inserted_bodyweight == 1
        assert len(engine.inserted) == 4

    def test_unticked_rows_are_not_written(self):
        draft = draft_for()
        draft.sets[1].include = False
        result, _ = commit(draft)
        assert result.inserted_sets == 2 and result.skipped_sets == 1

    def test_an_unticked_bodyweight_row_is_counted_separately(self):
        draft = draft_for()
        draft.bodyweight.include = False
        result, _ = commit(draft)
        assert result.inserted_bodyweight == 0 and result.skipped_bodyweight == 1

    def test_a_low_score_no_longer_holds_a_row_back(self):
        """The threshold gates the unattended path; here a person decided."""
        draft = draft_for()
        assert draft.sets[1].confidence < CONFIDENCE_THRESHOLD
        result, _ = commit(draft)
        assert result.inserted_sets == 3 and result.review_items == []

    def test_an_edited_row_is_stored_as_human_confirmed(self):
        draft = draft_for()
        draft.sets[1].edited = True
        _, engine = commit(draft)
        assert engine.inserted[1]["extraction_confidence"] == 1.0

    def test_an_untouched_row_keeps_its_computed_score(self):
        draft = draft_for()
        _, engine = commit(draft)
        assert engine.inserted[1]["extraction_confidence"] == draft.sets[1].confidence

    def test_the_original_entry_is_stored_against_every_row(self):
        _, engine = commit(draft_for())
        assert all(params["raw_source"] == RAW_ENTRY for params in engine.inserted)

    def test_a_row_the_database_would_reject_is_reported_not_dropped(self):
        """A guard: the route checks `ready` first, so this should be unreachable."""
        draft = draft_for({"sets": [{"exercise_name": "Bench", "reps": "eleven"}]})
        result, engine = commit(draft)
        assert engine.inserted == [] and len(result.review_items) == 1

    def test_nothing_ticked_writes_nothing(self):
        draft = draft_for()
        for row in draft.rows:
            row.include = False
        result, engine = commit(draft)
        assert engine.log == [] and result.total_inserted == 0

    def test_replace_only_deletes_when_something_replaces_it(self):
        """An all-unticked draft must never empty the day."""
        draft = draft_for()
        draft.replace_existing = True
        for row in draft.rows:
            row.include = False
        result, engine = commit(draft)
        assert engine.log == [] and result.replaced is None

    def test_fragments_that_were_never_rows_carry_through_to_the_result(self):
        draft = draft_for({"sets": ["a bare string"], "bodyweight": None})
        result, _ = commit(draft)
        assert len(result.review_items) == 1

    def test_an_extraction_failure_writes_nothing(self):
        draft = pipeline.EntryDraft(RAW_ENTRY, SESSION, error="Extraction failed")
        result, engine = commit(draft)
        assert engine.log == [] and result.error == "Extraction failed"


class TestUnattendedPathIsUnchanged:
    """`process_entry` is the CLI's route and has no reviewer, so the threshold
    still decides there."""

    def test_a_low_score_is_held_back_for_review(self, monkeypatch):
        monkeypatch.setattr(pipeline, "extract_entities", lambda *a, **k: PAYLOAD)
        monkeypatch.setattr(pipeline, "load_exercise_names", lambda conn: {})
        engine = FakeEngine()
        result = pipeline.process_entry(RAW_ENTRY, SESSION, engine=engine)
        assert result.inserted_sets == 1
        assert len(result.review_items) == 2
        assert all("below threshold" in item.reason for item in result.review_items)

    def test_an_invalid_row_is_reported_the_same_way_as_before(self, monkeypatch):
        payload = {"sets": [{"exercise_name": "Bench", "reps": "eleven"}], "bodyweight": None}
        monkeypatch.setattr(pipeline, "extract_entities", lambda *a, **k: payload)
        monkeypatch.setattr(pipeline, "load_exercise_names", lambda conn: {})
        result = pipeline.process_entry(RAW_ENTRY, SESSION, engine=FakeEngine())
        assert result.review_items[0].reason.startswith("failed validation:")
