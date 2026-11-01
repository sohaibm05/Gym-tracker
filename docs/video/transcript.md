# Assignment 2 demo video: narration transcript

Video: [assignment2-demo.mp4](assignment2-demo.mp4) · captions: [assignment2-demo.srt](assignment2-demo.srt)

Recorded against the local Docker Compose stack (`python scripts/quickstart.py`). Every terminal and browser scene is a real run: the terminal output is replayed from a capture of the actual commands, and long stretches of a run are sped up to keep pace with the narration. The narration is a synthetic voice (Kokoro TTS) reading this script; the timestamps below mark where each scene starts.

**[00:00]** This is the Assignment 2 demo: HTTP APIs, gRPC and metrics, built on the Gym Tracker from Assignment 1. The user task is logging one set during a live workout. Over HTTP it is a POST to a workout's sets; over gRPC it is WorkoutService.LogSet, served by a separate process. We go through Part A, the HTTP API; Part B, the gRPC service; and Part C, the metrics.

**[00:28]** Everything runs locally with Docker Compose. Beside the app container, gym-tracker, is the new gym-grpc container, both on the same Postgres, with Prometheus, Grafana and the Elastic stack from Assignment 1.

**[00:43]** Part A starts with the six REST constraints. Client-server: the browser app and the Python caller share only the JSON contract with the server. Stateless: every request carries its credentials and names its resource in the path. Cacheable, partly: static files carry Cache-Control; per-user JSON is deliberately not cached. Uniform interface: plural nouns, standard methods, real status codes, one error body. Layered system: clients only know a base URL, so a proxy in between changes nothing. And code on demand, the optional one, is shown: the browser runs the app.js and service worker the server sends.

**[01:22]** Every endpoint under /api was reviewed, leaving out health and metrics. For logging a set, this table compares the old endpoint with the new one. The singular path became the plural /api/sessions, with the old path kept as a deprecated alias for installed phones. Untyped dictionaries became typed models, so bad input is a 400 before any database work. Errors now use 400, 401, 404, 409, 429 or 500, always in one body with a code, a message and the request id. And an Idempotency-Key header makes retries safe. Elsewhere, /session became /sessions, PUT became PATCH, the rebuild endpoint gained a rate limit, and the rest were left alone. The browser app was updated to match.

**[02:09]** The OpenAPI Specification is a standard, machine-readable description of an HTTP API: its paths, methods, parameters, bodies, responses and authentication. Tools read it to render documentation, generate clients, and test requests, so it is the contract between an API and its callers. Here it is generated from the code, and a test fails if the committed file and the code disagree.

**[02:35]** Swagger UI renders the spec, with every route, its inputs and its responses. We authorize with the demo account and try the log-a-set endpoint. The request has an Idempotency-Key header and a body: the exercise, 80 kilograms, 8 reps. The answer is 201 Created, with the new set and its log id. The same request again, with the same key, returns 200, with idempotent-replayed: true and the same log id. Nothing new was written. With reps set to zero, the answer is 400, in the shared error format: a code, a message and the request id.

**[03:28]** Logging a set is what gets retried: on gym wifi, a response can be lost after the set was written. So the client sends one key per set, unchanged on every retry. The server stores it under a unique index per account, looks it up first, and returns the stored set instead of writing another. Two copies racing past the lookup are settled by that index. Other calls are safe to repeat by design: starting a workout returns the live one, and rebuilding records twice gives the same result.

**[03:59]** Rate limiting protects POST /api/records/rebuild, the most expensive call. The limit is three calls per sixty seconds, in a sliding window, shared by everything one account does; other users are unaffected. Every attempt counts, and Retry-After says when a call will be accepted again.

**[04:16]** Five calls: three are accepted, and the fourth and fifth get 429 Too Many Requests. The full response has Retry-After: 60 and the standard error body. After waiting those 60 seconds, the call is accepted again.

**[04:39]** The tests cover a valid request, invalid input and a repeated request. 201 for a valid set, 400 in the shared shape for bad input, and one row for a repeat, even with two copies racing on a real Postgres. All 56 Assignment 2 tests pass, and the committed OpenAPI document matches the code.

**[05:01]** The Python caller plays another service, say a watch app's sync job, logging several sets in one run. Each attempt times out after 2 seconds. A failure is retried at most twice, with backoff from half a second, doubling, plus jitter. Only timeouts, connection errors and 5xx are retried, never a 4xx, and the Idempotency-Key makes retrying this POST safe. The circuit breaker opens after three consecutive failures and allows one trial call after ten seconds.

**[05:31]** The demo restarts the app so that every fourth request is held for three seconds, longer than the timeout. The first sets succeed. Set four times out, and succeeds on the retry. Then the app container is stopped. Set six is refused three times, and the breaker opens. Now calls are blocked at once, without touching the network. The first trial fails, since the app is still down, so the breaker opens again. With the container back, the next trial succeeds and the breaker closes. Ten sets written, nineteen blocked, one failed, none written twice.

**[06:09]** Part B serves one operation, logging a set, from a new gRPC service. It runs in its own container, gym-grpc, on port 50051, with metrics on 9102. The HTTP version stays in the app. Both handlers call the same function, sessions.log_set, on the same database, so the business rules are one piece of code. The gRPC server never calls the HTTP API. It is a unary RPC: one set in, one result out.

**[06:42]** The contract is workout.proto: one service, one RPC, LogSet. Weight and reps are optional, so a missing value differs from zero, and the idempotency key follows the HTTP rule. The response has the set, any records it beat, and a replayed flag.

**[06:59]** The code is generated with grpc_tools.protoc, producing the messages, type hints and gRPC module, which holds the client stub and the servicer base class. Our handler subclasses the servicer and implements LogSet, and both handlers call the same sessions_module.log_set. Regenerating gives exactly the committed code.

**[07:20]** The gRPC client takes its input from flags and sets a two-second deadline. A valid set returns OK, with the set, the records it beat, and our request id echoed back. Zero reps fails with INVALID_ARGUMENT and the same message as HTTP. And a one-millisecond deadline gives DEADLINE_EXCEEDED.

**[07:44]** The traffic script sends the same four calls through both versions. HTTP: 201, then 200 replayed, 400 for zero reps, and 404 for a missing workout. gRPC: OK, OK replayed, INVALID_ARGUMENT and NOT_FOUND. Directly over HTTP, the failure is a 400 with our request id in the error body.

**[08:08]** Both map the same business exceptions, so an input fails the same way over each. HTTP tells created from replayed with 201 and 200; gRPC has one OK and a replayed flag. A bad field is 400 or INVALID_ARGUMENT: retrying the same input cannot help. A missing workout is 404 or NOT_FOUND; a finished one is 409 or FAILED_PRECONDITION. Bad credentials are 401 or UNAUTHENTICATED; a bug is 500 or INTERNAL, with details only in the logs. And a caller that stops waiting gets a client timeout over HTTP, but DEADLINE_EXCEEDED over gRPC, which also tells the server.

**[08:50]** Here are both call paths. HTTP: JSON to port 8000, the middleware that sets the request id and counts the request, then the handler that validates the body. gRPC: the client calls the generated stub, which sends protobuf over HTTP/2 to port 50051, where the generated servicer dispatches to our LogSet handler. Both end in sessions.log_set and the same Postgres table. Neither wraps the other.

**[09:20]** Logs and trace correlation still work across the gRPC boundary. The client sends x-request-id as metadata; the server logs under it and returns it, as the HTTP middleware does with X-Request-ID. So one Kibana search on our request ids finds the gRPC lines from gym-tracker-grpc, set logged and rpc served, OK and INVALID_ARGUMENT, beside the HTTP failure from the app.

**[09:52]** Part C starts with an audit of the Assignment 1 metrics. The HTTP counter, duration histogram and in-flight gauge already cover the HTTP LogSet, 429s included, so they stay. The gRPC service had no metrics at all. Live-workout sets were never counted, the gap Assignment 1 listed. And nothing showed idempotency working. Build info and the LLM gauge were left alone.

**[10:18]** Four metrics were added. A counter of gRPC calls by service, method and status code, for rate and errors. A duration histogram in seconds, for p50 and p95. A gauge of calls in flight. And a counter of live sets by transport and outcome, written or replayed. All labels come from fixed lists; no user, request or trace ids.

**[10:41]** The gRPC service exposes them on its own endpoint, with only bounded labels, and buckets from five milliseconds to ten seconds, the same as HTTP.

**[10:54]** Duration is measured at the server for both transports, so network time is left out and they compare like for like. The buckets are dense from five to a hundred milliseconds, where a LogSet normally falls; p50 and p95 use a five-minute window. In-flight calls beat message size: a request is tens of bytes, a flat line, while concurrency is what runs out first, with eight worker threads. And the live-sets counter fills the Assignment 1 gap.

**[11:22]** Prometheus scrapes the gRPC service as its own job, and it is up beside the app. The cardinality demo is down by design; it only runs for an Assignment 1 experiment. Here is the gRPC p95 over a five-minute window.

**[11:42]** In Grafana, every gRPC panel sits beside its HTTP twin, filled by the repeatable traffic script. Calls per second and error ratios. About half the calls fail by design: invalid and not-found calls in every round. Calls by status code for each transport. Server-side p50 and p95 over five minutes; in this small virtual machine both sit at a few hundred milliseconds. In-flight calls, and live sets written versus replayed. And the rate-limited endpoint, with its 200s and 429s.

**[12:19]** These are the queries behind the panels: calls by status code, and the error ratio; histogram_quantile for p50 and p95 over five minutes; and in-flight calls and live sets. The HTTP side uses the same panels on the sets route.

**[12:36]** Everything is in the repository: code, OpenAPI document, proto and generation command, both clients, tests, Prometheus configuration and Grafana dashboard. The README has the run and test commands, and the report has the tables and evidence. Thanks for watching.
