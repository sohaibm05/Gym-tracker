# Anomaly experiment results

| Stage | Requests | Achieved rps | p50 | p95 | p99 | Mean | Error rate |
|---|---|---|---|---|---|---|---|
| 01-baseline | 598 | 4.98 | 17.2ms | 35.6ms | 45.5ms | 19.3ms | 0.00% |
| 02-fault | 406 | 3.38 | 24.2ms | 540.0ms | 563.3ms | 167.5ms | 0.00% |
| 03-recovery | 579 | 4.82 | 21.0ms | 83.4ms | 277.7ms | 34.5ms | 0.00% |

## Time windows

- **01-baseline**: `2026-09-21T11:31:37Z` .. `2026-09-21T11:33:38Z`
- **02-fault**: `2026-09-21T11:34:04Z` .. `2026-09-21T11:36:05Z`
- **03-recovery**: `2026-09-21T11:36:34Z` .. `2026-09-21T11:38:34Z`
