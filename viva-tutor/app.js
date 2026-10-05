// Viva tutor UI. Vanilla JS, no build step, works from file:// or any static
// server. Content lives in content.js / content-qa.js; code and line numbers in
// the generated snippets.js.

(function () {
  'use strict';

  // -------------------------------------------------------------------------
  // Helpers
  // -------------------------------------------------------------------------

  const $ = (sel, root = document) => root.querySelector(sel);
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function refLink(name) {
    const r = window.REFS[name];
    if (!r) return `<span class="badge b-no">missing ref ${esc(name)}</span>`;
    return `<a class="ref" href="../${encodeURI(r.file)}" target="_blank" rel="noopener" title="${esc(r.text)}">${esc(r.file)}:${r.line}</a>`;
  }

  // Resolve [[ref]] placeholders inside trusted content HTML.
  const R = (html) => String(html == null ? '' : html).replace(/\[\[([a-z0-9_]+)\]\]/g, (_, n) => refLink(n));

  function showSnip(id, highlight = []) {
    const s = window.SNIPPETS[id];
    if (!s) return `<div class="badbox">Snippet ${esc(id)} not found — run build_snippets.py</div>`;
    const marks = new Set(highlight.map((h) => (typeof h === 'number' ? h : (window.REFS[h] || {}).line)));
    const body = s.code.split('\n').map((line, i) => {
      const n = s.start + i;
      const text = `<span class="ln">${n}</span>${esc(line)}`;
      return marks.has(n) ? `<span class="hl">${text}</span>` : text;
    }).join('\n');
    return `<div class="snip-head"><a href="../${encodeURI(s.file)}" target="_blank" rel="noopener">${esc(s.file)}</a> lines ${s.start}–${s.end} (extracted from the real file)</div><pre><code>${body}</code></pre>`;
  }

  const badge = (text, cls) => `<span class="badge ${cls}">${esc(text)}</span>`;
  const typeBadge = (t) => badge(t.toUpperCase(), 'b-' + t);
  const impBadge = (i) => badge(i, 'b-' + i.toLowerCase());
  const diffBadge = (d) => badge(d, { Easy: 'b-easy', Medium: 'b-medium-d', Hard: 'b-hard' }[d] || '');
  const prof = (q, a) => `<div class="prof"><div class="q">${R(q)}</div><div class="a">${R(a)}</div></div>`;

  const store = {
    get(key, fallback) { try { const v = localStorage.getItem(key); return v ? JSON.parse(v) : fallback; } catch (e) { return fallback; } },
    set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* private mode */ } },
  };

  // -------------------------------------------------------------------------
  // Navigation
  // -------------------------------------------------------------------------

  const PAGES = [
    ['Start', [
      ['start', 'Start Here Before Your Viva', 'GO'],
      ['crash', '15-Minute Crash Course'],
      ['cheatsheet', 'Viva Cheat Sheet'],
    ]],
    ['The system', [
      ['architecture', 'Architecture Explorer'],
      ['docker', 'Docker / Infrastructure'],
      ['persistence', 'Persistence & Data Lifetime'],
      ['data', 'Data Structure Explorer'],
      ['code', 'Code → Result'],
      ['files', 'Explain This File'],
    ]],
    ['Metrics', [
      ['metrics', 'Metric Explorer (4 types)'],
      ['percentiles', 'Percentile Explainer'],
      ['promql', 'PromQL Explainer'],
      ['metric-trace', 'Follow One Metric'],
    ]],
    ['Logs', [
      ['logs', 'Log Explorer'],
      ['log-trace', 'Follow One Log'],
      ['vs', 'Metrics vs Logs'],
      ['security', 'Security / Safe Logging'],
    ]],
    ['Experiments', [
      ['fault', 'E1 Fault Injection'],
      ['cardinality', 'E2 Cardinality Explosion'],
    ]],
    ['Defend it', [
      ['requirements', 'What the Assignment Expects'],
      ['xref', 'Report → Code'],
      ['traps', 'Potential Viva Traps', '!'],
      ['professor', 'Professor Mode'],
      ['defend', 'Can I Defend This?'],
    ]],
  ];

  function renderNav(current) {
    $('#nav').innerHTML = PAGES.map(([group, items]) =>
      `<div class="nav-group">${esc(group)}</div>` + items.map(([id, title, tag]) =>
        `<a href="#${id}" class="${id === current ? 'active' : ''}">${esc(title)}${tag ? `<span class="tag">${esc(tag)}</span>` : ''}</a>`).join('')
    ).join('');
  }

  // "I understand this" never just records a click: it asks you to prove it.
  function understandButton(defendId) {
    return `<div class="card" data-defend="${defendId}"><div class="row"><button class="primary" data-act="understand">I understand this</button><span class="muted small">— prove it: you will be asked to explain it in your own words.</span></div><div class="defend-slot"></div></div>`;
  }

  // -------------------------------------------------------------------------
  // Pages
  // -------------------------------------------------------------------------

  const pages = {};

  pages.start = () => `
    <div class="hero">
      <h1>Start Here Before Your Viva</h1>
      <p>This tutor explains <b>your</b> implementation. Every file:line badge was located in the real source by <code>build_snippets.py</code>; hover one to see that line, or click it to open the file.</p>
    </div>
    <h2>If you have 30 minutes</h2>
    <table>
      <tr><th>#</th><th>Do this</th><th>Time</th><th>You can then say</th></tr>
      <tr><td>1</td><td><a href="#cheatsheet">Cheat sheet</a>: read "Architecture in 30 seconds" aloud twice</td><td>3 min</td><td>The whole system in one breath</td></tr>
      <tr><td>2</td><td><a href="#metric-trace">Follow One Metric</a>, all 6 steps</td><td>5 min</td><td>Why Grafana says 33 when the counter says 16</td></tr>
      <tr><td>3</td><td><a href="#log-trace">Follow One Log</a>, all 6 steps</td><td>5 min</td><td>Python → Kibana in under 60 seconds</td></tr>
      <tr><td>4</td><td><a href="#fault">E1 Fault</a>: the slider and "why 687.5"</td><td>7 min</td><td>Why p95 exploded and p50 didn't</td></tr>
      <tr><td>5</td><td><a href="#cardinality">E2 Cardinality</a>: press the buttons</td><td>5 min</td><td>Why request ids live in logs</td></tr>
      <tr><td>6</td><td><a href="#traps">Viva Traps</a>: skim the HIGH ones</td><td>5 min</td><td>Honest answers to awkward questions</td></tr>
    </table>
    <h2>If you have 60 minutes, also</h2>
    <p><a href="#metrics">Metric Explorer</a> (the six deep metrics) → <a href="#percentiles">Percentile Explainer</a> → <a href="#architecture">Architecture</a> ("if it dies") → <a href="#professor">Professor Mode</a> → <a href="#defend">Can I Defend This?</a></p>
    <h2>Numbers worth memorising</h2>
    <div class="grid three">
      ${[
        ['19 metrics', '10 counters, 4 gauges, 3 histograms, 2 summaries'],
        ['15 s / 15 d', 'scrape interval / Prometheus retention'],
        ['10 MB × 3 / 7 d', 'Docker log rotation / Elasticsearch ILM delete'],
        ['33 = 17 + 16', 'increase() across a counter reset'],
        ['2.5 → 3.3 ms', 'p50 normal → fault'],
        ['4.8 → 687.5 ms', 'p95 normal → fault (back to 5.6)'],
        ['118 = 118', 'faults injected = fault warning logs, 0 errors'],
        ['595 / 5 = 119', 'server-side requests ÷ 5 (client saw 406)'],
        ['1→10→25→50→100', 'series with request_id label; safe counter stays 1'],
        ['8.64 M/day', '100 rps × 86,400 s'],
        ['0.5 + 0.75 × 0.25', '= 0.6875 s, the p95 interpolation'],
        ['259fd665642941e0', 'the traced request id (GET /login, 191.5 ms)'],
      ].map(([a, b]) => `<div class="stat"><div class="big" style="font-size:20px">${a}</div><div class="muted small">${b}</div></div>`).join('')}
    </div>
    <h2>The one principle</h2>
    <div class="goodbox">Metrics are cheap aggregates with a few bounded labels ("how many, how fast"). Logs are per-event records with high-cardinality detail like request ids ("what happened to this one"). Almost every design choice in this project follows from that.</div>
    <h2>Discrepancies found while building this</h2>
    <p>The tutor treats the code as the source of truth. Read <a href="#traps">Potential Viva Traps</a>. The biggest: the report cites <code>docs/REPORT.md</code>, which was deleted; live-workout sets are not counted; 406 vs 118 is client vs server side.</p>`;

  pages.crash = () => {
    const done = store.get('viva-checklist', {});
    return `<h1>15-Minute Viva Crash Course</h1>
      <p class="muted">Prioritised. Each item says what you <b>must</b> be able to explain and a safe sentence to say.</p>
      ${window.CRASH_COURSE.map((c) => `
        <div class="card">
          <div class="row" style="justify-content:space-between"><h3 style="margin:0">${c.n}. <a href="#${c.page}">${esc(c.topic)}</a></h3><div>${impBadge(c.imp)} <span class="badge">${c.mins} min</span></div></div>
          <p><b>You MUST be able to explain:</b> ${esc(c.must)}</p>
          <div class="goodbox"><b>Safe to say:</b> ${esc(c.safe)}</div>
        </div>`).join('')}
      <h2>Final checklist</h2>
      <div class="card" id="checklist">
        ${window.CHECKLIST.map((item, i) => `<label class="check"><input type="checkbox" data-check="${i}" ${done[i] ? 'checked' : ''}> <span>${esc(item)}</span></label>`).join('')}
        <p class="muted small">Saved in this browser only. Tick honestly — then check yourself in <a href="#defend">Can I Defend This?</a></p>
      </div>`;
  };

  pages.cheatsheet = () => `<h1>Viva Cheat Sheet</h1>
    <p class="muted">Written to be said out loud. Also in <code>VIVA_GUIDE.md</code> at the repository root.</p>
    ${window.CHEAT.map(([h, t]) => `<div class="card"><h3 style="margin-top:0">${esc(h)}</h3><p>${esc(t)}</p></div>`).join('')}`;

  // ---- Architecture -------------------------------------------------------

  pages.architecture = () => {
    const color = { app: 'var(--app)', metrics: 'var(--metrics)', logs: 'var(--logs)', external: 'var(--ext)', experiment: 'var(--exp)' };
    const W = 150, H = 50;
    const byId = Object.fromEntries(window.COMPONENTS.map((c) => [c.id, c]));
    const center = (c) => [c.x + W / 2, c.y + H / 2];
    const edges = window.EDGES.map((e) => {
      const a = byId[e.from], b = byId[e.to];
      let [x1, y1] = center(a), [x2, y2] = center(b);
      // Clip to the box edge so the arrowhead is visible.
      const clip = (x, y, ox, oy) => {
        const dx = ox - x, dy = oy - y;
        const t = Math.min(Math.abs((W / 2) / (dx || 1e-9)), Math.abs((H / 2) / (dy || 1e-9)));
        return [x + dx * t, y + dy * t];
      };
      const [sx, sy] = clip(x1, y1, x2, y2), [ex, ey] = clip(x2, y2, x1, y1);
      const col = { app: 'var(--app)', metrics: 'var(--metrics)', logs: 'var(--logs)' }[e.kind];
      const flat = Math.abs(ey - sy) < 5;
      const lx = flat ? (sx + ex) / 2 : (sx + ex) / 2 + 7, ly = flat ? sy - 7 : (sy + ey) / 2 + 4;
      return `<line class="edge" x1="${sx}" y1="${sy}" x2="${ex}" y2="${ey}" stroke="${col}" marker-end="url(#arr-${e.kind})"/>
        <text class="edge-label" x="${lx}" y="${ly}" text-anchor="${flat ? 'middle' : 'start'}">${esc(e.label)}</text>`;
    }).join('');
    const nodes = window.COMPONENTS.map((c) => `
      <g class="node" data-comp="${c.id}"><rect x="${c.x}" y="${c.y}" width="${W}" height="${H}" stroke="${color[c.group]}"/>
      <text x="${c.x + 10}" y="${c.y + 21}">${esc(c.name)}</text><text class="sub" x="${c.x + 10}" y="${c.y + 38}">${esc(c.group)}</text></g>`).join('');
    const markers = ['app', 'metrics', 'logs'].map((k) => `<marker id="arr-${k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="${color[k]}"/></marker>`).join('');
    return `<h1>Architecture Explorer</h1>
      <p>Only components present in <code>docker-compose.yml</code> (plus Groq, the one external API). <span style="color:var(--metrics)">Blue = metrics (pulled)</span>, <span style="color:var(--logs)">orange = logs (pushed)</span>, <span style="color:var(--app)">green = app data</span>, <span style="color:var(--exp)">purple = experiment profile</span>. <b>Click a box.</b></p>
      <div class="arch-wrap"><svg viewBox="0 0 980 475" role="img" aria-label="Architecture diagram"><defs>${markers}</defs>${edges}${nodes}</svg></div>
      <div id="comp-panel"></div>
      <p class="muted small">The report's own diagram is <a href="../docs/architecture.png" target="_blank">docs/architecture.png</a>.</p>`;
  };

  function compPanel(id) {
    const c = window.COMPONENTS.find((x) => x.id === id);
    document.querySelectorAll('.node').forEach((n) => n.classList.toggle('sel', n.dataset.comp === id));
    $('#comp-panel').innerHTML = `<div class="card">
      <h2 style="margin-top:4px">${esc(c.name)}</h2>
      <div class="kv">
        <div>What?</div><div>${R(c.what)}</div>
        <div>Where?</div><div><ul style="margin:0;padding-left:18px">${c.where.map((w) => `<li>${R(w)}</li>`).join('')}</ul></div>
        <div>Why?</div><div>${R(c.why)}</div>
        <div>Talks to</div><div><ul style="margin:0;padding-left:18px">${c.talks.map((w) => `<li>${R(w)}</li>`).join('')}</ul></div>
        <div>Stores</div><div>${R(c.stores)}</div>
        <div>If it dies</div><div>${R(c.dies)}</div>
      </div>
      <h3>Viva questions</h3>${c.viva.map((v) => prof(v.q, v.a)).join('')}
    </div>`;
  }

  // ---- Docker and persistence --------------------------------------------

  pages.docker = () => `<h1>Docker / Infrastructure</h1>
    <h2>Five words, in this project</h2>
    <div class="grid two">
      ${[
        ['Image', 'A read-only template. <code>app</code> and <code>cardinality-demo</code> are built from the repo\'s Dockerfile (python:3.11-slim); the rest are pulled, e.g. <code>prom/prometheus:v2.54.1</code>.'],
        ['Container', 'A running instance of an image, e.g. <code>gym-tracker</code>. Recreating it gives a new id — and a new Docker log file.'],
        ['Volume', 'Named storage that outlives containers: postgres-data, prometheus-data, grafana-data, elasticsearch-data, filebeat-data.'],
        ['Network', 'One bridge, <code>gym-net</code>, subnet pinned to 172.28.77.0/24 so the gateway 172.28.77.1 (the host, for node-exporter) is known. Containers reach each other by service name (<code>app:8000</code>, <code>elasticsearch:9200</code>).'],
        ['Service', 'An entry under <code>services:</code> in docker-compose.yml: image or build, ports, env, volumes, dependencies.'],
        ['Profile', '<code>cardinality-demo</code> has <code>profiles: ["experiment"]</code>, so plain <code>docker compose up</code> does not start it.'],
      ].map(([h, t]) => `<div class="card tight"><b>${h}</b><p class="small">${t}</p></div>`).join('')}
    </div>
    <h2>Services (from docker-compose.yml)</h2>
    <div class="table-wrap"><table><tr><th>Service</th><th>Image / build</th><th>Ports</th><th>Volumes</th><th>Env / flags</th><th>Depends on</th><th>Health</th><th>Persistent?</th></tr>
      ${window.SERVICES.map((s) => `<tr><td><b>${s.name}</b></td><td class="small">${esc(s.image)}</td><td class="mono">${esc(s.ports)}</td><td class="small">${esc(s.volumes)}</td><td class="small">${esc(s.env)}</td><td class="small">${esc(s.deps)}</td><td class="small">${esc(s.health)}</td><td>${esc(s.persist)}</td></tr>`).join('')}
    </table></div>
    <h2>Commands</h2>
    <table>
      <tr><th>Command</th><th>What happens</th></tr>
      <tr><td><code>docker compose up -d --build</code></td><td>Build the app image, create the network and volumes, start 8 services.</td></tr>
      <tr><td><code>docker compose restart app</code></td><td>Same container restarts: in-memory metrics reset; log file kept.</td></tr>
      <tr><td><code>docker compose up -d --force-recreate app</code></td><td>New container (how E1 switches the fault on/off): metrics reset, new log file, env re-read.</td></tr>
      <tr><td><code>docker compose down</code> / <code>./scripts/stack.sh down</code></td><td>Containers and network removed. <b>Volumes kept</b> — workouts, metrics history, logs survive.</td></tr>
      <tr><td><code>docker compose down -v</code> / <code>./scripts/stack.sh destroy</code></td><td><b>Volumes deleted</b> — all data gone. stack.sh makes you type "destroy".</td></tr>
    </table>
    <h2>The app service</h2>${showSnip('dc_app', ['dc_logging', 'dc_label'])}
    <h2>Prometheus service</h2>${showSnip('dc_prometheus', ['dc_retention'])}
    <h2>Filebeat service</h2>${showSnip('dc_filebeat', ['dc_filebeat_registry'])}
    <h2>Volumes and network</h2>${showSnip('dc_volumes')}
    <h2>One worker</h2>${showSnip('df_cmd')}
    ${prof('Why does node-exporter use network_mode: host?', 'So its network counters are the host\'s real NICs, not a container veth pair. Consequence: it is not on gym-net, so Prometheus scrapes it via the bridge gateway 172.28.77.1:9100.')}
    ${prof('Why does Filebeat run as root?', 'It reads the root-owned Docker socket and /var/lib/docker/containers. --strict.perms=false because a bind-mounted config is usually group/world-writable.')}`;

  pages.persistence = () => `<h1>Persistence & Data Lifetime</h1>
    <h2>Where each kind of data lives</h2>
    <div class="card"><b style="color:var(--app)">Application data</b>
      <div class="pipeline"><span class="p">POST /save</span><span class="arrow">→</span><span class="p">pipeline.commit_draft</span><span class="arrow">→</span><span class="p lit">Postgres · volume postgres-data</span><span class="arrow">→</span><span class="p">until you delete it / down -v</span></div></div>
    <div class="card"><b style="color:var(--metrics)">Metrics</b>
      <div class="pipeline"><span class="p">process memory (reset on restart)</span><span class="arrow">→</span><span class="p">/metrics text</span><span class="arrow">→</span><span class="p lit">Prometheus TSDB · volume prometheus-data</span><span class="arrow">→</span><span class="p">deleted after 15 days</span></div>
      <p class="small">Retention flag: ${refLink('dc_retention')}. A missed scrape is a permanent gap.</p></div>
    <div class="card"><b style="color:var(--logs)">Logs</b>
      <div class="pipeline"><span class="p">stdout</span><span class="arrow">→</span><span class="p lit">Docker file /var/lib/docker/containers/&lt;id&gt;/ · 10 MB × 3</span><span class="arrow">→</span><span class="p">Filebeat offset · volume filebeat-data</span><span class="arrow">→</span><span class="p lit">Elasticsearch · volume elasticsearch-data</span><span class="arrow">→</span><span class="p">ILM: rollover 1 d / 1 GB → delete 7 d later</span></div>
      <p class="small">${refLink('dc_logging')} · ${refLink('dc_filebeat_registry')} · ${refLink('ilm_rollover')} · ${refLink('ilm_delete')}</p></div>
    <h2>What survives what</h2>
    <div class="table-wrap"><table><tr><th>Data</th>${window.LIFETIME.columns.map((c) => `<th>${esc(c)}</th>`).join('')}</tr>
      ${window.LIFETIME.rows.map((r) => `<tr>${r.map((v, i) => `<td class="${i ? 'mono small' : ''}" style="${/DELETED|GONE|reset|lost/.test(v) && i ? 'color:var(--bad)' : (i ? 'color:var(--good)' : '')}">${esc(v)}</td>`).join('')}</tr>`).join('')}
    </table></div>
    <h2>The ILM policy</h2>${showSnip('ilm')}
    ${prof('Is the 7 days counted from when a log was written?', 'From when its index was rolled over (delete phase min_age 7d is measured from rollover). With daily rollover, a log is kept roughly 7–8 days.')}
    ${prof('Where can a log be lost?', 'If Filebeat (or Elasticsearch) is down long enough for Docker to rotate past 3 × 10 MB, or if the container is removed before Filebeat has read its file.')}`;

  // ---- Data structures / code -> result / files ---------------------------

  pages.data = () => `<h1>Data Structure Explorer</h1>
    <p>The objects and formats your implementation actually uses, in the order data flows through them.</p>
    <h2>Interactive: what does <code>.observe()</code> change?</h2>
    <div class="card">
      <div class="row">Observe a request taking <input type="number" id="obs-ms" value="620" min="0" step="1" style="width:90px"> ms into <code>gym_http_request_duration_seconds</code> <button id="obs-go" class="primary">observe()</button> <button id="obs-reset">reset</button></div>
      <div id="obs-out"></div>
    </div>
    ${window.DATA_STRUCTURES.map((d) => `
      <details><summary>${esc(d.name)} <span class="muted small mono">— ${esc(d.type)}</span></summary><div class="body">
        <div class="kv">
          <div>File</div><div>${esc(d.file)} ${R(d.where)}</div>
          <div>Purpose</div><div>${esc(d.purpose)}</div>
          <div>Fields</div><div class="small">${esc(d.fields)}</div>
          <div>Created by</div><div>${R(d.creates)}</div>
          <div>Consumed by</div><div>${R(d.consumes)}</div>
          <div>Lifetime</div><div>${esc(d.life)}</div>
          <div>Example</div><div><code>${esc(d.example)}</code></div>
          <div>Why this structure</div><div>${esc(d.why)}</div>
        </div>
        <div class="prof"><div class="q">${esc(d.q)}</div></div>
      </div></details>`).join('')}
    <h2>Key excerpts</h2>
    <h3>DraftTracker (dict → gauge)</h3>${showSnip('m_draft_tracker', ['m_draft_dict', 'm_draft_publish'])}
    <h3>The request-id contextvar</h3>${showSnip('l_rid')}
    <h3>DemoState (set + cap)</h3>${showSnip('c_record', ['c_cap', 'c_unsafe_inc'])}
    ${understandButton('gauge')}`;

  const OBS_BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1, 2.5, 5, 10, Infinity];
  let obsState = { buckets: OBS_BUCKETS.map(() => 0), sum: 0, count: 0, last: null };
  function renderObs() {
    const s = obsState;
    const lines = OBS_BUCKETS.map((le, i) => {
      const hit = s.last != null && s.last <= le;
      const label = le === Infinity ? '+Inf' : String(le);
      return `${hit ? '<span class="hl">' : ''}gym_http_request_duration_seconds_bucket{le="${label}",method="GET",route="/login"} ${s.buckets[i].toFixed(1)}${hit ? '   ← +1' : ''}${hit ? '</span>' : ''}`;
    });
    lines.push(`gym_http_request_duration_seconds_count{method="GET",route="/login"} ${s.count.toFixed(1)}`);
    lines.push(`gym_http_request_duration_seconds_sum{method="GET",route="/login"} ${s.sum.toFixed(4)}`);
    $('#obs-out').innerHTML = `<pre>${lines.join('\n')}</pre><p class="small muted">Buckets are <b>cumulative</b> ("le" = less than or equal): one observation adds 1 to every bucket whose bound is ≥ the value, plus _count +1 and _sum += value. Buckets from <code>HTTP_DURATION_BUCKETS</code> ${refLink('m_buckets_http')}.</p>`;
  }

  pages.code = () => `<h1>Code → Result</h1>
    <p>Pick a line of code and follow it all the way to what a person sees.</p>
    ${window.CODE_RESULT.map((c) => `
      <div class="card">
        <pre><code>${esc(c.code)}</code></pre><div class="small">${R(c.ref)}</div>
        <ol>${c.steps.map(([h, t]) => `<li><b>${esc(h)}</b> — ${R(t)}</li>`).join('')}</ol>
      </div>`).join('')}
    <h2>The full middleware, for reference</h2>
    ${showSnip('mw_full', ['mw_set_rid', 'inflight_inc', 'timer_start', 'fault_call', 'inflight_dec', 'ok_observe', 'ok_count', 'log_served'])}`;

  pages.files = () => `<h1>Explain This File</h1>
    <p class="muted">The 20% of the code that carries 80% of the viva. Click a file.</p>
    <div class="row">${window.FILES.map((f, i) => `<button data-file="${i}">${esc(f.path)}</button>`).join('')}</div>
    <div id="file-panel"></div>`;

  function filePanel(i) {
    const f = window.FILES[i];
    $('#file-panel').innerHTML = `<div class="card">
      <h2 style="margin-top:4px"><a href="../${encodeURI(f.path)}" target="_blank">${esc(f.path)}</a></h2>
      <div class="kv">
        <div>Purpose</div><div>${esc(f.purpose)}</div>
        <div>Important functions</div><div><ul style="margin:0;padding-left:18px">${f.fns.map((x) => `<li>${R(x)}</li>`).join('')}</ul></div>
        <div>Important variables</div><div><ul style="margin:0;padding-left:18px">${f.vars.map((x) => `<li>${R(x)}</li>`).join('') || '<li>—</li>'}</ul></div>
        <div>Data structures</div><div>${f.ds.map(esc).join('; ')}</div>
        <div>Dependencies</div><div>${esc(f.deps)}</div>
        <div>If it disappeared</div><div>${esc(f.breaks)}</div>
      </div>
      <h3>Likely viva questions</h3><ul>${f.viva.map((q) => `<li>${esc(q)}</li>`).join('')}</ul>
    </div>`;
  }

  // ---- Metrics ------------------------------------------------------------

  const DECL_SNIP = {
    gym_http_requests_total: 'm_counter_decl', gym_sets_written_total: 'm_sets_decl', gym_workout_entries_total: 'm_entries_decl',
    gym_review_drafts_open: 'm_gauge_decl', gym_http_requests_in_flight: 'm_inflight_decl', gym_http_request_duration_seconds: 'm_hist_decl', gym_sets_per_entry: 'm_summary_decl',
  };
  const TYPE_INTRO = {
    counter: 'A number that only goes up (or resets to 0 when the process restarts). You never read it raw — you ask PromQL for <code>rate()</code> (per second) or <code>increase()</code> (how many in a window), which both detect resets. In this project: <b>10 counters</b>.',
    gauge: 'A number that goes up and down: a current level. Read it directly. In this project: <b>4 gauges</b>, two of them not charted.',
    histogram: 'Each observation is counted into cumulative buckets (<code>_bucket{le="…"}</code>) plus <code>_sum</code> and <code>_count</code>. PromQL turns buckets into percentiles with <code>histogram_quantile</code>. In this project: <b>3 histograms</b>.',
    summary: 'In the Python client: only <code>_sum</code> and <code>_count</code> — no quantiles. Good for averages (sum ÷ count). In this project: <b>2 summaries</b>.',
  };
  let metricTab = 'counter';

  pages.metrics = () => `<h1>Metric Explorer</h1>
    <p>All 19 metrics from <code>metrics.py</code>, grouped by type. ★ = deep explanation; know at least three of these cold.</p>
    <div class="tabs">${['counter', 'gauge', 'histogram', 'summary'].map((t) => `<button data-mtab="${t}" class="${t === metricTab ? 'on' : ''}">${t.toUpperCase()} (${window.METRIC_LIST.filter((m) => m.type === t).length})</button>`).join('')}</div>
    <div id="mtab-body">${metricTabBody(metricTab)}</div>
    ${understandButton('hist')}`;

  function queryParts(q) {
    const found = window.QUERIES.find((x) => x.q === q);
    if (!found) return '';
    return `<p class="small"><b>Token by token (inside → out):</b></p><ol class="small">${found.parts.map(([t, d], i) => `<li><span class="tok tok-${i % 5} mono">${esc(t)}</span> — ${d}</li>`).join('')}</ol>`;
  }

  function metricCard(m) {
    const header = `${m.deep ? '★ ' : ''}<code>${m.name}</code> ${typeBadge(m.type)} <span class="badge">${m.kind}</span>`;
    const facts = `<div class="kv">
      <div>Purpose</div><div>${R(m.purpose)}</div>
      <div>Unit</div><div>${esc(m.unit)}</div>
      <div>Labels</div><div>${m.labels.length ? m.labels.map((l) => `<code>${l}</code>`).join(' ') : '<span class="muted">none — one series</span>'}</div>
      <div>Declared</div><div>${refLink(m.decl)} <span class="muted small">as <code>${esc(m.attr)}</code></span></div>
      <div>Updated</div><div>${m.updates.map(refLink).join(' ')}</div>
      <div>Grafana query</div><div>${m.q ? `<code>${esc(m.q)}</code>` : '<span class="muted">not charted</span>'}</div>
      ${m.labelsWhy ? `<div>About the labels</div><div>${R(m.labelsWhy)}</div>` : ''}
      ${m.note ? `<div>Note</div><div>${R(m.note)}</div>` : ''}
    </div>`;
    if (!m.deep) return `<details><summary>${header}</summary><div class="body">${facts}<div class="why">${R(m.whyType)}</div></div></details>`;
    return `<details open><summary>${header}</summary><div class="body">
      ${facts}
      ${DECL_SNIP[m.name] ? showSnip(DECL_SNIP[m.name]) : ''}
      <h3>Why this type?</h3><div class="why">${R(m.whyType)}</div>
      <div class="warnbox"><b>Why not another type?</b> ${R(m.whyNot)}</div>
      <h3>Example value (real)</h3><pre>${esc(m.example)}</pre><p class="small muted">Source: ${R(m.exampleSrc)}</p>
      <h3>How the value changes</h3><p>${R(m.changes)}</p>
      <h3>The query</h3><pre>${esc(m.q)}</pre>${queryParts(m.q)}
      <p><b>Grafana shows:</b> ${R(m.shows)}</p>
      <h3>Professor asks…</h3>${m.professor.map((p) => prof(p.q, p.a)).join('')}
    </div></details>`;
  }

  function metricTabBody(t) {
    const list = window.METRIC_LIST.filter((m) => m.type === t).sort((a, b) => (b.deep ? 1 : 0) - (a.deep ? 1 : 0));
    return `<div class="card"><p style="margin:0">${TYPE_INTRO[t]}</p></div>${list.map(metricCard).join('')}`;
  }

  // ---- Percentiles --------------------------------------------------------

  const PRESETS = {
    baseline: [1.2, 1.8, 2.1, 2.3, 2.4, 2.6, 2.7, 2.9, 3.0, 3.1, 3.3, 3.4, 3.6, 3.8, 3.9, 4.1, 4.3, 4.5, 4.7, 4.9],
    fault: [2.1, 2.4, 2.6, 2.8, 3.0, 3.1, 3.3, 3.4, 3.6, 3.7, 3.9, 4.0, 4.2, 4.4, 4.6, 4.8, 503, 504, 505, 506],
    boundary: [2.1, 2.4, 2.6, 2.8, 3.0, 3.1, 3.3, 3.4, 3.6, 3.7, 3.9, 4.0, 4.2, 4.4, 4.6, 4.8, 500, 500, 500, 500],
  };
  let pValues = PRESETS.fault.slice();

  function histQuantile(q, valuesMs) {
    const counts = OBS_BUCKETS.map(() => 0);
    valuesMs.forEach((ms) => { const s = ms / 1000; const i = OBS_BUCKETS.findIndex((le) => s <= le); counts[i] += 1; });
    const cum = []; counts.reduce((acc, c, i) => (cum[i] = acc + c), 0);
    const total = cum[cum.length - 1];
    if (!total) return { value: NaN, counts, cum };
    const rank = q * total;
    let i = cum.findIndex((c) => c >= rank);
    if (OBS_BUCKETS[i] === Infinity) return { value: OBS_BUCKETS[i - 1] * 1000, counts, cum, i, rank, note: 'rank fell in +Inf: Prometheus returns the highest finite bound' };
    const start = i === 0 ? 0 : OBS_BUCKETS[i - 1];
    const prev = i === 0 ? 0 : cum[i - 1];
    const inBucket = cum[i] - prev;
    const frac = (rank - prev) / inBucket;
    return { value: (start + (OBS_BUCKETS[i] - start) * frac) * 1000, counts, cum, i, rank, start, end: OBS_BUCKETS[i], prev, inBucket, frac, total };
  }

  function nearestRank(q, values) {
    const v = values.slice().sort((a, b) => a - b);
    if (!v.length) return NaN;
    return v[Math.min(v.length - 1, Math.max(0, Math.round(q * v.length) - 1))];
  }

  pages.percentiles = () => `<h1>Interactive Percentile Explainer</h1>
    <p>Each value is one request duration. Watch how the same data becomes buckets, then a p50/p95/p99 estimate, exactly the way <code>histogram_quantile</code> computes it with <b>your</b> bucket bounds.</p>
    <div class="card">
      <div class="row"><b>Presets:</b>
        <button data-preset="baseline">Normal (all &lt; 5 ms)</button>
        <button data-preset="fault">E1 fault (every 5th +500 ms)</button>
        <button data-preset="boundary">Exactly 500 ms (bucket boundary)</button>
        <button data-preset="clear" class="danger">clear</button></div>
      <div class="row">Add a duration: <input type="number" id="p-add" value="12" min="0" step="0.1" style="width:90px"> ms <button id="p-add-btn" class="primary">add</button> <button id="p-add-fast">+5 fast</button> <button id="p-add-slow">+1 slow (503 ms)</button> <span class="muted small">Click a value to remove it.</span></div>
      <div class="chips" id="p-chips"></div>
      <div id="p-out"></div>
    </div>
    <h2>The query, inside out</h2>
    <pre>histogram_quantile(0.95, sum by (le) (rate(gym_http_request_duration_seconds_bucket[5m])))</pre>
    <ol>
      <li><code>gym_http_request_duration_seconds_bucket</code> — one cumulative counter per bucket bound <code>le</code>, per (method, route). E.g. <code>{le="0.75",route="/login"} 243</code>.</li>
      <li><code>[5m]</code> — for each of those series take the last 5 minutes of samples (~20 at a 15 s scrape).</li>
      <li><code>rate(…)</code> — turn each ever-growing bucket counter into "observations per second in the last 5 minutes" (handles restarts). This is what makes it a <i>sliding window</i> rather than "since the process started".</li>
      <li><code>sum by (le)</code> — add all routes and methods together, keeping the bucket bound: one histogram for the whole app.</li>
      <li><code>histogram_quantile(0.95, …)</code> — rank = 0.95 × total; find the first bucket whose cumulative count reaches the rank; interpolate linearly between that bucket's lower and upper bound. Ratios are unchanged by the "per second", so the widget above can use plain counts.</li>
    </ol>
    <div class="grid two">
      <div class="card tight"><b><code>_bucket</code></b><p class="small">Cumulative counts per upper bound. The input to histogram_quantile.</p></div>
      <div class="card tight"><b><code>_count</code></b><p class="small">Total observations (= the +Inf bucket). <code>rate(_count)</code> is requests per second.</p></div>
      <div class="card tight"><b><code>_sum</code></b><p class="small">Total seconds observed. <code>rate(_sum)/rate(_count)</code> = mean latency (the dashboard's "mean" line).</p></div>
      <div class="card tight"><b>Why p95 is an estimate</b><p class="small">The histogram only knows how many requests fell in each bucket, not their exact values. Inside a bucket it assumes they are spread evenly. With the fault, every slow request was ~503 ms, but they were assumed spread over 500–750 ms → 687.5 ms.</p></div>
      <div class="card tight"><b>Why a 5-minute window</b><p class="small">Shorter reacts faster but jumps around; with only a sample every 15 s, under a minute is too few points. The E1 tables used <code>[2m]</code> to match the 2-minute stages.</p></div>
      <div class="card tight"><b>Why scrape frequency matters</b><p class="small">rate() needs ≥ 2 samples in the window. 15 s scrape → [1m] has 4, [5m] has 20. Anything shorter than one scrape is invisible to Prometheus — but each request is still counted in the buckets, so it is not "lost", just not time-resolved.</p></div>
    </div>
    ${understandButton('p95')}`;

  function renderPercentiles() {
    $('#p-chips').innerHTML = pValues.map((v, i) => `<span class="chip ${v >= 100 ? 'slow' : ''}" data-rm="${i}">${v} ms</span>`).join('') || '<span class="muted">no values</span>';
    if (!pValues.length) { $('#p-out').innerHTML = ''; return; }
    const r95 = histQuantile(0.95, pValues), r50 = histQuantile(0.5, pValues), r99 = histQuantile(0.99, pValues);
    const visible = OBS_BUCKETS.slice(0, 13);
    const max = Math.max(...r95.counts, 1);
    const bars = visible.map((le, i) => `<div class="bar ${i === r95.i ? 'hit' : ''}" style="height:${(r95.counts[i] / max) * 100}%"><span>${r95.counts[i] || ''}</span></div>`).join('');
    const labels = visible.map((le, i) => `<div>≤${le >= 1 ? le + 's' : le * 1000 + 'ms'}</div>`).join('');
    const sum = pValues.reduce((a, b) => a + b, 0);
    const fmt = (x) => (isNaN(x) ? '—' : x < 10 ? x.toFixed(2) : x.toFixed(1));
    const explain = r95.note ? r95.note : `rank = 0.95 × ${r95.total} = <b>${r95.rank.toFixed(2)}</b>. Cumulative count reaches it in bucket <b>${r95.start * 1000}–${r95.end * 1000} ms</b> (holds ${r95.inBucket}, ${r95.prev} below it). Fraction into the bucket = (${r95.rank.toFixed(2)} − ${r95.prev}) / ${r95.inBucket} = <b>${r95.frac.toFixed(3)}</b>. Estimate = ${r95.start * 1000} + ${r95.frac.toFixed(3)} × ${((r95.end - r95.start) * 1000).toFixed(0)} = <b>${fmt(r95.value)} ms</b>.`;
    $('#p-out').innerHTML = `
      <div class="grid three" style="margin:10px 0">
        ${[['p50', r50, 0.5], ['p95', r95, 0.95], ['p99', r99, 0.99]].map(([n, r, q]) => `<div class="stat"><div class="label">${n} (histogram estimate)</div><div class="big" style="font-size:24px">${fmt(r.value)} ms</div><div class="small muted">exact value from raw data (nearest rank): ${fmt(nearestRank(q, pValues))} ms</div></div>`).join('')}
      </div>
      <p class="small muted">Bucket counts (non-cumulative), highlighted = the bucket that holds the 95th percentile rank:</p>
      <div class="bars">${bars}</div><div class="bar-labels">${labels}</div>
      <div class="card tight small" style="margin-top:12px"><b>How p95 was computed:</b> ${explain}</div>
      <p class="small"><code>_count</code> = ${pValues.length} · <code>_sum</code> = ${(sum / 1000).toFixed(4)} s · mean = ${fmt(sum / pValues.length)} ms</p>
      ${pValues.some((v) => v === 500) ? '<div class="warnbox small">A value of exactly 500 ms falls in the <b>le="0.5"</b> bucket (buckets are "less than or equal"), i.e. 250–500 ms — so p95 drops to 437.5 ms. In the real experiment the delayed requests took ~503 ms, so they fell in the 0.5–0.75 s bucket. This answers the report\'s open question about values exactly on a boundary.</div>' : ''}`;
  }

  // ---- PromQL --------------------------------------------------------------

  pages.promql = () => `<h1>PromQL Explainer</h1>
    <h2>Vocabulary</h2>
    <div class="grid two">
      ${[
        ['Instant vector', 'One value per series at one moment. <code>gym_review_drafts_open</code>.'],
        ['Range vector <code>[5m]</code>', 'For each series, all samples in the last 5 minutes. Only functions like rate() accept it.'],
        ['Label matcher <code>{status=~"5.."}</code>', '<code>=</code> exact, <code>!=</code> not, <code>=~</code> regex, <code>!~</code> not regex.'],
        ['<code>rate(x[w])</code>', 'Per-second average increase of a counter over w. Handles counter resets. Counters only.'],
        ['<code>increase(x[w])</code>', 'Total increase over w (≈ rate × seconds). Handles resets. Slightly extrapolated → 33.0007.'],
        ['<code>sum by (l) (…)</code>', 'Add series together, keep only label l. <code>sum(…)</code> with no by = one total.'],
        ['<code>histogram_quantile(φ, …)</code>', 'Estimate the φ-quantile from cumulative buckets that keep the <code>le</code> label.'],
        ['<code>count(…)</code>', 'How many <i>series</i> — not how many requests. Used to measure cardinality.'],
        ['<code>clamp_min(x, 0.0001)</code>', 'Floor a value, here to avoid dividing by zero.'],
        ['<code>or vector(0)</code>', 'If the left side returns nothing, use 0 — shows 0 instead of "No data".'],
      ].map(([h, t]) => `<div class="card tight"><b>${h}</b><p class="small">${t}</p></div>`).join('')}
    </div>
    <h2>Your actual queries</h2>
    <p class="muted small">Each query below is checked by <code>build_snippets.py --check</code> to appear verbatim in the named file.</p>
    ${window.QUERIES.map((x) => `
      <div class="card">
        <pre>${esc(x.q)}</pre>
        <div class="small muted">From <a href="../${encodeURI(x.src)}" target="_blank">${esc(x.src)}</a> — ${esc(x.panel)}</div>
        <ol class="small">${x.parts.map(([t, d], i) => `<li><span class="tok tok-${i % 5} mono">${esc(t)}</span> — ${d}</li>`).join('')}</ol>
        <div class="goodbox small"><b>Output:</b> ${esc(x.out)}</div>
      </div>`).join('')}`;

  // ---- Metric trace -------------------------------------------------------

  let mtStep = 0;
  let sim = { value: 0, samples: [[0, 0]], t: 0, log: ['start: process up, counter 0'] };

  pages['metric-trace'] = () => `<h1>Follow One Metric: <code>gym_sets_written_total</code></h1>
    <p>The report follows this one end-to-end. Values are the real ones from <code>observability/results-rerun/metric-trace-sets-written.json</code>.</p>
    <div class="stepper" id="mt-steps"></div>
    <div id="mt-body"></div>
    <div class="row"><button id="mt-prev">← Prev</button><button id="mt-next" class="primary">Next →</button></div>
    <h2>The code</h2>${showSnip('save_success', ['sets_inc', 'spe_observe'])}
    <h2>Counter-reset simulator</h2>
    <div class="card">
      <p class="small">Prometheus stores what it scrapes. Save entries, restart the app, and compare the raw counter with <code>increase()</code>.</p>
      <div class="row">Save an entry with <input type="number" id="sim-n" value="8" min="0" style="width:70px"> sets <button id="sim-save" class="primary">save (inc)</button> <button id="sim-restart" class="danger">restart app</button> <button id="sim-report">load the report's sequence</button> <button id="sim-reset">reset</button></div>
      <div id="sim-out"></div>
    </div>
    ${understandButton('trace')}`;

  function renderMetricTrace() {
    const steps = window.METRIC_TRACE;
    $('#mt-steps').innerHTML = steps.map((s, i) => `<div class="s ${i === mtStep ? 'on' : i < mtStep ? 'done' : ''}" data-mt="${i}">${i + 1}. ${esc(s.title)}</div>`).join('');
    const s = steps[mtStep];
    $('#mt-body').innerHTML = `<div class="card"><div class="muted small">Who: ${esc(s.who)}</div><pre>${esc(s.rep)}</pre><p>${R(s.detail)}</p><div class="row"><span class="badge b-ok">value: ${esc(s.value)}</span> ${R(s.where)}</div></div>`;
    $('#mt-prev').disabled = mtStep === 0; $('#mt-next').disabled = mtStep === steps.length - 1;
  }

  function increaseOf(samples) {
    let inc = 0;
    for (let i = 1; i < samples.length; i++) { const d = samples[i][1] - samples[i - 1][1]; inc += d >= 0 ? d : samples[i][1]; }
    return inc;
  }

  function renderSim() {
    const s = sim;
    const last = s.samples[s.samples.length - 1][1], first = s.samples[0][1];
    const max = Math.max(...s.samples.map((x) => x[1]), 1);
    const w = 600, h = 120;
    const pts = s.samples.map((x, i) => `${(i / Math.max(s.samples.length - 1, 1)) * (w - 20) + 10},${h - 10 - (x[1] / max) * (h - 30)}`).join(' ');
    $('#sim-out').innerHTML = `
      <svg viewBox="0 0 ${w} ${h}" style="width:100%;max-width:${w}px;background:var(--code-bg);border:1px solid var(--line);border-radius:8px"><polyline points="${pts}" fill="none" stroke="var(--metrics)" stroke-width="2"/>${s.samples.map((x, i) => `<circle cx="${(i / Math.max(s.samples.length - 1, 1)) * (w - 20) + 10}" cy="${h - 10 - (x[1] / max) * (h - 30)}" r="3.5" fill="var(--metrics)"/><text x="${(i / Math.max(s.samples.length - 1, 1)) * (w - 20) + 14}" y="${h - 14 - (x[1] / max) * (h - 30)}" font-size="11" fill="#8b97a6">${x[1]}</text>`).join('')}</svg>
      <div class="grid three" style="margin-top:8px">
        <div class="stat"><div class="label">raw counter now</div><div class="big">${last}</div></div>
        <div class="stat"><div class="label">naive last − first</div><div class="big" style="color:var(--bad)">${last - first}</div></div>
        <div class="stat"><div class="label">increase() (handles resets)</div><div class="big" style="color:var(--good)">${increaseOf(s.samples)}</div></div>
      </div>
      <p class="small mono muted">${s.log.map(esc).join(' → ')}</p>`;
  }

  // ---- Logs ---------------------------------------------------------------

  const FIELD_EXPLAIN = [
    ['@timestamp', 'When the event happened, from the LogRecord\'s creation time, UTC with milliseconds. overwrite_keys makes it win over Filebeat\'s read time.', 'date'],
    ['log.level', 'Severity, lowercase (info, warning, error). Exact-match filter in Kibana.', 'keyword'],
    ['message', 'A short human sentence ("request served"), not a key=value blob.', 'match_only_text (full-text)'],
    ['service.name', 'gym-tracker, from SERVICE_NAME. Tells services apart in one index.', 'keyword'],
    ['http.request.id', 'The correlation id from the contextvar. Same on every line of one request; also the x-request-id response header.', 'keyword (exact match, not tokenised)'],
    ['url.path', 'The raw path the client asked for. (The metric uses the route template instead.)', 'wildcard'],
    ['http.response.status_code', 'Integer status. Numeric → can filter >= 500 and aggregate.', 'long'],
    ['event.duration_ms', 'Request time in ms, rounded to 0.1. Not an ECS field, so dynamically mapped from the JSON number.', 'float'],
  ];

  pages.logs = () => `<h1>Log Explorer</h1>
    <p>Six real log events. Know the first two cold.</p>
    <h2>The fields every request log carries</h2>
    <div class="table-wrap"><table><tr><th>Field</th><th>Meaning / where it comes from</th><th>Elasticsearch type</th></tr>
      ${FIELD_EXPLAIN.map(([f, m, t]) => `<tr><td><code>${f}</code></td><td class="small">${m}</td><td class="mono small">${t}</td></tr>`).join('')}</table></div>
    <div class="why">Numbers stay numbers because the app wrote them as JSON numbers (200, 191.5) and Filebeat's decode_json_fields kept the type; the ECS template / dynamic mapping then stored them as long/float — so <code>event.duration_ms &gt; 500</code> works. The request id is a <b>keyword</b>: stored as one exact token, so <code>http.request.id: "259fd665642941e0"</code> is a precise term lookup, not a fuzzy text match.</div>
    ${window.LOGS.map((l) => `
      <details ${l.priority <= 2 ? 'open' : ''}><summary>${l.priority}. "${esc(l.title)}" ${badge(l.level, l.level.includes('error') ? 'b-high' : l.level.includes('warning') ? 'b-medium' : 'b-low')} <span class="muted small">${refLink(l.ref)}</span></summary><div class="body">
        <div class="why">${R(l.why)}</div>
        <p><b>Event fields (extra=):</b> ${l.extra.map((e) => `<code>${esc(e)}</code>`).join(' ')}</p>
        <h3>Application code</h3>${showSnip(l.snippet, [l.ref])}
        <h3>JSON log line</h3><pre>${esc(prettyJson(l.sample))}</pre><p class="small muted">${R(l.sampleSrc)}</p>
        <div class="pipeline"><span class="p">APP code</span><span class="arrow">→</span><span class="p">JSON on stdout</span><span class="arrow">→</span><span class="p">Docker json-file</span><span class="arrow">→</span><span class="p">Filebeat decode</span><span class="arrow">→</span><span class="p">Elasticsearch typed doc</span><span class="arrow">→</span><span class="p lit">Kibana: ${esc(l.kibana)}</span></div>
        <p class="small">Saved search: ${esc(l.savedSearch)}</p>
        ${prof(l.professor.q, l.professor.a)}
      </div></details>`).join('')}
    ${understandButton('logtrace')}`;

  function prettyJson(s) { try { return JSON.stringify(JSON.parse(s), null, 2); } catch (e) { return s; } }

  let ltStep = 0;
  pages['log-trace'] = () => `<h1>Follow One Log</h1>
    <p>Real request <code>GET /login</code>, id <code>259fd665642941e0</code>, 22 Sept 2026, 191.5 ms. Every representation below is copied from <code>observability/results-rerun/log-trace-example.json</code>.</p>
    <div class="stepper" id="lt-steps"></div>
    <div id="lt-body"></div>
    <div class="row"><button id="lt-prev">← Prev</button><button id="lt-next" class="primary">Next →</button></div>
    <h2>Your 60-second answer</h2>
    <div class="goodbox">"The middleware calls logger.info("request served") with the method, path, status and duration. My formatter turns that into one JSON line on stdout, adding the timestamp, level, service name and the request id from a contextvar. Docker's json-file driver wraps the line in its own JSON and appends it to the container's log file. Filebeat, which only watches containers with the co.elastic.logs label, strips Docker's wrapper and decode_json_fields parses our JSON into real fields, keeping our timestamp and nesting the dotted names. Elasticsearch indexes it with types — status as long, duration as float, request id as keyword — and in Kibana http.request.id: "259fd665642941e0" returns exactly this request's lines."</div>
    <h2>The Filebeat config that does step 4</h2>${showSnip('fb_processors', ['fb_decode', 'fb_target', 'fb_overwrite', 'fb_expand'])}
    <h2>Which containers Filebeat reads</h2>${showSnip('fb_autodiscover', ['fb_condition'])}
    ${understandButton('filebeat')}`;

  function renderLogTrace() {
    const steps = window.LOG_TRACE;
    $('#lt-steps').innerHTML = steps.map((s, i) => `<div class="s ${i === ltStep ? 'on' : i < ltStep ? 'done' : ''}" data-lt="${i}">STEP ${i + 1} — ${esc(s.title)}</div>`).join('');
    const s = steps[ltStep];
    const rep = ltStep === 1 ? prettyJson(s.rep) : s.rep;
    $('#lt-body').innerHTML = `<div class="card">
      <div class="kv"><div>Who changed it</div><div>${esc(s.who)}</div></div>
      <h3>Representation at this stage</h3><pre>${esc(rep)}</pre>
      <div class="kv"><div>What changed</div><div>${R(s.changed)}</div><div>Why</div><div>${R(s.why)}</div><div>Where</div><div>${R(s.where)}</div></div></div>`;
    $('#lt-prev').disabled = ltStep === 0; $('#lt-next').disabled = ltStep === steps.length - 1;
  }

  pages.vs = () => `<h1>Metrics vs Logs</h1>
    <div class="grid two">
      <div class="card"><h3 style="color:var(--metrics);margin-top:0">Metric</h3><ul><li>Aggregate: a number per series</li><li>Cheap: cost grows with number of <b>series</b>, not requests</li><li>Trends, rates, latency percentiles, counts</li><li>Dashboards and alerts</li><li>Only small, bounded labels</li><li>Kept 15 days here</li></ul></div>
      <div class="card"><h3 style="color:var(--logs);margin-top:0">Log</h3><ul><li>One record per event</li><li>Cost grows with number of <b>events</b></li><li>Detailed context: path, ids, error, stack</li><li>Debugging one request, exact failure</li><li>High-cardinality fields are fine (request id)</li><li>Kept 7 days here</li></ul></div>
    </div>
    <h2>The SAME request, both ways</h2>
    <p>GET /login, 191.5 ms, status 200, request id 259fd665642941e0 — what changed in each system:</p>
    <div class="grid two">
      <div class="card"><h3 style="margin-top:0;color:var(--metrics)">As metrics (the request disappears into totals)</h3><pre>gym_http_requests_total{method="GET",route="/login",status="200"}   +1
gym_http_request_duration_seconds_bucket{le="0.25",…,route="/login"}  +1
gym_http_request_duration_seconds_bucket{le="0.5",…}  +1   (and every
… le="0.75", "1.0", … "+Inf"                          +1    larger bucket)
gym_http_request_duration_seconds_count{…}            +1
gym_http_request_duration_seconds_sum{…}              +0.1915
gym_http_requests_in_flight      +1 then −1</pre><p class="small">You can now answer "how many /login requests per second?" and "what is p95?", but not "what happened to request 259fd665642941e0?". There is no id anywhere.</p></div>
      <div class="card"><h3 style="margin-top:0;color:var(--logs)">As a log (the request keeps its identity)</h3><pre>${esc(prettyJson(window.LOGS[0].sample))}</pre><p class="small">Searchable by its exact id, path, status, duration — but to get p95 over 5 minutes you would have to aggregate thousands of documents.</p></div>
    </div>
    <h2>Why request ids go in logs, not metric labels</h2>
    <div class="why">A metric label value creates a series. If request_id were a label, the GET /login above would create <code>gym_http_requests_total{…,request_id="259fd665642941e0"} 1</code> — a series that will only ever hold 1, and the next request makes another. Series count = request count: unbounded memory and index in Prometheus, for no aggregate value. In a log, the id is just one more field in a document that exists anyway. Proven in <a href="#cardinality">E2</a>; enforced by ${refLink('t_bounded_labels')}.</div>
    ${prof('When would you use a metric and when a log?', 'Metric when I want a number over time for all requests — rate, error rate, latency percentiles — and to alert on it. Log when I need the story of one specific event — which request failed, with what error and stack, by request id. In E1 the metric told me something was slow; the logs told me which requests and why (injected fault, sequence number).')}`;

  pages.security = () => `<h1>Security / Safe Logging</h1>
    <h2>What is NOT logged, and why</h2>
    <table><tr><th>Never logged</th><th>Why</th><th>What is logged instead</th></tr>
      <tr><td>Journal text (<code>raw_text</code>)</td><td>Health data</td><td><code>entry.text_bytes</code>, <code>entry.sets</code> (size and counts)</td></tr>
      <tr><td>Passwords</td><td>Credentials</td><td>nothing; login logs only event.action</td></tr>
      <tr><td>Usernames on login failure</td><td>Reveals who has an account; personal data</td><td><code>event.action: auth_failed</code></td></tr>
      <tr><td>Session tokens, cookies</td><td>Session hijack</td><td>—</td></tr>
      <tr><td>Groq API key, Authorization headers</td><td>Secret</td><td>—</td></tr>
      <tr><td>DATABASE_URL passwords</td><td>Secret</td><td>—</td></tr></table>
    <h2>Where the protection happens</h2>
    <ol>
      <li><b>Policy (call sites):</b> log ids and counts. See the extra={…} at ${refLink('log_drafted')}.</li>
      <li><b>SensitiveDataFilter</b> on the handler (${refLink('l_add_filter')}) — scrubs the message and its args with value patterns. On the handler, so library logs (uvicorn) are covered too.</li>
      <li><b>EcsJsonFormatter</b> — every extra field goes through <code>scrub_value</code> (${refLink('l_extras_scrub')}): keys in SENSITIVE_KEYS become "[redacted]", strings are pattern-scrubbed, dicts/lists recursed; exception message and stack trace are scrubbed too.</li>
      <li><b>Tests</b> — TestRedaction, including ${refLink('t_journal')}.</li>
    </ol>
    ${showSnip('l_sensitive')}${showSnip('l_patterns')}${showSnip('l_scrub_value')}${showSnip('l_filter')}
    <h2>Redaction playground</h2>
    <div class="card"><p class="small muted">A JavaScript port of <code>scrub_value</code> / <code>scrub_text</code>, using the same key list and regexes.</p>
      <div class="row">key <input type="text" id="rd-key" value="error.message" style="width:170px"> value <input type="text" id="rd-val" value="connect failed postgresql://gym:secret@postgres:5432 using gsk_ABCDEFGH12345678" style="flex:1;min-width:240px"> <button id="rd-go" class="primary">scrub</button></div>
      <pre id="rd-out"></pre>
      <div class="row small"><b>Try:</b> <button data-rd='["password","hunter2"]'>password</button><button data-rd='["raw_text","bench 60x8"]'>raw_text</button><button data-rd='["message","Authorization: Bearer abcdefghijkl"]'>Bearer</button><button data-rd='["http.request.id","259fd665642941e0"]'>request id (kept)</button></div>
    </div>
    ${prof('Why don\'t you log the journal text?', 'It is health data, and nothing in operations needs it: size and counts tell me cost and extraction quality. raw_text is also in SENSITIVE_KEYS as a backstop.')}
    ${prof('What happens if a developer accidentally logs a token?', 'If it is passed under a known key (token, session_token, cookie, authorization…) the formatter replaces it with [redacted]. If it appears inside a message, the patterns catch gsk_ keys, Bearer/Basic credentials and user:password@ in URLs. Anything else shaped differently would get through — the filter is a backstop, not the policy.')}
    ${prof('Where is sensitive data filtered?', 'In logging_setup.py, at two points: SensitiveDataFilter on the handler for message text, and scrub_value in EcsJsonFormatter.format for every extra field and the exception text.')}
    ${prof('Is /metrics a leak?', 'It exposes counts like login failures. Locally only the Docker network reaches it; METRICS_TOKEN enables a Bearer check with hmac.compare_digest ' + refLink('metrics_token') + '.')}`;

  const SENSITIVE = ['password', 'new_password', 'current_password', 'password_hash', 'token', 'session_token', 'session', 'cookie', 'set-cookie', 'authorization', 'api_key', 'apikey', 'groq_api_key', 'secret', 'database_url', 'raw_text', 'raw_source'];
  function scrubText(v) {
    return v.replace(/\bgsk_[A-Za-z0-9]{8,}\b/g, '[redacted]')
      .replace(/\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}/gi, '[redacted]')
      .replace(/(?<=:\/\/)[^\s:@/]+:[^\s@/]+(?=@)/g, '[redacted]');
  }
  function scrubValue(key, v) {
    const leaf = key.split('.').pop().toLowerCase();
    if (SENSITIVE.includes(leaf) || SENSITIVE.includes(key.toLowerCase())) return '[redacted]';
    return scrubText(v);
  }

  // ---- E1 fault -----------------------------------------------------------

  const E1 = [
    { stage: 'Normal', window: '11:31:37–11:33:38', req: '598 (4.98/s)', p50: 2.5, p95: 4.8, p99: 5.0, faults: 0, flogs: 0, errors: 0 },
    { stage: 'Fault', window: '11:34:04–11:36:05', req: '406 (3.38/s)', p50: 3.3, p95: 687.5, p99: 737.5, faults: 118, flogs: 118, errors: 0 },
    { stage: 'Recovered', window: '11:36:34–11:38:34', req: '579 (4.82/s)', p50: 2.6, p95: 5.6, p99: 20.9, faults: 0, flogs: 0, errors: 0 },
  ];
  let faultOn = false, faultSeq = 0, faultReqs = [];

  const QUIZ_E1 = [
    { q: 'What do you expect p95 to do?', opts: ['Barely move', 'Rise a lot (hundreds of ms)', 'Fall'], right: 1, why: 'p95 4.8 → 687.5 ms. 20% of requests are slow, so the 95th percentile is inside the slow group.' },
    { q: 'What do you expect p50 to do?', opts: ['Barely move', 'Rise to ~500 ms', 'Rise to ~100 ms'], right: 0, why: 'p50 2.5 → 3.3 ms. The median request is one of the 80% fast ones.' },
    { q: 'Will errors increase?', opts: ['Yes', 'No'], right: 1, why: '0 errors. A delay is not a failure; every request still returned its normal status.' },
    { q: 'Will throughput change?', opts: ['No change', 'Drop', 'Rise'], right: 1, why: '4.98 → 3.38 rps (−32%). The load generator waits for each response (closed loop), so slower responses = fewer requests sent.' },
    { q: 'Will log volume change?', opts: ['No', 'Yes — one extra warning per delayed request'], right: 1, why: '118 "injected fault" warnings in the fault window, 0 before and after.' },
  ];
  const QUIZ_E2 = [
    { q: 'You send 50 unique request ids. How many series does demo_requests_total have?', opts: ['1', '50', '100'], right: 1, why: 'One series per unique label combination: 50.' },
    { q: 'How many series does demo_requests_safe_total have?', opts: ['1', '50', '2'], right: 0, why: 'Only the endpoint label, one value: 1 series.' },
    { q: 'You send 120 unique ids. demo_requests_total series?', opts: ['120', '100', '1'], right: 1, why: 'MAX_UNIQUE_IDS caps it at 100; past the cap an existing id is reused.' },
    { q: 'You restart without the label. Query the period before the restart. What do you get?', opts: ['Nothing — deleted', 'Still 100'], right: 1, why: 'Evaluated at 04:57:30 the query still returned 100. Written series stay until 15-day retention.' },
  ];

  function quizHtml(id, quiz) {
    return `<div class="card" id="${id}"><h3 style="margin-top:0">Predict before you reveal</h3>
      ${quiz.map((item, qi) => `<div style="margin:10px 0"><b>${qi + 1}. ${esc(item.q)}</b><div class="row">${item.opts.map((o, oi) => `<button data-quiz="${id}" data-q="${qi}" data-o="${oi}">${esc(o)}</button>`).join('')}</div><div class="quiz-ans small" data-ans="${id}-${qi}"></div></div>`).join('')}
    </div>`;
  }

  pages.fault = () => `<h1>E1: Fault Injection — slow requests</h1>
    <p>Same load (2 min, 5 rps, <code>scripts/load_generator.py</code>) three times: normal, fault, recovered. The fault: <b>every 5th request +500 ms</b>, switched on by env vars and <code>docker compose up -d --force-recreate app</code>.</p>
    ${quizHtml('quiz-e1', QUIZ_E1)}
    <h2>Simulate it</h2>
    <div class="card">
      <div class="row"><button id="f-toggle" class="primary">Enable fault</button> <button id="f-send">Send 5 requests</button> <button id="f-reset">reset</button> <span class="muted small">FAULT_INJECTION_ENABLED=<b id="f-flag">false</b>, FAULT_LATENCY_MS=500, FAULT_LATENCY_EVERY_N=5</span></div>
      <div class="req-strip" id="f-strip"></div>
      <div id="f-flow"></div>
    </div>
    <h2>Results (21 Sept, UTC; percentiles from Prometheus over [2m])</h2>
    <div class="table-wrap"><table><tr><th>Stage</th><th>Window</th><th>Requests (client)</th><th>p50</th><th>p95</th><th>p99</th><th>Faults</th><th>Fault logs</th><th>Errors</th></tr>
      ${E1.map((r) => `<tr><td><b>${r.stage}</b></td><td class="mono small">${r.window}</td><td class="mono">${r.req}</td><td class="mono">${r.p50} ms</td><td class="mono" style="${r.p95 > 100 ? 'color:var(--logs)' : ''}">${r.p95} ms</td><td class="mono" style="${r.p99 > 100 ? 'color:var(--logs)' : ''}">${r.p99} ms</td><td class="mono">${r.faults}</td><td class="mono">${r.flogs}</td><td class="mono">${r.errors}</td></tr>`).join('')}</table></div>
    ${e1Chart()}
    <p class="small muted">Sources: observability/results-rerun/prometheus-percentiles.json, summary.md (client side), predictions.md. Screenshots: <a href="../docs/screenshots/10-prometheus-e1-latency.png" target="_blank">10-prometheus-e1-latency.png</a>, <a href="../docs/screenshots/09-kibana-e1-fault-logs.png" target="_blank">09-kibana-e1-fault-logs.png</a>.</p>
    <h2>Why 687.5 ms and not 500? Drag the slider</h2>
    <div class="card">
      <div class="row">Delay every <input type="range" id="f-n" min="1" max="25" value="5"> <b id="f-n-val">5th</b> request by 500 ms</div>
      <div id="f-n-out"></div>
    </div>
    <h2>The six questions</h2>
    <div class="grid two">
      <div class="card"><b>What did I predict?</b><p class="small">Written in predictions.md before the fault ran: p95 rises to ~500 ms, p50 barely moves, no errors, throughput falls or holds, fault counter goes above zero, one warning per delayed request.</p></div>
      <div class="card"><b>What actually happened?</b><p class="small">p95 4.8 → 687.5 ms (~143×), p99 5.0 → 737.5, p50 2.5 → 3.3, 0 errors, 118 faults = 118 warning logs, throughput −32%. Prediction right on every direction; p95 value higher only because of bucket interpolation.</p></div>
      <div class="card"><b>Why?</b><p class="small">20% of requests slow → 95th percentile inside the slow group, median inside the fast group. The delay is <code>asyncio.sleep</code> inside the timed region (timer at ${refLink('timer_start')}, fault hook at ${refLink('fault_call')}), so it is measured; asyncio so other requests are not blocked.</p></div>
      <div class="card"><b>What did users experience?</b><p class="small">Every 5th request half a second slower. A page load makes several requests, so most page loads would hit at least one slow one. Uptime checks and error-rate alerts stayed green — only percentiles showed it.</p></div>
      <div class="card"><b>How did we know?</b><p class="small">Metrics: p95/p99 panels and the heatmap splitting in two bands; <code>gym_faults_injected_total</code>. Logs: saved search 07 <code>fault.kind: *</code>, event.duration_ms > 500 (search 03). /healthz said "degraded".</p></div>
      <div class="card"><b>How did we recover?</b><p class="small">Recreate the app with FAULT_INJECTION_ENABLED=false; /healthz back to "ok"; same test: p95 5.6 ms, 0 faults, 0 fault logs. Recovery p99 20.9 ms = a few requests in the 10–25 ms bucket.</p></div>
    </div>
    <div class="warnbox"><b>406 requests, 118 faults?</b> 406 is client-side. The injector counts server-side requests with one global counter: / and /progress redirect to /login (2 server requests each) and Prometheus scrapes /metrics. The fault-stage scrape shows 139 + 50 + 123 + 243 + 40 = <b>595</b> server requests → 595 / 5 = 119.</div>
    <h2>The fault code</h2>${showSnip('f_before', ['f_sequence', 'f_latency_branch', 'f_record_inc', 'f_latency_log', 'f_sleep'])}
    <h2>Safety</h2>${showSnip('f_init', ['f_prod_refuse'])}
    ${understandButton('p95')}`;

  function e1Chart() {
    // Log scale, because 4.8 and 687.5 on one linear axis hides the baseline.
    const W = 640, H = 220, pad = 40, lo = Math.log10(1), hi = Math.log10(1000);
    const y = (v) => H - pad - ((Math.log10(v) - lo) / (hi - lo)) * (H - pad - 15);
    const cols = { p50: 'var(--app)', p95: 'var(--logs)', p99: 'var(--bad)' };
    let bars = '';
    E1.forEach((r, i) => {
      ['p50', 'p95', 'p99'].forEach((k, j) => {
        const x = pad + i * 195 + j * 50 + 20;
        bars += `<rect x="${x}" y="${y(r[k])}" width="40" height="${H - pad - y(r[k])}" fill="${cols[k]}" opacity=".8"/><text x="${x + 20}" y="${y(r[k]) - 4}" fill="#d8dee6" font-size="10.5" text-anchor="middle">${r[k]}</text>`;
      });
      bars += `<text x="${pad + i * 195 + 95}" y="${H - pad + 18}" fill="#8b97a6" font-size="12" text-anchor="middle">${r.stage}</text>`;
    });
    const ticks = [1, 10, 100, 1000].map((t) => `<line x1="${pad}" x2="${W - 10}" y1="${y(t)}" y2="${y(t)}" stroke="#2a3441"/><text x="${pad - 6}" y="${y(t) + 4}" fill="#8b97a6" font-size="10" text-anchor="end">${t}</text>`).join('');
    return `<div class="card"><svg viewBox="0 0 ${W} ${H}" style="width:100%;max-width:${W}px">${ticks}${bars}<text x="8" y="14" fill="#8b97a6" font-size="11">ms (log scale)</text></svg><div class="small"><span style="color:var(--app)">■ p50</span> <span style="color:var(--logs)">■ p95</span> <span style="color:var(--bad)">■ p99</span></div></div>`;
  }

  function renderFaultSlider() {
    const n = +$('#f-n').value, f = 1 / n;
    $('#f-n-val').textContent = n === 1 ? 'every' : n + (n === 2 ? 'nd' : n === 3 ? 'rd' : 'th');
    // Model: fast requests all in the first bucket (0–5 ms); slow ones (~503 ms) in 0.5–0.75 s.
    const q = (p) => {
      const fast = 1 - f;
      if (p <= fast) return (p / fast) * 5;
      return 500 + ((p - fast) / f) * 250;
    };
    const mean = (1 - f) * 2.5 + f * 503;
    $('#f-n-out').innerHTML = `<div class="grid three"><div class="stat"><div class="label">slow share</div><div class="big" style="font-size:22px">${(f * 100).toFixed(1)}%</div></div><div class="stat"><div class="label">p50 / p95 / p99 estimate</div><div class="big" style="font-size:18px">${q(0.5).toFixed(1)} / ${q(0.95).toFixed(1)} / ${q(0.99).toFixed(1)} ms</div></div><div class="stat"><div class="label">true slow duration</div><div class="big" style="font-size:22px">~503 ms</div></div></div>
      <p class="small">Model: fast requests sit in the 0–5 ms bucket, delayed ones in the 0.5–0.75 s bucket; histogram_quantile interpolates linearly inside the bucket that holds the rank. For every 5th: rank 0.95 is (0.95 − 0.80) / 0.20 = <b>0.75</b> of the way through 500–750 ms → 500 + 0.75 × 250 = <b>687.5 ms</b>; p99: 500 + 0.95 × 250 = <b>737.5 ms</b> — both exactly the measured values. Mean ≈ ${mean.toFixed(0)} ms. Notice: delay every 20th or rarer (≤ 5%) and p95 stays in the fast bucket — the fault becomes invisible to p95, only p99 sees it.</p>`;
  }

  function renderFaultSim(selected) {
    $('#f-flag').textContent = String(faultOn);
    $('#f-toggle').textContent = faultOn ? 'Disable fault (recover)' : 'Enable fault';
    $('#f-strip').innerHTML = faultReqs.map((r, i) => `<div class="req ${r.slow ? 'slow' : ''}" title="request #${r.seq}" data-req="${i}">${r.seq}</div>`).join('') || '<span class="muted small">Send some requests.</span>';
    const r = selected != null ? faultReqs[selected] : faultReqs[faultReqs.length - 1];
    if (!r) { $('#f-flow').innerHTML = ''; return; }
    const ms = r.slow ? 503.2 : 3.1;
    $('#f-flow').innerHTML = `<p class="small">Request #${r.seq} ${r.slow ? '<b style="color:var(--logs)">was delayed</b>' : 'was not delayed'}:</p>
      <div class="pipeline">
        <span class="p">request in · timer starts</span><span class="arrow">→</span>
        <span class="p ${r.slow ? 'lit' : ''}">fault check: ${r.armed ? `${r.seq} % 5 = ${r.seq % 5}` : 'not armed'}</span><span class="arrow">→</span>
        <span class="p ${r.slow ? 'lit' : ''}">${r.slow ? 'faults_injected_total{kind="latency"} +1 · warning log · asyncio.sleep(0.5)' : 'no delay'}</span><span class="arrow">→</span>
        <span class="p">route handler · response</span><span class="arrow">→</span>
        <span class="p ${r.slow ? 'lit' : ''}">histogram.observe(${(ms / 1000).toFixed(4)}) → bucket ${r.slow ? '0.5–0.75 s' : '0–5 ms'}</span><span class="arrow">→</span>
        <span class="p">"request served" event.duration_ms: ${ms}</span>
      </div>`;
  }

  // ---- E2 cardinality ------------------------------------------------------

  let card = { sent: 0, ids: [], safe: 0, safeMode: false, history: [[0, 0]], stale: 0 };
  const CARD_STEPS = [1, 10, 25, 50, 100, 120];

  pages.cardinality = () => `<h1>E2: Cardinality Explosion</h1>
    <p><code>observability/cardinality_demo.py</code> runs as its own container (profile <code>experiment</code>, port 8001) with two counters:</p>
    ${showSnip('c_counters')}
    ${quizHtml('quiz-e2', QUIZ_E2)}
    <h2>Send unique request ids</h2>
    <div class="card">
      <div class="row"><b>Total unique ids sent:</b> ${CARD_STEPS.map((n) => `<button data-card="${n}">${n}</button>`).join('')} <button data-card="+1">+1</button>
        <button id="card-safe" class="danger">Restart in safe mode (remove label)</button> <button id="card-reset">reset</button></div>
      <div id="card-out"></div>
    </div>
    <h2>Actual results (22 Sept)</h2>
    <table><tr><th>Unique ids sent</th><th>1</th><th>10</th><th>25</th><th>50</th><th>100</th><th>120</th><th>label removed, 2 scrapes later</th></tr>
      <tr><td><code>count(demo_requests_total)</code></td><td>1</td><td>10</td><td>25</td><td>50</td><td>100</td><td>100</td><td>no data</td></tr>
      <tr><td><code>count(demo_requests_safe_total)</code></td><td>1</td><td>1</td><td>1</td><td>1</td><td>1</td><td>1</td><td>1</td></tr></table>
    <p class="small muted">Screenshots: <a href="../docs/screenshots/08-prometheus-cardinality-staircase.png" target="_blank">08-prometheus-cardinality-staircase.png</a>, <a href="../docs/screenshots/06-prometheus-cardinality.png" target="_blank">06-prometheus-cardinality.png</a>.</p>
    <h2>Words you must use correctly</h2>
    <div class="grid two">
      ${[
        ['Label', 'A key on a metric: <code>endpoint</code>, <code>request_id</code>.'],
        ['Label value', 'What the key holds this time: <code>"/demo"</code>, <code>"3fa9c2…"</code>.'],
        ['Time series', 'Metric name + one exact combination of label values, with its own samples.'],
        ['Cardinality', 'How many distinct series a metric produces = product of distinct values of each label actually seen.'],
        ['Memory / index cost', 'Every active series sits in Prometheus\'s in-memory head block and in its inverted index (label → series). Cost grows with series, not with requests.'],
        ['Why ids belong in logs', 'A log stores the id once in a document that exists anyway; cost is linear in events and it is searchable. A label makes a permanent series per id.'],
      ].map(([h, t]) => `<div class="card tight"><b>${h}</b><p class="small">${t}</p></div>`).join('')}
    </div>
    <h2>Removing the label does not delete history</h2>
    <div class="warnbox">After restarting in safe mode, the old 100 series stop receiving samples, so a query about <i>now</i> returns no data after they go stale. But they are still on disk: evaluated at 04:57:30 (before the restart) the query still returns 100. They disappear only when 15-day retention (${refLink('dc_retention')}) removes them. Fixing the code stops the bleeding; it does not undo the damage.</div>
    <h2>Cost at scale</h2>
    <div class="card"><div class="row"><input type="number" id="sc-rps" value="100" min="0" style="width:90px"> requests/second × <input type="number" id="sc-h" value="24" min="0" style="width:70px"> hours</div><div id="sc-out"></div></div>
    <h2>How it is enforced in the real app</h2>${showSnip('m_route_label')}
    <p>And ${refLink('t_bounded_labels')} fails if any metric gains a label like user_id, request_id, session_date, path, url or ip.</p>
    ${understandButton('rid')}`;

  function hex12() { let s = ''; for (let i = 0; i < 12; i++) s += '0123456789abcdef'[Math.floor(Math.random() * 16)]; return s; }

  function cardSendTo(target) {
    const goal = target === '+1' ? card.sent + 1 : target;
    while (card.sent < goal) {
      card.sent += 1; card.safe += 1;
      if (!card.safeMode && card.ids.length < 100) card.ids.push(hex12());
      card.history.push([card.sent, card.safeMode ? 0 : card.ids.length]);
    }
    renderCard();
  }

  function renderCard() {
    const unsafe = card.safeMode ? 0 : card.ids.length;
    const W = 520, H = 170, maxX = Math.max(130, card.sent), pad = 30;
    const px = (x) => pad + (x / maxX) * (W - pad - 10), py = (v) => H - pad - (v / 110) * (H - pad - 10);
    const line = card.history.map(([x, v]) => `${px(x)},${py(v)}`).join(' ');
    $('#card-out').innerHTML = `
      <div class="grid three" style="margin:8px 0">
        <div class="stat"><div class="label">unique ids sent</div><div class="big">${card.sent}</div></div>
        <div class="stat"><div class="label">count(demo_requests_total)</div><div class="big" style="color:var(--bad)">${card.safeMode ? 'no data' : unsafe}</div>${card.ids.length >= 100 && !card.safeMode ? '<div class="small muted">cap reached: reusing an existing id</div>' : ''}</div>
        <div class="stat"><div class="label">count(demo_requests_safe_total)</div><div class="big" style="color:var(--good)">${card.sent ? 1 : 0}</div></div>
      </div>
      <svg viewBox="0 0 ${W} ${H}" style="width:100%;max-width:${W}px;background:var(--code-bg);border:1px solid var(--line);border-radius:8px">
        <line x1="${pad}" y1="${py(100)}" x2="${W - 10}" y2="${py(100)}" stroke="#f8514955" stroke-dasharray="4 4"/><text x="${W - 12}" y="${py(100) - 4}" fill="#f85149" font-size="10" text-anchor="end">cap 100</text>
        <polyline points="${line}" fill="none" stroke="var(--bad)" stroke-width="2"/>
        <line x1="${px(0)}" y1="${py(card.sent ? 1 : 0)}" x2="${px(card.sent)}" y2="${py(card.sent ? 1 : 0)}" stroke="var(--good)" stroke-width="2"/>
        <text x="${pad}" y="${H - 8}" fill="#8b97a6" font-size="10">unique ids sent →</text><text x="4" y="12" fill="#8b97a6" font-size="10">series</text>
      </svg>
      <p class="small muted">Prometheus series right now (${card.safeMode ? 'label removed; old series are stale on disk until retention' : 'one per id'}):</p>
      <div class="series-list">${card.sent ? `<div style="color:var(--good)">demo_requests_safe_total{endpoint="/demo"} ${card.safe}</div>` : ''}${card.ids.map((id) => `<div style="${card.safeMode ? 'opacity:.35;text-decoration:line-through' : 'color:#ffb3ad'}">demo_requests_total{endpoint="/demo",request_id="${id}"} 1</div>`).join('')}</div>`;
    $('#card-safe').disabled = card.safeMode;
  }

  function renderScale() {
    const rps = Math.max(0, +$('#sc-rps').value || 0), h = Math.max(0, +$('#sc-h').value || 0);
    const series = rps * 3600 * h;
    $('#sc-out').innerHTML = `<p class="mono">${rps} × 3,600 s × ${h} h = <b style="font-size:20px;color:var(--bad)">${series.toLocaleString('en-US')}</b> new series with a request_id label</p><p class="small">vs <b>1</b> series for <code>demo_requests_safe_total</code>, whatever the traffic. (The report: 100 rps for a day = 8,640,000.) Per-series memory varies by version and churn — the report lists estimating it as not yet understood, so do not quote a byte figure as fact.</p>`;
  }

  // ---- Requirements / xref / traps ----------------------------------------

  pages.requirements = () => `<h1>Things the Assignment Expects ME to Understand</h1>
    <p class="muted">The assignment brief is not in the repository; Parts A–E below follow the structure of your report.</p>
    <div class="table-wrap"><table><tr><th>Part</th><th>Requirement</th><th>Where my repo implements it</th><th>Evidence</th><th>What I must explain</th><th>Likely question</th></tr>
      ${window.REQUIREMENTS.map((r) => `<tr><td><b>${r.part}</b></td><td>${esc(r.req)}</td><td class="small">${esc(r.where)}</td><td class="small">${esc(r.evidence)}</td><td class="small">${esc(r.explain)}</td><td class="small"><i>${esc(r.q)}</i></td></tr>`).join('')}</table></div>`;

  pages.xref = () => `<h1>Report → Code Cross-Reference</h1>
    <p class="muted">Each claim from the report, with the code, configuration and result that back it — or don't.</p>
    ${window.XREF.map((x) => `<details><summary>${x.ok === true ? badge('VERIFIED', 'b-ok') : x.ok === 'partly' ? badge('NUANCE', 'b-partly') : badge('STALE', 'b-no')} ${esc(x.claim)}</summary><div class="body"><div class="kv">
      <div>Code</div><div>${R(x.code)}</div><div>Config</div><div>${R(x.config)}</div><div>Result</div><div>${R(x.result)}</div>${x.note ? `<div>Note</div><div>${esc(x.note)}</div>` : ''}</div></div></details>`).join('')}`;

  pages.traps = () => `<h1>Potential Viva Traps</h1>
    <p>Things an examiner may notice, what is actually happening in the code, and an honest answer. Nothing here is papered over.</p>
    ${window.TRAPS.map((t) => `<div class="card">
      <h3 style="margin-top:0">${impBadge(t.sev)} ${esc(t.title)}</h3>
      <div class="kv"><div>They might notice</div><div>${R(t.notice)}</div><div>What is actually happening</div><div>${R(t.actual)}</div></div>
      <div class="goodbox"><b>Answer honestly:</b> ${esc(t.answer)}</div></div>`).join('')}`;

  // ---- Professor mode -----------------------------------------------------

  let profCat = 'All';
  pages.professor = () => {
    const cats = ['All', ...new Set(window.PROFESSOR.map((p) => p.cat))];
    const list = window.PROFESSOR.filter((p) => profCat === 'All' || p.cat === profCat);
    return `<h1>Professor Mode</h1>
      <p>Every question is about this implementation. Try answering aloud <b>before</b> opening each one.</p>
      <div class="row">${cats.map((c) => `<button data-cat="${esc(c)}" class="${c === profCat ? 'primary' : ''}">${esc(c)}</button>`).join('')}</div>
      <div class="row"><button id="prof-random" class="primary">🎲 Ask me a random question</button></div>
      <div id="prof-random-slot"></div>
      ${list.map(profCard).join('')}`;
  };

  function profCard(p, open) {
    return `<details ${open ? 'open' : ''}><summary>${diffBadge(p.diff)} <span class="badge">${esc(p.cat)}</span> ${esc(p.q)}</summary><div class="body">
      <p><b>Expected answer:</b> ${R(p.a)}</p>
      <p><b>Key points they expect:</b> ${p.keys.map((k) => `<code>${esc(k)}</code>`).join(' ')}</p>
      <p class="small"><b>Relevant:</b> ${esc(p.file || '—')} ${R(p.ref)}</p>
      ${p.wrong ? `<div class="badbox small"><b>Common wrong answer:</b> ${esc(p.wrong)}</div>` : ''}
      ${p.follow ? prof('Follow-up: ' + p.follow.q, p.follow.a) : ''}
    </div></details>`;
  }

  // ---- Defend ---------------------------------------------------------------

  pages.defend = () => `<h1>Can I Defend This?</h1>
    <p>Type an answer as you would say it. It is checked against the key points of <b>your</b> implementation. The check looks for the ideas by keywords, so it is a study aid, not a grade: a GOOD with vague wording still needs practice aloud.</p>
    ${window.DEFEND.map((d) => `<div class="card" data-defend="${d.id}"><h3 style="margin-top:0">${esc(d.concept)}</h3><div class="defend-slot">${defendForm(d)}</div></div>`).join('')}`;

  function defendForm(d) {
    return `<p><b>${esc(d.prompt)}</b></p><textarea data-defend-text="${d.id}" placeholder="Explain in your own words…"></textarea><div class="row"><button class="primary" data-act="check" data-id="${d.id}">Check my answer</button><a href="#${d.page}" class="small">study this</a></div><div class="defend-result" data-result="${d.id}"></div>`;
  }

  function checkAnswer(id) {
    const d = window.DEFEND.find((x) => x.id === id);
    const text = (document.querySelector(`[data-defend-text="${id}"]`).value || '').toLowerCase();
    const results = d.points.map((p) => ({ label: p.label, hit: p.any.some((rx) => new RegExp(rx, 'i').test(text)) }));
    const hits = results.filter((r) => r.hit).length, ratio = hits / d.points.length;
    const [cls, word] = text.trim().length < 15 ? ['missing', 'MISSING IMPORTANT POINTS'] : ratio >= 0.8 ? ['good', 'GOOD'] : ratio >= 0.4 ? ['partial', 'PARTIALLY CORRECT'] : ['missing', 'MISSING IMPORTANT POINTS'];
    const missing = results.filter((r) => !r.hit);
    document.querySelector(`[data-result="${id}"]`).innerHTML = `<p><span class="score ${cls}">${word}</span> <span class="muted small">${hits}/${d.points.length} key points</span></p>
      <ul class="small">${results.map((r) => `<li style="color:${r.hit ? 'var(--good)' : 'var(--bad)'}">${r.hit ? '✓' : '✗'} ${esc(r.label)}</li>`).join('')}</ul>
      ${missing.length ? `<div class="warnbox small"><b>What to learn next:</b> ${missing.map((m) => esc(m.label)).join('; ')}. Go to <a href="#${d.page}">${esc(d.page)}</a>.</div>` : '<div class="goodbox small">Now say it out loud in under a minute.</div>'}`;
  }

  // -------------------------------------------------------------------------
  // Router and events
  // -------------------------------------------------------------------------

  function route() {
    const id = (location.hash || '#start').slice(1);
    const page = pages[id] ? id : 'start';
    renderNav(page);
    $('#page').innerHTML = pages[page]();
    $('#sidebar').classList.remove('open');
    window.scrollTo(0, 0);
    // Per-page initialisation.
    if (page === 'architecture') compPanel('app');
    if (page === 'percentiles') renderPercentiles();
    if (page === 'metric-trace') { renderMetricTrace(); renderSim(); }
    if (page === 'log-trace') renderLogTrace();
    if (page === 'fault') { renderFaultSim(); renderFaultSlider(); }
    if (page === 'cardinality') { renderCard(); renderScale(); }
    if (page === 'data') renderObs();
    if (page === 'files') filePanel(0);
    if (page === 'security') $('#rd-go').click();
  }

  document.addEventListener('click', (ev) => {
    const t = ev.target.closest('[data-comp],[data-mtab],[data-preset],[data-rm],[data-mt],[data-lt],[data-file],[data-cat],[data-quiz],[data-card],[data-req],[data-rd],[data-act],button[id]');
    if (!t) return;
    const d = t.dataset;
    if (d.comp) return compPanel(d.comp);
    if (d.mtab) { metricTab = d.mtab; document.querySelectorAll('[data-mtab]').forEach((b) => b.classList.toggle('on', b.dataset.mtab === metricTab)); $('#mtab-body').innerHTML = metricTabBody(metricTab); return; }
    if (d.preset) { pValues = d.preset === 'clear' ? [] : PRESETS[d.preset].slice(); return renderPercentiles(); }
    if (d.rm) { pValues.splice(+d.rm, 1); return renderPercentiles(); }
    if (d.mt) { mtStep = +d.mt; return renderMetricTrace(); }
    if (d.lt) { ltStep = +d.lt; return renderLogTrace(); }
    if (d.file) return filePanel(+d.file);
    if (d.cat) { profCat = d.cat; $('#page').innerHTML = pages.professor(); return; }
    if (d.card) return cardSendTo(d.card === '+1' ? '+1' : +d.card);
    if (d.req) return renderFaultSim(+d.req);
    if (d.rd) { const [k, v] = JSON.parse(d.rd); $('#rd-key').value = k; $('#rd-val').value = v; return $('#rd-go').click(); }
    if (d.quiz) {
      const quiz = d.quiz === 'quiz-e1' ? QUIZ_E1 : QUIZ_E2, item = quiz[+d.q], ok = +d.o === item.right;
      document.querySelectorAll(`[data-quiz="${d.quiz}"][data-q="${d.q}"]`).forEach((b) => { b.disabled = true; if (+b.dataset.o === item.right) b.classList.add('primary'); });
      document.querySelector(`[data-ans="${d.quiz}-${d.q}"]`).innerHTML = `<div class="${ok ? 'goodbox' : 'badbox'}">${ok ? 'Correct.' : 'Not quite.'} ${esc(item.why)}</div>`;
      return;
    }
    if (d.act === 'understand') {
      const box = t.closest('[data-defend]'); const def = window.DEFEND.find((x) => x.id === box.dataset.defend);
      box.querySelector('.defend-slot').innerHTML = `<div class="warnbox small">Not so fast — explain it first.</div>${defendForm(def)}`;
      return;
    }
    if (d.act === 'check') return checkAnswer(d.id);

    switch (t.id) {
      case 'menu-btn': $('#sidebar').classList.toggle('open'); break;
      case 'obs-go': { const v = Math.max(0, +$('#obs-ms').value || 0) / 1000; obsState.last = v; obsState.count += 1; obsState.sum += v; OBS_BUCKETS.forEach((le, i) => { if (v <= le) obsState.buckets[i] += 1; }); renderObs(); break; }
      case 'obs-reset': obsState = { buckets: OBS_BUCKETS.map(() => 0), sum: 0, count: 0, last: null }; renderObs(); break;
      case 'p-add-btn': { const v = parseFloat($('#p-add').value); if (!isNaN(v) && v >= 0) { pValues.push(v); renderPercentiles(); } break; }
      case 'p-add-fast': for (let i = 0; i < 5; i++) pValues.push(+(1.5 + Math.random() * 3.3).toFixed(1)); renderPercentiles(); break;
      case 'p-add-slow': pValues.push(503); renderPercentiles(); break;
      case 'mt-prev': if (mtStep > 0) { mtStep -= 1; renderMetricTrace(); } break;
      case 'mt-next': if (mtStep < window.METRIC_TRACE.length - 1) { mtStep += 1; renderMetricTrace(); } break;
      case 'lt-prev': if (ltStep > 0) { ltStep -= 1; renderLogTrace(); } break;
      case 'lt-next': if (ltStep < window.LOG_TRACE.length - 1) { ltStep += 1; renderLogTrace(); } break;
      case 'sim-save': { const n = Math.max(0, parseInt($('#sim-n').value, 10) || 0); sim.value += n; sim.samples.push([++sim.t, sim.value]); sim.log.push(`save +${n} → ${sim.value}`); renderSim(); break; }
      case 'sim-restart': sim.value = 0; sim.samples.push([++sim.t, 0]); sim.log.push('RESTART → 0'); renderSim(); break;
      case 'sim-report': sim = { value: 16, t: 4, samples: [[0, 0], [1, 17], [2, 0], [3, 3], [4, 16]], log: ['05:06 0', '05:12 17 (two saves)', '05:21 restart → 0', '05:21:45 3', '05:22 16'] }; renderSim(); break;
      case 'sim-reset': sim = { value: 0, samples: [[0, 0]], t: 0, log: ['start: process up, counter 0'] }; renderSim(); break;
      case 'f-toggle': faultOn = !faultOn; faultSeq = 0; renderFaultSim(); break;
      case 'f-send': for (let i = 0; i < 5; i++) { faultSeq += 1; faultReqs.push({ seq: faultSeq, armed: faultOn, slow: faultOn && faultSeq % 5 === 0 }); } faultReqs = faultReqs.slice(-60); renderFaultSim(); break;
      case 'f-reset': faultReqs = []; faultSeq = 0; renderFaultSim(); break;
      case 'card-safe': card.safeMode = true; card.history.push([card.sent, 0]); renderCard(); break;
      case 'card-reset': card = { sent: 0, ids: [], safe: 0, safeMode: false, history: [[0, 0]], stale: 0 }; renderCard(); break;
      case 'prof-random': { const list = window.PROFESSOR.filter((p) => profCat === 'All' || p.cat === profCat); $('#prof-random-slot').innerHTML = `<div class="card"><p><b>${esc(list[Math.floor(Math.random() * list.length)].q)}</b></p><p class="muted small">Answer aloud, then find it in the list below.</p></div>`; break; }
      case 'rd-go': { const k = $('#rd-key').value, v = $('#rd-val').value; $('#rd-out').textContent = JSON.stringify({ [k]: scrubValue(k, v) }); break; }
      default: break;
    }
  });

  document.addEventListener('change', (ev) => {
    const t = ev.target;
    if (t.dataset && t.dataset.check != null) {
      const done = store.get('viva-checklist', {}); done[t.dataset.check] = t.checked; store.set('viva-checklist', done);
    }
  });
  document.addEventListener('input', (ev) => {
    if (ev.target.id === 'f-n') renderFaultSlider();
    if (ev.target.id === 'sc-rps' || ev.target.id === 'sc-h') renderScale();
  });

  window.addEventListener('hashchange', route);
  route();
})();
