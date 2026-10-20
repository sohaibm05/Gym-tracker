#!/usr/bin/env python3
"""Static checks for the observability configuration under observability/.

Catches the mistakes that otherwise only show up after `docker compose up`, as
an empty panel, a missing Kibana search or a silent Filebeat:

  * Grafana dashboards: valid JSON, unique uids and titles, unique panel ids,
    and every datasource reference resolves to a provisioned datasource uid.
  * PromQL in dashboards: every expression parses (needs promtool; see
    --promtool), and every gym_* metric it names exists in metrics.py.
  * Alert rules: unique alert names, gym_* metrics exist, and every alert has
    at least one unit test in observability/prometheus/alerts.test.yml.
  * Kibana saved objects: valid NDJSON, unique ids, and every saved search
    points at a data view that is in the same file.
  * Elasticsearch ILM policy: valid JSON with phases and actions.
  * Cross-file consistency: the paths in prometheus.yml, filebeat.yml and the
    Grafana provider match where docker-compose.yml mounts those files, and the
    Kibana data view, Filebeat template and Grafana datasource name the same
    index pattern.

Standard library only, so it runs before any dependency is installed:

    python scripts/check_observability_config.py
    python scripts/check_observability_config.py --promtool /path/to/promtool

Exits 0 when every check passes and 1 otherwise. Prometheus and Filebeat syntax
are checked by their own tools in CI (promtool check, filebeat test config);
this script covers what those tools cannot see.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OBS = ROOT / "observability"
COMPOSE = ROOT / "docker-compose.yml"
METRICS_PY = ROOT / "metrics.py"
DASHBOARDS = OBS / "grafana" / "dashboards"
DATASOURCES = OBS / "grafana" / "provisioning" / "datasources" / "datasources.yml"
DASHBOARD_PROVIDERS = OBS / "grafana" / "provisioning" / "dashboards" / "dashboards.yml"
PROMETHEUS_YML = OBS / "prometheus" / "prometheus.yml"
ALERTS = OBS / "prometheus" / "alerts.yml"
ALERT_TESTS = OBS / "prometheus" / "alerts.test.yml"
FILEBEAT = OBS / "filebeat" / "filebeat.yml"
ILM_POLICY = OBS / "elasticsearch" / "ilm-policy.json"
KIBANA_OBJECTS = OBS / "kibana" / "saved-objects.ndjson"

# Datasource references Grafana resolves itself, not from provisioning.
BUILTIN_DATASOURCE_UIDS = {"grafana", "-- Grafana --", "-- Mixed --", "-- Dashboard --"}

# Grafana's interval variables, replaced by a fixed duration so that a query
# using them still parses as PromQL.
GRAFANA_INTERVAL_VARS = re.compile(
    r"\$__(?:rate_interval|interval|range)(?:_ms|_s)?\b|\$\{__(?:rate_interval|interval|range)(?:_ms|_s)?\}"
)
OTHER_GRAFANA_VARS = re.compile(r"\$\{(\w+)(?::\w+)?\}|\$(\w+)|\[\[(\w+)\]\]")
GYM_METRIC = re.compile(r"\bgym_[a-z0-9_]+\b")
METRIC_SUFFIXES = ("_bucket", "_sum", "_count", "_created")


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self._section_start = 0

    def section(self, title: str) -> None:
        self._section_start = len(self.errors)
        print(f"\n== {title}")

    def clean(self) -> bool:
        """True if the current section has reported nothing so far."""
        return len(self.errors) == self._section_start

    def ok(self, message: str) -> None:
        print(f"  ok    {message}")

    def note(self, message: str) -> None:
        print(f"  note  {message}")

    def fail(self, where: Path | str, message: str) -> None:
        label = where.relative_to(ROOT).as_posix() if isinstance(where, Path) else where
        line = f"{label}: {message}"
        self.errors.append(line)
        print(f"  FAIL  {line}")


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def read_text(path: Path, report: Report) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        report.fail(path, f"cannot read: {exc}")
        return None


def load_json(path: Path, report: Report):
    text = read_text(path, report)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        report.fail(path, f"invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}")
        return None


# ---------------------------------------------------------------------------
# A deliberately tiny YAML reader for the provisioning files. It understands
# only what they use: a top-level key holding a block list of mappings, whose
# scalar fields sit one indent inside each item. Nested mappings (jsonData,
# options) are skipped. Anything fancier is out of scope; PyYAML is not a
# dependency of this project and this script must run without installing one.
# ---------------------------------------------------------------------------

def _scalar(raw: str) -> str:
    raw = raw.strip()
    if raw[:1] in {'"', "'"}:
        end = raw.find(raw[0], 1)
        return raw[1:end] if end > 0 else raw[1:]
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()


def yaml_list_of_mappings(text: str, key: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    in_list = False
    list_indent: int | None = None
    item: dict[str, str] | None = None
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent == 0:
            in_list = stripped == f"{key}:"
            item = None
            continue
        if not in_list:
            continue
        new_item = re.match(r"-\s+([A-Za-z_][\w.]*):(.*)$", stripped)
        if new_item and (list_indent is None or indent == list_indent):
            list_indent = indent
            item = {new_item.group(1): _scalar(new_item.group(2))}
            items.append(item)
            continue
        if item is not None and list_indent is not None and indent == list_indent + 2:
            field = re.match(r"([A-Za-z_][\w.]*):(.*)$", stripped)
            if field:
                item[field.group(1)] = _scalar(field.group(2))
    return items


def compose_mount_target(compose_text: str, source: str) -> str | None:
    """Container path that docker-compose.yml bind-mounts ./<source> to."""
    pattern = re.compile(rf"^\s*-\s*\"?\./{re.escape(source)}:([^:\s\"]+)", re.MULTILINE)
    match = pattern.search(compose_text)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Grafana
# ---------------------------------------------------------------------------

def check_datasources(report: Report) -> dict[str, dict[str, str]]:
    report.section("Grafana datasources")
    text = read_text(DATASOURCES, report)
    if text is None:
        return {}
    sources = yaml_list_of_mappings(text, "datasources")
    if not sources:
        report.fail(DATASOURCES, "no datasources found")
        return {}
    by_uid: dict[str, dict[str, str]] = {}
    names: set[str] = set()
    for source in sources:
        name = source.get("name", "?")
        for required in ("name", "type", "uid"):
            if not source.get(required):
                report.fail(DATASOURCES, f"datasource {name!r} has no {required}")
        uid = source.get("uid")
        if uid:
            if uid in by_uid:
                report.fail(DATASOURCES, f"duplicate datasource uid {uid!r}")
            by_uid[uid] = source
        if name in names:
            report.fail(DATASOURCES, f"duplicate datasource name {name!r}")
        names.add(name)
    defaults = [s.get("name") for s in sources if s.get("isDefault", "").lower() == "true"]
    if len(defaults) != 1:
        report.fail(DATASOURCES, f"expected exactly one isDefault datasource, found {defaults or 'none'}")
    if report.clean():
        summary = ", ".join(f"{uid} ({s.get('type')})" for uid, s in by_uid.items())
        report.ok(f"{len(sources)} datasources: {summary}")
    return by_uid


def iter_panels(panels):
    for panel in panels or []:
        yield panel
        # Collapsed rows carry their panels inside the row.
        yield from iter_panels(panel.get("panels"))


def resolve_datasource(ref, by_uid, default, variables, where, report):
    """Return the datasource type a reference points at, reporting bad ones."""
    if ref is None:
        return default.get("type") if default else None
    if isinstance(ref, str):
        if ref.startswith("$"):
            name = ref.strip("${}").split(":")[0]
            if name not in variables:
                report.fail(where, f"datasource {ref!r} names no dashboard variable")
            return None
        matches = [s for s in by_uid.values() if s.get("name") == ref] or [by_uid.get(ref)]
        if matches[0] is None and ref not in BUILTIN_DATASOURCE_UIDS:
            report.fail(where, f"datasource {ref!r} is not a provisioned datasource name or uid")
            return None
        return matches[0].get("type") if matches[0] else None
    if isinstance(ref, dict):
        uid, kind = ref.get("uid"), ref.get("type")
        if uid is None:
            return kind or (default.get("type") if default else None)
        if uid in BUILTIN_DATASOURCE_UIDS or kind == "datasource":
            return kind
        if isinstance(uid, str) and uid.startswith("$"):
            name = uid.strip("${}").split(":")[0]
            if name not in variables:
                report.fail(where, f"datasource uid {uid!r} names no dashboard variable")
            return kind
        source = by_uid.get(uid)
        if source is None:
            report.fail(where, f"datasource uid {uid!r} is not provisioned in {rel(DATASOURCES)}")
            return kind
        if kind and kind != source.get("type"):
            report.fail(where, f"datasource uid {uid!r} is type {source.get('type')!r}, panel says {kind!r}")
        return source.get("type")
    report.fail(where, f"unrecognised datasource reference {ref!r}")
    return None


def check_dashboards(report: Report, by_uid) -> list[tuple[str, str]]:
    """Validate every dashboard; return (record-name, expr) for each PromQL target."""
    report.section("Grafana dashboards")
    files = sorted(DASHBOARDS.glob("*.json"))
    if not files:
        report.fail(DASHBOARDS, "no dashboard JSON files found")
        return []
    default = next((s for s in by_uid.values() if s.get("isDefault", "").lower() == "true"), None)
    seen_uids: dict[str, Path] = {}
    seen_titles: dict[str, Path] = {}
    exprs: list[tuple[str, str]] = []
    for path in files:
        before = len(report.errors)
        dashboard = load_json(path, report)
        if not isinstance(dashboard, dict):
            if dashboard is not None:
                report.fail(path, "top level is not a JSON object")
            continue
        if "dashboard" in dashboard and "panels" not in dashboard:
            report.fail(path, "looks like an API export wrapper; provision the inner 'dashboard' object")
            continue
        uid, title = dashboard.get("uid"), dashboard.get("title")
        if not uid:
            report.fail(path, "no uid (Grafana would generate a random one and links would break)")
        elif uid in seen_uids:
            report.fail(path, f"uid {uid!r} already used by {rel(seen_uids[uid])}")
        else:
            seen_uids[uid] = path
            if len(uid) > 40:
                report.fail(path, f"uid {uid!r} is longer than Grafana's 40-character limit")
        if not title:
            report.fail(path, "no title")
        elif title in seen_titles:
            report.fail(path, f"title {title!r} already used by {rel(seen_titles[title])} (same folder)")
        else:
            seen_titles[title] = path
        if "__inputs" in dashboard:
            report.fail(path, "has __inputs, which file provisioning cannot fill in")

        templating = dashboard.get("templating", {}).get("list", [])
        variables = {v.get("name") for v in templating if v.get("name")}
        for variable in templating:
            resolve_datasource(variable.get("datasource"), by_uid, default, variables,
                               f"{rel(path)} variable {variable.get('name')!r}", report)
        for annotation in dashboard.get("annotations", {}).get("list", []):
            resolve_datasource(annotation.get("datasource"), by_uid, default, variables,
                               f"{rel(path)} annotation {annotation.get('name')!r}", report)

        panel_ids: set = set()
        panel_count = target_count = 0
        for panel in iter_panels(dashboard.get("panels")):
            panel_count += 1
            pid = panel.get("id")
            where = f"{rel(path)} panel {pid} {panel.get('title')!r}"
            if pid is None:
                report.fail(where, "panel has no id")
            elif pid in panel_ids:
                report.fail(where, f"duplicate panel id {pid}")
            panel_ids.add(pid)
            panel_type = resolve_datasource(panel.get("datasource"), by_uid, default, variables,
                                            where, report)
            for target in panel.get("targets") or []:
                target_count += 1
                ref_id = target.get("refId", "?")
                twhere = f"{where} target {ref_id}"
                ttype = resolve_datasource(target.get("datasource"), by_uid, default, variables,
                                           twhere, report)
                kind = ttype or panel_type
                expr = target.get("expr")
                if kind == "prometheus":
                    if not expr or not str(expr).strip():
                        report.fail(twhere, "Prometheus target has an empty expr")
                        continue
                    name = re.sub(r"[^A-Za-z0-9_:]", "_", f"dashboard:{uid}:panel_{pid}:{ref_id}")
                    exprs.append((name, str(expr)))
        if len(report.errors) == before:
            report.ok(f"{rel(path)}: uid={uid!r}, {panel_count} panels, {target_count} targets")
    return exprs


def promql_for_parsing(expr: str) -> str:
    expr = GRAFANA_INTERVAL_VARS.sub("5m", expr)
    return OTHER_GRAFANA_VARS.sub("grafana_variable", expr)


def check_promql_parses(report: Report, exprs, promtool: str | None, require: bool) -> None:
    report.section("PromQL in dashboards parses")
    if not exprs:
        report.note("no Prometheus expressions found")
        return
    if not promtool:
        message = "promtool not found; skipped (pass --promtool PATH or put it on PATH)"
        if require:
            report.fail("promtool", message)
        else:
            report.note(message)
        return
    # A recording-rule file is the one input promtool parses PromQL from
    # offline. JSON is valid YAML, so no YAML library is needed to write it.
    rules = [{"record": name, "expr": promql_for_parsing(expr)} for name, expr in exprs]
    document = {"groups": [{"name": "grafana-dashboard-expressions", "rules": rules}]}
    with tempfile.TemporaryDirectory() as tmp:
        rules_file = Path(tmp) / "dashboard-exprs.rules.yml"
        rules_file.write_text(json.dumps(document, indent=2), encoding="utf-8")
        try:
            result = subprocess.run([promtool, "check", "rules", "--lint=none", str(rules_file)],
                                    capture_output=True, text=True, check=False, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            report.fail("promtool", f"could not run {promtool}: {exc}")
            return
    if result.returncode == 0:
        report.ok(f"{len(exprs)} expressions parse ({promtool})")
    else:
        output = (result.stdout + result.stderr).strip()
        report.fail("dashboards",
                    "PromQL parse errors (rule names are dashboard:<uid>:panel_<id>:<refId>):\n" + output)


def defined_gym_metrics(report: Report) -> set[str]:
    text = read_text(METRICS_PY, report)
    if text is None:
        return set()
    names = set(re.findall(r"[\"'](gym_[a-z0-9_]+)[\"']", text))
    if not names:
        report.fail(METRICS_PY, "no gym_* metric names found; has the module moved?")
    return names


def unknown_gym_metrics(used: set[str], defined: set[str]) -> set[str]:
    unknown = set()
    for name in used:
        candidates = {name}
        candidates.update(name[: -len(s)] for s in METRIC_SUFFIXES if name.endswith(s))
        if name.endswith("_total"):
            candidates.add(name[: -len("_total")])
        if not candidates & defined:
            unknown.add(name)
    return unknown


def check_dashboard_metrics(report: Report, exprs, defined: set[str]) -> None:
    report.section("Dashboard metrics exist in metrics.py")
    used = {m for _, expr in exprs for m in GYM_METRIC.findall(expr)}
    unknown = unknown_gym_metrics(used, defined)
    for name in sorted(unknown):
        holders = sorted({n for n, e in exprs if name in GYM_METRIC.findall(e)})
        report.fail("dashboards", f"{name} is not defined in metrics.py (used by {', '.join(holders)})")
    if report.clean():
        report.ok(f"{len(used)} distinct gym_* series names, all defined")


def check_provider(report: Report, compose_text: str) -> None:
    report.section("Grafana dashboard provider")
    text = read_text(DASHBOARD_PROVIDERS, report)
    if text is None:
        return
    paths = re.findall(r"^\s*path:\s*(\S+)", text, re.MULTILINE)
    mount = compose_mount_target(compose_text, "observability/grafana/dashboards")
    if not paths:
        report.fail(DASHBOARD_PROVIDERS, "no provider options.path")
    elif mount is None:
        report.fail(COMPOSE, "observability/grafana/dashboards is not bind-mounted into grafana")
    elif _scalar(paths[0]) != mount:
        report.fail(DASHBOARD_PROVIDERS,
                    f"path {paths[0]!r} but docker-compose.yml mounts the dashboards at {mount!r}")
    else:
        report.ok(f"provider path {mount} matches the docker-compose.yml mount")


# ---------------------------------------------------------------------------
# Prometheus alert rules
# ---------------------------------------------------------------------------

def check_alerts(report: Report, defined: set[str], compose_text: str) -> None:
    report.section("Prometheus alert rules")
    text = read_text(ALERTS, report)
    if text is None:
        return
    names = re.findall(r"^\s*-\s*alert:\s*(\S+)", text, re.MULTILINE)
    if not names:
        report.fail(ALERTS, "no alert rules found")
    duplicates = sorted({n for n in names if names.count(n) > 1})
    for name in duplicates:
        report.fail(ALERTS, f"alert name {name!r} is defined more than once")

    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for name in sorted(unknown_gym_metrics(set(GYM_METRIC.findall(code)), defined)):
        report.fail(ALERTS, f"{name} is not defined in metrics.py")

    tests = read_text(ALERT_TESTS, report)
    if tests is not None:
        tested = set(re.findall(r"^\s*alertname:\s*(\S+)", tests, re.MULTILINE))
        for name in names:
            if name not in tested:
                report.fail(ALERT_TESTS, f"alert {name!r} has no alert_rule_test")
        for name in sorted(tested - set(names)):
            report.fail(ALERT_TESTS, f"tests alert {name!r}, which alerts.yml does not define")

    prom = read_text(PROMETHEUS_YML, report) or ""
    rule_block = re.search(r"^rule_files:\s*\n((?:\s+-\s*.+\n?)+)", prom, re.MULTILINE)
    rule_files = re.findall(r"-\s*(\S+)", rule_block.group(1)) if rule_block else []
    mount = compose_mount_target(compose_text, "observability/prometheus/alerts.yml")
    if mount is None:
        report.fail(COMPOSE, "observability/prometheus/alerts.yml is not bind-mounted into prometheus")
    elif [_scalar(f) for f in rule_files] != [mount]:
        report.fail(PROMETHEUS_YML,
                    f"rule_files {rule_files} but docker-compose.yml mounts alerts.yml at {mount!r}")

    if report.clean():
        report.ok(f"{len(names)} alerts, unique names, each covered by {rel(ALERT_TESTS)}; "
                  "rule_files matches the mount")


# ---------------------------------------------------------------------------
# Logs: Filebeat, Elasticsearch, Kibana
# ---------------------------------------------------------------------------

def check_ilm_policy(report: Report) -> None:
    report.section("Elasticsearch ILM policy")
    policy = load_json(ILM_POLICY, report)
    if policy is None:
        return
    phases = policy.get("policy", {}).get("phases") if isinstance(policy, dict) else None
    if not isinstance(phases, dict) or not phases:
        report.fail(ILM_POLICY, "expected {\"policy\": {\"phases\": {...}}}")
        return
    for phase, body in phases.items():
        if not isinstance(body, dict) or not isinstance(body.get("actions"), dict):
            report.fail(ILM_POLICY, f"phase {phase!r} has no actions object")
    if "rollover" not in phases.get("hot", {}).get("actions", {}):
        report.fail(ILM_POLICY, "hot phase has no rollover action, so the rollover alias never rolls over")
    if report.clean():
        report.ok(f"valid JSON, phases: {', '.join(phases)}")


def check_kibana_objects(report: Report) -> str | None:
    """Validate the saved objects; return the data view title."""
    report.section("Kibana saved objects")
    text = read_text(KIBANA_OBJECTS, report)
    if text is None:
        return None
    objects = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            report.fail(KIBANA_OBJECTS, f"line {number} is not valid JSON: {exc.msg}")
            continue
        if not isinstance(obj, dict) or not obj.get("type") or not obj.get("id") or "attributes" not in obj:
            report.fail(KIBANA_OBJECTS, f"line {number} is not a saved object (needs type, id, attributes)")
            continue
        objects.append(obj)
    ids = [(o["type"], o["id"]) for o in objects]
    for duplicate in sorted({i for i in ids if ids.count(i) > 1}):
        report.fail(KIBANA_OBJECTS, f"duplicate saved object {duplicate}")

    views = {o["id"]: o for o in objects if o["type"] == "index-pattern"}
    searches = [o for o in objects if o["type"] == "search"]
    if not views:
        report.fail(KIBANA_OBJECTS, "no index-pattern (data view) object")
    for search in searches:
        where = f"{rel(KIBANA_OBJECTS)} search {search['id']!r}"
        refs = {r.get("name"): r for r in search.get("references", [])}
        meta = search["attributes"].get("kibanaSavedObjectMeta", {}).get("searchSourceJSON", "{}")
        try:
            source = json.loads(meta)
        except json.JSONDecodeError as exc:
            report.fail(where, f"searchSourceJSON is not valid JSON: {exc.msg}")
            continue
        ref_name = source.get("indexRefName")
        ref = refs.get(ref_name)
        if not ref_name or ref is None:
            report.fail(where, f"indexRefName {ref_name!r} matches no entry in references")
        elif ref.get("type") != "index-pattern" or ref.get("id") not in views:
            report.fail(where, f"references data view {ref.get('id')!r}, which is not in this file")
    title = None
    if views:
        title = next(iter(views.values()))["attributes"].get("title")
    if report.clean():
        report.ok(f"{len(objects)} objects: {len(views)} data view(s) {sorted(views)}, "
                  f"{len(searches)} saved searches, all pointing at a data view in the file")
    return title


def check_log_pipeline_consistency(report: Report, compose_text: str, data_view_title: str | None,
                                   by_uid) -> None:
    report.section("Filebeat / Elasticsearch / Kibana / Grafana agree")
    text = read_text(FILEBEAT, report)
    if text is None:
        return
    pattern = re.search(r"^setup\.template\.pattern:\s*(.+)$", text, re.MULTILINE)
    policy_file = re.search(r"^setup\.ilm\.policy_file:\s*(.+)$", text, re.MULTILINE)
    template_pattern = _scalar(pattern.group(1)) if pattern else None
    if template_pattern is None:
        report.fail(FILEBEAT, "no setup.template.pattern")
    if data_view_title and template_pattern and data_view_title != template_pattern:
        report.fail(KIBANA_OBJECTS,
                    f"data view title {data_view_title!r} != Filebeat template pattern {template_pattern!r}")
    es_sources = [s for s in by_uid.values() if s.get("type") == "elasticsearch"]
    for source in es_sources:
        database = source.get("database")
        if database and template_pattern and database != template_pattern:
            report.fail(DATASOURCES, f"Elasticsearch datasource index {database!r} "
                                     f"!= Filebeat template pattern {template_pattern!r}")
    mount = compose_mount_target(compose_text, "observability/elasticsearch/ilm-policy.json")
    if policy_file is None:
        report.fail(FILEBEAT, "no setup.ilm.policy_file")
    elif mount is None:
        report.fail(COMPOSE, "ilm-policy.json is not bind-mounted into filebeat")
    elif _scalar(policy_file.group(1)) != mount:
        report.fail(FILEBEAT, f"setup.ilm.policy_file {policy_file.group(1)!r} "
                              f"but docker-compose.yml mounts it at {mount!r}")
    if report.clean():
        report.ok(f"index pattern {template_pattern!r} everywhere; "
                  f"ILM policy path matches the mount ({mount})")


# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--promtool", default=shutil.which("promtool"),
                        help="promtool binary used to parse dashboard PromQL (default: from PATH)")
    parser.add_argument("--require-promtool", action="store_true",
                        help="fail instead of skipping the PromQL parse check when promtool is missing")
    args = parser.parse_args(argv)

    report = Report()
    compose_text = read_text(COMPOSE, report) or ""
    by_uid = check_datasources(report)
    exprs = check_dashboards(report, by_uid)
    check_promql_parses(report, exprs, args.promtool, args.require_promtool)
    defined = defined_gym_metrics(report)
    check_dashboard_metrics(report, exprs, defined)
    check_provider(report, compose_text)
    check_alerts(report, defined, compose_text)
    check_ilm_policy(report)
    title = check_kibana_objects(report)
    check_log_pipeline_consistency(report, compose_text, title, by_uid)

    print()
    if report.errors:
        print(f"{len(report.errors)} problem(s) found:")
        for error in report.errors:
            print(f"  - {error}")
        return 1
    print("All observability config checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
