-- Adds cheat_reps to workout_logs.
--
-- Run once against a database created before this column existed. Additive and
-- non-destructive: existing rows default to 0 cheat reps, which is how they
-- were already being scored.

ALTER TABLE workout_logs
    ADD COLUMN IF NOT EXISTS cheat_reps INTEGER NOT NULL DEFAULT 0;

ALTER TABLE workout_logs
    DROP CONSTRAINT IF EXISTS workout_logs_cheat_reps_valid;

ALTER TABLE workout_logs
    ADD CONSTRAINT workout_logs_cheat_reps_valid
    CHECK (cheat_reps >= 0 AND (reps IS NULL OR cheat_reps <= reps));
