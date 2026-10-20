-- Idempotent set logging (Assignment 2, Part A).
--
-- The workout app retries a set it could not confirm: a phone on gym wifi sends
-- the POST, the set is written, and the response is lost on the way back. The
-- retry used to write the set a second time. Each logged set now carries the
-- client's Idempotency-Key, and a key already stored for that person returns
-- the existing set instead of inserting another.
--
-- Safe to re-run and safe on existing data: the column is nullable and every
-- set already stored keeps NULL, which the partial index ignores.
--
--   psql "$DATABASE_URL" -f migrations/004_idempotent_sets.sql

BEGIN;

ALTER TABLE workout_logs ADD COLUMN IF NOT EXISTS client_set_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_workout_logs_client_set_id
    ON workout_logs (user_id, client_set_id) WHERE client_set_id IS NOT NULL;

COMMIT;
