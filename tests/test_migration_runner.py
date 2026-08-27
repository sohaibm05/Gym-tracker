"""The migration has to survive both ways of running it.

`migrations/002_multi_user.sql` is pasted into a SQL console as often as it is
run through `migrate_multi_user.py`, so it carries no bind parameters - a
`:owner_id` in the file is a syntax error the moment psql or the Supabase
editor sees it. These tests hold that line, and hold the statement splitter to
keeping a PL/pgSQL block whole: split one on the semicolons inside its body and
each fragment reaches the server as nonsense.
"""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import migrate_multi_user  # noqa: E402

MIGRATION_SQL = migrate_multi_user.MIGRATION.read_text(encoding="utf-8")


class TestSplittingStatements:
    def test_plain_statements_split_on_the_semicolon(self):
        parts = migrate_multi_user.statements("SELECT 1; SELECT 2;")
        assert parts == ["SELECT 1", "SELECT 2"]

    def test_comment_lines_are_dropped(self):
        parts = migrate_multi_user.statements("-- a note\nSELECT 1;\n    -- indented\n")
        assert parts == ["SELECT 1"]

    def test_a_block_stays_whole_despite_its_semicolons(self):
        parts = migrate_multi_user.statements(
            "SELECT 1;\n"
            "DO $b$ BEGIN RAISE NOTICE 'one'; RAISE NOTICE 'two'; END $b$;\n"
            "SELECT 2;"
        )
        assert len(parts) == 3
        assert parts[1].startswith("DO $b$") and parts[1].endswith("$b$")
        assert "RAISE NOTICE 'two'" in parts[1]

    def test_an_untagged_block_stays_whole_too(self):
        parts = migrate_multi_user.statements("DO $$ BEGIN PERFORM 1; END $$;")
        assert len(parts) == 1

    def test_a_trailing_statement_without_its_semicolon_still_counts(self):
        assert migrate_multi_user.statements("SELECT 1") == ["SELECT 1"]


class TestTheMigrationFile:
    def test_no_statement_carries_a_bind_parameter(self):
        # What broke the paste into the SQL console: `SET user_id = :owner_id`
        # is a syntax error to everything except a driver that fills it in.
        for part in migrate_multi_user.statements(MIGRATION_SQL):
            assert not text(part)._bindparams, f"bind parameter in: {part[:80]}"

    def test_every_block_is_closed(self):
        for part in migrate_multi_user.statements(MIGRATION_SQL):
            if part.startswith("DO $"):
                tag = part[3 : part.index("$", 3) + 1]
                assert part.endswith(tag), f"unterminated block: {part[:80]}"

    def test_the_owner_is_named_through_the_setting_the_runner_sets(self):
        naming = [
            part
            for part in migrate_multi_user.statements(MIGRATION_SQL)
            if migrate_multi_user.OWNER_SETTING in part
        ]
        assert len(naming) == 1, "exactly one statement should ask who the owner is"
        assert naming[0].startswith("DO $")
