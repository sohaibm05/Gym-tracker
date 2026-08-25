-- 002: accounts, and one owner for everything already logged.
--
-- Turns the single-user database into a multi-user one. Every existing row is
-- assigned to one owner account, which the runner creates from APP_USERNAME /
-- APP_PASSWORD (or --username/--password) before this file's backfill step.
--
-- Do not run this file by hand unless you have already inserted that owner row
-- and know its user_id: the statements below reference :owner_id. Run
-- `python migrate_multi_user.py` instead, which creates the owner (password
-- hashing needs Python), runs these statements in order, and is safe to re-run.
--
-- Ordering matters. Columns go on as nullable, are backfilled, and only then
-- become NOT NULL - the other way round would fail on the first existing row.

-- --- 1. Accounts ----------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    user_id       SERIAL PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    timezone      TEXT,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ,

    CONSTRAINT users_username_lowercase CHECK (username = lower(username))
);

CREATE TABLE IF NOT EXISTS user_sessions (
    token_hash   TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_user_sessions_user_id ON user_sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_user_sessions_expires_at ON user_sessions (expires_at);

-- --- 2. Ownership columns, nullable for now -------------------------------

ALTER TABLE exercises       ADD COLUMN IF NOT EXISTS user_id INTEGER;
ALTER TABLE workout_logs    ADD COLUMN IF NOT EXISTS user_id INTEGER;
ALTER TABLE bodyweight_logs ADD COLUMN IF NOT EXISTS user_id INTEGER;
ALTER TABLE weekly_reports  ADD COLUMN IF NOT EXISTS user_id INTEGER;

-- --- 3. Backfill ----------------------------------------------------------
-- Everything that exists today was logged by one person, so it all goes to the
-- owner. Only NULLs are touched, which is what makes a re-run a no-op.

UPDATE exercises       SET user_id = :owner_id WHERE user_id IS NULL;
UPDATE workout_logs    SET user_id = :owner_id WHERE user_id IS NULL;
UPDATE bodyweight_logs SET user_id = :owner_id WHERE user_id IS NULL;
UPDATE weekly_reports  SET user_id = :owner_id WHERE user_id IS NULL;

-- --- 4. Lock it down ------------------------------------------------------

ALTER TABLE exercises       ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE workout_logs    ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE bodyweight_logs ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE weekly_reports  ALTER COLUMN user_id SET NOT NULL;

-- --- 5. Foreign keys ------------------------------------------------------
-- Named so the runner can check for them before adding; Postgres has no
-- ADD CONSTRAINT IF NOT EXISTS.

ALTER TABLE exercises
    ADD CONSTRAINT exercises_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE;
ALTER TABLE workout_logs
    ADD CONSTRAINT workout_logs_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE;
ALTER TABLE bodyweight_logs
    ADD CONSTRAINT bodyweight_logs_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE;
ALTER TABLE weekly_reports
    ADD CONSTRAINT weekly_reports_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE;

-- --- 6. Uniqueness becomes per-user ---------------------------------------
-- An exercise name is unique within an account, not across the deployment;
-- likewise a weekly report is unique per user per week. Dropping the old
-- global constraints is the step that lets a second person log a "Bench Press".

ALTER TABLE exercises DROP CONSTRAINT IF EXISTS exercises_name_key;
ALTER TABLE exercises
    ADD CONSTRAINT exercises_user_name_unique UNIQUE (user_id, name);

ALTER TABLE weekly_reports DROP CONSTRAINT IF EXISTS weekly_reports_week_start_date_key;
ALTER TABLE weekly_reports
    ADD CONSTRAINT weekly_reports_user_week_unique UNIQUE (user_id, week_start_date);

-- --- 7. Indexes, now user-first -------------------------------------------

CREATE INDEX IF NOT EXISTS idx_workout_logs_user_exercise_logged_at
    ON workout_logs (user_id, exercise_id, logged_at);
CREATE INDEX IF NOT EXISTS idx_bodyweight_logs_user_logged_at
    ON bodyweight_logs (user_id, logged_at);
CREATE INDEX IF NOT EXISTS idx_workout_logs_user_created_at
    ON workout_logs (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_bodyweight_logs_user_created_at
    ON bodyweight_logs (user_id, created_at);

DROP INDEX IF EXISTS idx_workout_logs_exercise_logged_at;
DROP INDEX IF EXISTS idx_bodyweight_logs_logged_at;
DROP INDEX IF EXISTS idx_workout_logs_created_at;
DROP INDEX IF EXISTS idx_bodyweight_logs_created_at;
