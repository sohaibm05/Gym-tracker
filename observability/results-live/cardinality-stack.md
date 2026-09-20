# Cardinality experiment (through Prometheus)

| Stage | count(demo_requests_total) | count(demo_requests_safe_total) |
|---|---|---|
| With request_id label | 100 | 1 |
| Label removed, app restarted | none | 1 |

## The part people get wrong

After removing the label the series count does NOT drop to zero straight
away. The old series stop receiving samples, but they stay in Prometheus
until they fall outside the retention window (15 days here). Removing a
bad label stops the bleeding; it does not undo the damage. That is why
cardinality is a code-review question, not an incident-response one.
