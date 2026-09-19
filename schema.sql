-- Gym Tracker schema (Postgres / Supabase free tier)
--
-- Design notes:
--   * Every row of training data belongs to exactly one row in `users`. There
--     is no shared data and no global view: the application always filters by
--     the logged-in user_id, and the foreign keys below make an orphaned row
--     impossible. Deleting a user deletes their training with it.
--   * `exercises` is per-user, not a shared catalogue. Two people can both have
--     a "Lat Pulldown" filed under different muscle groups, and one person
--     correcting their own filing cannot touch anyone else's.
--   * All `logged_at` columns are timestamptz. The application converts local
--     wall-clock time to UTC at insert time, using the user's own timezone
--     (users.timezone) and falling back to the LOCAL_TIMEZONE env var.
--   * `raw_source` keeps the original journal text for every row so any parse
--     can be audited or re-run later.
--   * `extraction_confidence` is computed by the pipeline from checkable
--     signals (see pipeline.compute_confidence). It is never self-reported by
--     the LLM. In the web app it marks a row red on the review screen rather
--     than gating the insert, so a value below CONFIDENCE_THRESHOLD here means
--     someone saw the row and saved it anyway; the CLI, which has nobody
--     watching, still refuses one. A row edited or typed by hand is stored at
--     1.0 - there is no extraction left to score.
--   * Schema is shaped for direct Power BI consumption: narrow fact tables
--     (workout_logs, bodyweight_logs), dimension tables (exercises, users), and
--     a precomputed report table (weekly_reports).
--
-- Safe to re-run: every object is created IF NOT EXISTS.
--
-- Upgrading a database that predates accounts? Do not run this file — run
-- `python migrate_multi_user.py`, which adds the tables and columns below to
-- the existing one and moves the data you already have onto an owner account.

CREATE TABLE IF NOT EXISTS users (
    user_id       SERIAL PRIMARY KEY,
    -- Lowercased. `display_name` keeps the capitalisation the person typed;
    -- this column is what logins and the unique index compare against, so
    -- "Sohaib" and "sohaib" cannot both be registered.
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    -- PBKDF2-HMAC-SHA256, `pbkdf2_sha256$iterations$salt$hash`. See auth.py.
    password_hash TEXT NOT NULL,
    -- IANA name. NULL means "use the deployment's LOCAL_TIMEZONE", which is
    -- what decides the calendar day - and so the week - a session falls in.
    timezone      TEXT,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ,

    CONSTRAINT users_username_lowercase CHECK (username = lower(username))
);

-- Login sessions. Only the SHA-256 of each token is stored, so a database dump
-- cannot be replayed as a live login.
CREATE TABLE IF NOT EXISTS user_sessions (
    token_hash   TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_user_sessions_user_id
    ON user_sessions (user_id);
-- Supports the expiry sweep.
CREATE INDEX IF NOT EXISTS idx_user_sessions_expires_at
    ON user_sessions (expires_at);

CREATE TABLE IF NOT EXISTS exercises (
    exercise_id  SERIAL PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    muscle_group TEXT,
    -- Equipment is a dimension of the exercise, not part of its name. Storing
    -- it separately is what lets the catalog filter by it rather than
    -- substring-matching "(Barbell)" out of the name.
    equipment    TEXT,
    -- FALSE for rows seeded from the built-in catalog, TRUE for anything the
    -- person typed. Only custom rows are safe to rename on their behalf.
    is_custom    BOOLEAN NOT NULL DEFAULT TRUE,
    -- Lowercased, punctuation-flattened form of `name`, for search. Derived,
    -- but stored, because an index cannot be used on a function applied to
    -- every row in a WHERE clause.
    search_key   TEXT,

    -- Per-user, not global: the same lift name may exist once for each person.
    CONSTRAINT exercises_user_name_unique UNIQUE (user_id, name)
);

CREATE INDEX IF NOT EXISTS idx_exercises_user_equipment
    ON exercises (user_id, equipment);
CREATE INDEX IF NOT EXISTS idx_exercises_user_search_key
    ON exercises (user_id, search_key);

-- --------------------------------------------------------------------------
-- Routines and live sessions
--
-- Declared before workout_logs because that table carries a foreign key to
-- workout_sessions, and Postgres resolves references in file order.
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS routines (
    routine_id  SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    notes       TEXT,
    position    INTEGER NOT NULL DEFAULT 0,
    -- Archived rather than deleted: finished sessions reference the routine
    -- they came from, and retiring a template must not rewrite history.
    archived    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT routines_name_not_blank CHECK (length(btrim(name)) > 0),
    CONSTRAINT routines_user_name_unique UNIQUE (user_id, name)
);

CREATE INDEX IF NOT EXISTS idx_routines_user_position
    ON routines (user_id, position, routine_id);

CREATE TABLE IF NOT EXISTS routine_exercises (
    routine_exercise_id SERIAL PRIMARY KEY,
    routine_id          INTEGER NOT NULL REFERENCES routines (routine_id) ON DELETE CASCADE,
    exercise_id         INTEGER NOT NULL REFERENCES exercises (exercise_id) ON DELETE CASCADE,
    -- Deliberately not unique per routine: reordering under a unique constraint
    -- needs a temporary-value dance on every move, and routine_exercise_id
    -- breaks ties deterministically in the ORDER BY.
    position            INTEGER NOT NULL DEFAULT 0,
    target_sets         INTEGER,
    -- A range, because "8-12" is how programmes are written. Equal low and high
    -- means a fixed target.
    target_reps_low     INTEGER,
    target_reps_high    INTEGER,
    target_weight_kg    NUMERIC(6, 2),
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

CREATE TABLE IF NOT EXISTS workout_sessions (
    session_id  BIGSERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    -- ON DELETE SET NULL so removing a template never deletes the training
    -- done under it.
    routine_id  INTEGER REFERENCES routines (routine_id) ON DELETE SET NULL,
    -- Copied at start time, not joined at read time: renaming a routine later
    -- must not rewrite what past sessions were called.
    name        TEXT,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- NULL means still in progress. The only live state the server keeps, and
    -- what lets someone close the app mid-workout and come back to it.
    finished_at TIMESTAMPTZ,
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT workout_sessions_finished_after_started
        CHECK (finished_at IS NULL OR finished_at >= started_at)
);

CREATE INDEX IF NOT EXISTS idx_workout_sessions_user_started
    ON workout_sessions (user_id, started_at DESC);

-- At most one live session per person. A partial unique index constrains only
-- the in-progress rows, so any number of finished sessions is fine but a
-- double-tapped "start workout" cannot split one workout across two sessions.
CREATE UNIQUE INDEX IF NOT EXISTS idx_workout_sessions_one_active_per_user
    ON workout_sessions (user_id) WHERE finished_at IS NULL;

CREATE TABLE IF NOT EXISTS workout_logs (
    log_id                BIGSERIAL PRIMARY KEY,
    user_id               INTEGER NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    exercise_id           INTEGER NOT NULL REFERENCES exercises (exercise_id),
    logged_at             TIMESTAMPTZ NOT NULL,
    weight_kg             NUMERIC(6, 2),
    reps                  INTEGER,
    -- Reps within the set completed with momentum/assistance ("10 reps, 3 were
    -- cheat"). Clean reps = reps - cheat_reps, and clean reps are what drive
    -- estimated 1RM and the rep-range rules; total reps still drive volume.
    cheat_reps            INTEGER NOT NULL DEFAULT 0,
    set_number            INTEGER,
    is_warmup             BOOLEAN NOT NULL DEFAULT FALSE,
    is_dropset            BOOLEAN NOT NULL DEFAULT FALSE,
    pain_flag             BOOLEAN NOT NULL DEFAULT FALSE,
    notes                 TEXT,
    raw_source            TEXT,
    extraction_confidence NUMERIC(4, 3),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- These three sit after created_at on purpose. Migration 003 adds them with
    -- ALTER TABLE, which appends, so declaring them here in the same position
    -- makes a freshly created database and a migrated one byte-identical under
    -- pg_dump --schema-only. That is what lets you diff a live database against
    -- this file to detect drift; a cosmetic column-order difference would bury
    -- a real one in the noise.
    --
    -- NULL for every set entered through the journal-paste flow, which has no
    -- session. Set only by the live logger.
    session_id            BIGINT REFERENCES workout_sessions (session_id) ON DELETE SET NULL,
    -- Two scales for the same judgement (RIR = 10 - RPE). People use one or the
    -- other, so both are stored and either may be null; deriving one from the
    -- other would invent a precision nobody gave.
    rpe                   NUMERIC(3, 1),
    rir                   INTEGER,

    CONSTRAINT workout_logs_weight_nonneg CHECK (weight_kg IS NULL OR weight_kg >= 0),
    CONSTRAINT workout_logs_reps_positive CHECK (reps IS NULL OR reps > 0),
    CONSTRAINT workout_logs_cheat_reps_valid
        CHECK (cheat_reps >= 0 AND (reps IS NULL OR cheat_reps <= reps)),
    CONSTRAINT workout_logs_confidence_range
        CHECK (extraction_confidence IS NULL
               OR (extraction_confidence >= 0 AND extraction_confidence <= 1)),
    CONSTRAINT workout_logs_rpe_range CHECK (rpe IS NULL OR (rpe >= 1 AND rpe <= 10)),
    CONSTRAINT workout_logs_rir_range CHECK (rir IS NULL OR (rir >= 0 AND rir <= 20))
);

CREATE INDEX IF NOT EXISTS idx_workout_logs_session
    ON workout_logs (session_id);

-- --------------------------------------------------------------------------
-- Measurements and personal records
--
-- Declared after workout_logs because personal_records references it.
-- --------------------------------------------------------------------------

-- Long format - one row per site per date - rather than a wide table with a
-- column per body part. Adding "forearm" to a wide table is a migration; here
-- it is a new value in an existing column. Bodyweight keeps its own table: it
-- predates this, the weekly report reads it, and it carries body-fat alongside.
CREATE TABLE IF NOT EXISTS measurements (
    measurement_id BIGSERIAL PRIMARY KEY,
    user_id        INTEGER NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    measured_at    TIMESTAMPTZ NOT NULL,
    site           TEXT NOT NULL,
    value_cm       NUMERIC(5, 1) NOT NULL,
    notes          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT measurements_site_not_blank CHECK (length(btrim(site)) > 0),
    -- An upper bound as well as a lower one: a waist of 900 is a units mistake,
    -- and catching it here is cheaper than explaining a ruined chart later.
    CONSTRAINT measurements_value_sane CHECK (value_cm > 0 AND value_cm <= 400)
);

CREATE INDEX IF NOT EXISTS idx_measurements_user_site_measured
    ON measurements (user_id, site, measured_at DESC);

-- A cache over workout_logs, kept because the interaction demands it: telling
-- someone they just hit a PR has to happen as they tap the checkmark, and
-- aggregating their whole history per set is the wrong thing to do on gym wifi.
-- One indexed row per (person, exercise, type) makes it a single-row read and a
-- single-row upsert. Rebuildable from workout_logs by records.rebuild().
CREATE TABLE IF NOT EXISTS personal_records (
    record_id    BIGSERIAL PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    exercise_id  INTEGER NOT NULL REFERENCES exercises (exercise_id) ON DELETE CASCADE,
    -- heaviest_weight      most weight moved for at least one clean rep
    -- best_1rm             best Epley estimate from a single set
    -- best_session_volume  most weight x reps for this lift in one session
    record_type  TEXT NOT NULL,
    value        NUMERIC(12, 2) NOT NULL,
    -- The set that set it, so the UI can say "100kg x 5" and not only the
    -- derived figure.
    weight_kg    NUMERIC(6, 2),
    reps         INTEGER,
    log_id       BIGINT REFERENCES workout_logs (log_id) ON DELETE SET NULL,
    session_id   BIGINT REFERENCES workout_sessions (session_id) ON DELETE SET NULL,
    achieved_at  TIMESTAMPTZ NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT personal_records_type_known
        CHECK (record_type IN ('heaviest_weight', 'best_1rm', 'best_session_volume')),
    CONSTRAINT personal_records_value_positive CHECK (value > 0),
    CONSTRAINT personal_records_unique UNIQUE (user_id, exercise_id, record_type)
);

CREATE INDEX IF NOT EXISTS idx_personal_records_user_achieved
    ON personal_records (user_id, achieved_at DESC);

CREATE TABLE IF NOT EXISTS bodyweight_logs (
    log_id                BIGSERIAL PRIMARY KEY,
    user_id               INTEGER NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    logged_at             TIMESTAMPTZ NOT NULL,
    weight_kg             NUMERIC(5, 2) NOT NULL,
    body_fat_pct          NUMERIC(4, 1),
    notes                 TEXT,
    raw_source            TEXT,
    extraction_confidence NUMERIC(4, 3),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT bodyweight_logs_weight_positive CHECK (weight_kg > 0),
    CONSTRAINT bodyweight_logs_bf_range
        CHECK (body_fat_pct IS NULL OR (body_fat_pct >= 0 AND body_fat_pct <= 100)),
    CONSTRAINT bodyweight_logs_confidence_range
        CHECK (extraction_confidence IS NULL
               OR (extraction_confidence >= 0 AND extraction_confidence <= 1))
);

-- (user_id, week_start_date) is UNIQUE so regenerating a report for an
-- already-computed week is an upsert (ON CONFLICT ... DO UPDATE), never a
-- duplicate row - and one person regenerating never overwrites another's.
CREATE TABLE IF NOT EXISTS weekly_reports (
    report_id       SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    week_start_date DATE NOT NULL,
    summary_text    TEXT,
    recommendations JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT weekly_reports_user_week_unique UNIQUE (user_id, week_start_date)
);

-- Progress queries are always "this user, this exercise, over time".
CREATE INDEX IF NOT EXISTS idx_workout_logs_user_exercise_logged_at
    ON workout_logs (user_id, exercise_id, logged_at);

-- Bodyweight trend is a straight per-user time series.
CREATE INDEX IF NOT EXISTS idx_bodyweight_logs_user_logged_at
    ON bodyweight_logs (user_id, logged_at);

-- Supports the /log duplicate-submission guard, which looks up recent rows by
-- their originating journal text within one account.
CREATE INDEX IF NOT EXISTS idx_workout_logs_user_created_at
    ON workout_logs (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_bodyweight_logs_user_created_at
    ON bodyweight_logs (user_id, created_at);
