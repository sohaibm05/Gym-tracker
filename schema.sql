-- Gym Tracker schema (Postgres / Supabase free tier)
--
-- Design notes:
--   * All `logged_at` columns are timestamptz. The application converts local
--     wall-clock time (LOCAL_TIMEZONE env var) to UTC at insert time. This is a
--     single-user personal tool, so one fixed timezone is assumed throughout.
--   * `raw_source` keeps the original journal text for every row so any parse
--     can be audited or re-run later.
--   * `extraction_confidence` is computed by the pipeline from checkable
--     signals (see pipeline.compute_confidence). It is never self-reported by
--     the LLM. Rows below CONFIDENCE_THRESHOLD are not inserted at all, so in
--     practice stored values are >= that threshold.
--   * Schema is shaped for direct Power BI consumption: narrow fact tables
--     (workout_logs, bodyweight_logs), one dimension table (exercises), and a
--     precomputed report table (weekly_reports).
--
-- Safe to re-run: every object is created IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS exercises (
    exercise_id  SERIAL PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    muscle_group TEXT
);

CREATE TABLE IF NOT EXISTS workout_logs (
    log_id                BIGSERIAL PRIMARY KEY,
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

-- week_start_date is UNIQUE so regenerating a report for an already-computed
-- week is an upsert (ON CONFLICT ... DO UPDATE), never a duplicate row.
CREATE TABLE IF NOT EXISTS weekly_reports (
    report_id       SERIAL PRIMARY KEY,
    week_start_date DATE NOT NULL UNIQUE,
    summary_text    TEXT,
    recommendations JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Progress queries are always "this exercise, over time".
CREATE INDEX IF NOT EXISTS idx_workout_logs_exercise_logged_at
    ON workout_logs (exercise_id, logged_at);

-- Bodyweight trend is a straight time series.
CREATE INDEX IF NOT EXISTS idx_bodyweight_logs_logged_at
    ON bodyweight_logs (logged_at);

-- Supports the /log duplicate-submission guard, which looks up recent rows by
-- their originating journal text.
CREATE INDEX IF NOT EXISTS idx_workout_logs_created_at
    ON workout_logs (created_at);
CREATE INDEX IF NOT EXISTS idx_bodyweight_logs_created_at
    ON bodyweight_logs (created_at);
