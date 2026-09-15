/* Live workout app.
 *
 * Vanilla JS, no framework, no build step. The repo has no JS toolchain and
 * this is one screen's worth of state; adding a bundler and a component library
 * to it would be more moving parts than the thing they manage.
 *
 * Three ideas hold it together:
 *
 *   1. The server owns the workout. `workout_sessions` with a null finished_at
 *      IS the live state. This file caches it to render, but the server is the
 *      truth, so a locked phone, a reload, or a second device all recover.
 *
 *   2. The rest timer is a timestamp, not a countdown. Storing "ends at
 *      1789451340123" and recomputing the remainder on every tick survives the
 *      screen locking, the tab being backgrounded and setInterval being
 *      throttled — all of which break a timer that decrements a counter.
 *
 *   3. A failed set-log is queued, not lost. Gym wifi drops mid-set. Sets go
 *      into an outbox in localStorage and are retried; the person keeps
 *      logging and the app reconciles when the signal comes back.
 */

'use strict';

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

const $ = (id) => document.getElementById(id);

/**
 * Build an element.
 *
 * Properties are assigned one at a time rather than with Object.assign, because
 * `dataset` and `style` are getter-only accessors: assigning an object to
 * either throws "Cannot set property dataset ... which has only a getter" in
 * strict mode, taking the whole render down with it. They get merged into the
 * existing object instead.
 */
const el = (tag, props = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value == null) continue;
    if (key === 'dataset' || (key === 'style' && typeof value === 'object')) {
      Object.assign(node[key], value);
    } else {
      node[key] = value;
    }
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
};

/** Numbers as people write them: 100, not 100.0; 102.5, not 102.50. */
const trim = (n) =>
  n == null || n === '' ? '' : String(Number(n).toFixed(2)).replace(/\.?0+$/, '');

const mmss = (seconds) => {
  const s = Math.max(0, Math.round(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
};

const relativeDay = (iso) => {
  if (!iso) return '';
  const then = new Date(iso);
  const days = Math.round((Date.now() - then) / 86400000);
  if (days <= 0) return 'today';
  if (days === 1) return 'yesterday';
  if (days < 7) return `${days} days ago`;
  if (days < 14) return 'last week';
  return then.toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
};

// ---------------------------------------------------------------------------
// Network
// ---------------------------------------------------------------------------

/**
 * Fetch JSON from the API.
 *
 * Same-origin, so the session cookie rides along without any token handling.
 * A 401 means the session expired: the app sends the person to the login page
 * rather than silently failing every request afterwards.
 */
async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    credentials: 'same-origin',
    headers: options.body ? { 'Content-Type': 'application/json' } : {},
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });

  if (response.status === 401) {
    window.location.href = `/login?next=${encodeURIComponent('/workout')}`;
    throw new Error('signed out');
  }
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      if (body && body.detail) detail = body.detail;
    } catch { /* a non-JSON error body is not worth a second failure */ }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
  view: 'home',
  session: null,      // the live session, as the server last described it
  routines: [],
  editing: null,      // routine being edited
  pickerTarget: null, // 'workout' | 'routine'
  pickerEquipment: null,
  measurements: null,
  history: [],
  records: [],
};

const OUTBOX_KEY = 'gym.outbox.v1';
const REST_KEY = 'gym.rest.v1';

// ---------------------------------------------------------------------------
// Toasts
// ---------------------------------------------------------------------------

// More than this on screen at once and the toasts are the app rather than a
// note about it — on a 390px phone, four stacked cards cover everything.
const MAX_TOASTS = 3;

function toast(title, detail = '', kind = '') {
  const tray = $('toasts');

  // Oldest out first, so the newest message is always the readable one.
  while (tray.children.length >= MAX_TOASTS) tray.firstElementChild.remove();

  const node = el('div', { className: `toast ${kind}` },
    el('strong', {}, title),
    detail ? el('span', {}, detail) : null);
  tray.append(node);
  setTimeout(() => {
    node.style.opacity = '0';
    setTimeout(() => node.remove(), 250);
  }, kind === 'pr' ? 4500 : 3000);
}

/**
 * Announce personal records.
 *
 * A first-ever record is worded differently from a beaten one. Calling the
 * first set of a new lift a "personal record" is technically true and reads as
 * a lie, which is the fastest way to make people stop trusting the feature.
 */
function announceRecords(breaks) {
  if (!breaks.length) return;

  // ONE toast per set, not one per record type. A first working set sets all
  // three records at once, and three near-identical cards stacked on a phone
  // screen is not three times the good news — it buries the app and reads as
  // noise. The detail line names which ones.
  const name = breaks[0].exercise_name;
  const beaten = breaks.filter((pr) => !pr.is_first);

  if (!beaten.length) {
    toast('First time logged', `${name} — your baseline is set`, 'pr');
  } else if (beaten.length === 1) {
    const pr = beaten[0];
    const by = pr.improvement != null ? ` (+${trim(pr.improvement)})` : '';
    toast(`🏆 ${pr.label}!`, `${name} — ${trim(pr.value)}${by}`, 'pr');
  } else {
    toast(
      `🏆 ${beaten.length} personal records!`,
      `${name} — ${beaten.map((pr) => pr.label.toLowerCase()).join(', ')}`,
      'pr',
    );
  }

  if (navigator.vibrate) navigator.vibrate([18, 60, 18]);
}

// ---------------------------------------------------------------------------
// Outbox: sets logged while offline
// ---------------------------------------------------------------------------

const outbox = {
  read() {
    try { return JSON.parse(localStorage.getItem(OUTBOX_KEY) || '[]'); }
    catch { return []; }
  },
  write(items) {
    try { localStorage.setItem(OUTBOX_KEY, JSON.stringify(items)); }
    catch { /* private mode, or full. The set is still in the UI. */ }
  },
  add(entry) {
    const items = this.read();
    items.push({ ...entry, queued_at: Date.now() });
    this.write(items);
    updateOfflineBanner();
  },
  get size() { return this.read().length; },

  /**
   * Retry everything queued, oldest first.
   *
   * Stops at the first failure and keeps the rest: sets belong to a session in
   * order, and draining past a failure would reorder them. A set the server
   * rejects outright (a 400 — the session was finished elsewhere, say) is
   * dropped rather than retried forever.
   */
  async flush() {
    let items = this.read();
    if (!items.length) return;
    while (items.length) {
      const entry = items[0];
      try {
        const result = await api(`/session/${entry.session_id}/sets`, {
          method: 'POST', body: entry.payload,
        });
        if (result.records && result.records.length) announceRecords(result.records);
      } catch (error) {
        if (!/^Request failed \(4/.test(error.message) && !navigator.onLine) break;
        // A 4xx will never succeed on retry; log it and move on.
        toast('A queued set was rejected', error.message, 'bad');
      }
      items = items.slice(1);
      this.write(items);
    }
    updateOfflineBanner();
    if (!items.length) await refreshSession();
  },
};

function updateOfflineBanner() {
  const queued = outbox.size;
  const banner = $('offline');
  banner.hidden = navigator.onLine && queued === 0;
  banner.textContent = !navigator.onLine
    ? `Offline — ${queued || 'no'} set${queued === 1 ? '' : 's'} waiting to sync`
    : `Syncing ${queued} set${queued === 1 ? '' : 's'}…`;
}

// ---------------------------------------------------------------------------
// Rest timer
// ---------------------------------------------------------------------------

/**
 * The timer stores an absolute end time in localStorage and renders the
 * difference from now. It therefore keeps correct time while the screen is
 * off, the tab is backgrounded, or the browser throttles timers — none of
 * which a decrementing counter survives.
 */
const rest = {
  handle: null,

  start(seconds, label) {
    const until = Date.now() + seconds * 1000;
    try { localStorage.setItem(REST_KEY, JSON.stringify({ until, label })); } catch {}
    this.tick();
    if (!this.handle) this.handle = setInterval(() => this.tick(), 250);
  },

  stop() {
    try { localStorage.removeItem(REST_KEY); } catch {}
    if (this.handle) { clearInterval(this.handle); this.handle = null; }
    $('rest').hidden = true;
  },

  extend(seconds) {
    const current = this.read();
    if (!current) return;
    current.until += seconds * 1000;
    try { localStorage.setItem(REST_KEY, JSON.stringify(current)); } catch {}
    this.tick();
  },

  read() {
    try { return JSON.parse(localStorage.getItem(REST_KEY) || 'null'); }
    catch { return null; }
  },

  tick() {
    const current = this.read();
    if (!current) { this.stop(); return; }

    const remaining = (current.until - Date.now()) / 1000;
    const box = $('rest');
    box.hidden = false;
    $('rest-label').textContent = current.label || 'Rest';

    if (remaining <= 0) {
      box.classList.add('over');
      $('rest-clock').textContent = `+${mmss(-remaining)}`;
      if (!current.notified) {
        current.notified = true;
        try { localStorage.setItem(REST_KEY, JSON.stringify(current)); } catch {}
        if (navigator.vibrate) navigator.vibrate([200, 90, 200]);
        toast('Rest over', current.label || '');
      }
      // Counts up past zero rather than vanishing: knowing you are 40 seconds
      // over is more useful than the timer disappearing the moment it ends.
      if (-remaining > 600) this.stop();
    } else {
      box.classList.remove('over');
      $('rest-clock').textContent = mmss(remaining);
    }
  },

  /**
   * Pick a stored timer back up after a reload.
   *
   * Never calls start(): that would write a NEW end time and reset a timer
   * that is already running. The stored `until` is the truth; this only
   * restarts the interval that renders it.
   */
  resume() {
    if (!this.read()) return;
    this.tick();
    if (!this.handle) this.handle = setInterval(() => this.tick(), 250);
  },
};

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------

const VIEWS = ['home', 'workout', 'picker', 'routine', 'records', 'measure', 'history'];

const TITLES = {
  home: 'Workout', workout: 'Logging', picker: 'Add exercise',
  routine: 'Routine', records: 'Personal records',
  measure: 'Measurements', history: 'Recent workouts',
};

function show(view, subtitle = '') {
  state.view = view;
  for (const name of VIEWS) $(`view-${name}`).hidden = name !== view;
  $('title').firstChild.textContent = TITLES[view] || 'Workout';
  $('subtitle').textContent = subtitle;
  $('back').hidden = view === 'home';
  window.scrollTo(0, 0);
  renderActions();
}

function renderActions() {
  const bar = $('actionbar');
  bar.textContent = '';

  if (state.view === 'home') {
    bar.append(el('button', {
      className: 'primary wide',
      onclick: () => startWorkout(null),
    }, state.session ? 'Resume workout' : 'Start empty workout'));
  } else if (state.view === 'workout') {
    bar.append(
      el('button', { className: 'ghost danger', onclick: discardWorkout }, 'Discard'),
      el('button', { className: 'primary grow', onclick: finishWorkout }, 'Finish workout'),
    );
  } else if (state.view === 'routine') {
    bar.append(
      el('button', { className: 'ghost', onclick: () => show('home') }, 'Cancel'),
      el('button', { className: 'primary grow', onclick: saveRoutine }, 'Save routine'),
    );
  } else {
    bar.append(el('button', { className: 'ghost wide', onclick: goBack }, 'Back'));
  }
}

function goBack() {
  if (state.view === 'picker' && state.pickerTarget === 'routine') show('routine');
  else if (state.view === 'picker') show('workout');
  else if (state.view === 'workout') show('home');
  else show('home');
}

// ---------------------------------------------------------------------------
// Home
// ---------------------------------------------------------------------------

async function loadHome() {
  const [{ routines }, { session }] = await Promise.all([
    api('/routines'),
    api('/session/active'),
  ]);
  state.routines = routines;
  state.session = session;
  renderRoutines();
  renderResume();
  renderActions();
  loadHeatmap();
}

function renderResume() {
  const card = $('resume');
  card.hidden = !state.session;
  if (!state.session) return;
  $('resume-name').textContent = state.session.name || 'Workout';
  $('resume-meta').textContent =
    `${state.session.total_sets} sets · ${mmss(state.session.duration_seconds)} elapsed`;
}

function renderRoutines() {
  const list = $('routine-list');
  list.textContent = '';

  if (!state.routines.length) {
    list.append(el('div', { className: 'empty' },
      el('span', { className: 'big' }, '📋'),
      el('div', {}, 'No routines yet.'),
      el('div', { className: 'faint' }, 'Create one, or start an empty workout and add lifts as you go.')));
    return;
  }

  for (const routine of state.routines) {
    const card = el('div', { className: 'card routine' },
      el('div', { className: 'row between' },
        el('div', { className: 'grow truncate' },
          el('h2', {}, routine.name),
          el('div', { className: 'muted truncate' },
            routine.exercises.map((e) => e.name.replace(/\s*\(.*\)$/, '')).join(' · ') || 'Empty')),
        el('button', {
          className: 'small',
          onclick: (event) => { event.stopPropagation(); editRoutine(routine.routine_id); },
        }, 'Edit')),
      el('div', { className: 'meta' },
        el('span', { className: 'chip' }, `${routine.exercise_count} exercises`),
        routine.total_sets ? el('span', { className: 'chip' }, `${routine.total_sets} sets`) : null,
        ...routine.muscle_groups.slice(0, 3).map((g) => el('span', { className: 'chip' }, g))));

    card.onclick = () => startWorkout(routine.routine_id);
    list.append(card);
  }
}

/** A GitHub-style contribution grid over the last 18 weeks. */
async function loadHeatmap() {
  try {
    const { sessions } = await api('/sessions?limit=100');
    const byDay = new Map();
    for (const s of sessions) {
      const key = (s.started_at || '').slice(0, 10);
      byDay.set(key, (byDay.get(key) || 0) + (s.total_sets || 0));
    }

    const grid = $('heatmap');
    grid.textContent = '';
    const today = new Date();
    // Start on the Sunday 17 weeks back so each grid column is one week.
    const start = new Date(today);
    start.setDate(start.getDate() - (17 * 7 + today.getDay()));

    let trained = 0;
    for (let i = 0; i < 18 * 7; i++) {
      const day = new Date(start);
      day.setDate(start.getDate() + i);
      const key = day.toISOString().slice(0, 10);
      const sets = byDay.get(key) || 0;
      if (sets) trained++;
      const level = sets === 0 ? 0 : sets < 8 ? 1 : sets < 16 ? 2 : sets < 25 ? 3 : 4;
      const cell = el('i', { title: sets ? `${key}: ${sets} sets` : key });
      cell.dataset.level = String(level);
      grid.append(cell);
    }
    $('heatmap-caption').textContent =
      `${trained} training day${trained === 1 ? '' : 's'} in the last 18 weeks`;
  } catch {
    $('heatmap-caption').textContent = 'Could not load training history.';
  }
}

// ---------------------------------------------------------------------------
// Workout
// ---------------------------------------------------------------------------

async function startWorkout(routineId) {
  try {
    const { session } = await api('/session/start', {
      method: 'POST',
      body: routineId ? { routine_id: routineId } : {},
    });
    state.session = session;
    renderWorkout();
    show('workout', session.name || '');
  } catch (error) {
    toast('Could not start', error.message, 'bad');
  }
}

async function refreshSession() {
  if (!state.session) return;
  try {
    const { session } = await api(`/session/${state.session.session_id}`);
    state.session = session;
    if (state.view === 'workout') renderWorkout();
  } catch { /* offline: keep showing what we have */ }
}

function renderWorkout() {
  const session = state.session;
  if (!session) { show('home'); return; }

  $('w-elapsed').textContent = `${mmss(session.duration_seconds)} elapsed`;
  $('w-totals').textContent = session.total_sets
    ? `${session.total_sets} sets · ${trim(session.total_volume_kg)} kg`
    : 'no sets yet';

  const list = $('exercise-list');
  list.textContent = '';

  if (!session.exercises.length) {
    list.append(el('div', { className: 'empty' },
      el('span', { className: 'big' }, '🏋️'),
      el('div', {}, 'Nothing added yet.'),
      el('button', { className: 'primary', style: 'margin-top:.8rem', onclick: openPicker },
        '+ Add an exercise')));
    return;
  }

  for (const exercise of session.exercises) list.append(renderExercise(exercise));
}

function renderExercise(exercise) {
  const done = exercise.is_complete;
  const target = exercise.target_sets
    ? `${exercise.working_sets_done}/${exercise.target_sets} sets` +
      (exercise.target_reps_label ? ` · ${exercise.target_reps_label} reps` : '')
    : `${exercise.working_sets_done} sets`;

  const table = el('table', { className: 'settable' },
    el('thead', {}, el('tr', {},
      el('th', {}, 'Set'),
      el('th', {}, 'Previous'),
      el('th', {}, exercise.is_bodyweight ? '+kg' : 'kg'),
      el('th', {}, 'Reps'),
      el('th', {}, ''))));

  const body = el('tbody');

  // Sets already logged.
  exercise.sets.forEach((set) => {
    body.append(el('tr', { className: 'logged' },
      el('td', { className: 'setno' },
        set.is_warmup ? el('span', { className: 'warmup-tag' }, 'W') : String(set.set_number)),
      el('td', { className: 'prev' }, ''),
      el('td', { className: 'num' }, trim(set.weight_kg) || '—'),
      el('td', { className: 'num' }, String(set.reps ?? '')),
      el('td', { className: 'tick' },
        el('button', {
          className: 'tickbtn on',
          title: 'Delete this set',
          onclick: () => deleteSet(set.log_id),
        }, '✓'))));
  });

  // One empty row to log the next set into, pre-filled from last time.
  const nextNumber = exercise.sets.filter((s) => !s.is_warmup).length + 1;
  const previous = exercise.previous[Math.min(nextNumber - 1, exercise.previous.length - 1)];
  const suggestedWeight = previous ? trim(previous.weight_kg) :
    (exercise.target_weight_kg ? trim(exercise.target_weight_kg) : '');

  const weightInput = el('input', {
    className: 'numfield', inputMode: 'decimal', placeholder: suggestedWeight || '0',
    value: '', 'aria-label': 'Weight in kilograms',
  });
  const repsInput = el('input', {
    className: 'numfield', inputMode: 'numeric', placeholder:
      previous ? String(previous.reps) : (exercise.target_reps_label || ''),
    value: '', 'aria-label': 'Reps',
  });

  const prevCell = el('td', { className: 'prev' });
  if (previous) {
    // Tapping Previous fills the row with it — the shortest path to "same as
    // last time", which is what most sets are.
    prevCell.append(el('button', {
      title: 'Use these numbers',
      onclick: () => {
        weightInput.value = trim(previous.weight_kg);
        repsInput.value = String(previous.reps ?? '');
        repsInput.focus();
      },
    }, previous.display));
  } else {
    prevCell.append(el('span', { className: 'faint' }, '—'));
  }

  const logButton = el('button', {
    className: 'tickbtn',
    'aria-label': 'Log this set',
    onclick: () => logSet(exercise, weightInput, repsInput, logButton),
  }, '✓');

  // Enter on the reps field logs the set: the keyboard stays up and the next
  // set is two taps away.
  repsInput.onkeydown = (event) => {
    if (event.key === 'Enter') { event.preventDefault(); logButton.click(); }
  };

  body.append(el('tr', {},
    el('td', { className: 'setno' }, String(nextNumber)),
    prevCell,
    el('td', { className: 'num' }, weightInput),
    el('td', { className: 'num' }, repsInput),
    el('td', { className: 'tick' }, logButton)));

  table.append(body);

  return el('section', { className: `card exercise${done ? ' done' : ''}` },
    el('header', {},
      el('div', { className: 'grow' },
        el('h3', {}, exercise.name),
        el('div', { className: 'faint' },
          target + (exercise.previous_date ? ` · last ${relativeDay(exercise.previous_date)}` : ''))),
      done ? el('span', { className: 'chip on' }, 'Done') : null),
    table,
    el('footer', {},
      el('button', {
        className: 'small grow',
        onclick: () => logSet(exercise, weightInput, repsInput, logButton, true),
      }, 'Log as warm-up'),
      el('button', {
        className: 'small grow',
        onclick: () => rest.start(exercise.rest_seconds || 120, exercise.name),
      }, `Rest ${mmss(exercise.rest_seconds || 120)}`)));
}

async function logSet(exercise, weightInput, repsInput, button, asWarmup = false) {
  const reps = parseInt(repsInput.value || repsInput.placeholder, 10);
  if (!Number.isFinite(reps) || reps <= 0) {
    repsInput.focus();
    toast('How many reps?', 'Enter a rep count for this set.', 'bad');
    return;
  }
  const rawWeight = weightInput.value || weightInput.placeholder;
  const weight = rawWeight === '' ? null : Number(rawWeight);

  const payload = {
    exercise_id: exercise.exercise_id,
    weight_kg: Number.isFinite(weight) ? weight : null,
    reps,
    is_warmup: asWarmup,
  };

  button.disabled = true;
  try {
    const result = await api(`/session/${state.session.session_id}/sets`, {
      method: 'POST', body: payload,
    });
    if (result.records && result.records.length) announceRecords(result.records);
    if (!asWarmup) rest.start(exercise.rest_seconds || 120, exercise.name);
    await refreshSession();
  } catch (error) {
    if (!navigator.onLine) {
      // Queue it and carry on. Losing a set because the wifi dropped is the
      // failure this whole outbox exists to prevent.
      outbox.add({ session_id: state.session.session_id, payload });
      toast('Saved on this phone', 'It will sync when you are back online.');
      if (!asWarmup) rest.start(exercise.rest_seconds || 120, exercise.name);
    } else {
      toast('Could not log that set', error.message, 'bad');
    }
  } finally {
    button.disabled = false;
  }
}

async function deleteSet(logId) {
  if (!confirm('Delete this set?')) return;
  try {
    await api(`/sets/${logId}`, { method: 'DELETE' });
    await refreshSession();
  } catch (error) {
    toast('Could not delete', error.message, 'bad');
  }
}

async function finishWorkout() {
  if (outbox.size) await outbox.flush();
  try {
    const { session } = await api(`/session/${state.session.session_id}/finish`, {
      method: 'POST', body: {},
    });
    rest.stop();
    state.session = null;
    toast('Workout saved',
      `${session.total_sets} sets · ${trim(session.total_volume_kg)} kg · ${mmss(session.duration_seconds)}`);
    await loadHome();
    show('home');
  } catch (error) {
    toast('Could not finish', error.message, 'bad');
  }
}

async function discardWorkout() {
  if (!confirm('Discard this workout and every set in it? This cannot be undone.')) return;
  try {
    await api(`/session/${state.session.session_id}`, { method: 'DELETE' });
    rest.stop();
    state.session = null;
    await loadHome();
    show('home');
  } catch (error) {
    toast('Could not discard', error.message, 'bad');
  }
}

// ---------------------------------------------------------------------------
// Exercise picker
// ---------------------------------------------------------------------------

function openPicker(target = 'workout') {
  state.pickerTarget = target;
  state.pickerEquipment = null;
  $('picker-q').value = '';
  show('picker');
  searchExercises();
  setTimeout(() => $('picker-q').focus(), 60);
}

let searchTimer = null;
function scheduleSearch() {
  clearTimeout(searchTimer);
  // Debounced: a request per keystroke would be a dozen in-flight fetches on a
  // phone keyboard, arriving out of order.
  searchTimer = setTimeout(searchExercises, 180);
}

async function searchExercises() {
  const query = $('picker-q').value.trim();
  const params = new URLSearchParams({ q: query, limit: '40' });
  if (state.pickerEquipment) params.set('equipment', state.pickerEquipment);

  try {
    const data = await api(`/catalog?${params}`);
    renderEquipmentChips(data.equipment);

    const results = $('picker-results');
    results.textContent = '';

    const rows = [
      ...data.mine.map((item) => ({ ...item, mine: true })),
      ...data.catalog.map((item) => ({ ...item, mine: false })),
    ];

    if (!rows.length) {
      results.append(el('div', { className: 'empty' },
        el('div', {}, 'Nothing matched.'),
        query ? el('button', {
          className: 'primary', style: 'margin-top:.8rem',
          onclick: () => pickExercise({ name: query }),
        }, `Create “${query}”`) : null));
      return;
    }

    for (const item of rows) {
      results.append(el('button', { className: 'result', onclick: () => pickExercise(item) },
        el('div', { className: 'grow' },
          el('div', { className: 'name' }, item.name),
          el('div', { className: 'faint' },
            [item.muscle_group, item.equipment_label].filter(Boolean).join(' · '))),
        item.mine && item.log_count
          ? el('span', { className: 'chip' }, `${item.log_count} sets`)
          : null));
    }
  } catch (error) {
    toast('Search failed', error.message, 'bad');
  }
}

function renderEquipmentChips(equipment) {
  const row = $('picker-equipment');
  if (row.dataset.built === '1') return;  // static list; build once
  row.textContent = '';
  row.append(el('button', {
    className: 'chip on', dataset: { key: '' },
    onclick: (e) => selectEquipment(null, e.target),
  }, 'All'));
  for (const item of equipment) {
    row.append(el('button', {
      className: 'chip', dataset: { key: item.key },
      onclick: (e) => selectEquipment(item.key, e.target),
    }, item.label));
  }
  // Marked built only once it has worked. Setting the flag first meant that a
  // failure part-way through left the row empty and every later call skipping
  // it — the row stayed broken for the life of the page.
  row.dataset.built = '1';
}

function selectEquipment(key, button) {
  state.pickerEquipment = key;
  for (const chip of $('picker-equipment').children) chip.classList.remove('on');
  button.classList.add('on');
  searchExercises();
}

async function pickExercise(item) {
  try {
    // Catalog entries have no id until the person uses one, so resolve first.
    const exercise = item.exercise_id
      ? item
      : await api('/exercises', { method: 'POST', body: { name: item.name } });

    if (state.pickerTarget === 'routine') {
      state.editing.exercises.push({
        exercise_id: exercise.exercise_id,
        name: exercise.name,
        muscle_group: exercise.muscle_group,
        target_sets: 3,
        target_reps_low: 8,
        target_reps_high: 12,
        rest_seconds: 120,
      });
      renderRoutineEditor();
      show('routine');
      return;
    }

    // Added to a live workout: log nothing, just make the block appear. A zero
    // set would be a lie; the exercise showing up with an empty row is the
    // intent.
    state.session.exercises.push({
      exercise_id: exercise.exercise_id,
      name: exercise.name,
      muscle_group: exercise.muscle_group,
      equipment: exercise.equipment,
      equipment_label: exercise.equipment_label,
      is_bodyweight: false,
      position: 1000 + state.session.exercises.length,
      target_sets: null, target_reps_label: '', target_weight_kg: null,
      rest_seconds: 120, sets: [], previous: [], previous_date: null,
      working_sets_done: 0, is_complete: false, added_ad_hoc: true,
    });
    renderWorkout();
    show('workout', state.session.name || '');

    // Fill in last time's numbers in the background.
    try {
      const data = await api(
        `/exercises/${exercise.exercise_id}/previous?exclude_session_id=${state.session.session_id}`);
      const block = state.session.exercises.find((e) => e.exercise_id === exercise.exercise_id);
      if (block) {
        block.previous = data.previous;
        block.previous_date = data.previous_date;
        renderWorkout();
      }
    } catch { /* the Previous column is a nicety, not a requirement */ }
  } catch (error) {
    toast('Could not add that', error.message, 'bad');
  }
}

// ---------------------------------------------------------------------------
// Routine editor
// ---------------------------------------------------------------------------

function newRoutine() {
  state.editing = { routine_id: null, name: '', notes: '', exercises: [] };
  renderRoutineEditor();
  show('routine', 'New');
}

async function editRoutine(routineId) {
  try {
    const routine = await api(`/routines/${routineId}`);
    state.editing = {
      routine_id: routine.routine_id,
      name: routine.name,
      notes: routine.notes || '',
      exercises: routine.exercises.map((e) => ({ ...e })),
    };
    renderRoutineEditor();
    show('routine', routine.name);
  } catch (error) {
    toast('Could not open', error.message, 'bad');
  }
}

function renderRoutineEditor() {
  const routine = state.editing;
  $('r-name').value = routine.name;
  $('r-notes').value = routine.notes || '';
  $('r-delete').hidden = !routine.routine_id;

  const list = $('r-exercises');
  list.textContent = '';

  routine.exercises.forEach((item, index) => {
    const setsInput = el('input', {
      inputMode: 'numeric', className: 'numfield', value: item.target_sets ?? '',
      oninput: (e) => { item.target_sets = e.target.value; },
    });
    const lowInput = el('input', {
      inputMode: 'numeric', className: 'numfield', value: item.target_reps_low ?? '',
      oninput: (e) => { item.target_reps_low = e.target.value; },
    });
    const highInput = el('input', {
      inputMode: 'numeric', className: 'numfield', value: item.target_reps_high ?? '',
      oninput: (e) => { item.target_reps_high = e.target.value; },
    });
    const restInput = el('input', {
      inputMode: 'numeric', className: 'numfield', value: item.rest_seconds ?? '',
      oninput: (e) => { item.rest_seconds = e.target.value; },
    });

    const move = (delta) => {
      const to = index + delta;
      if (to < 0 || to >= routine.exercises.length) return;
      const [moved] = routine.exercises.splice(index, 1);
      routine.exercises.splice(to, 0, moved);
      renderRoutineEditor();
    };

    list.append(el('div', { className: 'card' },
      el('div', { className: 'row between' },
        el('div', { className: 'grow truncate' },
          el('strong', {}, item.name),
          el('div', { className: 'faint' }, item.muscle_group || '')),
        el('button', { className: 'small', onclick: () => move(-1), 'aria-label': 'Move up' }, '↑'),
        el('button', { className: 'small', onclick: () => move(1), 'aria-label': 'Move down' }, '↓'),
        el('button', {
          className: 'small danger', 'aria-label': 'Remove',
          onclick: () => { routine.exercises.splice(index, 1); renderRoutineEditor(); },
        }, '✕')),
      el('div', { className: 'row', style: 'margin-top:.5rem;gap:.4rem' },
        el('div', { className: 'grow' }, el('label', {}, 'Sets'), setsInput),
        el('div', { className: 'grow' }, el('label', {}, 'Reps'), lowInput),
        el('div', { className: 'grow' }, el('label', {}, 'to'), highInput),
        el('div', { className: 'grow' }, el('label', {}, 'Rest s'), restInput))));
  });

  if (!routine.exercises.length) {
    list.append(el('div', { className: 'empty' }, 'No exercises yet.'));
  }
}

async function saveRoutine() {
  const routine = state.editing;
  // Already kept current by the oninput handlers; re-read once more so a value
  // set by autofill or a paste that fired no input event is not missed.
  routine.name = $('r-name').value;
  routine.notes = $('r-notes').value;

  const payload = {
    name: routine.name,
    notes: routine.notes,
    exercises: routine.exercises.map((item) => ({
      exercise_id: item.exercise_id,
      target_sets: item.target_sets || null,
      target_reps_low: item.target_reps_low || null,
      target_reps_high: item.target_reps_high || null,
      rest_seconds: item.rest_seconds || null,
    })),
  };

  try {
    if (routine.routine_id) {
      await api(`/routines/${routine.routine_id}`, { method: 'PUT', body: payload });
    } else {
      await api('/routines', { method: 'POST', body: payload });
    }
    toast('Routine saved');
    await loadHome();
    show('home');
  } catch (error) {
    toast('Could not save', error.message, 'bad');
  }
}

async function deleteRoutine() {
  if (!state.editing.routine_id) return;
  if (!confirm('Delete this routine? Workouts already logged from it are kept.')) return;
  try {
    await api(`/routines/${state.editing.routine_id}`, { method: 'DELETE' });
    await loadHome();
    show('home');
  } catch (error) {
    toast('Could not delete', error.message, 'bad');
  }
}

// ---------------------------------------------------------------------------
// Records
// ---------------------------------------------------------------------------

async function loadRecords() {
  show('records');
  const list = $('records-list');
  list.textContent = '';
  list.append(el('div', { className: 'empty' }, el('span', { className: 'spinner' })));

  try {
    const { records } = await api('/records?limit=100');
    list.textContent = '';
    if (!records.length) {
      list.append(el('div', { className: 'empty' },
        el('span', { className: 'big' }, '🏆'),
        el('div', {}, 'No records yet.'),
        el('div', { className: 'faint' }, 'Log a working set and your first one appears here.')));
      return;
    }

    // Grouped by lift: three record types per exercise reads as noise in a flat
    // list and as a profile when grouped.
    const byExercise = new Map();
    for (const record of records) {
      if (!byExercise.has(record.exercise_name)) byExercise.set(record.exercise_name, []);
      byExercise.get(record.exercise_name).push(record);
    }

    for (const [name, items] of byExercise) {
      list.append(el('div', { className: 'card' },
        el('h2', {}, name),
        ...items.map((record) => el('div', { className: 'row between', style: 'margin-top:.35rem' },
          el('span', { className: 'muted' }, record.label),
          el('strong', {}, record.display)))
      , el('div', { className: 'faint', style: 'margin-top:.4rem' },
          `set ${relativeDay(items[0].achieved_at)}`)));
    }
  } catch (error) {
    list.textContent = '';
    list.append(el('div', { className: 'empty' }, error.message));
  }
}

async function rebuildRecords() {
  try {
    const { rebuilt } = await api('/records/rebuild', { method: 'POST' });
    toast('Records recalculated', `${rebuilt} across all your lifts`);
    loadRecords();
  } catch (error) {
    toast('Could not recalculate', error.message, 'bad');
  }
}

// ---------------------------------------------------------------------------
// Measurements
// ---------------------------------------------------------------------------

async function loadMeasurements() {
  show('measure');
  try {
    const data = await api('/measurements');
    state.measurements = data;
    renderMeasurements();
  } catch (error) {
    toast('Could not load', error.message, 'bad');
  }
}

function renderMeasurements() {
  const data = state.measurements;

  const tiles = $('measure-tiles');
  tiles.textContent = '';
  for (const trend of data.trends) {
    const change = trend.change_cm;
    // Direction only — the app shows which way a number moved and does not
    // decide whether that is good.
    const cls = change == null || change === 0 ? 'flat' : change > 0 ? 'up' : 'down';
    const arrow = change == null || change === 0 ? '' : change > 0 ? '▲' : '▼';
    tiles.append(el('div', { className: 'tile' },
      el('div', { className: 'lbl' }, trend.label),
      el('div', { className: 'val' }, `${trim(trend.latest_cm)} cm`),
      el('div', { className: `delta ${cls}` },
        change == null ? 'first reading' : `${arrow} ${trim(Math.abs(change))} cm`)));
  }
  if (!data.trends.length) {
    tiles.append(el('div', { className: 'empty' }, 'Nothing measured in the last 90 days.'));
  }

  const form = $('measure-form');
  if (form.dataset.built !== '1') {
    for (const site of data.sites) {
      form.append(el('div', { className: 'row' },
        el('label', { className: 'grow', htmlFor: `m-${site.key}` }, site.label),
        el('input', {
          id: `m-${site.key}`, inputMode: 'decimal', className: 'numfield',
          style: 'max-width:6.5rem', placeholder: '—', dataset: { site: site.key },
        })));
    }
    form.dataset.built = '1';
  }

  renderMeasureChart();
}

/** Inline SVG, matching charts.py's no-chart-library approach. */
function renderMeasureChart() {
  const data = state.measurements;
  const sites = Object.keys(data.series).filter((s) => data.series[s].length > 1);
  const card = $('measure-chart-card');
  card.hidden = sites.length === 0;
  if (!sites.length) return;

  const box = $('measure-chart');
  box.textContent = '';

  const W = 320, H = 150, PAD = 26;
  for (const site of sites.slice(0, 4)) {
    const points = data.series[site];
    const values = points.map((p) => p.value_cm);
    const times = points.map((p) => new Date(p.at).getTime());
    const [lo, hi] = [Math.min(...values), Math.max(...values)];
    const [t0, t1] = [Math.min(...times), Math.max(...times)];
    const span = hi - lo || 1;
    const tspan = t1 - t0 || 1;

    const coords = points.map((p) => {
      const x = PAD + ((new Date(p.at).getTime() - t0) / tspan) * (W - PAD * 2);
      const y = H - PAD - ((p.value_cm - lo) / span) * (H - PAD * 2);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    });

    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    svg.setAttribute('width', '100%');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label',
      `${site} from ${trim(values[0])} to ${trim(values[values.length - 1])} centimetres`);

    const line = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
    line.setAttribute('points', coords.join(' '));
    line.setAttribute('fill', 'none');
    line.setAttribute('stroke', 'var(--accent)');
    line.setAttribute('stroke-width', '2');
    line.setAttribute('stroke-linejoin', 'round');
    svg.append(line);

    for (const [label, y] of [[trim(hi), PAD], [trim(lo), H - PAD]]) {
      const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      text.setAttribute('x', '2');
      text.setAttribute('y', String(y + 4));
      text.setAttribute('font-size', '10');
      text.setAttribute('fill', 'currentColor');
      text.setAttribute('opacity', '.55');
      text.textContent = label;
      svg.append(text);
    }

    box.append(el('div', { className: 'faint', style: 'margin-top:.6rem' },
      (data.sites.find((s) => s.key === site) || {}).label || site));
    box.append(svg);
  }
}

async function saveMeasurements() {
  const values = {};
  for (const input of $('measure-form').querySelectorAll('input[data-site]')) {
    if (input.value.trim()) values[input.dataset.site] = input.value.trim();
  }
  if (!Object.keys(values).length) {
    toast('Nothing to save', 'Fill in at least one measurement.', 'bad');
    return;
  }
  try {
    await api('/measurements', { method: 'POST', body: { values } });
    for (const input of $('measure-form').querySelectorAll('input[data-site]')) input.value = '';
    toast('Measurements saved');
    await loadMeasurements();
  } catch (error) {
    toast('Could not save', error.message, 'bad');
  }
}

// ---------------------------------------------------------------------------
// History
// ---------------------------------------------------------------------------

async function loadHistory() {
  show('history');
  const list = $('history-list');
  list.textContent = '';
  try {
    const { sessions } = await api('/sessions?limit=50');
    if (!sessions.length) {
      list.append(el('div', { className: 'empty' }, 'No finished workouts yet.'));
      return;
    }
    for (const session of sessions) {
      list.append(el('div', { className: 'card' },
        el('div', { className: 'row between' },
          el('div', { className: 'grow' },
            el('strong', {}, session.name || 'Workout'),
            el('div', { className: 'faint' },
              new Date(session.started_at).toLocaleDateString(undefined,
                { weekday: 'short', day: 'numeric', month: 'short' }))),
          el('div', { style: 'text-align:right' },
            el('div', {}, `${session.total_sets} sets`),
            el('div', { className: 'faint' }, `${trim(session.total_volume_kg)} kg`)))));
    }
  } catch (error) {
    list.append(el('div', { className: 'empty' }, error.message));
  }
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

$('back').onclick = goBack;
$('refresh').onclick = () => (state.view === 'workout' ? refreshSession() : loadHome());
$('new-routine').onclick = newRoutine;
$('resume-go').onclick = () => { renderWorkout(); show('workout', state.session?.name || ''); };
$('add-exercise').onclick = () => openPicker('workout');
$('picker-q').oninput = scheduleSearch;
$('r-add').onclick = () => openPicker('routine');

// The name and notes fields write straight into state on every keystroke.
//
// Without this, adding an exercise silently wipes the name: picking one
// re-renders the editor, and the render sets the input's value from
// state.editing.name — which stays empty if typing only ever changed the DOM.
$('r-name').oninput = (event) => {
  if (state.editing) state.editing.name = event.target.value;
};
$('r-notes').oninput = (event) => {
  if (state.editing) state.editing.notes = event.target.value;
};
$('r-delete').onclick = deleteRoutine;
$('go-records').onclick = loadRecords;
$('records-rebuild').onclick = rebuildRecords;
$('go-measure').onclick = loadMeasurements;
$('measure-save').onclick = saveMeasurements;
$('go-history').onclick = loadHistory;
$('rest-skip').onclick = () => rest.stop();
$('rest-add').onclick = () => rest.extend(30);

window.addEventListener('online', () => { updateOfflineBanner(); outbox.flush(); });
window.addEventListener('offline', updateOfflineBanner);

// Coming back to a backgrounded tab: re-sync, because the timer kept running
// and sets may have been logged on another device.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    rest.tick();
    if (state.view === 'workout') refreshSession();
  }
});

// Keeps the elapsed-time readout honest without re-rendering the whole screen.
setInterval(() => {
  if (state.view === 'workout' && state.session) {
    state.session.duration_seconds += 1;
    $('w-elapsed').textContent = `${mmss(state.session.duration_seconds)} elapsed`;
  }
}, 1000);

async function boot() {
  updateOfflineBanner();
  rest.resume();

  try {
    await loadHome();
    if (outbox.size && navigator.onLine) outbox.flush();
    // A live session is where the person left off, so go straight there.
    if (state.session) { renderWorkout(); show('workout', state.session.name || ''); }
    else show('home');
  } catch (error) {
    toast('Could not load', error.message, 'bad');
    show('home');
  }

  if ('serviceWorker' in navigator) {
    // Registered after boot so it never delays first paint.
    navigator.serviceWorker.register('/sw.js').catch(() => {
      /* No service worker means no offline shell. Everything else still works. */
    });
  }
}

boot();
