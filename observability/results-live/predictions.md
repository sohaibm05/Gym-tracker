# Predictions (written before the fault stage ran)

Fault: 500ms delay on every 5th request,
i.e. 20% of requests.

## Metrics I expect to change

| Metric | Prediction | Reasoning |
|---|---|---|
| `gym_http_request_duration_seconds` p95 | rises to ~500ms | 20% of requests are delayed, so the 95th percentile lands inside the delayed group |
| `gym_http_request_duration_seconds` p50 | barely moves | the median request is not one of the delayed ones |
| mean latency | rises by ~500/5ms | the delay averaged over all requests |
| `gym_http_requests_in_flight` | rises slightly | delayed requests overlap with new arrivals |
| `gym_http_requests_total` rate | falls, or holds | a closed-loop client issues fewer requests when each takes longer |
| `gym_faults_injected_total{kind="latency"}` | 0 -> non-zero | proves the fault was actually live |
| error rate | unchanged at 0 | a delay is not a failure; every request still returns 200 |

## Logs I expect to change

- `event.duration_ms` above 500 on the delayed requests only.
- A `warning` line per delayed request: "injected fault: delaying this request
  deliberately", carrying `fault.sequence` and `fault.latency_ms`.
- No `error` lines: this fault degrades, it does not break.

## Effect on users

Roughly one request in 5 takes half a second longer. A
page load usually involves several requests, so in practice most page loads feel
slow rather than one in 5 — this is why a p95 matters
more than an average when judging how an app *feels*.
