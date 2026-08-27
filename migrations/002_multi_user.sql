-- 002: accounts, and one owner for everything already logged.
--
-- Turns the single-user database into a multi-user one. Every existing row is
-- assigned to one owner account, and from then on the application filters
-- everything by the logged-in user.
--
-- Two ways to run it, both safe to re-run - every step checks before it acts:
--
--   1. python migrate_multi_user.py
--      Creates the owner from APP_USERNAME / APP_PASSWORD (or
--      --username/--password) and then runs this file.
--
--   2. By hand, in psql or a SQL console (Supabase, Neon, ...)
--      Paste this file as it stands. The owner is the single account already
--      in `users`; with none there yet, or more than one, say who it is first
--      in the same session, and generate the hash with the application's own
--      hasher so the password you pick actually works at the login form:
--
--          python -c "import auth; print(auth.hash_password('YOUR-PASSWORD'))"
--
--          SELECT set_config('gym_tracker.owner_username',      'sohaib', false);
--          SELECT set_config('gym_tracker.owner_password_hash', 'pbkdf2_sha256$600000$...', false);
--          SELECT set_config('gym_tracker.owner_timezone',      'Asia/Karachi', false);  -- optional
--
--      The hash is only read when that account does not exist yet; an account
--      that is already there is never touched.
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

-- --- 3. The owner, and the backfill ---------------------------------------
-- Everything that exists today was logged by one person, so it all goes to the
-- owner. Only NULLs are touched, which is what makes a re-run a no-op.
--
-- The owner id cannot be written into this file as a literal - it is not known
-- until the account exists - so it is resolved here instead: the account named
-- by gym_tracker.owner_username, or the only account in `users` when nothing
-- was named. Anything ambiguous stops the migration rather than guessing, and
-- since this all happens inside one statement, nothing below it has run yet.
-- A database whose rows all have an owner already needs none of this and asks
-- for nothing, which is what keeps the second run quiet.

DO $$
DECLARE
    typed_name    text    := trim(coalesce(current_setting('gym_tracker.owner_username', true), ''));
    wanted        text    := lower(typed_name);
    supplied_hash text    := trim(coalesce(current_setting('gym_tracker.owner_password_hash', true), ''));
    zone          text    := nullif(trim(coalesce(current_setting('gym_tracker.owner_timezone', true), '')), '');
    owner_id      integer;
    account_count integer;
    unowned       boolean;
BEGIN
    IF wanted <> '' THEN
        SELECT user_id INTO owner_id FROM users WHERE username = wanted;

        IF owner_id IS NULL THEN
            -- Named an account that is not there yet: create it, but only with
            -- a hash the login form will accept. auth.py stores
            -- `pbkdf2_sha256$<iterations>$<salt>$<hash>` and rejects anything
            -- else, so a plaintext password pasted in here would lock the
            -- owner out of their own data.
            IF supplied_hash = '' THEN
                RAISE EXCEPTION 'No account named "%", and no password hash to create one with.', wanted
                    USING HINT = 'Set gym_tracker.owner_password_hash to the output of '
                                 '`python -c "import auth; print(auth.hash_password(''YOUR-PASSWORD''))"`, '
                                 'or run python migrate_multi_user.py instead.';
            END IF;
            IF supplied_hash NOT LIKE 'pbkdf2\_sha256$%$%$%' THEN
                RAISE EXCEPTION 'gym_tracker.owner_password_hash is not a hash this application can verify.'
                    USING HINT = 'It must look like pbkdf2_sha256$600000$<salt>$<hash> - see auth.hash_password.';
            END IF;
            IF length(wanted) < 3 OR length(wanted) > 32
               OR wanted !~ '^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$' THEN
                RAISE EXCEPTION 'Username "%" is not one the login form accepts.', typed_name
                    USING HINT = '3-32 characters, lowercase letters, digits, dot, dash or underscore.';
            END IF;

            INSERT INTO users (username, display_name, password_hash, timezone)
            VALUES (wanted, typed_name, supplied_hash, zone)
            RETURNING user_id INTO owner_id;
            RAISE NOTICE 'Created owner account "%" (user_id %).', wanted, owner_id;
        END IF;
    END IF;

    -- An assignment, not SELECT ... INTO: the INTO form reads as a plain-SQL
    -- CREATE TABLE AS to anything that sees this line outside the block, which
    -- is exactly what a tool that mis-splits the file does to it.
    unowned :=  EXISTS (SELECT 1 FROM exercises       WHERE user_id IS NULL)
             OR EXISTS (SELECT 1 FROM workout_logs    WHERE user_id IS NULL)
             OR EXISTS (SELECT 1 FROM bodyweight_logs WHERE user_id IS NULL)
             OR EXISTS (SELECT 1 FROM weekly_reports  WHERE user_id IS NULL);

    IF NOT unowned THEN
        -- Either a re-run, or a database with nothing in it yet. Nothing to
        -- hand over, so an owner is not needed and none is demanded.
        RAISE NOTICE 'Every row already has an owner; nothing to backfill.';
        RETURN;
    END IF;

    IF owner_id IS NULL THEN
        SELECT count(*) INTO account_count FROM users;

        IF account_count = 1 THEN
            SELECT user_id INTO owner_id FROM users;
        ELSIF account_count = 0 THEN
            RAISE EXCEPTION 'There is no account for the existing training data to belong to.'
                USING HINT = 'Run python migrate_multi_user.py, or set gym_tracker.owner_username '
                             'and gym_tracker.owner_password_hash and re-run - see the header of this file.';
        ELSE
            RAISE EXCEPTION 'This database has % accounts, so which one owns the existing data is ambiguous.', account_count
                USING HINT = 'Name it: SELECT set_config(''gym_tracker.owner_username'', ''<username>'', false); then re-run.';
        END IF;
    END IF;

    UPDATE exercises       SET user_id = owner_id WHERE user_id IS NULL;
    UPDATE workout_logs    SET user_id = owner_id WHERE user_id IS NULL;
    UPDATE bodyweight_logs SET user_id = owner_id WHERE user_id IS NULL;
    UPDATE weekly_reports  SET user_id = owner_id WHERE user_id IS NULL;

    RAISE NOTICE 'Rows without an owner now belong to user_id %.', owner_id;
END
$$;

-- --- 4. Lock it down ------------------------------------------------------
-- A no-op on a column that is already NOT NULL, so this survives a re-run.

ALTER TABLE exercises       ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE workout_logs    ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE bodyweight_logs ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE weekly_reports  ALTER COLUMN user_id SET NOT NULL;

-- --- 5. Foreign keys ------------------------------------------------------
-- Postgres has no ADD CONSTRAINT IF NOT EXISTS, so each one is added only when
-- the catalogue says it is missing.

DO $$
DECLARE
    target text;
BEGIN
    FOREACH target IN ARRAY ARRAY['exercises', 'workout_logs', 'bodyweight_logs', 'weekly_reports']
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = target::regclass AND conname = target || '_user_id_fkey'
        ) THEN
            EXECUTE format(
                'ALTER TABLE %I ADD CONSTRAINT %I FOREIGN KEY (user_id) '
                'REFERENCES users (user_id) ON DELETE CASCADE',
                target, target || '_user_id_fkey'
            );
        END IF;
    END LOOP;
END
$$;

-- --- 6. Uniqueness becomes per-user ---------------------------------------
-- An exercise name is unique within an account, not across the deployment;
-- likewise a weekly report is unique per user per week. Dropping the old
-- global constraints is the step that lets a second person log a "Bench Press".

ALTER TABLE exercises DROP CONSTRAINT IF EXISTS exercises_name_key;
ALTER TABLE weekly_reports DROP CONSTRAINT IF EXISTS weekly_reports_week_start_date_key;

-- Same names again in case an older database enforced them with a bare unique
-- index rather than a constraint. Dropping the constraint above already took
-- its index with it, so these are no-ops in the ordinary case.
DROP INDEX IF EXISTS exercises_name_key;
DROP INDEX IF EXISTS weekly_reports_week_start_date_key;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'exercises'::regclass AND conname = 'exercises_user_name_unique'
    ) THEN
        ALTER TABLE exercises
            ADD CONSTRAINT exercises_user_name_unique UNIQUE (user_id, name);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'weekly_reports'::regclass AND conname = 'weekly_reports_user_week_unique'
    ) THEN
        ALTER TABLE weekly_reports
            ADD CONSTRAINT weekly_reports_user_week_unique UNIQUE (user_id, week_start_date);
    END IF;
END
$$;

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
