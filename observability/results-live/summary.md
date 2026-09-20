# Anomaly experiment results

| Stage | Requests | Achieved rps | p50 | p95 | p99 | Mean | Error rate |
|---|---|---|---|---|---|---|---|
| 01-baseline | 599 | 4.99 | 6.3ms | 29.1ms | 38.9ms | 12.2ms | 0.00% |
| 02-fault | 415 | 3.45 | 20.2ms | 524.3ms | 531.2ms | 156.1ms | 0.00% |
| 03-recovery | 599 | 4.99 | 6.2ms | 31.8ms | 41.0ms | 12.9ms | 0.00% |

## Time windows

- **01-baseline**: `2026-09-19T15:56:52Z` .. `2026-09-19T15:58:53Z`
- **02-fault**: `2026-09-19T15:59:16Z` .. `2026-09-19T16:01:17Z`
- **03-recovery**: `2026-09-19T16:01:40Z` .. `2026-09-19T16:03:40Z`
