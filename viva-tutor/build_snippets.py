#!/usr/bin/env python3
"""Generate viva-tutor/snippets.js from the real source, and check the tutor.

The tutor never hardcodes a line number or copies code by hand. Every code
excerpt and every `file:line` it shows is located here by searching the actual
file for an anchor, so after the code changes, re-running this script moves the
references with it.

    python viva-tutor/build_snippets.py           # regenerate snippets.js
    python viva-tutor/build_snippets.py --check   # regenerate in memory and
                                                  # validate the content files

--check fails (exit 1) if:
  * an anchor no longer matches anything in its file,
  * snippets.js on disk is stale,
  * a content file names a metric that metrics.py does not declare,
  * a content file names a repository file that does not exist,
  * a query marked as "copied from a dashboard/alert" is not in that file,
  * a [[ref]] placeholder in a content file has no matching anchor.

Standard library only. Nothing here is imported by the application.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "snippets.js"

# ---------------------------------------------------------------------------
# Single-line references: name -> (file, regex, occurrence, line offset)
# The regex is searched line by line; `occurrence` is 1-based.
# ---------------------------------------------------------------------------

REFS: dict[str, tuple] = {
    # --- app.py: wiring
    "app_configure_logging": ("app.py", r"logging_setup\.configure_logging\(", 1, 0),
    "app_metrics_singleton": ("app.py", r"^METRICS = app_metrics\.METRICS", 1, 0),
    "app_faults_init": ("app.py", r"^FAULTS = faults\.FaultInjector", 1, 0),
    "app_build_info": ("app.py", r"app_metrics\.set_build_info\(\)", 1, 0),
    "app_rid_header_const": ("app.py", r"^REQUEST_ID_HEADER = ", 1, 0),
    "app_inbound_rid": ("app.py", r"def _inbound_request_id", 1, 0),
    # --- app.py: the middleware
    "mw_decorator": ("app.py", r'@app\.middleware\("http"\)', 1, 0),
    "mw_def": ("app.py", r"async def log_requests", 1, 0),
    "mw_set_rid": ("app.py", r"logging_setup\.set_request_id\(", 1, 0),
    "inflight_inc": ("app.py", r"METRICS\.http_requests_in_flight\.inc\(\)", 1, 0),
    "timer_start": ("app.py", r"started = time\.perf_counter\(\)", 1, 0),
    "fault_call": ("app.py", r"await FAULTS\.before_request", 1, 0),
    "exc_observe": ("app.py", r"METRICS\.http_request_duration\.labels\(", 1, 0),
    "exc_count": ("app.py", r"METRICS\.http_requests_total\.labels\(", 1, 0),
    "exc_exceptions": ("app.py", r"METRICS\.http_exceptions_total\.labels\(", 1, 0),
    "log_failed": ("app.py", r"logger\.exception\(", 1, 0),
    "inflight_dec": ("app.py", r"METRICS\.http_requests_in_flight\.dec\(\)", 1, 0),
    "ok_observe": ("app.py", r"METRICS\.http_request_duration\.labels\(", 2, 0),
    "ok_count": ("app.py", r"METRICS\.http_requests_total\.labels\(", 2, 0),
    "log_served": ("app.py", r'"request served",', 1, -1),
    "rid_header_set": ("app.py", r"response\.headers\[REQUEST_ID_HEADER\]", 1, 0),
    # --- app.py: routes
    "login_route": ("app.py", r'@app\.post\("/login"', 1, 0),
    "login_rl_inc": ("app.py", r'logins_total\.labels\(result="rate_limited"\)', 1, 0),
    "log_login_rl": ("app.py", r'logger\.warning\("login rate limited"', 1, 0),
    "login_fail_inc": ("app.py", r'logins_total\.labels\(result="failure"\)', 1, 0),
    "log_login_fail": ("app.py", r'logger\.warning\("login failed"', 1, 0),
    "login_ok_inc": ("app.py", r'logins_total\.labels\(result="success"\)', 1, 0),
    "log_login_ok": ("app.py", r'"login succeeded",', 1, -1),
    "healthz_route": ("app.py", r'@app\.get\("/healthz"\)', 1, 0),
    "metrics_route": ("app.py", r'@app\.get\("/metrics"\)', 1, 0),
    "metrics_token": ("app.py", r"hmac\.compare_digest\(offered", 1, 0),
    "log_route": ("app.py", r'@app\.post\("/log"', 1, 0),
    "entry_bytes_observe": ("app.py", r"METRICS\.entry_text_bytes\.observe", 1, 0),
    "drafts_opened": ("app.py", r"DRAFTS\.opened\(", 1, 0),
    "log_drafted": ("app.py", r'"journal entry drafted",', 1, -1),
    "save_route": ("app.py", r'@app\.post\("/save"', 1, 0),
    "blocked_inc": ("app.py", r'entries_total\.labels\(outcome="blocked"\)', 1, 0),
    "log_blocked": ("app.py", r'"save blocked by the review form",', 1, -1),
    "overridden_inc": ("app.py", r'decision="overridden"', 1, 0),
    "commit_timer": ("app.py", r"observe_duration\(METRICS\.db_commit_duration\)", 1, 0),
    "commit_call": ("app.py", r"result = pipeline\.commit_draft\(", 1, 0),
    "dup_held_inc": ("app.py", r'entries_total\.labels\(outcome="duplicate_held"\)', 1, 0),
    "dup_decision_held": ("app.py", r'decision="held"', 1, 0),
    "saved_inc": ("app.py", r'entries_total\.labels\(outcome="saved"\)', 1, 0),
    "sets_inc": ("app.py", r"METRICS\.sets_written_total\.inc\(", 1, 0),
    "bw_inc": ("app.py", r"METRICS\.bodyweight_written_total\.inc\(", 1, 0),
    "spe_observe": ("app.py", r"METRICS\.sets_per_entry\.observe\(", 1, 0),
    "drafts_closed": ("app.py", r"DRAFTS\.closed\(", 1, 0),
    "log_saved": ("app.py", r'"journal entry saved",', 1, -1),
    "progress_route": ("app.py", r'@app\.get\("/progress"', 1, 0),
    "weekly_inc": ("app.py", r"METRICS\.weekly_reports_total\.labels\(", 1, 0),
    "log_weekly": ("app.py", r'"weekly report generated",', 1, -1),
    # --- metrics.py
    "m_noop": ("metrics.py", r"class _NoopMetric", 1, 0),
    "m_buckets_http": ("metrics.py", r"^HTTP_DURATION_BUCKETS = \(", 1, 0),
    "m_buckets_llm": ("metrics.py", r"^LLM_DURATION_BUCKETS = ", 1, 0),
    "m_buckets_db": ("metrics.py", r"^DB_DURATION_BUCKETS = ", 1, 0),
    "m_label_values": ("metrics.py", r"^ENTRY_OUTCOMES = ", 1, 0),
    "m_class": ("metrics.py", r"^class Metrics:", 1, 0),
    "m_http_requests_total": ("metrics.py", r"self\.http_requests_total = Counter\(", 1, 0),
    "m_http_exceptions_total": ("metrics.py", r"self\.http_exceptions_total = Counter\(", 1, 0),
    "m_entries_total": ("metrics.py", r"self\.entries_total = Counter\(", 1, 0),
    "m_sets_written_total": ("metrics.py", r"self\.sets_written_total = Counter\(", 1, 0),
    "m_bodyweight_written_total": ("metrics.py", r"self\.bodyweight_written_total = Counter\(", 1, 0),
    "m_duplicate_decisions_total": ("metrics.py", r"self\.duplicate_decisions_total = Counter\(", 1, 0),
    "m_llm_extractions_total": ("metrics.py", r"self\.llm_extractions_total = Counter\(", 1, 0),
    "m_weekly_reports_total": ("metrics.py", r"self\.weekly_reports_total = Counter\(", 1, 0),
    "m_logins_total": ("metrics.py", r"self\.logins_total = Counter\(", 1, 0),
    "m_faults_injected_total": ("metrics.py", r"self\.faults_injected_total = Counter\(", 1, 0),
    "m_zero_init": ("metrics.py", r"for counter, values in \(", 1, 0),
    "m_http_requests_in_flight": ("metrics.py", r"self\.http_requests_in_flight = Gauge\(", 1, 0),
    "m_llm_extractions_in_flight": ("metrics.py", r"self\.llm_extractions_in_flight = Gauge\(", 1, 0),
    "m_review_drafts_open": ("metrics.py", r"self\.review_drafts_open = Gauge\(", 1, 0),
    "m_build_info": ("metrics.py", r"self\.build_info = Gauge\(", 1, 0),
    "m_http_request_duration": ("metrics.py", r"self\.http_request_duration = Histogram\(", 1, 0),
    "m_llm_extraction_duration": ("metrics.py", r"self\.llm_extraction_duration = Histogram\(", 1, 0),
    "m_db_commit_duration": ("metrics.py", r"self\.db_commit_duration = Histogram\(", 1, 0),
    "m_entry_text_bytes": ("metrics.py", r"self\.entry_text_bytes = Summary\(", 1, 0),
    "m_sets_per_entry": ("metrics.py", r"self\.sets_per_entry = Summary\(", 1, 0),
    "m_singleton": ("metrics.py", r"^METRICS = Metrics\(\)", 1, 0),
    "m_set_build_info": ("metrics.py", r"^def set_build_info", 1, 0),
    "m_unmatched": ("metrics.py", r'^UNMATCHED_ROUTE = ', 1, 0),
    "m_route_label": ("metrics.py", r"^def route_label", 1, 0),
    "m_draft_tracker": ("metrics.py", r"^class DraftTracker", 1, 0),
    "m_draft_dict": ("metrics.py", r"self\._open: ", 1, 0),
    "m_draft_publish": ("metrics.py", r"self\._gauge\.set\(len\(self\._open\)\)", 1, 0),
    "m_drafts": ("metrics.py", r"^DRAFTS = DraftTracker", 1, 0),
    "m_observe_duration": ("metrics.py", r"^class observe_duration", 1, 0),
    "m_render_latest": ("metrics.py", r"^def render_latest", 1, 0),
    # --- logging_setup.py
    "l_rid_var": ("logging_setup.py", r"^request_id_var: ContextVar", 1, 0),
    "l_new_rid": ("logging_setup.py", r"^def new_request_id", 1, 0),
    "l_sensitive_keys": ("logging_setup.py", r"^SENSITIVE_KEYS = frozenset", 1, 0),
    "l_value_patterns": ("logging_setup.py", r"^_VALUE_PATTERNS = \(", 1, 0),
    "l_scrub_value": ("logging_setup.py", r"^def scrub_value", 1, 0),
    "l_filter": ("logging_setup.py", r"^class SensitiveDataFilter", 1, 0),
    "l_reserved": ("logging_setup.py", r"^_RESERVED = frozenset", 1, 0),
    "l_formatter": ("logging_setup.py", r"^class EcsJsonFormatter", 1, 0),
    "l_format": ("logging_setup.py", r"    def format\(self, record: logging\.LogRecord\) -> str:", 1, 0),
    "l_add_rid": ("logging_setup.py", r'document\["http\.request\.id"\] = request_id', 1, 0),
    "l_extras_loop": ("logging_setup.py", r"for key, value in record\.__dict__\.items\(\):", 1, 0),
    "l_extras_scrub": ("logging_setup.py", r"document\[key\] = scrub_value\(key, value\)", 1, 0),
    "l_dumps": ("logging_setup.py", r"return json\.dumps\(document", 1, 0),
    "l_configure": ("logging_setup.py", r"^def configure_logging", 1, 0),
    "l_add_filter": ("logging_setup.py", r"handler\.addFilter\(SensitiveDataFilter\(\)\)", 1, 0),
    "l_uvicorn": ("logging_setup.py", r'for name in \("uvicorn", "uvicorn\.error", "uvicorn\.access"\)', 1, 0),
    # --- faults.py
    "f_max": ("faults.py", r"^MAX_LATENCY_MS = ", 1, 0),
    "f_class": ("faults.py", r"^class FaultInjector", 1, 0),
    "f_prod_refuse": ("faults.py", r'"fault injection refused: APP_ENV is production"', 1, -1),
    "f_armed_log": ("faults.py", r'"FAULT INJECTION ARMED', 1, -1),
    "f_armed": ("faults.py", r"def armed\(self\) -> bool:", 1, 0),
    "f_record_inc": ("faults.py", r"faults_injected_total\.labels\(kind=kind\)\.inc\(\)", 1, 0),
    "f_before": ("faults.py", r"async def before_request", 1, 0),
    "f_sequence": ("faults.py", r"sequence = next\(self\._counter\)", 1, 0),
    "f_error_branch": ("faults.py", r"if self\.error_every_n and sequence % self\.error_every_n == 0:", 1, 0),
    "f_latency_branch": ("faults.py", r"if self\.latency_ms > 0 and sequence % self\.latency_every_n == 0:", 1, 0),
    "f_latency_log": ("faults.py", r'"injected fault: delaying this request deliberately",', 1, -1),
    "f_error_log": ("faults.py", r'"injected fault: failing this request deliberately",', 1, -1),
    "f_sleep": ("faults.py", r"await asyncio\.sleep\(self\.latency_ms / 1000\.0\)", 1, 0),
    "f_describe": ("faults.py", r"def describe\(self\)", 1, 0),
    # --- pipeline.py
    "p_import_metrics": ("pipeline.py", r"from metrics import METRICS as _METRICS", 1, 0),
    "p_record_extraction": ("pipeline.py", r"^def _record_extraction", 1, 0),
    "p_llm_inc": ("pipeline.py", r"_METRICS\.llm_extractions_total\.labels\(outcome=outcome\)\.inc\(\)", 1, 0),
    "p_llm_observe": ("pipeline.py", r"_METRICS\.llm_extraction_duration\.observe\(seconds\)", 1, 0),
    "p_inflight_inc": ("pipeline.py", r"_METRICS\.llm_extractions_in_flight\.inc\(\)", 1, 0),
    "p_inflight_dec": ("pipeline.py", r"_METRICS\.llm_extractions_in_flight\.dec\(\)", 1, 0),
    "p_attempt_failed": ("pipeline.py", r'"extraction attempt failed",', 1, -1),
    "p_unavailable": ("pipeline.py", r'"extraction unavailable: GROQ_API_KEY is not set",', 1, -1),
    # --- cardinality_demo.py
    "c_max": ("observability/cardinality_demo.py", r"^MAX_UNIQUE_IDS = ", 1, 0),
    "c_unsafe": ("observability/cardinality_demo.py", r"^UNSAFE = Counter\(", 1, 0),
    "c_safe": ("observability/cardinality_demo.py", r"^SAFE = Counter\(", 1, 0),
    "c_state": ("observability/cardinality_demo.py", r"^class DemoState", 1, 0),
    "c_seen": ("observability/cardinality_demo.py", r"self\.seen_ids: set\[str\] = set\(\)", 1, 0),
    "c_record": ("observability/cardinality_demo.py", r"def record\(self, endpoint", 1, 0),
    "c_cap": ("observability/cardinality_demo.py", r"if len\(self\.seen_ids\) >= self\.max_ids:", 1, 0),
    "c_unsafe_inc": ("observability/cardinality_demo.py", r"UNSAFE\.labels\(endpoint=endpoint, request_id=request_id\)\.inc\(\)", 1, 0),
    "c_print": ("observability/cardinality_demo.py", r'"demo request served"', 1, -1),
    # --- configuration
    "dc_postgres": ("docker-compose.yml", r"^  postgres:", 1, 0),
    "dc_app": ("docker-compose.yml", r"^  app:", 1, 0),
    "dc_logging": ("docker-compose.yml", r'max-size: "10m"', 1, 0),
    "dc_label": ("docker-compose.yml", r'co\.elastic\.logs/enabled: "true"', 1, 0),
    "dc_prometheus": ("docker-compose.yml", r"^  prometheus:", 1, 0),
    "dc_retention": ("docker-compose.yml", r"--storage\.tsdb\.retention\.time=15d", 1, 0),
    "dc_node_exporter": ("docker-compose.yml", r"^  node-exporter:", 1, 0),
    "dc_grafana": ("docker-compose.yml", r"^  grafana:", 1, 0),
    "dc_elasticsearch": ("docker-compose.yml", r"^  elasticsearch:", 1, 0),
    "dc_kibana": ("docker-compose.yml", r"^  kibana:", 1, 0),
    "dc_filebeat": ("docker-compose.yml", r"^  filebeat:", 1, 0),
    "dc_filebeat_registry": ("docker-compose.yml", r"filebeat-data:/usr/share/filebeat/data", 1, 0),
    "dc_cardinality": ("docker-compose.yml", r"^  cardinality-demo:", 1, 0),
    "dc_volumes": ("docker-compose.yml", r"^volumes:", 1, 0),
    "dc_network": ("docker-compose.yml", r"^networks:", 1, 0),
    "prom_scrape_interval": ("observability/prometheus/prometheus.yml", r"scrape_interval: 15s", 1, 0),
    "prom_job_app": ("observability/prometheus/prometheus.yml", r"job_name: gym-tracker", 1, 0),
    "prom_job_node": ("observability/prometheus/prometheus.yml", r"job_name: node-exporter", 1, 0),
    "prom_job_card": ("observability/prometheus/prometheus.yml", r"job_name: cardinality-demo", 1, 0),
    "alert_target_down": ("observability/prometheus/alerts.yml", r"alert: TargetDown", 1, 0),
    "alert_error_rate": ("observability/prometheus/alerts.yml", r"alert: HighErrorRate", 1, 0),
    "alert_slow": ("observability/prometheus/alerts.yml", r"alert: SlowRequests", 1, 0),
    "alert_fault": ("observability/prometheus/alerts.yml", r"alert: FaultInjectionActive", 1, 0),
    "fb_autodiscover": ("observability/filebeat/filebeat.yml", r"^filebeat\.autodiscover:", 1, 0),
    "fb_condition": ("observability/filebeat/filebeat.yml", r"docker\.container\.labels\.co\.elastic\.logs/enabled", 1, 0),
    "fb_decode": ("observability/filebeat/filebeat.yml", r"- decode_json_fields:", 1, 0),
    "fb_target": ("observability/filebeat/filebeat.yml", r'target: ""', 1, 0),
    "fb_overwrite": ("observability/filebeat/filebeat.yml", r"overwrite_keys: true", 1, 0),
    "fb_expand": ("observability/filebeat/filebeat.yml", r"expand_keys: true", 1, 0),
    "fb_docker_meta": ("observability/filebeat/filebeat.yml", r"- add_docker_metadata:", 1, 0),
    "fb_output": ("observability/filebeat/filebeat.yml", r"^output\.elasticsearch:", 1, 0),
    "fb_ilm": ("observability/filebeat/filebeat.yml", r"^setup\.ilm\.enabled: true", 1, 0),
    "ilm_rollover": ("observability/elasticsearch/ilm-policy.json", r'"rollover"', 1, 0),
    "ilm_delete": ("observability/elasticsearch/ilm-policy.json", r'"delete": \{', 1, 0),
    "gf_ds_prom": ("observability/grafana/provisioning/datasources/datasources.yml", r"- name: Prometheus", 1, 0),
    "gf_ds_es": ("observability/grafana/provisioning/datasources/datasources.yml", r"- name: Elasticsearch", 1, 0),
    "gf_sets_panel": ("observability/grafana/dashboards/02-business.json", r"increase\(gym_sets_written_total\[24h\]\)", 1, 0),
    "kb_trace": ("observability/kibana/saved-objects.ndjson", r'"gym-trace"', 1, 0),
    "kb_faults": ("observability/kibana/saved-objects.ndjson", r'"gym-faults"', 1, 0),
    "df_unbuffered": ("Dockerfile", r"ENV PYTHONUNBUFFERED=1", 1, 0),
    "df_workers": ("Dockerfile", r'^CMD \["uvicorn"', 1, 0),
    "df_report_ref": ("Dockerfile", r"docs/REPORT\.md", 1, 0),
    "lg_routes": ("scripts/load_generator.py", r"^ROUTES = \[", 1, 0),
    "t_bounded_labels": ("tests/test_observability.py", r"def test_labels_are_bounded_dimensions_only", 1, 0),
    "t_zero": ("tests/test_observability.py", r"def test_labelled_counters_start_at_zero", 1, 0),
    "t_journal": ("tests/test_observability.py", r"def test_journal_text_is_never_logged", 1, 0),
    "t_cap": ("tests/test_observability.py", r"def test_unique_ids_are_capped_at_one_hundred", 1, 0),
}

# ---------------------------------------------------------------------------
# Multi-line excerpts: name -> (file, start regex, end regex, start occurrence)
# Both ends inclusive. The end is searched from the start line onwards.
# ---------------------------------------------------------------------------

SNIPPETS: dict[str, tuple] = {
    "mw_full": ("app.py", r'@app\.middleware\("http"\)', r"^    return response$", 1),
    "mw_success_tail": ("app.py", r"^    duration_ms = \(time\.perf_counter\(\) - started\) \* 1000$", r"^    return response$", 1),
    "mw_exception": ("app.py", r"^    except Exception as exc:", r"^        raise$", 1),
    "mw_finally": ("app.py", r"^    finally:", r"METRICS\.http_requests_in_flight\.dec\(\)", 1),
    "save_success": ("app.py", r'entries_total\.labels\(outcome="saved"\)', r'"entry\.replaced": result\.replaced,', 1),
    "save_blocked": ("app.py", r"^    if not draft\.ready:", r"^        return _review_page\(draft, user\)", 1),
    "save_duplicate": ("app.py", r"^    if result\.duplicate_of_recent:", r"^        \)$", 1),
    "log_entry_metrics": ("app.py", r"METRICS\.entry_text_bytes\.observe", r'"entry\.failed": bool\(draft\.error\),', 1),
    "login_paths": ("app.py", r'logins_total\.labels\(result="failure"\)', r'"login succeeded",', 1),
    "healthz": ("app.py", r'@app\.get\("/healthz"\)', r"return JSONResponse\(body\)", 1),
    "metrics_endpoint": ("app.py", r'@app\.get\("/metrics"\)', r"media_type=app_metrics\.CONTENT_TYPE_LATEST,", 1),
    "inbound_rid": ("app.py", r"def _inbound_request_id", r"return raw if re\.fullmatch", 1),
    "m_counter_decl": ("metrics.py", r"self\.http_requests_total = Counter\(", r"^        \)$", 1),
    "m_sets_decl": ("metrics.py", r"self\.sets_written_total = Counter\(", r"^        \)$", 1),
    "m_entries_decl": ("metrics.py", r"self\.entries_total = Counter\(", r"^        \)$", 1),
    "m_zero_init": ("metrics.py", r"for counter, values in \(", r"counter\.labels\(value\)", 1),
    "m_gauge_decl": ("metrics.py", r"self\.review_drafts_open = Gauge\(", r"^        \)$", 1),
    "m_inflight_decl": ("metrics.py", r"self\.http_requests_in_flight = Gauge\(", r"^        \)$", 1),
    "m_hist_decl": ("metrics.py", r"self\.http_request_duration = Histogram\(", r"^        \)$", 1),
    "m_summary_decl": ("metrics.py", r"self\.sets_per_entry = Summary\(", r"^        \)$", 1),
    "m_buckets": ("metrics.py", r"^HTTP_DURATION_BUCKETS = \(", r"^\)$", 1),
    "m_label_values": ("metrics.py", r"^ENTRY_OUTCOMES = ", r"^FAULT_KINDS = ", 1),
    "m_route_label": ("metrics.py", r"^UNMATCHED_ROUTE = ", r"return path if isinstance", 1),
    "m_draft_tracker": ("metrics.py", r"    def __init__\(self, gauge: Any, ttl_seconds", r"^DRAFTS = DraftTracker", 1),
    "m_observe_duration": ("metrics.py", r"^class observe_duration", r"return self\.seconds \* 1000\.0", 1),
    "m_noop": ("metrics.py", r"^try:$", r"^    Gauge = Histogram = Summary = Counter", 1),
    "l_rid": ("logging_setup.py", r"^request_id_var: ContextVar", r"^    return token$", 1),
    "l_sensitive": ("logging_setup.py", r"^SENSITIVE_KEYS = frozenset", r"^\)$", 1),
    "l_patterns": ("logging_setup.py", r"^_VALUE_PATTERNS = \(", r"^    return value$", 1),
    "l_scrub_value": ("logging_setup.py", r"^def scrub_value", r"^    return value$", 1),
    "l_filter": ("logging_setup.py", r"^class SensitiveDataFilter", r"^        return True$", 1),
    "l_format": ("logging_setup.py", r"    def format\(self, record: logging\.LogRecord\) -> str:", r"return json\.dumps\(document", 1),
    "l_configure": ("logging_setup.py", r"    handler = logging\.StreamHandler", r"uvicorn_logger\.propagate = True", 1),
    "f_init": ("faults.py", r"    def __init__\(self, metrics: Optional\[object\] = None\)", r"self\._metrics = metrics$", 1),
    "f_before": ("faults.py", r"    async def before_request", r"^        return None$", 1),
    "p_record": ("pipeline.py", r"^def _record_extraction", r"_METRICS\.llm_extraction_duration\.observe\(seconds\)", 1),
    "p_extract_loop": ("pipeline.py", r"    started = _perf_counter\(\)", r"_METRICS\.llm_extractions_in_flight\.dec\(\)", 1),
    "c_counters": ("observability/cardinality_demo.py", r"^UNSAFE = Counter\(", r"^\)$", 2),
    "c_record": ("observability/cardinality_demo.py", r"    def record\(self, endpoint", r"^        return request_id$", 1),
    "dc_app": ("docker-compose.yml", r"^  app:", r"networks: \[gym-net\]", 1),
    "dc_prometheus": ("docker-compose.yml", r"^  prometheus:", r"networks: \[gym-net\]", 1),
    "dc_filebeat": ("docker-compose.yml", r"^  filebeat:", r"networks: \[gym-net\]", 1),
    "dc_volumes": ("docker-compose.yml", r"^volumes:", r"gateway: 172\.28\.77\.1", 1),
    "prom_global": ("observability/prometheus/prometheus.yml", r"^global:", r"service: gym-tracker$", 1),
    "alert_slow": ("observability/prometheus/alerts.yml", r"alert: SlowRequests", r'summary: "p95 latency above 1s', 1),
    "alert_fault": ("observability/prometheus/alerts.yml", r"alert: FaultInjectionActive", r'summary: "Fault injection is ACTIVE', 1),
    "fb_autodiscover": ("observability/filebeat/filebeat.yml", r"^filebeat\.autodiscover:", r"clean_removed: true", 1),
    "fb_processors": ("observability/filebeat/filebeat.yml", r"^processors:", r"ignore_missing: true", 1),
    "fb_output": ("observability/filebeat/filebeat.yml", r"^output\.elasticsearch:", r'setup\.ilm\.overwrite: true', 1),
    "ilm": ("observability/elasticsearch/ilm-policy.json", r"^\{", r"^\}", 1),
    "df_cmd": ("Dockerfile", r"^# One worker, deliberately\.", r'^CMD \["uvicorn"', 1),
}

# ---------------------------------------------------------------------------
# Locating
# ---------------------------------------------------------------------------


class AnchorError(Exception):
    pass


_cache: dict[str, list[str]] = {}


def lines_of(path: str) -> list[str]:
    if path not in _cache:
        _cache[path] = (ROOT / path).read_text(encoding="utf-8").splitlines()
    return _cache[path]


def find(path: str, pattern: str, occurrence: int = 1, start: int = 0) -> int:
    """0-based index of the `occurrence`-th line matching `pattern`."""
    regex = re.compile(pattern)
    seen = 0
    for index, line in enumerate(lines_of(path)[start:], start=start):
        if regex.search(line):
            seen += 1
            if seen == occurrence:
                return index
    raise AnchorError(f"{path}: no match #{occurrence} for /{pattern}/")


def build() -> tuple[dict, dict, list[str]]:
    errors: list[str] = []
    refs: dict[str, dict] = {}
    for name, (path, pattern, occurrence, offset) in REFS.items():
        try:
            index = find(path, pattern, occurrence) + offset
            refs[name] = {
                "file": path,
                "line": index + 1,
                "text": lines_of(path)[index].strip(),
            }
        except (AnchorError, FileNotFoundError) as exc:
            errors.append(f"ref {name}: {exc}")

    snippets: dict[str, dict] = {}
    for name, (path, start_pattern, end_pattern, occurrence) in SNIPPETS.items():
        try:
            begin = find(path, start_pattern, 1)
            end = find(path, end_pattern, occurrence if occurrence > 1 else 1, begin)
            # "occurrence" for snippets counts end matches from the start line,
            # which lets an end anchor skip an earlier identical line.
            body = lines_of(path)[begin : end + 1]
            snippets[name] = {
                "file": path,
                "start": begin + 1,
                "end": end + 1,
                "code": "\n".join(body),
            }
        except (AnchorError, FileNotFoundError) as exc:
            errors.append(f"snippet {name}: {exc}")
    return refs, snippets, errors


def render_js(refs: dict, snippets: dict) -> str:
    return (
        "// GENERATED by viva-tutor/build_snippets.py from the real source.\n"
        "// Do not edit by hand: re-run the script after the code changes.\n"
        "window.REFS = " + json.dumps(refs, indent=1, ensure_ascii=False) + ";\n"
        "window.SNIPPETS = " + json.dumps(snippets, indent=1, ensure_ascii=False) + ";\n"
    )


# ---------------------------------------------------------------------------
# Validation of the hand-written content
# ---------------------------------------------------------------------------

CONTENT_FILES = ["content.js", "content-qa.js", "app.js"]

# Repository paths named in the content. Anything that looks like a path to a
# file in this repo must exist.
PATH_RE = re.compile(
    r"(?<![\w/.-])(docs/Assignment report 2\.docx|(?:observability|scripts|tests|docs|static|migrations)/[\w./-]*?\.(?:py|yml|yaml|json|ndjson|sh|md|png|docx|txt|log)|"
    r"(?:app|metrics|logging_setup|faults|pipeline|api|sessions|auth|review|insights|charts|records|routines|measurements)\.py|"
    r"docker-compose\.yml|Dockerfile|README\.md|requirements(?:-dev)?\.txt|schema\.sql)"
)

# Paths the tutor deliberately names because they do NOT exist (discrepancies).
EXPECTED_MISSING = {"docs/REPORT.md"}

# Metric families named in content must be declared. Derived names (_bucket,
# _sum, _count) and demo/node metrics are mapped back to their family.
METRIC_RE = re.compile(r"\bgym_[a-z_]+\b")


def declared_metrics() -> set[str]:
    source = (ROOT / "metrics.py").read_text(encoding="utf-8")
    return set(re.findall(r'"(gym_[a-z_]+)"', source))


def family(name: str) -> str:
    if name.endswith("_created"):
        # prometheus_client's companion series: gym_x_created belongs to gym_x_total.
        return name[: -len("_created")] + "_total"
    for suffix in ("_bucket", "_sum", "_count"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def check_content(refs: dict) -> list[str]:
    problems: list[str] = []
    declared = declared_metrics()
    # Strings that look like metric names but are not metrics.
    not_metrics = {"gym_tracker", "gym_net", "gym_"}
    for name in CONTENT_FILES:
        path = HERE / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")

        for match in sorted(set(METRIC_RE.findall(text))):
            if match in not_metrics or match.startswith("gym_tracker"):
                continue
            if family(match) not in declared and match not in declared:
                problems.append(f"{name}: metric {match!r} is not declared in metrics.py")

        for match in sorted(set(m.strip() for m in PATH_RE.findall(text))):
            if match in EXPECTED_MISSING:
                if (ROOT / match).exists():
                    problems.append(f"{name}: {match} now exists; update the discrepancy note")
                continue
            if not (ROOT / match).exists():
                problems.append(f"{name}: path {match!r} does not exist in the repository")

        # "name", "ref" and "id" only appear in comments that describe the syntax.
        placeholders = {"name", "ref", "id"}
        for ref in sorted(set(re.findall(r"\[\[([a-z0-9_]+)\]\]", text)) - placeholders):
            if ref not in refs:
                problems.append(f"{name}: [[{ref}]] has no anchor in build_snippets.py")

        for snippet in sorted(set(re.findall(r"snip\(['\"]([a-z0-9_]+)['\"]", text)) - placeholders):
            if snippet not in SNIPPETS:
                problems.append(f"{name}: snip('{snippet}') is not defined in build_snippets.py")

    # Queries the content claims are copied verbatim from a file.
    content = HERE / "content.js"
    if content.exists():
        text = content.read_text(encoding="utf-8")
        for query, source in re.findall(r"q:\s*'((?:[^'\\]|\\.)*)',\s*src:\s*'([^']+)'", text):
            query = query.replace("\\'", "'")
            haystack = (ROOT / source).read_text(encoding="utf-8")
            # Dashboards are JSON: the query appears with its quotes escaped.
            if query not in haystack and query.replace('"', '\\"') not in haystack:
                problems.append(f"content.js: query not found verbatim in {source}: {query}")
    return problems


def main() -> int:
    check = "--check" in sys.argv[1:]
    refs, snippets, errors = build()
    rendered = render_js(refs, snippets)

    if errors:
        print("Anchors that no longer match the source:", file=sys.stderr)
        for error in errors:
            print("  " + error, file=sys.stderr)
        return 1

    if not check:
        OUT.write_text(rendered, encoding="utf-8")
        print(f"wrote {OUT.relative_to(ROOT)}: {len(refs)} refs, {len(snippets)} snippets")
        return 0

    problems = check_content(refs)
    if not OUT.exists() or OUT.read_text(encoding="utf-8") != rendered:
        problems.insert(0, "snippets.js is stale: run python viva-tutor/build_snippets.py")
    if problems:
        print("Viva tutor check FAILED:", file=sys.stderr)
        for problem in problems:
            print("  " + problem, file=sys.stderr)
        return 1
    print(f"Viva tutor check passed: {len(refs)} refs, {len(snippets)} snippets, content verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
