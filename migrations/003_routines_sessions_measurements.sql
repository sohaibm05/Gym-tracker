-- Routines, live workout sessions, measurements and personal records.
--
-- Adds the structured "log during the workout" half of the app alongside the
-- existing "paste a journal afterwards" half. Both write to `workout_logs`, so
-- the progress charts and the weekly report see every set regardless of how it
-- was entered. That is the whole reason this is a set of additions rather than
-- a second schema: one fact table, two input methods.
--
-- Safe to re-run: every statement is IF NOT EXISTS or guarded by a DO block.
-- Safe on a database with existing data: every new column is nullable or has a
-- default, and nothing already stored is rewritten.
--
--   psql "$DATABASE_URL" -f migrations/003_routines_sessions_measurements.sql

BEGIN;

-- --------------------------------------------------------------------------
-- exercises: equipment, so the catalog can be filtered the way people think
-- --------------------------------------------------------------------------

-- Equipment is a real dimension of an exercise, not part of its name. "Bench
-- Press (Barbell)" and "Bench Press (Dumbbell)" are different lifts with
-- different loads, and storing the equipment separately is what lets the
-- catalog filter by it instead of substring-matching the name.
ALTER TABLE exercises ADD COLUMN IF NOT EXISTS equipment TEXT;

-- Whether the row came from the seeded catalog or the person typed it. Only
-- custom rows are safe to rename or delete on their behalf.
ALTER TABLE exercises ADD COLUMN IF NOT EXISTS is_custom BOOLEAN NOT NULL DEFAULT TRUE;

-- Lowercase, hyphenless key for search and for matching a typed name against
-- the catalog. Denormalised on purpose: it is derived from `name`, but having
-- it as a column means the search index is usable, which a function on every
-- row in the WHERE clause would not be.
ALTER TABLE exercises ADD COLUMN IF NOT EXISTS search_key TEXT;

CREATE INDEX IF NOT EXISTS idx_exercises_user_equipment
    ON exercises (user_id, equipment);
CREATE INDEX IF NOT EXISTS idx_exercises_user_search_key
    ON exercises (user_id, search_key);

-- --------------------------------------------------------------------------
-- routines: a reusable workout template
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS routines (
    routine_id  SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    notes       TEXT,
    -- Order in the person's own list, so they can put the split they are
    -- running this block at the top.
    position    INTEGER NOT NULL DEFAULT 0,
    -- Archived rather than deleted: a finished session references the routine
    -- it came from, and deleting the template should not rewrite history.
    archived    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT routines_name_not_blank CHECK (length(btrim(name)) > 0),
    CONSTRAINT routines_user_name_unique UNIQUE (user_id, name)
);

CREATE INDEX IF NOT EXISTS idx_routines_user_position
    ON routines (user_id, position, routine_id);

-- --------------------------------------------------------------------------
-- routine_exercises: the ordered contents of a routine
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS routine_exercises (
    routine_exercise_id SERIAL PRIMARY KEY,
    routine_id          INTEGER NOT NULL REFERENCES routines (routine_id) ON DELETE CASCADE,
    exercise_id         INTEGER NOT NULL REFERENCES exercises (exercise_id) ON DELETE CASCADE,
    -- Order within the routine. Deliberately NOT unique per routine: reordering
    -- a list under a unique constraint needs a temporary-value dance on every
    -- move, and ties are harmless here because routine_exercise_id breaks them
    -- deterministically in the ORDER BY.
    position            INTEGER NOT NULL DEFAULT 0,
    target_sets         INTEGER,
    -- A rep range, not a single number: "8-12" is how programmes are actually
    -- written, and a single target cannot express it. Equal low/high means a
    -- fixed target.
    target_reps_low     INTEGER,
    target_reps_high    INTEGER,
    target_weight_kg    NUMERIC(6, 2),
    -- Per-exercise rest, because 3 minutes after a heavy squat and 45 seconds
    -- after a cable fly are both correct.
    rest_seconds        INTEGER,
    notes               TEXT,
    CONSTRAINT routine_exercises_sets_positive
        CHECK (target_sets IS NULL OR target_sets > 0),
    CONSTRAINT routine_exercises_reps_positive
        CHECK ((target_reps_low IS NULL OR target_reps_low > 0)
               AND (target_reps_high IS NULL OR target_reps_high > 0)),
    CONSTRAINT routine_exercises_reps_ordered
        CHECK (target_reps_low IS NULL OR target_reps_high IS NULL
               OR target_reps_low <= target_reps_high),
    CONSTRAINT routine_exercises_weight_nonneg
        CHECK (target_weight_kg IS NULL OR target_weight_kg >= 0),
    CONSTRAINT routine_exercises_rest_sane
        CHECK (rest_seconds IS NULL OR (rest_seconds >= 0 AND rest_seconds <= 3600))
);

CREATE INDEX IF NOT EXISTS idx_routine_exercises_routine_position
    ON routine_exercises (routine_id, position, routine_exercise_id);

-- --------------------------------------------------------------------------
-- workout_sessions: one trip to the gym
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS workout_sessions (
    session_id  BIGSERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    -- NULL for a freestyle session, and ON DELETE SET NULL so archiving or
    -- removing a template never deletes the training done under it.
    routine_id  INTEGER REFERENCES routines (routine_id) ON DELETE SET NULL,
    -- Copied from the routine at start time rather than joined at read time:
    -- renaming a routine later must not rewrite what past sessions were called.
    name        TEXT,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- NULL means the session is still in progress. This is the only piece of
    -- live state the server keeps, and it is what lets someone close the app
    -- mid-workout and come back to the same session.
    finished_at TIMESTAMPTZ,
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT workout_sessions_finished_after_started
        CHECK (finished_at IS NULL OR finished_at >= started_at)
);

CREATE INDEX IF NOT EXISTS idx_workout_sessions_user_started
    ON workout_sessions (user_id, started_at DESC);

-- At most one session in progress per person. A partial unique index is the
-- right tool: it constrains only the rows where finished_at IS NULL, so a
-- person can have any number of finished sessions but never two live ones.
-- Without it, a double-tapped "start workout" silently creates a second
-- session and half the sets land in each.
CREATE UNIQUE INDEX IF NOT EXISTS idx_workout_sessions_one_active_per_user
    ON workout_sessions (user_id) WHERE finished_at IS NULL;

-- --------------------------------------------------------------------------
-- workout_logs: tie a set to its session, and record effort
-- --------------------------------------------------------------------------

-- Nullable, because every set logged before this migration — and every set that
-- still arrives through the journal-paste flow — has no session.
ALTER TABLE workout_logs
    ADD COLUMN IF NOT EXISTS session_id BIGINT
    REFERENCES workout_sessions (session_id) ON DELETE SET NULL;

-- RPE (rate of perceived exertion, 6-10) and RIR (reps in reserve, 0-10) are
-- two scales for the same judgement and people use one or the other, so both
-- are stored and either may be null. They are related by RIR = 10 - RPE, but
-- deriving one from the other would invent a precision the person did not give.
ALTER TABLE workout_logs ADD COLUMN IF NOT EXISTS rpe NUMERIC(3, 1);
ALTER TABLE workout_logs ADD COLUMN IF NOT EXISTS rir INTEGER;

CREATE INDEX IF NOT EXISTS idx_workout_logs_session
    ON workout_logs (session_id);

-- Added in a DO block rather than inline: ALTER TABLE ... ADD CONSTRAINT has no
-- IF NOT EXISTS, so a plain statement would fail the second time this file ran.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'workout_logs_rpe_range'
    ) THEN
        ALTER TABLE workout_logs ADD CONSTRAINT workout_logs_rpe_range
            CHECK (rpe IS NULL OR (rpe >= 1 AND rpe <= 10));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'workout_logs_rir_range'
    ) THEN
        ALTER TABLE workout_logs ADD CONSTRAINT workout_logs_rir_range
            CHECK (rir IS NULL OR (rir >= 0 AND rir <= 20));
    END IF;
END $$;

-- --------------------------------------------------------------------------
-- measurements: body circumferences
-- --------------------------------------------------------------------------

-- Long format — one row per site per date — rather than a wide table with a
-- column per body part. Adding "forearm" to a wide table is a migration; here
-- it is a new value in an existing column. Bodyweight stays in its own table
-- because it already exists, is referenced by the report, and carries body-fat
-- percentage alongside it.
CREATE TABLE IF NOT EXISTS measurements (
    measurement_id BIGSERIAL PRIMARY KEY,
    user_id        INTEGER NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    measured_at    TIMESTAMPTZ NOT NULL,
    site           TEXT NOT NULL,
    value_cm       NUMERIC(5, 1) NOT NULL,
    notes          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT measurements_site_not_blank CHECK (length(btrim(site)) > 0),
    -- An upper bound as well as a lower one: a waist of 900 is a units mistake
    -- (inches typed as cm, or a stray digit), and catching it here is cheaper
    -- than explaining a ruined chart later.
    CONSTRAINT measurements_value_sane CHECK (value_cm > 0 AND value_cm <= 400)
);

CREATE INDEX IF NOT EXISTS idx_measurements_user_site_measured
    ON measurements (user_id, site, measured_at DESC);

-- --------------------------------------------------------------------------
-- personal_records: the cached best, so a PR can be announced mid-set
-- --------------------------------------------------------------------------

-- These are derivable from workout_logs by aggregation, and are cached anyway.
-- The reason is the interaction: telling someone they just hit a PR has to
-- happen in the moment they tap the checkmark, and a full scan of their history
-- per set is the wrong thing to do on a phone on gym wifi. One indexed row per
-- (person, exercise, record type) makes it a single-row read and a single-row
-- write.
--
-- Being a cache, it is rebuildable: records.rebuild_records() recomputes the
-- whole table from workout_logs, which is the answer to it ever drifting.
CREATE TABLE IF NOT EXISTS personal_records (
    record_id    BIGSERIAL PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    exercise_id  INTEGER NOT NULL REFERENCES exercises (exercise_id) ON DELETE CASCADE,
    -- heaviest_weight    most weight moved for at least one clean rep
    -- best_1rm           best Epley estimate from a single set
    -- best_session_volume  most weight x reps for this lift in one session
    record_type  TEXT NOT NULL,
    -- The comparable number for this record type: kg, estimated kg, or kg-reps.
    value        NUMERIC(12, 2) NOT NULL,
    -- The set that set it, kept so the UI can say "100kg x 5" rather than only
    -- the derived figure.
    weight_kg    NUMERIC(6, 2),
    reps         INTEGER,
    log_id       BIGINT REFERENCES workout_logs (log_id) ON DELETE SET NULL,
    session_id   BIGINT REFERENCES workout_sessions (session_id) ON DELETE SET NULL,
    achieved_at  TIMESTAMPTZ NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT personal_records_type_known
        CHECK (record_type IN ('heaviest_weight', 'best_1rm', 'best_session_volume')),
    CONSTRAINT personal_records_value_positive CHECK (value > 0),
    -- One current record per person, per exercise, per type. This is what makes
    -- "did I just beat it?" a single-row lookup and the update an upsert.
    CONSTRAINT personal_records_unique UNIQUE (user_id, exercise_id, record_type)
);

CREATE INDEX IF NOT EXISTS idx_personal_records_user_achieved
    ON personal_records (user_id, achieved_at DESC);

-- --------------------------------------------------------------------------
-- Backfill
-- --------------------------------------------------------------------------

-- Every exercise that predates this migration was typed by a person, so the
-- is_custom default of TRUE is already right. Only the search key is missing.
UPDATE exercises
   SET search_key = lower(regexp_replace(name, '[^a-zA-Z0-9]+', ' ', 'g'))
 WHERE search_key IS NULL;

COMMIT;

-- Personal records are NOT backfilled here. Recomputing every person's history
-- inside the migration transaction would hold locks for as long as the largest
-- account takes, and it is not needed for correctness — an absent record simply
-- means the next set of that lift sets one. Run this afterwards, per account or
-- for everybody, outside the migration:
--
--     python manage_records.py rebuild --all
