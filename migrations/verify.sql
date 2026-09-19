-- Does this database match the code?
--
-- A schema diagram shows tables and columns, which is the half that is hard to
-- get wrong. This checks the half that fails silently: the constraints and
-- indexes that enforce the rules, and whether Supabase is handing your data to
-- anyone holding the anon key.
--
-- Paste it into a SQL console (Supabase, Neon) or run it directly. It only
-- reads catalogs, so it is safe on production and needs no special privileges.
-- Seven PASS rows means the database is what the application expects.
--
--   psql "$DATABASE_URL" -f migrations/verify.sql
--
-- What a failure means:
--   1, 2, 3, 4, 5  migration 003 has not been applied (or only partly)
--   6              exercises created before 003 landed; re-running 003 fixes it
--   7              named tables are readable and deletable via the public REST
--                  API; see the RLS section of the README

WITH expected_tables(t) AS (VALUES
  ('users'),('user_sessions'),('exercises'),('workout_logs'),('bodyweight_logs'),
  ('weekly_reports'),('routines'),('routine_exercises'),('workout_sessions'),
  ('measurements'),('personal_records'))
SELECT * FROM (
  SELECT 1 AS n, 'tables' AS check_name,
         CASE WHEN count(*) FILTER (WHERE to_regclass('public.'||t) IS NULL) = 0
              THEN 'PASS' ELSE 'FAIL' END AS status,
         coalesce(string_agg(t, ', ') FILTER (WHERE to_regclass('public.'||t) IS NULL),
                  count(*)||'/11 present') AS detail
    FROM expected_tables

  UNION ALL
  SELECT 2, 'columns added by 003',
         CASE WHEN count(*) = 5 THEN 'PASS' ELSE 'FAIL' END,
         count(*)||'/5 present'
    FROM information_schema.columns
   WHERE table_schema='public'
     AND (table_name, column_name) IN
         (('exercises','equipment'),('exercises','is_custom'),('exercises','search_key'),
          ('workout_logs','session_id'),('workout_logs','rpe'))

  UNION ALL
  SELECT 3, 'foreign keys',
         CASE WHEN count(*) = 17 THEN 'PASS' ELSE 'FAIL' END,
         count(*)||'/17 present'
    FROM pg_constraint
   WHERE contype = 'f' AND connamespace = 'public'::regnamespace

  UNION ALL
  SELECT 4, 'rpe/rir check constraints',
         CASE WHEN count(*) = 2 THEN 'PASS' ELSE 'FAIL' END,
         count(*)||'/2 present'
    FROM pg_constraint
   WHERE conname IN ('workout_logs_rpe_range','workout_logs_rir_range')

  UNION ALL
  SELECT 5, 'one active session per user',
         CASE WHEN count(*) = 1 THEN 'PASS' ELSE 'FAIL' END,
         CASE WHEN count(*) = 1 THEN 'partial unique index present'
              ELSE 'MISSING - two live sessions could open at once' END
    FROM pg_indexes
   WHERE schemaname = 'public'
     AND indexname = 'idx_workout_sessions_one_active_per_user'

  UNION ALL
  SELECT 6, 'search_key backfilled',
         CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'WARN' END,
         CASE WHEN count(*) = 0 THEN 'no NULLs'
              ELSE count(*)||' exercises invisible to search - re-run 003' END
    -- via to_jsonb so this still parses when the column is absent entirely
    FROM exercises e WHERE to_jsonb(e) ->> 'search_key' IS NULL

  UNION ALL
  SELECT 7, 'row level security',
         CASE WHEN count(*) = 0 THEN 'PASS' ELSE 'FAIL' END,
         CASE WHEN count(*) = 0 THEN 'every table protected'
              ELSE 'EXPOSED TO ANON KEY: '||string_agg(c.relname, ', ' ORDER BY c.relname) END
    FROM pg_class c JOIN pg_namespace ns ON ns.oid = c.relnamespace
   WHERE ns.nspname = 'public' AND c.relkind = 'r' AND NOT c.relrowsecurity
) checks ORDER BY n;
