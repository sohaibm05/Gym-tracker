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

    -- Per-user, not global: the same lift name may exist once for each person.
    CONSTRAINT exercises_user_name_unique UNIQUE (user_id, name)
);

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

    CONSTRAINT workout_logs_weight_nonneg CHECK (weight_kg IS NULL OR weight_kg >= 0),
    CONSTRAINT workout_logs_reps_positive CHECK (reps IS NULL OR reps > 0),
    CONSTRAINT workout_logs_cheat_reps_valid
        CHECK (cheat_reps >= 0 AND (reps IS NULL OR cheat_reps <= reps)),
    CONSTRAINT workout_logs_confidence_range
        CHECK (extraction_confidence IS NULL
               OR (extraction_confidence >= 0 AND extraction_confidence <= 1))
);

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
