# Assignment 2 — every change from Assignment 1, and why

Read this top to bottom once; then use the tables as a lookup when you need to explain a choice.
Each section says **what changed**, **why**, which **rubric item** it serves, and **where to see it**.

## 0. The big picture in five sentences

1. A1's HTTP app kept its state in a Python dict. A gRPC service in a **second process** cannot see
   that dict, so the state moved into a **shared SQLite database** (`attendance_core/store.py`).
2. The attendance **rules** moved out of the Flask handlers into one module, `attendance_core/service.py`.
   Flask and the gRPC server are now thin **transports** that call it — that is what "same business
   rules, same source of truth, not a wrapper around the HTTP endpoint" means.
3. The HTTP API was redesigned (`/api/v1/...`, proper methods/status codes, one error format) and
   gained OpenAPI, idempotency and rate limiting (Part A).
4. The check-in operation was added as a gRPC service with its own `.proto`, handler, client and
   metrics (Part B).
5. Metrics were audited, a few real flaws fixed, new metrics added, and a dashboard compares HTTP
   and gRPC (Part C).

## 1. File map: new, changed, unchanged

| File / folder | Status | What it is now |
|---|---|---|
| `app.py` | **rewritten** | Flask transport only: routes, JSON parsing, status codes, problem+json, rate limiting, HTTP metrics. No business rules. |
| `attendance_core/service.py` | **new** | The business rules (validation, conflict checks, scan, idempotency, on-time/late). |
| `attendance_core/store.py` | **new** | SQLite store (users, attendance sessions, idempotency keys, events). Replaces A1's in-memory dicts. |
| `attendance_core/errors.py` | **new** | Domain errors, each carrying its HTTP status **and** gRPC code. |
| `attendance_core/metrics.py` | **new** | Business metrics shared by both processes (A1's metrics moved here + 3 new). |
| `attendance_core/logging_setup.py`, `tracing.py` | **new** | A1's JSON logger, now also writing `trace_id`; W3C `traceparent` helpers. |
| `attendance_core/ratelimit.py`, `config.py` | **new** | Sliding-window rate limiter; constants + environment overrides. |
| `grpc_service/` | **new** | `server.py` (handler), `interceptors.py` (metrics + logs), generated `attendance_pb2*.py`. |
| `proto/attendance.proto` | **new** | The gRPC contract. |
| `openapi.yaml` | **new** | The HTTP contract. |
| `clients/` | **new** | `resilience.py` (timeout/retry/breaker), `http_caller.py`, `grpc_client.py`. |
| `scripts/` | **new** | `generate_proto.py`, `demo_calls.py`, `rate_limit_demo.py`. |
| `tests/` | **new** | 82 pytest tests. |
| `static/index.html` | **changed** | Calls the new endpoints; shows problem+json errors; sends an `Idempotency-Key`. CSS/layout unchanged. |
| `traffic_generator.py` | **changed** | Uses the new endpoints, handles 429, syncs with persisted state. `--inject-delay` / `--cardinality-demo` kept. |
| `observability/prometheus/prometheus.yml` | **changed** | + job `attendance-grpc` (`host.docker.internal:9102`). |
| `observability/filebeat/filebeat.yml` | **changed** | Path is now the glob `*.log` so `grpc.log` is shipped too. |
| `observability/grafana/dashboards/attendance-dashboard.json` | **changed (1 query)** | "Currently Checked In" now `max(currently_checked_in)`. |
| `observability/grafana/dashboards/http-vs-grpc-dashboard.json` | **new** | The Part C dashboard. |
| `requirements.txt`, `requirements-dev.txt` | **changed / new** | + grpcio, protobuf; dev file adds grpcio-tools, pytest, PyYAML, openapi-spec-validator. |
| `docker-compose.yml`, ILM files, A1 report, A1 diagram | **unchanged** | Still valid. |

---

## 2. Part A — HTTP API

### 2.1 The six REST constraints, applied to *our* API

| Constraint | Plain-language meaning | Example in our API |
|---|---|---|
| Client–server | UI and data/logic are separate; each can change independently. | The browser page (`static/index.html`), the Python caller and `curl` all use the same API; the server knows nothing about any UI. |
| Stateless | Every request contains everything the server needs; the server remembers no conversation between requests. | No cookies/login session. `PATCH /api/v1/attendance/{id}` carries the session id in the URL and everything else in the body. (The attendance *data* lives in the database — that is resource state, not session state.) |
| Cacheable | Responses say whether they may be reused. | We send `Cache-Control: no-store` on every `/api/` response because the data changes constantly (test: `test_api_responses_declare_themselves_non_cacheable`). |
| Uniform interface | Resources have URLs; standard methods and status codes; self-describing messages. HATEOAS = responses contain links to next actions. | URLs are nouns (`/users`, `/attendance/{id}`); GET/POST/PATCH mean what they say; `201` + `Location`; one error type, `application/problem+json`. **HATEOAS is not demonstrated** — responses carry no links. |
| Layered system | A client cannot tell whether it talks to the real server or an intermediary. | **Not demonstrated** in our code: callers talk straight to Flask. (Nothing in the API would stop us putting a proxy/rate limiter in front.) |
| Code on demand *(optional)* | Server sends code the client runs. | Loosely: Flask serves `index.html` with JavaScript the browser executes. |

### 2.2 Endpoint redesign (what changed, endpoint by endpoint)

| A1 endpoint | A2 endpoint | Change |
|---|---|---|
| `POST /api/checkin` | `POST /api/v1/attendance` | **Changed** — see before/after table below. |
| `POST /api/checkout` | `PATCH /api/v1/attendance/{id}` | **Changed** — see below. |
| `GET /api/status` | `GET /api/v1/attendance?state=open` + `GET /api/v1/events` | **Changed** — split into the two resources it mixed together. |
| `POST /api/signup` | `POST /api/v1/users` | **Changed** — `201 Created` + `Location`; **rate limited**; strict validation. |
| `GET /api/users` | `GET /api/v1/users` (+ `GET /api/v1/users/{id}`) | **Changed** — list wrapped as `{"items": [...], "count": n}`; added get-one. |
| `GET /api/health`, `GET /metrics` | same | **Left alone** (excluded from the review per the brief). Not counted in HTTP metrics any more (see 4.1). |
| `POST /api/demo/scan` | same | **Left alone** — the A1 cardinality-experiment endpoint, still in its A1 "fixed" state. |
| — | `GET /openapi.yaml`, `GET /docs` | **New** — the spec and a Swagger UI page. |

### 2.3 Before/after table for the user task "Check in and out of work"

| Old endpoint | Problem | Change | Reason |
|---|---|---|---|
| `POST /api/checkin` | Verb in the path (`checkin`). Returned a `success: true/false` flag in the body on top of the HTTP status (two sources of truth). A duplicate check-in burned a biometric scan **before** being rejected (could even fail with a scan error instead of 409). Accepted any content type and ignored malformed JSON (`force=True, silent=True`). A retry after a timeout either double-processed or returned a confusing 409. Echoed the user's name. | `POST /api/v1/attendance` creates an **attendance session** → `201 Created` + `Location`. Strict JSON/field validation → `400` with a per-field `errors` list, `415` for non-JSON. Conflict and unknown-user are checked **before** the scan. Optional `Idempotency-Key` header. | URLs name resources; the HTTP status alone is the outcome; bad input gets precise, consistent feedback; retries become safe (2.5). |
| `POST /api/checkout` | Same verb-in-path and `success` flag; identified the person by `user_id`, so there was nothing to point at when checking out. | `PATCH /api/v1/attendance/{id}` with `{"state":"closed","method":...}` → `200` and the closed session. Repeating it returns the same closed session (`Idempotent-Replayed: true`), no second effect. | Check-out is a state change of the session resource → PATCH. Being idempotent by design, it is safe to retry. |
| `GET /api/status` | One endpoint returned two unrelated things (who is in + an event log) and the log was an unbounded in-memory list. | `GET /api/v1/attendance?state=open` and `GET /api/v1/events?limit=n` (validated, bounded). Events are stored in the database. | One resource per URL; filters instead of special endpoints. |
| *(all)* | Errors came in two shapes: `{"error":…}` and `{"success":false,"reason":…}`; scan mismatch was `401` without the required `WWW-Authenticate` header; simulated signup failure was `500`. | Every error is `application/problem+json` (RFC 9457): `type, title, status, detail, instance, request_id, trace_id, errors?`. Scan mismatch is `401` + `WWW-Authenticate: Biometric realm="attendance"`; simulated enrolment outage is `503`. | One predictable format callers can parse; correct semantics (5xx = server side, retry later). |

HTTP status vocabulary used: `201` created, `200` ok, `400` invalid input, `401` scan not matched,
`404` unknown resource, `409` already checked in, `415` wrong media type, `422` Idempotency-Key reused
with a different body, `429` rate limited, `503` temporary enrolment failure.

### 2.4 OpenAPI (Part A, practice 1)

* **What it is:** a machine-readable (YAML/JSON) description of an HTTP API — paths, methods, inputs,
  and every success/error response — that tools use to generate documentation, clients and tests.
* **Our file:** `openapi.yaml` (hand-written, then validated with `openapi-spec-validator`).
* **How it is kept honest:** `test_every_route_is_in_the_spec_and_vice_versa` fails if a Flask route and
  the spec disagree, and the helper `documented(...)` in the HTTP tests fails if any tested request
  returns a status code the spec does not list.
* **How we used it to test an endpoint:** open `http://localhost:5000/docs` (Swagger UI), expand
  `POST /api/v1/attendance`, click *Try it out* with `{"user_id":"u1","method":"face"}` and an
  `Idempotency-Key`, execute, then execute again and see `Idempotent-Replayed: true`.

### 2.5 Idempotency (practice 2) — check-in

* **Problem:** if a check-in request times out, the client does not know whether it was applied. Retrying
  naively could double-process; not retrying could lose the check-in.
* **Fix:** the client sends `Idempotency-Key: <uuid>`. The server stores the **successful** response under
  that key (`idempotency_keys` table) in the **same transaction** that creates the session. A repeat with
  the same key and body returns the stored response (`201` + `Idempotent-Replayed: true`) without
  scanning or writing again; the same key with a different body is `422`.
* **Why only successes are stored:** a failed attempt (scan mismatch, 5xx) changed nothing, so re-executing
  it cannot duplicate anything — and a user who gets "scan not matched" should be able to scan again.
* **Races:** two identical concurrent requests both scan, but the second one's re-check inside the
  transaction finds the first one's key and replays (test: `test_race_between_two_threads_with_same_key_executes_once`).
  Independently, a **partial unique index** in the database (`one_open_session_per_user`) makes a second open
  session impossible, even across the HTTP and gRPC processes.
* **Check-out** needs no key: it is naturally idempotent (closing a closed session returns it unchanged).

### 2.6 Rate limiting (practice 3) — `POST /api/v1/users`

* **Why signup:** enrolment is the expensive, abusable operation; check-in is meant to be fast and frequent.
* **Limit:** 10 requests per 60 seconds (`RATE_LIMIT_SIGNUP=10/60`).
* **Who shares it:** each **client IP** (`request.remote_addr`) has its own budget. In our local setup every
  caller (browser, scripts, traffic generator) comes from the same IP, so they all share one budget.
* **Algorithm:** sliding-window log — keep timestamps of recent requests; allow if fewer than the limit fall
  in the last 60 s. Even invalid requests to that endpoint count.
* **When calls are allowed again:** when the **oldest** counted request leaves the window; that exact number
  of seconds is returned in `Retry-After` (and `RateLimit-Reset`). Every response also carries
  `RateLimit-Limit` and `RateLimit-Remaining`.
* **Limits of the design:** counters live in one process's memory (fine for one instance; several instances
  would each enforce their own limit — a shared store such as Redis would be needed).
* **Evidence:** `docs/evidence/rate_limit_output.txt`, tests `test_signup_rate_limit_429_retry_after_and_recovery`.
* New metric `rate_limited_total{endpoint}` counts the rejections.

### 2.7 The Python caller (`clients/http_caller.py`, `clients/resilience.py`)

| Setting | Value |
|---|---|
| Timeout | connect 0.5 s, read 1.0 s |
| Retry limit | 3 attempts in total (1 try + 2 retries) |
| Backoff | `0.2 s × 2^(retry−1)` with full jitter, capped at 2 s |
| Circuit breaker | opens after **3 consecutive failed attempts**; recovery period **5 s**; then **one** probe call (half-open) |
| Retries only when | the request is safe: GET/PUT/DELETE, or carries an `Idempotency-Key` |
| Counted as failure | connection error, timeout, `502/503/504` |
| NOT retried / NOT counted | other `4xx` (retrying cannot fix them); `429` (server is healthy — honour `Retry-After`, do not trip the breaker) |

Demo (`python clients/http_caller.py --demo`, output in `docs/evidence/http_caller_output.txt`) shows, in one
run: healthy calls → a call that gets `503` twice then succeeds → a call whose first attempt times out and
whose retry succeeds → an outage where the breaker **opens** → further calls **blocked without touching the
network** → after 5 s the probe succeeds and the breaker **closes**. Failures are triggered with the test-only
`X-Inject-Fault` header (needs `ENABLE_FAULT_INJECTION=1`). Rate limiting is demonstrated separately
(`scripts/rate_limit_demo.py`), as the brief asks.

---

## 3. Part B — gRPC

* **Operation chosen:** check-in (same as the HTTP task). Contract: `proto/attendance.proto` — one unary RPC,
  `CheckIn(CheckInRequest) returns (CheckInResponse)`, with enums for the scan method/status and an
  `idempotency_key` field.
* **Generated code:** `python scripts/generate_proto.py` (wraps `python -m grpc_tools.protoc -I proto
  --python_out=grpc_service --grpc_python_out=grpc_service proto/attendance.proto` + one import fix).
* **Server handler:** `grpc_service/server.py` → `AttendanceServicer.CheckIn` converts protobuf → calls
  `AttendanceService.check_in` → converts the result, or maps a `DomainError` to a gRPC status.
* **Same rules, same truth:** both transports call the same service and the same database. Tests prove it:
  an HTTP check-in makes the gRPC call `ALREADY_EXISTS`, and vice versa
  (`test_http_checkin_is_visible_to_grpc_and_vice_versa`).
* **Client:** `clients/grpc_client.py` takes input on the command line, sets a **deadline**, forwards trace
  metadata, prints the result or the status code + details.
* **Deadline handling:** if the deadline passes while the server is busy, the handler notices
  (`context.is_active()`) and does **not** start the work (test `test_deadline_exceeded_and_no_work_done...`).

### HTTP ⇄ gRPC status mapping (same failure, two vocabularies)

| Situation | HTTP | gRPC |
|---|---|---|
| Success | `201 Created` | `OK` |
| Bad input (unknown method, bad id) | `400` problem+json with `errors` | `INVALID_ARGUMENT` |
| Unknown user | `404` | `NOT_FOUND` |
| Already checked in | `409` | `ALREADY_EXISTS` |
| Scan did not match | `401` | `UNAUTHENTICATED` |
| Idempotency key reused with different body | `422` | `INVALID_ARGUMENT` |
| Temporary server-side failure | `503` | `UNAVAILABLE` |
| Rate limited (HTTP only today) | `429` + `Retry-After` | `RESOURCE_EXHAUSTED` |
| Client gave up waiting | client timeout | `DEADLINE_EXCEEDED` |

The mapping lives in one place (`attendance_core/errors.py`: each error carries both) so the two can never drift.
HTTP carries the extra detail in the body (problem+json); gRPC carries it in the status message and the
`x-problem-type` trailer.

### Keeping A1 logging/trace correlation working
* Both processes write the A1 JSON schema (`time, service, severity, message, request_id, trace_id`), still with
  **no user data**. `trace_id` is new: it comes from the W3C `traceparent` header (HTTP) / metadata (gRPC).
* Proof: `docs/evidence/log_correlation.txt` shows one `trace_id` in `app.log` (HTTP call) **and** `grpc.log`
  (gRPC call). In Kibana: `trace_id : "4bf92f3577b34da6a3ce929d0e0e4736"` returns both. Test:
  `test_same_trace_id_links_an_http_call_and_a_grpc_call`.
* Filebeat reads both files via the `*.log` glob; no change to the `json.keys_under_root` setting.

---

## 4. Part C — metrics

### 4.1 Audit of the A1 metrics

| A1 metric / behaviour | Verdict | What I did |
|---|---|---|
| `http_requests_total`, `app_errors_total` | Useful, but **`endpoint` was the raw path** (`request.path`) | Now the **route template** (`/api/v1/attendance/<session_id>`). With path ids the raw path would create one time series per id (A1's cardinality lesson); unknown URLs used to create a series per random URL. |
| `http_request_duration_seconds` | Useful, but **no `method` label** (POST check-in indistinguishable from GET list) and buckets too coarse below 50 ms | Added `method`; buckets now `0.01 … 2.5 s`, identical to the gRPC histogram so p50/p95 are comparable. |
| `/metrics` and `/api/health` were counted as traffic | **Flaw:** Prometheus scrapes `/metrics` every 5 s, flooding counts and dragging percentiles down | These two paths are no longer measured. |
| `in_flight_requests` | Useful | Kept (HTTP only). |
| `currently_checked_in` | **Flaw:** a gauge set by Python code; would read 0 after a restart even though people are checked in, and two processes would disagree | Now computed **from the database at every scrape** (`Gauge.set_function`). Both processes report the same value; the dashboard uses `max()`. |
| `scan_*`, `checkins_total`, `checkouts_total`, `signups_total`, `signup_duration_seconds` (Summary) | Useful | Unchanged; now incremented inside the shared service, so a gRPC check-in is counted too. |
| `demo_requests_total` | Experiment-only | Left exactly as submitted for A1. |
| Missing: retries/duplicates | **Gap** | `idempotent_replays_total` |
| Missing: abuse protection visibility | **Gap** | `rate_limited_total` |
| Missing: database health | **Gap** (new shared dependency) | `store_operation_duration_seconds` |
| Missing: any gRPC visibility | **Gap** | the three `grpc_server_*` metrics |
| Missing: payload size comparison | **Gap** | `http_message_size_bytes` (same shape as gRPC's) |

### 4.2 New metrics (purpose, type, unit, labels, code location, query)

| Metric | Type | Unit | Labels (all bounded) | Code location | Query used |
|---|---|---|---|---|---|
| `grpc_server_handled_total` | Counter | calls | `grpc_method`, `grpc_code` | `grpc_service/interceptors.py` (`finally` of `wrapped`) | `sum by (grpc_code) (rate(grpc_server_handled_total{grpc_method="CheckIn"}[5m]))`; error %: `100 * sum(rate(…{grpc_code!="OK"}[5m])) / sum(rate(…[5m]))` |
| `grpc_server_handling_seconds` | Histogram | seconds | `grpc_method` | same | `histogram_quantile(0.95, sum by (le) (rate(grpc_server_handling_seconds_bucket{grpc_method="CheckIn"}[5m])))` (p50 with `0.50`) |
| `grpc_server_message_size_bytes` | Histogram | bytes | `grpc_method`, `direction` (request/response) | same | `sum by (direction) (rate(…_sum[5m])) / sum by (direction) (rate(…_count[5m]))` |
| `http_message_size_bytes` | Histogram | bytes | `endpoint`, `direction` | `app.py` `after_request` | same shape, `endpoint="/api/v1/attendance"` |
| `rate_limited_total` | Counter | requests | `endpoint` | `app.py` `enforce_rate_limit` | `sum by (endpoint) (rate(rate_limited_total[5m]))` |
| `idempotent_replays_total` | Counter | requests | `operation` (check_in/check_out) | `service.py` | `sum by (operation) (rate(idempotent_replays_total[5m]))` |
| `store_operation_duration_seconds` | Histogram | seconds | `operation` (≈12 fixed names) | `store.py` `@timed` | `histogram_quantile(0.95, sum by (le, operation) (rate(…_bucket[5m])))` |

* **Duration is measured at the SERVER** (HTTP: Flask `after_request`; gRPC: the interceptor). The Python
  caller/clients do not expose metrics, as the brief allows.
* **Time window:** `[5m]` — long enough to smooth a handful of calls, short enough to react within a minute or
  two. With only a few demo calls, run `scripts/demo_calls.py` and look ~15 s later.
* **In-flight vs message size — I chose message size.** A 5 s scrape cannot reliably catch a 50–400 ms call
  being "in flight", so an in-flight gauge would read ~0 in a no-load-test demo. Message size always has data
  and answers a real question the lecture raises: how much smaller is protobuf than JSON? Measured
  (`docs/evidence/metrics_sample.txt`): the gRPC success response is ~81 bytes vs ~251 bytes of JSON.
  (The HTTP average also includes small error bodies, so compare successes by eye; noted on the panel.)
* **Labels are bounded:** methods (1), gRPC codes (≤17), directions (2), operations (fixed names), endpoint
  templates. Never a user id, request id or trace id — those live in the logs. Test:
  `test_grpc_metric_labels_are_bounded`, `test_metrics_use_route_templates_not_raw_ids`.
* **Missing project metric I identified:** database latency, replays, rate limiting (above). Nothing further
  is needed: scans, signups, check-ins and errors were already covered in A1.
* **Dashboard:** `Attendance: HTTP vs gRPC (Assignment 2)` — scrape health; calls by status for HTTP and gRPC;
  error rate; p50/p95; message size; rate-limited; replays; DB p95; checked-in.

---

## 5. Cross-cutting changes

| Change | Why |
|---|---|
| **SQLite shared store** instead of in-memory dicts | Two processes need one source of truth; also makes check-ins survive restarts (this changes the A1 report's "what survives a restart" answer for *attendance state* — the A1 report remains correct for A1). |
| **DB-enforced rule:** partial unique index "one open session per user" | The guarantee holds even if an HTTP and a gRPC request race; Python locks cannot span two processes. |
| **Rule order fixed:** unknown user / already-checked-in are checked **before** the scan | A1 scanned first (wasted scans, and a duplicate could fail as a scan error instead of 409). |
| **Strict input validation** in one place (`service.py`) | Both transports reject the same bad input for the same reason; A1 would crash (500) on e.g. a numeric `name`. |
| **Fault injection is opt-in** (`ENABLE_FAULT_INJECTION=1`) | A1's `X-Inject-Delay` header worked on any server. Anyone could slow it down. Now it must be turned on deliberately. **When running the A1 `--inject-delay` experiment, start the server with that variable.** New generalised header `X-Inject-Fault: delay=<ms>,status=<5xx>,times=<n>` (used by the caller demo) and gRPC metadata `x-inject-delay-ms`. |
| **Env overrides** (`SCAN_FAILURE_RATE`, `SIGNUP_FAILURE_RATE`, …) | The A1 simulation fails ~5–12 % of scans at random, which makes demos and tests unrepeatable. |
| **Log schema** `time, service, severity, message, request_id, trace_id` | Same as A1 plus `trace_id`. `service` is `attendance-app` or `attendance-grpc`. Still no user data (a test asserts it). |
| **`X-Request-ID` and `traceparent`** accepted and echoed by the HTTP API; request ids sanitised | Correlation across both services; a caller cannot inject junk into logs. |
| **`Cache-Control: no-store`** on API responses | Makes the REST "cacheable" constraint explicit. |
| **Two log files, one Filebeat glob** | Each process owns its file (no interleaved writes from two processes). |

## 6. A1 flaws found while auditing (all fixed above)

1. Raw `request.path` as a metric label (unbounded series). 2. `/metrics` scrapes counted as traffic.
3. `currently_checked_in` not restart-safe. 4. Duplicate check-in consumed a scan first.
5. Two error formats and misleading statuses (`401` without `WWW-Authenticate`, signup failure as `500`).
6. Malformed/non-JSON bodies silently accepted; wrong types crashed with 500. 7. No safe way to retry a POST.
8. Fault injection open to anyone. 9. A1 state lost on restart.

## 7. Questions you may be asked — and the short answers

* **Why SQLite and not Redis/Postgres?** Zero extra services; WAL mode lets two processes share a file; the
  brief only needs one source of truth. A shared server (Postgres) would be the production choice.
* **Why is gRPC not just a wrapper around the HTTP endpoint?** It never calls Flask. It has its own handler
  and calls `AttendanceService` directly — the same code Flask calls.
* **Why PATCH for check-out?** It changes the state of an existing resource (`open` → `closed`).
* **Why store only successful responses under an idempotency key?** Failures changed nothing, so
  re-executing is safe, and a failed scan should be retryable.
* **Why does the breaker ignore 429?** The server is healthy, just limiting us; opening the breaker would
  punish a polite client. We wait `Retry-After` instead.
* **Why a `[5m]` window?** Smooths noise with few samples but still reacts within minutes.
* **Why server-side duration?** It excludes network/client effects and is what Prometheus can scrape; client-side
  would include network time but needs the caller to expose metrics.
* **Why message size, not in-flight?** See 4.2.

## 8. Rubric → where the evidence is

| Rubric item | Evidence |
|---|---|
| REST table | §2.1 (copy into the report) |
| OpenAPI explanation + use | §2.4, `openapi.yaml`, `/docs`, parity test |
| Before/after table + other endpoints | §2.2, §2.3 |
| Valid / invalid / repeated request tests | `tests/test_http_api.py`, `docs/evidence/http_and_grpc_examples.txt` |
| Idempotency | §2.5, tests, replay in evidence |
| Rate limiting | §2.6, `docs/evidence/rate_limit_output.txt` |
| Python caller + reliability results | §2.7, `docs/evidence/http_caller_output.txt` |
| gRPC: proto, generation, client | `proto/`, `scripts/generate_proto.py`, `clients/grpc_client.py` |
| Success + failure through both, status explanation | `docs/evidence/http_and_grpc_examples.txt`, `http_vs_grpc_table.txt`, §3 |
| Call-path diagram | `docs/call_paths_diagram.png` |
| Logs/trace across gRPC | `docs/evidence/log_correlation.txt`, §3 |
| Metric audit / gap table | §4.1 |
| New metrics (purpose, type, unit, labels, location, query) | §4.2 |
| Grafana dashboard | `observability/grafana/dashboards/http-vs-grpc-dashboard.json` |
| Prometheus scrapes gRPC | `observability/prometheus/prometheus.yml` |
