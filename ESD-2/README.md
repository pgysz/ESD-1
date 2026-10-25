# Biometric Attendance Tracker — Assignment 2 (HTTP API, gRPC, Metrics)

A simulated biometric attendance system (face / fingerprint / voice). Assignment 1 added
metrics (Prometheus + Grafana) and logging (Filebeat + Elasticsearch + Kibana). Assignment 2
extends it with:

* **Part A** — a redesigned REST API (`/api/v1/...`) with OpenAPI, idempotency, rate limiting,
  `application/problem+json` errors, and a resilient Python caller (timeout, retry, circuit breaker).
* **Part B** — the **check-in** operation as a **gRPC service** (separate process) that shares the
  same business rules and the same database as the HTTP API.
* **Part C** — audited and extended metrics, with a Grafana dashboard comparing HTTP and gRPC.

No real biometrics are involved: every scan is a mocked random pass/fail with an artificial delay.

> **Want to understand every change?** Read [`docs/A2_CHANGES.md`](docs/A2_CHANGES.md). It lists
> each change from Assignment 1, why it was made, which rubric item it serves, and where to see it.

## Architecture

```
HOST (your machine)
  app.py                         Flask HTTP API        :5000   (+ /metrics, /docs)
  python -m grpc_service.server  gRPC service          :50051  (+ metrics on :9102)
        both import  attendance_core/  (same rules)  and share  data/attendance.db  (SQLite)
  logs/app.log, logs/grpc.log    JSON logs (same trace_id across both)

DOCKER (docker compose)
  Prometheus :9090   scrapes :5000, :9102 and Node Exporter
  Grafana    :3000   two dashboards (A1 + "HTTP vs gRPC")
  Filebeat -> Elasticsearch :9200 -> Kibana :5601
  Node Exporter :9100
```

Call-path diagram: [`docs/call_paths_diagram.png`](docs/call_paths_diagram.png).

## Prerequisites

* Python 3.9+ (3.12 tested), Docker Desktop (≥ 4 GB RAM for Elasticsearch)

## Setup

```bash
python3 -m venv venv
source venv/bin/activate              # Windows: venv\Scripts\activate
pip install -r requirements-dev.txt   # runtime + test + proto-generation tools
```

## Run everything

Open one terminal per line (all from the project root, venv activated):

```bash
python app.py                           # 1. HTTP API on :5000
python -m grpc_service.server           # 2. gRPC service on :50051, metrics on :9102
docker compose up -d                    # 3. Prometheus, Grafana, Elasticsearch, Kibana, Filebeat, Node Exporter
./observability/elasticsearch/setup-ilm.sh   # 4. one-time: 3-day log retention
```

**Upgrading from the Assignment 1 stack?** Two config files changed, so restart those containers:
`docker compose restart prometheus filebeat`. Grafana loads the new dashboard by itself (≤ 30 s).

For the **demos and tests of failure handling** start the servers like this instead (fault
injection is opt-in; `SCAN_FAILURE_RATE=0` stops random scan mismatches muddying the output):

```bash
# macOS / Linux
ENABLE_FAULT_INJECTION=1 SCAN_FAILURE_RATE=0 SIGNUP_FAILURE_RATE=0 python app.py
ENABLE_FAULT_INJECTION=1 SCAN_FAILURE_RATE=0 python -m grpc_service.server
# Windows PowerShell
$env:ENABLE_FAULT_INJECTION="1"; $env:SCAN_FAILURE_RATE="0"; $env:SIGNUP_FAILURE_RATE="0"; python app.py
```

## Where to look

| What | URL |
|---|---|
| Web page | http://localhost:5000 |
| Swagger UI (try the API) | http://localhost:5000/docs  (spec: `/openapi.yaml`, also `openapi.yaml`) |
| HTTP metrics / gRPC metrics | http://localhost:5000/metrics · http://localhost:9102/metrics |
| Grafana (admin / admin) | http://localhost:3000 → dashboards *Attendance: HTTP vs gRPC (Assignment 2)* and *Attendance System Observability* |
| Prometheus | http://localhost:9090 |
| Kibana | http://localhost:5601 (Data View `attendance-logs-*`; search `trace_id : "<id>"`) |

## Demos (one per rubric item)

```bash
# Part A — valid request, invalid input, repeated request (see also the tests below)
curl -i -X POST localhost:5000/api/v1/attendance -H 'Content-Type: application/json' \
     -H 'Idempotency-Key: demo-1' -d '{"user_id":"u1","method":"face"}'          # 201 Created
curl -i -X POST localhost:5000/api/v1/attendance -H 'Content-Type: application/json' \
     -d '{"user_id":"u1","method":"retina"}'                                      # 400 problem+json
curl -i -X POST localhost:5000/api/v1/attendance -H 'Content-Type: application/json' \
     -H 'Idempotency-Key: demo-1' -d '{"user_id":"u1","method":"face"}'          # 201 + Idempotent-Replayed: true

# Part A — rate limiting (start the server with RATE_LIMIT_SIGNUP=5/10 for a quick demo)
python scripts/rate_limit_demo.py            # accepted calls, 429 + Retry-After, allowed again

# Part A — resilient caller: success, retry, breaker opens, calls blocked, recovery
python clients/http_caller.py --demo         # needs ENABLE_FAULT_INJECTION=1 on the HTTP server

# Part B — gRPC client (sets a deadline, prints result or status code)
python clients/grpc_client.py --user-id u2 --method face
python clients/grpc_client.py --user-id u2 --method face        # ALREADY_EXISTS
python clients/grpc_client.py --user-id u3 --deadline 0.2 --inject-delay-ms 600   # DEADLINE_EXCEEDED (fault injection on)

# Part C — populate the HTTP-vs-gRPC dashboard with repeatable success + failure calls
python scripts/demo_calls.py --rounds 10     # then wait ~15 s and open the dashboard

# A1 experiments still work
python traffic_generator.py --requests 200 --rate 5
python traffic_generator.py --requests 200 --rate 5 --inject-delay   # fault injection on
python traffic_generator.py --cardinality-demo
```

Real outputs of these runs are saved in [`docs/evidence/`](docs/evidence/).

## Tests

```bash
python -m pytest tests -v        # 82 tests, ~3 s, no Docker or running server needed
```

Covers: valid / invalid / repeated requests, the common error format, idempotency (including a
concurrency race), 429 + `Retry-After`, OpenAPI ⇔ routes parity, gRPC status codes, deadline,
HTTP ⇄ gRPC shared state, trace-id propagation into logs, metric label hygiene, circuit breaker
state machine, retry policy, and rate-limiter timing.

## Regenerate the gRPC code (only if you edit `proto/attendance.proto`)

```bash
python scripts/generate_proto.py
# which is: python -m grpc_tools.protoc -I proto --python_out=grpc_service \
#           --grpc_python_out=grpc_service proto/attendance.proto
# plus a one-line import fix so the generated files work inside the grpc_service package
```

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `ATTENDANCE_DB` | `data/attendance.db` | shared SQLite file (both processes must use the same one) |
| `ATTENDANCE_LOG_DIR` | `logs/` | where `app.log` / `grpc.log` are written |
| `ENABLE_FAULT_INJECTION` | `0` | `1` enables `X-Inject-Delay`, `X-Inject-Fault` and gRPC `x-inject-delay-ms` |
| `SCAN_FAILURE_RATE` | per method (5–12 %) | force every scan's failure probability (`0` = always succeed) |
| `SIGNUP_FAILURE_RATE` | `0.05` | force signup failure probability |
| `RATE_LIMIT_SIGNUP` | `10/60` | `<requests>/<seconds>` limit for `POST /api/v1/users`, per client IP |
| `GRPC_PORT` / `GRPC_METRICS_PORT` | `50051` / `9102` | gRPC and its Prometheus port |

## Project layout

```
app.py                     Flask HTTP transport (routes, errors, rate limit, HTTP metrics)
attendance_core/           shared by HTTP and gRPC: service (rules), store (SQLite), metrics,
                           logging, tracing, errors, rate limiter, config
grpc_service/              server.py (handler), interceptors.py (metrics/logs), generated stubs
proto/attendance.proto     the gRPC contract
openapi.yaml               the HTTP contract
clients/                   resilience.py (breaker/retry), http_caller.py, grpc_client.py
scripts/                   generate_proto.py, demo_calls.py, rate_limit_demo.py
tests/                     pytest suite
observability/             Prometheus, Grafana (2 dashboards), Filebeat, Elasticsearch config
docs/                      A2_CHANGES.md, call_paths_diagram.png, evidence/
traffic_generator.py       A1 load generator (updated to the new API)
```

## Clean up

```bash
# stop app.py and the gRPC server with Ctrl+C
docker compose down        # keep stored data
docker compose down -v     # also wipe Prometheus/Grafana/Elasticsearch data
rm -rf data logs           # also delete the attendance database and log files
```

## Notes and limits

* State now lives in SQLite, so **check-ins survive restarts** (in A1 they were lost). Delete `data/`
  for a fresh start.
* The rate limiter and idempotency store are single-instance by design (the assignment allows it);
  with several app instances the limiter would need a shared store such as Redis.
* Swagger UI loads its JavaScript from a CDN (unpkg), so `/docs` needs internet access.
* Reused/assisted work is credited in the report (AI-assisted development).
