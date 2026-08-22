# Gym Tracker

Turns messy free-text gym journal entries into structured Postgres rows, and
generates a weekly progress report on top of them.

You keep logging in your phone's Notes app exactly as you do now — typos,
voice-to-text run-ons, times like "6:20ish", commentary like "almost died" —
then paste the text into a small web form. The pipeline extracts the sets,
validates them, scores how much it trusts each one, and inserts only what clears
the bar. Everything else is listed for you to look at rather than quietly saved.

Power BI connects straight to the Postgres tables. That side is out of scope
here; the schema is just shaped for it.

## How it works

```
Notes app (unchanged)
  -> paste into the mobile web form            app.py
  -> Groq extraction, JSON mode, 1 retry       pipeline.extract_entities
  -> Pydantic validation                       pipeline.validate_extraction
  -> computed confidence heuristic             pipeline.compute_confidence
  -> fuzzy match against existing exercises    pipeline.find_matching_exercise
  -> below threshold? -> review list, not inserted
  -> above threshold? -> parameterized INSERT into Postgres
  -> Power BI reads Postgres directly (later, not part of this build)
```

## Files

| File | What it is |
|---|---|
| `schema.sql` | Postgres schema: `exercises`, `workout_logs`, `bodyweight_logs`, `weekly_reports` |
| `pipeline.py` | All extraction / validation / insert logic. The CLI and web app both import this; neither reimplements any of it |
| `parse_workout_log.py` | CLI: `python parse_workout_log.py <file> <date>` |
| `app.py` | FastAPI web app — the phone-facing form, plus the weekly-report button |
| `insights.py` | Weekly report. Stage A computes every number; Stage B only writes prose |
| `charts.py` | The `/progress` page - inline-SVG charts, no chart library |
| `seed_sample_data.py` | Seeds three weeks of realistic data so the report can be tried out |
| `tests/` | Unit tests for the Stage A rules and the confidence / fuzzy-match logic |
| `sample_entry.txt` | A messy journal entry in the real style, for trying the CLI |

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # then fill it in
psql "$DATABASE_URL" -f schema.sql
```

`.env` is loaded automatically at import — you do not need to export anything by
hand. A real environment variable always wins over the file, so Render's
dashboard configuration is never shadowed by a stray `.env`. `.env` is
gitignored; only `.env.example` is committed.

When filling it in, replace the whole `[YOUR-PASSWORD]` placeholder, **square
brackets included** — leaving them turns the password into a literal
`[YOUR-PASSWORD]` string, and Python's URL parser reads the brackets as an IPv6
host and fails with a misleading error about IP addresses.

`schema.sql` is safe to re-run — every object is `IF NOT EXISTS`.

**Existing databases:** apply anything in `migrations/` that postdates your
setup. They are additive and re-runnable:

```bash
psql "$DATABASE_URL" -f migrations/001_add_cheat_reps.sql
```

**Supabase:** enable RLS on all four tables. The app connects as the table owner
so it bypasses RLS, but Supabase auto-exposes a REST API over the `public`
schema to the anon key, and without RLS that key can read and delete your data.
No policies are needed — RLS on with zero policies denies the API entirely:

```sql
ALTER TABLE exercises       ENABLE ROW LEVEL SECURITY;
ALTER TABLE workout_logs    ENABLE ROW LEVEL SECURITY;
ALTER TABLE bodyweight_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE weekly_reports  ENABLE ROW LEVEL SECURITY;
```

### Try it

```bash
python parse_workout_log.py sample_entry.txt 2026-08-14 --dry-run   # no writes
python parse_workout_log.py sample_entry.txt 2026-08-14

python seed_sample_data.py --reset      # three weeks of sample data
uvicorn app:app --reload                # then open http://127.0.0.1:8000
```

Stuck on configuration? `--check-config` reports where every setting is coming
from and tests both connections, without printing a secret:

```bash
python parse_workout_log.py --check-config
```

It names the three things that actually go wrong: a `[YOUR-PASSWORD]` placeholder
left in the URL, a shell variable silently shadowing `.env`, and a malformed key.

`--dry-run` calls Groq and prints what came back without touching the database,
so it needs only `GROQ_API_KEY`. It shows the resolved local time and the
clean-rep split, which is the only way to check either before anything is
inserted:

```
  [INSERT] 1.00   16:35  Dumbbell Shoulder Press      10kg x 10  (warmup)
  [INSERT] 1.00  ~18:00  Lateral Raise                7.5kg x 10 (3 cheat -> 7 clean)
  [REVIEW] 0.65  ~18:00  Skull Crusher                10kg x ?
```

A leading `~` means the text carried no time marker and the default session hour
was applied — which is how you spot a time the extraction missed.

## The decisions worth knowing about

### The LLM does not produce any number in the report

`insights.py` is split in two, and the split is the point.

**Stage A** is plain Python and SQL. Estimated 1RM (Epley: `weight × (1 + reps/30)`),
volume, this week's e1RM versus the trailing 4-week average, rep-range
distribution, plateau detection, the program-stagnation rollup, and every
load recommendation are computed in code.

**Stage B** hands Stage A's finished numbers to Groq and asks for prose. Its
prompt states that it may not alter, invent, or re-round any figure, and may not
create or second-guess a recommendation. If the Groq call fails, you get
`fallback_summary()` — the same numbers, less polish. A failed narration never
costs you the report and never tempts anyone into letting the model fill a gap.

### Confidence is computed, not asked for

LLMs are badly calibrated at rating their own certainty, so the model is
explicitly told not to report a confidence score. `compute_confidence()` derives
one from things that can actually be checked:

- did Pydantic validation pass;
- did the required fields (exercise name, weight, reps) come back non-null;
- does the exercise name actually match the text span it was supposedly read
  from — the check that catches a name the model invented rather than read.

Below `CONFIDENCE_THRESHOLD` (0.7) an entry goes to the review list and is not
inserted. A set missing its weight or reps lands at 0.65 and so is always
surfaced. That is deliberate: a set with no load recorded is not much use for
progression, and a bodyweight exercise logged this way is worth a glance.

### Exercise names are fuzzy-matched before a new row is created

The model normalizes names, but not identically on every call. Left alone, "Chest
Bench Press", "Bench Press (Chest)" and "Barbell Chest Bench Press" become three
rows and your progress history splits three ways — which defeats the point of
tracking.

Real journal entries also produce `Skullcrushers` vs `Skull Crushers` (word
boundaries only) and `Cable Hammer Curls` vs `Hammer Curl` (an equipment
qualifier and a plural at once). Names are singularized before comparison, and
compared with spaces stripped as an extra signal, so both merge.

No single rapidfuzz scorer gets this right. `token_set_ratio` merges "Bench
Press" with "Incline Bench Press" (score 100), and `token_sort_ratio` refuses to
merge "Barbell Chest Bench Press" with "Chest Bench Press" (score 81). So
`name_match_score()` uses `token_sort_ratio` as the base, and only lifts it to
`token_set_ratio` when one name's tokens are a strict subset of the other's *and*
every extra token is a known equipment/muscle qualifier (`barbell`, `dumbbell`,
`chest`, `lat`…). Words outside that allowlist — `incline`, `front`, `close
grip`, `romanian` — mark a genuinely different movement and block the merge.
Unknown words fail closed, into a separate row.

Both names must have at least two tokens for the subset bonus, so "Curl" never
absorbs "Leg Curl". Threshold is 85, tunable via `FUZZY_MATCH_THRESHOLD`.

### Cheat reps are counted, not flagged

Entries like `Lateral raises 7.5kg 10 reps 3 were cheat` mean three reps *inside*
a ten-rep set were completed with momentum — not three cheat sets. So
`cheat_reps` is a count, and **clean reps** (`reps - cheat_reps`) drive estimated
1RM and the rep-range rules. Volume still uses total reps, because the work was
performed.

This matters more than it looks. Scoring those ten reps as clean inflates that
set's e1RM by 8%, and at the top of the range it flips the recommendation
outright: 12 reps with 3 cheat reps is 9 clean reps, which is a hold, not the
load increase you would otherwise be told to take on a lift you cheated to
finish.

### Recommendation precedence

In order — the first one that applies wins:

1. **Pain safeguard.** Any `pain_flag` on this exercise in the last 2 sessions
   holds the load regardless of performance, and adds one light warm-up set at
   ~45% of the working weight. Pain flagged 3+ consecutive sessions escalates the
   note to suggest seeing a professional. Controlled by `PAIN_SAFEGUARD_ENABLED`,
   default `true`; nothing else in the code path can turn it off.
2. **Increase** when every working set in the last session hit the top of the rep
   range — next practical increment up (~2.5–5%, rounded to a real plate change).
3. **Deload or swap** when e1RM has been flat or declining for 3+ sessions.
4. **Hold** otherwise.

Increase deliberately outranks the plateau branch. Topping out the rep range at
the same load for three sessions *does* read as a flat e1RM, but the right answer
there is more weight, not a deload — ranking them the other way would recommend
deloading someone who is progressing.

On light lifts a single 2.5 kg change is more than 5%; that is simply the
smallest change the plates allow. Set `PLATE_INCREMENT_KG` lower if your gym has
finer dumbbells.

### Program-level stagnation is a rollup, not a second metric

Whole-program staleness is derived from the per-exercise plateau flags: it fires
when at least `PROGRAM_STAGNATION_FRACTION` (0.6) of *regularly-trained*
exercises — those appearing in 2 of the last 3 weeks — are simultaneously
plateaued, and only once there are at least `PROGRAM_STAGNATION_MIN_EXERCISES`
(4) of them. With two or three exercises logged, "60% plateaued" is noise.

It deliberately does **not** key off total-volume trend. A volume drop is just as
likely to be an intentional deload or a dropped accessory lift, and reading that
as staleness would flag a deliberate program change as a problem.

When it fires you get the *category* of action — rotate the split, deload week,
new mesocycle. It never names a specific replacement program, because picking one
is a coaching judgement call a text-log parser has no business making.

### What the pain handling deliberately does not do

It does not guess why something hurts, and it does not prescribe corrective or
rehab work. A text note cannot separate a strain from a tendon issue from
ordinary fatigue, and those want different responses. The tool holds the load,
adds a light warm-up set, and after three consecutive flagged sessions says it is
worth getting looked at. That is the whole of it — the Stage B prompt forbids the
model from going further, and a unit test asserts the note never mentions a
diagnosis or a corrective exercise.

### Two entries on the same day

Logging a second time against a date that already has rows asks which you meant:

- **Add** (the default) - a second session that day, or sets you forgot. Rows
  accumulate, which is what you want for a morning and an evening session.
- **Replace** - discard everything already logged on that date and use this
  entry instead. For correcting a bad parse, not for adding.

Replace is deliberately never the default, and never implicit. Two safeguards
back it: the delete and the insert that follows commit in one transaction, so a
failure mid-way cannot leave the day emptied with nothing put back; and it only
runs when there is something to insert, so a failed extraction or an
all-review entry leaves the existing day untouched.

The delete window is a UTC range covering one *local* day, so it cannot reach
into a neighbouring date - tested at an offset zone, not just UTC.

`--replace` does the same from the CLI.

### Duplicate submissions

Render's free tier cold-starts in 30–50s after idle, which is exactly when you
double-tap submit. Before extracting anything, `/log` checks whether the same raw
text was inserted in the last `DUPLICATE_WINDOW_MINUTES` (5) and returns the
earlier result instead of re-running the model and re-inserting.

## Charts

`/progress` plots the logged data: weekly volume, estimated 1RM per exercise as
small multiples, bodyweight, volume by muscle group, and a pain-flag view. Drawn
as inline SVG from a JSON blob, so there is no chart library, no external
request, and nothing added to `requirements.txt`.

Every figure comes from the same Stage A functions the weekly report uses, so a
number on a chart and the same number in the report cannot drift apart.

A few decisions that are easy to get wrong:

- **Each exercise carries a fitted trend line and its slope in kg/week.** The
  shape of a line does not give the rate: two lifts can both end higher while
  one is gaining three times as fast. The line drawn and the rate quoted beside
  it come from the same least-squares fit, computed server-side, so they cannot
  disagree.
- **Every chart plots one series**, so there is no categorical palette and no
  legend - the card title names what is plotted. The single hue is validated
  against both the light and dark chart surfaces.
- **Muscle groups all share one colour.** They are nominal categories, so
  colouring them darker-where-bigger would double-encode bar length as hue and
  spend the only free channel on information the bar already shows.
- **A constant series shows one axis tick, at its actual value.** Scaling a flat
  line to fill the plot invents ticks that appear nowhere in the data - a lift
  held at 65kg must not be labelled 63.7 and 66.3.
- **Pain uses the reserved status palette, never colour alone** - each row
  carries an icon and a word.
- **Every chart has a table twin**, so no value is reachable only by hovering.

## Security

- **Parameterized SQL everywhere.** Every statement is `sqlalchemy.text()` with
  bound `:param` placeholders. No value — user-typed or model-generated — is ever
  formatted into a query string.
- **Secrets are env vars only.** `GROQ_API_KEY`, `DATABASE_URL`, `APP_USERNAME`,
  `APP_PASSWORD` are never hardcoded, never rendered to the page, never logged.
  `.env` is gitignored; `.env.example` holds placeholders only.
- **HTTP Basic Auth** on every route except `/healthz`, compared with
  `secrets.compare_digest`. Basic Auth credentials are base64, not encrypted, so
  this is **only safe over HTTPS** — Render terminates TLS by default, but confirm
  your deployed URL is `https://` before relying on it. The app logs a warning on
  any non-local request that arrives over plain HTTP.
- **Model output is escaped before it reaches the page.** Extracted exercise
  names are rendered through `html.escape`, so a name containing markup cannot
  inject anything.

## Deployment

**Database — Supabase free tier.** Create a project, run `schema.sql`, and take
the pooled connection URI as `DATABASE_URL`. Free projects pause after 7 days of
inactivity; at normal logging frequency you will not notice, but that is why the
first request after a long gap can be slow.

**Web app — Vercel (Hobby).** Free, no card. Vercel auto-detects the FastAPI
`app` in `app.py`, so there is nothing to configure beyond environment variables.

- Import the repo at vercel.com, accept the detected settings
- Set `DATABASE_URL`, `GROQ_API_KEY`, `APP_USERNAME`, `APP_PASSWORD` and
  `LOCAL_TIMEZONE` in Project Settings → Environment Variables (`.env` is
  gitignored, so it is not deployed)
- `vercel.json` pins `maxDuration` to 60s, which covers a Groq call plus inserts

Use the Supabase **transaction pooler (port 6543)** rather than session mode
here. Serverless gives each request its own short-lived process, and the
transaction pooler is built for exactly that churn. Session mode on 5432 also
works, since the app already avoids retaining a pool.

That last part matters: Vercel sets `VERCEL=1`, which switches
`pipeline.get_engine` to SQLAlchemy's `NullPool`. A pool held between requests
can never be reused when the process does not outlive the request, and would
just hold Postgres slots open for nothing. Set `SERVERLESS=true` by hand on any
other serverless host.

**Web app — Render free tier** (alternative; now requires a card on file).**

- Build: `pip install -r requirements.txt`
- Start: `uvicorn app:app --host 0.0.0.0 --port $PORT`
- Set `GROQ_API_KEY`, `DATABASE_URL`, `APP_USERNAME`, `APP_PASSWORD` in the
  dashboard, plus any tuning vars from `.env.example`.

Free instances spin down after 15 minutes idle and take 30–50s to wake. That is
fine for personal use and is exactly why the duplicate-submission guard exists.

**LLM — Groq free tier.** No billing account is needed anywhere in this stack.

### A note on the model ID

Groq deprecated `llama-3.3-70b-versatile` and `llama-3.1-8b-instant` for free and
developer tiers on **2026-06-17**. This project defaults to
**`openai/gpt-oss-120b`**, Groq's recommended general-purpose replacement,
verified against their deprecation notice at build time. Override with
`GROQ_MODEL`, and re-check <https://console.groq.com/docs/models> before assuming
the default is still current.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

201 tests, no network and no database required — they cover the Stage A rule
branches (e1RM, plateau detection, the program-stagnation rollup, every
increase/hold/deload branch, pain safeguard on and off, the escalation
threshold) and `pipeline.py`'s confidence heuristic, fuzzy matching, timestamp
resolution, and JSON-mode retry behaviour. These are pure functions, so they are
cheap to cover, and they are exactly the code where a silent bug produces a wrong
training recommendation that nobody notices.

## Not built (by design)

- **iOS Shortcut** posting from the Notes Share Sheet straight to `/log`. Listed
  as a stretch goal in the spec; ask for it when you want it.
- **Scheduled report generation.** The weekly report runs from a button, which
  keeps the stack free of scheduling infrastructure. A cron version is a fine
  later upgrade.
- **Power BI dashboards.** The schema is shaped for them; building them is not
  part of this.
