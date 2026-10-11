#!/usr/bin/env python3
"""Assertions for the stack smoke test in .github/workflows/stack.yml.

CI-only helper; nothing in the app or the student-facing scripts imports it.
Standard library only, so it runs on the runner's Python without installing
anything.

    smoke_assert.py summary LOG          quickstart's [OK]/[WARN]/[FAIL] lines
                                         -> job summary table + annotations
    smoke_assert.py e1 RESULTS_DIR       Part E1: the fault moved p95, the
                                         recovery brought it back
    smoke_assert.py e2 RESULTS_DIR       Part E2: 100 series with the label,
                                         1 without, none after the fix

`summary` always exits 0: whether quickstart passed is decided by quickstart's
own exit code in the step that ran it. `e1` and `e2` exit 1 on any failed check.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

VERIFY_LINE = re.compile(r"^\s+\[(OK|WARN|FAIL)\]\s+(.+?)(?:\s{2,}(.*))?$")


def annotate(level: str, title: str, message: str) -> None:
    # Workflow commands: show up on the run's summary page, not only in the log.
    # Escaping as in @actions/core: data escapes % CR LF; properties also : and ,.
    message = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    title = (title.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
             .replace(":", "%3A").replace(",", "%2C"))
    print(f"::{level} title={title}::{message}", flush=True)


def append_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(markdown + "\n")
    else:
        print(markdown)


# ---------------------------------------------------------------------------
# quickstart summary
# ---------------------------------------------------------------------------

def cmd_summary(args: argparse.Namespace) -> int:
    log = Path(args.log)
    text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    rows = []
    for line in text.splitlines():
        match = VERIFY_LINE.match(line)
        if match:
            rows.append((match.group(1), match.group(2).strip(), (match.group(3) or "").strip()))
    if not rows:
        annotate("error", "quickstart",
                 "quickstart stopped before its verification step; the ERROR line in its log says why")
        append_summary(f"### {args.title}\n\nquickstart stopped before verifying anything. "
                       "See the step log and the uploaded artifact.\n")
        return 0
    icon = {"OK": "OK", "WARN": "WARN", "FAIL": "**FAIL**"}
    lines = [f"### {args.title}", "", "| Result | Check | Detail |", "|---|---|---|"]
    for state, what, detail in rows:
        lines.append(f"| {icon[state]} | {what} | {detail.replace('|', '/')} |")
        if state == "WARN":
            annotate("warning", f"quickstart: {what}", detail or what)
        elif state == "FAIL":
            annotate("error", f"quickstart: {what}", detail or what)
    append_summary("\n".join(lines) + "\n")
    return 0


# ---------------------------------------------------------------------------
# Prometheus
# ---------------------------------------------------------------------------

def promql(prom_url: str, query: str, at: str) -> float | None:
    """Instant query evaluated at `at` (RFC 3339). None when there is no sample."""
    url = f"{prom_url}/api/v1/query?" + urllib.parse.urlencode({"query": query, "time": at})
    with urllib.request.urlopen(url, timeout=15) as response:
        payload = json.load(response)
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus error for {query!r}: {payload}")
    result = payload["data"]["result"]
    if not result:
        return None
    value = float(result[0]["value"][1])
    return None if math.isnan(value) else value


class Checks:
    def __init__(self, title: str) -> None:
        self.title = title
        self.rows: list[tuple[bool, str, str]] = []

    def check(self, ok: bool, what: str, detail: str) -> None:
        self.rows.append((ok, what, detail))
        print(f"    [{'OK' if ok else 'FAIL'}] {what}: {detail}", flush=True)
        if not ok:
            annotate("error", self.title, f"{what}: {detail}")

    def finish(self) -> int:
        lines = [f"### {self.title}", "", "| Result | Check | Measured |", "|---|---|---|"]
        lines += [f"| {'OK' if ok else '**FAIL**'} | {what} | {detail} |" for ok, what, detail in self.rows]
        append_summary("\n".join(lines) + "\n")
        return 0 if all(ok for ok, _, _ in self.rows) else 1


# ---------------------------------------------------------------------------
# Part E1
# ---------------------------------------------------------------------------

P95 = ('histogram_quantile(0.95, sum by (le) '
       '(rate(gym_http_request_duration_seconds_bucket{job="gym-tracker"}[1m])))')
FAULTS = 'sum(increase(gym_faults_injected_total{job="gym-tracker",kind="latency"}[1m]))'
STAGES = ("01-baseline", "02-fault", "03-recovery")


def cmd_e1(args: argparse.Namespace) -> int:
    results = Path(args.results_dir)
    checks = Checks("Part E1: latency fault through Prometheus")
    limit = args.threshold

    stages = {}
    for name in STAGES:
        path = results / f"{name}.json"
        if not path.exists():
            checks.check(False, f"{name} results written", f"{path} is missing")
            return checks.finish()
        stages[name] = json.loads(path.read_text())

    def healthz(name: str) -> dict:
        try:
            data = json.loads((results / name).read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    fault = healthz("02-fault-healthz.json")
    injector = fault.get("fault_injection") or {}
    checks.check(bool(injector.get("armed")), "fault armed during stage 2 (/healthz)",
                 f"status={fault.get('status', 'missing')}, latency_ms={injector.get('latency_ms')}, "
                 f"every_n={injector.get('latency_every_n')}")

    status = healthz("03-recovery-healthz.json").get("status", "missing")
    checks.check(status == "ok", "fault gone in stage 3 (/healthz status ok)", f"status={status}")

    for name, data in stages.items():
        checks.check(data.get("error_rate", 1) == 0, f"{name}: no client errors",
                     f"{data.get('errors')} errors in {data.get('requests')} requests")

    client_fault = stages["02-fault"]["latency_ms"]["p95"]
    checks.check(client_fault > limit * 1000, "02-fault: client-side p95 above threshold",
                 f"{client_fault:.1f} ms (threshold {limit * 1000:.0f} ms)")

    for name, data in stages.items():
        at = data.get("window", {}).get("to")
        if not at:
            checks.check(False, f"{name}: stage window recorded", "no window.to in results")
            continue
        try:
            p95 = promql(args.prom, P95, at)
            faults = promql(args.prom, FAULTS, at)
        except (OSError, RuntimeError, ValueError) as exc:
            checks.check(False, f"{name}: Prometheus query", str(exc))
            continue
        p95_text = "no data" if p95 is None else f"{p95 * 1000:.1f} ms"
        faults_text = "no data" if faults is None else f"{faults:.1f}"
        if name == "02-fault":
            checks.check(p95 is not None and p95 > limit, f"{name}: server-side p95 > {limit}s at {at}", p95_text)
            checks.check(faults is not None and faults > 0, f"{name}: latency faults counted", faults_text)
        else:
            checks.check(p95 is not None and p95 < limit, f"{name}: server-side p95 < {limit}s at {at}", p95_text)
            checks.check(faults is not None and faults == 0, f"{name}: no latency faults", faults_text)

    # Client-side recovery is reported, not enforced: the first requests to a
    # freshly recreated container are the ones a busy shared runner slows down.
    client_recovery = stages["03-recovery"]["latency_ms"]["p95"]
    if client_recovery >= limit * 1000:
        annotate("warning", "Part E1", f"03-recovery client-side p95 is {client_recovery:.1f} ms")
    return checks.finish()


# ---------------------------------------------------------------------------
# Part E2
# ---------------------------------------------------------------------------

ROW = re.compile(r"^\|\s*(With request_id label|Label removed, app restarted)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|")


def cmd_e2(args: argparse.Namespace) -> int:
    path = Path(args.results_dir) / "cardinality-stack.md"
    checks = Checks("Part E2: cardinality through Prometheus")
    if not path.exists():
        checks.check(False, "cardinality-stack.md written", f"{path} is missing")
        return checks.finish()
    rows = {}
    for line in path.read_text().splitlines():
        match = ROW.match(line)
        if match:
            rows[match.group(1)] = (match.group(2), match.group(3))
    before = rows.get("With request_id label", ("missing", "missing"))
    after = rows.get("Label removed, app restarted", ("missing", "missing"))
    checks.check(before[0] == str(args.requests), "with the label: one series per request",
                 f"count(demo_requests_total) = {before[0]} (expected {args.requests})")
    checks.check(before[1] == "1", "with the label: safe counter is one series",
                 f"count(demo_requests_safe_total) = {before[1]}")
    checks.check(after[0] == "none", "label removed: labelled series gone from instant queries",
                 f"count(demo_requests_total) = {after[0]}")
    checks.check(after[1] == "1", "label removed: safe counter still one series",
                 f"count(demo_requests_safe_total) = {after[1]}")
    return checks.finish()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    summary = sub.add_parser("summary")
    summary.add_argument("log")
    summary.add_argument("--title", default="quickstart verification")
    summary.set_defaults(func=cmd_summary)

    e1 = sub.add_parser("e1")
    e1.add_argument("results_dir")
    e1.add_argument("--prom", default=os.getenv("PROM_URL", "http://localhost:9090"))
    e1.add_argument("--threshold", type=float, default=0.3, help="seconds")
    e1.set_defaults(func=cmd_e1)

    e2 = sub.add_parser("e2")
    e2.add_argument("results_dir")
    e2.add_argument("--requests", type=int, default=int(os.getenv("REQUESTS", "100")))
    e2.set_defaults(func=cmd_e2)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
