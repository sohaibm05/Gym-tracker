"""Progress page: CSS, SVG chart renderer, and the page shell.

Charts are drawn client-side as inline SVG from a JSON blob the server embeds.
No chart library and no external requests, so the page stays fast on a phone and
adds nothing to requirements.txt.

Design decisions worth knowing:
  * Every chart plots ONE series, so there is no categorical palette and no
    legend - the card title names what is plotted. The single hue is validated
    against both the light and dark chart surfaces.
  * Muscle groups are nominal, so every bar carries the same hue. Colouring them
    darker-where-bigger would double-encode length as hue and burn the only free
    channel on information the bar already shows.
  * Pain uses the reserved status palette, always with an icon and a word, never
    colour alone.
  * Every chart has a table twin, so no value is reachable only by hovering.
"""

from __future__ import annotations

import html
import json
from typing import Any

# Tokens are declared for both the OS setting and an explicit theme stamp, so a
# viewer's choice wins either way.
PROGRESS_CSS = """
.viz-root {
  color-scheme: light;
  --surface-1: #fcfcfb;
  --plane: #f9f9f7;
  --ink-1: #0b0b0b;
  --ink-2: #52514e;
  --ink-muted: #898781;
  --grid: #e1e0d9;
  --axis: #c3c2b7;
  --series-1: #2a78d6;
  --hairline: rgba(11,11,11,0.10);
  --good: #0ca30c;
  --warning: #fab219;
  --serious: #ec835a;
  --up-good: #006300;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) .viz-root {
    color-scheme: dark;
    --surface-1: #1a1a19;
    --plane: #0d0d0d;
    --ink-1: #ffffff;
    --ink-2: #c3c2b7;
    --ink-muted: #898781;
    --grid: #2c2c2a;
    --axis: #383835;
    --series-1: #3987e5;
    --hairline: rgba(255,255,255,0.10);
    --up-good: #0ca30c;
  }
}
:root[data-theme="dark"] .viz-root {
  color-scheme: dark;
  --surface-1: #1a1a19;
  --plane: #0d0d0d;
  --ink-1: #ffffff;
  --ink-2: #c3c2b7;
  --ink-muted: #898781;
  --grid: #2c2c2a;
  --axis: #383835;
  --series-1: #3987e5;
  --hairline: rgba(255,255,255,0.10);
  --up-good: #0ca30c;
}

.viz-root { background: var(--plane); color: var(--ink-1);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
.viz-root * { box-sizing: border-box; }

.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr)); gap: .6rem; }
.tile { background: var(--surface-1); border: 1px solid var(--hairline);
  border-radius: .6rem; padding: .75rem .85rem; }
.tile .label { color: var(--ink-2); font-size: .78rem; }
.tile .value { font-size: 1.6rem; font-weight: 600; margin-top: .15rem; line-height: 1.1; }
.tile .delta { font-size: .78rem; color: var(--ink-2); margin-top: .1rem; }
.tile .delta.up { color: var(--up-good); }

.card { background: var(--surface-1); border: 1px solid var(--hairline);
  border-radius: .6rem; padding: .9rem 1rem 1rem; margin: .8rem 0; }
.card h2 { font-size: 1rem; margin: 0; }
.card .sub { color: var(--ink-2); font-size: .8rem; margin: .15rem 0 .7rem; }
.plot { width: 100%; }
.plot svg { display: block; overflow: visible; }

.smalls { display: grid; grid-template-columns: repeat(auto-fit, minmax(15rem, 1fr)); gap: .9rem; }
.small h3 { font-size: .85rem; margin: 0 0 .1rem; font-weight: 600; }
.small .sub { font-size: .75rem; margin: 0 0 .4rem; }

details { margin-top: .7rem; }
summary { cursor: pointer; color: var(--ink-2); font-size: .8rem; }
table { border-collapse: collapse; width: 100%; margin-top: .5rem; font-size: .8rem;
  font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: .3rem .5rem; border-bottom: 1px solid var(--hairline); }
th { color: var(--ink-2); font-weight: 600; }
td.num, th.num { text-align: right; }

.status-row { display: flex; align-items: flex-start; gap: .55rem; padding: .5rem 0;
  border-bottom: 1px solid var(--hairline); }
.status-row:last-child { border-bottom: 0; }
.status-dot { width: .6rem; height: .6rem; border-radius: 50%; flex: none; margin-top: .35rem; }
.status-body { min-width: 0; flex: 1; }
.status-word { font-size: .75rem; color: var(--ink-2); white-space: nowrap; flex: none;
  margin-top: .1rem; }
.empty { color: var(--ink-2); font-size: .85rem; }

#viz-tip { position: fixed; pointer-events: none; z-index: 50; opacity: 0;
  transition: opacity .1s; background: var(--surface-1); color: var(--ink-1);
  border: 1px solid var(--hairline); border-radius: .4rem; padding: .4rem .55rem;
  font-size: .78rem; box-shadow: 0 4px 14px rgba(0,0,0,.18); max-width: 15rem; }
#viz-tip .tip-head { color: var(--ink-2); font-size: .72rem; margin-bottom: .15rem; }
#viz-tip .tip-val { font-weight: 600; }
#viz-tip .tip-key { display: inline-block; width: .9rem; height: 2px; vertical-align: middle;
  margin-right: .35rem; background: var(--series-1); }
@media (prefers-reduced-motion: reduce) { #viz-tip { transition: none; } }
"""

PROGRESS_JS = """
(function () {
  var DATA = JSON.parse(document.getElementById('viz-data').textContent);
  var NS = 'http://www.w3.org/2000/svg';
  var root = document.querySelector('.viz-root');
  var tip = document.getElementById('viz-tip');

  function tok(name) { return getComputedStyle(root).getPropertyValue(name).trim(); }
  function el(n, a) {
    var e = document.createElementNS(NS, n);
    for (var k in (a || {})) e.setAttribute(k, a[k]);
    return e;
  }
  function compact(n) {
    var v = Math.abs(n);
    if (v >= 1000000) return (n / 1000000).toFixed(1).replace(/\\.0$/, '') + 'M';
    if (v >= 10000) return (n / 1000).toFixed(1).replace(/\\.0$/, '') + 'K';
    return n.toLocaleString(undefined, { maximumFractionDigits: 1 });
  }
  function shortDate(iso) {
    var d = new Date(iso + 'T00:00:00');
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }
  function niceMax(v) {
    if (v <= 0) return 1;
    var mag = Math.pow(10, Math.floor(Math.log10(v)));
    return Math.ceil(v / (mag / 2)) * (mag / 2);
  }

  // Labels come from extracted text: always textContent, never innerHTML.
  function showTip(evt, head, value, unit) {
    tip.textContent = '';
    var h = document.createElement('div');
    h.className = 'tip-head';
    h.textContent = head;
    var v = document.createElement('div');
    v.className = 'tip-val';
    var key = document.createElement('span');
    key.className = 'tip-key';
    v.appendChild(key);
    v.appendChild(document.createTextNode(value + (unit ? ' ' + unit : '')));
    tip.appendChild(h); tip.appendChild(v);
    tip.style.opacity = '1';
    var box = (evt.target.getBoundingClientRect ? evt.target.getBoundingClientRect() : null);
    var x = evt.clientX != null && evt.clientX ? evt.clientX : (box ? box.left + box.width / 2 : 0);
    var y = evt.clientY != null && evt.clientY ? evt.clientY : (box ? box.top : 0);
    var w = tip.offsetWidth, hh = tip.offsetHeight;
    tip.style.left = Math.max(6, Math.min(window.innerWidth - w - 6, x - w / 2)) + 'px';
    tip.style.top = Math.max(6, y - hh - 12) + 'px';
  }
  function hideTip() { tip.style.opacity = '0'; }

  function interactive(node, head, value, unit) {
    node.setAttribute('tabindex', '0');
    node.setAttribute('role', 'img');
    node.setAttribute('aria-label', head + ': ' + value + (unit ? ' ' + unit : ''));
    node.addEventListener('pointerenter', function (e) { showTip(e, head, value, unit); });
    node.addEventListener('pointermove', function (e) { showTip(e, head, value, unit); });
    node.addEventListener('pointerleave', hideTip);
    node.addEventListener('focus', function (e) { showTip(e, head, value, unit); });
    node.addEventListener('blur', hideTip);
  }

  // --- column chart: magnitude over time, one series ------------------------
  function columns(host, rows, unit) {
    var W = host.clientWidth || 320, H = 190;
    var padL = 42, padR = 10, padT = 12, padB = 26;
    var svg = el('svg', { width: W, height: H });
    var iw = W - padL - padR, ih = H - padT - padB;
    var max = niceMax(Math.max.apply(null, rows.map(function (r) { return r[1]; }).concat([1])));
    var band = iw / rows.length;
    var bw = Math.min(24, Math.max(4, band - 6));   // cap thickness, leave air

    [0, 0.5, 1].forEach(function (f) {
      var y = padT + ih - ih * f;
      svg.appendChild(el('line', { x1: padL, y1: y, x2: W - padR, y2: y,
        stroke: f === 0 ? tok('--axis') : tok('--grid'), 'stroke-width': 1 }));
      var t = el('text', { x: padL - 6, y: y + 4, 'text-anchor': 'end',
        fill: tok('--ink-muted'), 'font-size': 10 });
      t.textContent = compact(max * f);
      svg.appendChild(t);
    });

    rows.forEach(function (r, i) {
      var v = r[1];
      var h = Math.max(v > 0 ? 2 : 0, (v / max) * ih);
      var x = padL + band * i + (band - bw) / 2;
      var y = padT + ih - h;
      if (h > 0) {
        // 4px rounded data-end, square at the baseline.
        var rx = Math.min(4, bw / 2), rr = Math.min(rx, h);
        var d = 'M' + x + ',' + (padT + ih) + ' L' + x + ',' + (y + rr) +
                ' Q' + x + ',' + y + ' ' + (x + rr) + ',' + y +
                ' L' + (x + bw - rr) + ',' + y +
                ' Q' + (x + bw) + ',' + y + ' ' + (x + bw) + ',' + (y + rr) +
                ' L' + (x + bw) + ',' + (padT + ih) + ' Z';
        svg.appendChild(el('path', { d: d, fill: tok('--series-1') }));
      }
      // Hit target spans the whole band and the full height: ~24px minimum.
      var hit = el('rect', { x: padL + band * i, y: padT, width: band, height: ih,
        fill: 'transparent', style: 'cursor:pointer' });
      interactive(hit, 'Week of ' + shortDate(r[0]), compact(v), unit);
      svg.appendChild(hit);

      if (i === rows.length - 1 || i % Math.ceil(rows.length / 4) === 0) {
        var lab = el('text', { x: padL + band * i + band / 2, y: H - 8,
          'text-anchor': 'middle', fill: tok('--ink-muted'), 'font-size': 10 });
        lab.textContent = shortDate(r[0]);
        svg.appendChild(lab);
      }
    });
    host.textContent = '';
    host.appendChild(svg);
  }

  // --- line chart: trend over time, one series ------------------------------
  function lineChart(host, rows, unit, height) {
    var W = host.clientWidth || 320, H = height || 190;
    var padL = 42, padR = 30, padT = 14, padB = 24;
    var svg = el('svg', { width: W, height: H });
    var iw = W - padL - padR, ih = H - padT - padB;
    var vals = rows.map(function (r) { return r[1]; });
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    var flat = hi === lo;
    if (flat) {
      // A constant series has no range to scale. Inventing one produces axis
      // ticks that appear nowhere in the data - a lift held at 65kg must not
      // be labelled 63.7 and 66.3. Band it symmetrically and show one tick.
      var span = Math.max(1, Math.abs(lo) * 0.08);
      lo -= span; hi += span;
    } else {
      var pad = (hi - lo) * 0.15; lo -= pad; hi += pad;
    }
    var X = function (i) { return rows.length === 1 ? padL + iw / 2
      : padL + (iw * i) / (rows.length - 1); };
    var Y = function (v) { return padT + ih - ((v - lo) / (hi - lo)) * ih; };

    var ticks = flat ? [vals[0]] : [lo, hi];
    ticks.forEach(function (v) {
      var y = Y(v);
      svg.appendChild(el('line', { x1: padL, y1: y, x2: W - padR, y2: y,
        stroke: flat || v !== lo ? tok('--grid') : tok('--axis'), 'stroke-width': 1 }));
      var t = el('text', { x: padL - 6, y: y + 4, 'text-anchor': 'end',
        fill: tok('--ink-muted'), 'font-size': 10 });
      t.textContent = compact(v);
      svg.appendChild(t);
    });

    var d = rows.map(function (r, i) { return (i ? 'L' : 'M') + X(i) + ',' + Y(r[1]); }).join(' ');
    svg.appendChild(el('path', { d: d + ' L' + X(rows.length - 1) + ',' + (padT + ih) +
      ' L' + X(0) + ',' + (padT + ih) + ' Z', fill: tok('--series-1'), 'fill-opacity': 0.10 }));
    svg.appendChild(el('path', { d: d, fill: 'none', stroke: tok('--series-1'),
      'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));

    rows.forEach(function (r, i) {
      var last = i === rows.length - 1;
      if (last) {
        // 2px surface ring keeps the end marker legible over the line.
        svg.appendChild(el('circle', { cx: X(i), cy: Y(r[1]), r: 5,
          fill: tok('--series-1'), stroke: tok('--surface-1'), 'stroke-width': 2 }));
        var lab = el('text', { x: X(i) + 9, y: Y(r[1]) + 4, fill: tok('--ink-1'),
          'font-size': 11, 'font-weight': 600 });
        lab.textContent = compact(r[1]);
        svg.appendChild(lab);
      }
      var hit = el('rect', { x: X(i) - Math.max(12, iw / rows.length / 2), y: padT,
        width: Math.max(24, iw / rows.length), height: ih, fill: 'transparent',
        style: 'cursor:pointer' });
      interactive(hit, shortDate(r[0]), compact(r[1]), unit);
      svg.appendChild(hit);
    });

    [0, rows.length - 1].forEach(function (i) {
      if (rows.length < 2 && i === 0) return;
      var t = el('text', { x: X(i), y: H - 6,
        'text-anchor': i === 0 ? 'start' : 'end', fill: tok('--ink-muted'), 'font-size': 10 });
      t.textContent = shortDate(rows[i][0]);
      svg.appendChild(t);
    });
    host.textContent = '';
    host.appendChild(svg);
  }

  // --- horizontal bars: magnitude across nominal categories -----------------
  function bars(host, rows, unit) {
    var W = host.clientWidth || 320;
    var rowH = 26, H = rows.length * rowH + 6;
    var labelW = Math.min(120, Math.max(70, W * 0.32));
    var svg = el('svg', { width: W, height: H });
    var max = Math.max.apply(null, rows.map(function (r) { return r[1]; }).concat([1]));
    var track = W - labelW - 56;

    rows.forEach(function (r, i) {
      var y = i * rowH + 4, bh = Math.min(24, rowH - 10);
      var w = Math.max(r[1] > 0 ? 2 : 0, (r[1] / max) * track);
      var name = el('text', { x: 0, y: y + bh / 2 + 4, fill: tok('--ink-2'), 'font-size': 11 });
      name.textContent = r[0];
      svg.appendChild(name);
      if (w > 0) {
        var rr = Math.min(4, bh / 2, w);
        var x = labelW;
        var d = 'M' + x + ',' + y + ' L' + (x + w - rr) + ',' + y +
                ' Q' + (x + w) + ',' + y + ' ' + (x + w) + ',' + (y + rr) +
                ' L' + (x + w) + ',' + (y + bh - rr) +
                ' Q' + (x + w) + ',' + (y + bh) + ' ' + (x + w - rr) + ',' + (y + bh) +
                ' L' + x + ',' + (y + bh) + ' Z';
        svg.appendChild(el('path', { d: d, fill: tok('--series-1') }));
      }
      var val = el('text', { x: labelW + w + 6, y: y + bh / 2 + 4,
        fill: tok('--ink-1'), 'font-size': 11, 'font-weight': 600 });
      val.textContent = compact(r[1]);
      svg.appendChild(val);
      var hit = el('rect', { x: 0, y: i * rowH, width: W, height: rowH,
        fill: 'transparent', style: 'cursor:pointer' });
      interactive(hit, r[0], compact(r[1]), unit);
      svg.appendChild(hit);
    });
    host.textContent = '';
    host.appendChild(svg);
  }

  function draw() {
    document.querySelectorAll('[data-chart]').forEach(function (host) {
      var kind = host.getAttribute('data-chart');
      if (kind === 'volume' && DATA.weekly_volume.length) {
        columns(host, DATA.weekly_volume, 'kg');
      } else if (kind === 'bodyweight' && DATA.bodyweight.length) {
        lineChart(host, DATA.bodyweight, 'kg');
      } else if (kind === 'muscle' && DATA.muscle_volume.length) {
        bars(host, DATA.muscle_volume, 'kg');
      } else if (kind.indexOf('e1rm:') === 0) {
        var idx = parseInt(kind.split(':')[1], 10);
        var s = DATA.exercise_e1rm[idx];
        if (s) lineChart(host, s.points, 'kg', 150);
      }
    });
  }

  draw();
  var timer;
  window.addEventListener('resize', function () {
    clearTimeout(timer);
    timer = setTimeout(draw, 150);   // re-render at true size, never scale text
  });
})();
"""


# --------------------------------------------------------------------------
# Page shell
# --------------------------------------------------------------------------

_STATUS_ICON = {"serious": "▲", "warning": "●", "good": "✓"}


def _fmt(value: Any, digits: int = 0) -> str:
    if value is None:
        return "—"
    if isinstance(value, float) and digits == 0:
        value = round(value)
    return f"{value:,.{digits}f}" if isinstance(value, (int, float)) else str(value)


def _tile(label: str, value: str, delta: str = "", up_is_good: bool = False) -> str:
    delta_class = "delta up" if (up_is_good and delta.startswith("+")) else "delta"
    delta_html = f'<div class="{delta_class}">{html.escape(delta)}</div>' if delta else ""
    return (
        f'<div class="tile"><div class="label">{html.escape(label)}</div>'
        f'<div class="value">{html.escape(value)}</div>{delta_html}</div>'
    )


def _table(headers: list[str], rows: list[list[str]], caption: str) -> str:
    """The table twin. Every plotted value is reachable here without hovering."""
    head = "".join(
        f'<th class="{"num" if i else ""}">{html.escape(h)}</th>' for i, h in enumerate(headers)
    )
    body = "".join(
        "<tr>" + "".join(
            f'<td class="{"num" if i else ""}">{html.escape(str(c))}</td>'
            for i, c in enumerate(row)
        ) + "</tr>"
        for row in rows
    )
    return (
        f"<details><summary>{html.escape(caption)}</summary>"
        f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></details>"
    )


def _card(title: str, subtitle: str, plot: str, table: str = "") -> str:
    return (
        f'<section class="card"><h2>{html.escape(title)}</h2>'
        f'<p class="sub">{html.escape(subtitle)}</p>{plot}{table}</section>'
    )


def render_progress_body(data: dict[str, Any]) -> str:
    """The progress page markup. Charts are drawn client-side from `data`."""
    payload = json.dumps(data).replace("<", "\\u003c")
    blob = f'<script type="application/json" id="viz-data">{payload}</script>'

    if not data["has_data"]:
        return (
            f'<div class="viz-root">{blob}<h1>Progress</h1>'
            '<section class="card"><p class="empty">Nothing logged yet. '
            'Once a few sessions are in, this page fills up with volume, '
            'estimated 1RM per exercise, and your bodyweight trend.</p></section></div>'
        )

    k = data["kpis"]
    volume_delta = (
        f"{k['volume_delta_pct']:+.1f}% vs 4-week average"
        if k["volume_delta_pct"] is not None else ""
    )
    bodyweight_delta = (
        f"{k['bodyweight_delta']:+.1f} kg over {data['weeks']} weeks"
        if k["bodyweight_delta"] is not None else ""
    )
    tiles = (
        '<div class="kpis">'
        + _tile("Volume this week", _fmt(k["volume_this_week"]) + " kg", volume_delta,
                up_is_good=True)
        + _tile("Working sets", _fmt(k["sets_this_week"]))
        + _tile("Exercises trained", _fmt(k["exercises_this_week"]))
        + _tile("Bodyweight", (_fmt(k["bodyweight"], 1) + " kg") if k["bodyweight"] else "—",
                bodyweight_delta)
        + "</div>"
    )

    volume_card = _card(
        "Weekly volume",
        f"Working sets only, weight x reps. Last {data['weeks']} weeks.",
        '<div class="plot" data-chart="volume"></div>',
        _table(["Week starting", "Volume (kg)"],
               [[week, _fmt(value)] for week, value in data["weekly_volume"]],
               "Table view"),
    )

    smalls, e1rm_rows = [], []
    for index, series in enumerate(data["exercise_e1rm"]):
        first, last = series["points"][0][1], series["points"][-1][1]
        change = last - first
        smalls.append(
            f'<div class="small"><h3>{html.escape(series["exercise"])}</h3>'
            f'<p class="sub">{change:+.1f} kg over {len(series["points"])} sessions</p>'
            f'<div class="plot" data-chart="e1rm:{index}"></div></div>'
        )
        for day, value in series["points"]:
            e1rm_rows.append([series["exercise"], day, _fmt(value, 1)])

    e1rm_card = _card(
        "Estimated 1RM by exercise",
        "Epley, from clean reps only - cheat reps are excluded. Most-trained first.",
        f'<div class="smalls">{"".join(smalls)}</div>' if smalls
        else '<p class="empty">Needs at least two sessions of an exercise to show a trend.</p>',
        _table(["Exercise", "Session", "e1RM (kg)"], e1rm_rows, "Table view") if e1rm_rows else "",
    )

    bodyweight_card = _card(
        "Bodyweight",
        f"Every reading pulled from your entries. Last {data['weeks']} weeks.",
        '<div class="plot" data-chart="bodyweight"></div>' if data["bodyweight"]
        else '<p class="empty">No bodyweight logged yet - mention it in an entry '
             '("was 82.4kg this morning") and it lands here.</p>',
        _table(["Date", "Weight (kg)"], [[d, _fmt(v, 1)] for d, v in data["bodyweight"]],
               "Table view") if data["bodyweight"] else "",
    )

    muscle_card = _card(
        "Volume by muscle group",
        "This week. Every bar shares one colour - length is the whole story.",
        '<div class="plot" data-chart="muscle"></div>' if data["muscle_volume"]
        else '<p class="empty">Nothing logged this week yet.</p>',
        _table(["Muscle group", "Volume (kg)"],
               [[name, _fmt(value)] for name, value in data["muscle_volume"]],
               "Table view") if data["muscle_volume"] else "",
    )

    if data["pain"]:
        rows = []
        for item in data["pain"]:
            status = item["status"]
            note = ("3+ sessions in a row - worth getting looked at"
                    if item["streak"] >= 3
                    else f"{item['streak']} session(s) in a row" if item["streak"]
                    else "not in the most recent session")
            rows.append(
                f'<div class="status-row">'
                f'<span class="status-dot" style="background:var(--{status})"></span>'
                f'<span class="status-body">{html.escape(item["exercise"])}'
                f'<br><span class="sub">last flagged {html.escape(item["last_flagged"])} '
                f'&middot; {html.escape(note)}</span></span>'
                f'<span class="status-word">{_STATUS_ICON[status]} '
                f'{html.escape(status)}</span></div>'
            )
        pain_body = "".join(rows)
    else:
        pain_body = '<p class="empty">No pain flags in this window.</p>'

    pain_card = _card(
        "Pain flags",
        "Colour is never the only signal here - each row carries an icon and a word.",
        pain_body,
    )

    return (
        f'<div class="viz-root">{blob}'
        f"<h1>Progress</h1>"
        f'<p class="sub">Last {data["weeks"]} weeks &middot; times in '
        f'{html.escape(data["timezone"])}</p>'
        f"{tiles}{volume_card}{e1rm_card}{bodyweight_card}{muscle_card}{pain_card}"
        f'<div id="viz-tip" role="status" aria-live="polite"></div></div>'
    )
