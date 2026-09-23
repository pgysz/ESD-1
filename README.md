# Biometric Attendance Tracker — Full Observability Stack

A simulated biometric attendance system (face / fingerprint / voice) with
full metrics (Prometheus + Grafana) and logging (Filebeat + Elasticsearch +
Kibana) wired in, plus Node Exporter for host metrics and a traffic
generator script for load testing and two Part E experiments.

No real biometric capture, ML model, or personal data is involved anywhere
— every "scan" is a mocked random pass/fail with an artificial delay.

## Architecture

```
Your machine (host):
  Flask app (app.py)            — python app.py, port 5000
  logs/app.log                  — structured JSON logs written here
  traffic_generator.py          — run manually to generate load

Docker Desktop (docker compose):
  Prometheus     :9090  — scrapes the app's /metrics + Node Exporter
  Node Exporter  :9100  — host CPU / memory / disk / network metrics
  Grafana        :3000  — dashboards, auto-provisioned from Prometheus
  Elasticsearch  :9200  — stores parsed logs
  Filebeat              — tails logs/app.log, ships to Elasticsearch
  Kibana         :5601  — search and visualize logs
```

The app runs directly on your host (not in a container). Filebeat reads
its log file via a read-only volume mount, so nothing needs to be
containerized on the app side.

## Prerequisites

- Python 3.9+
- Docker Desktop, running, with at least ~4GB RAM allocated
  (Settings → Resources in Docker Desktop)

## Setup

```bash
git clone <this-repo-url>
cd attendance-app
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Running everything

**1. Start the Flask app** (in its own terminal, leave it running):

```bash
python app.py
```

Visit **http://localhost:5000** for the web page, or
**http://localhost:5000/metrics** to see the raw Prometheus metrics text.

**2. Start the observability stack** (in a second terminal):

```bash
docker compose up -d
```

This starts Prometheus, Node Exporter, Grafana, Elasticsearch, Kibana, and
Filebeat. First run will take a minute or two while images download.

Check everything came up:

```bash
docker compose ps
```

If a container is unhealthy or restarting, check its logs, e.g.:

```bash
docker compose logs -f elasticsearch
```

**3. Apply the log retention (ILM) policy** — one-time, after Elasticsearch
is up:

```bash
chmod +x observability/elasticsearch/setup-ilm.sh
./observability/elasticsearch/setup-ilm.sh
```

This tells Elasticsearch to delete attendance logs after 3 days.

**4. Generate some traffic** (in a third terminal):

```bash
python traffic_generator.py --requests 200 --rate 5
```

This fires check-ins, check-outs, and signups against random users so
Prometheus and Elasticsearch have real data to show.

## Where to look

| Tool | URL | What you'll see |
|---|---|---|
| Grafana | http://localhost:3000 (user: `admin`, pass: `admin`) | "Attendance System Observability" dashboard, auto-loaded |
| Prometheus | http://localhost:9090 | Raw metrics, PromQL query box, scrape target status |
| Kibana | http://localhost:5601 | Create a Data View on `attendance-logs-*` to search logs |
| Elasticsearch | http://localhost:9200 | REST API, mostly used by the setup script |

## Metrics tracked

| Metric | Type | Purpose |
|---|---|---|
| `http_requests_total` | Counter | Total requests, by endpoint/method/status |
| `http_request_duration_seconds` | Histogram | Request latency → p95/p99 via `histogram_quantile` |
| `in_flight_requests` | Gauge | Requests currently being processed |
| `app_errors_total` | Counter | 4xx/5xx responses |
| `scan_attempts_total` | Counter | Every scan, by method/action/result |
| `scan_duration_seconds` | Histogram | Simulated biometric match latency |
| `checkins_total` | Counter | Successful check-ins, by on_time/late |
| `checkouts_total` | Counter | Successful check-outs |
| `currently_checked_in` | Gauge | People checked in right now |
| `signups_total` | Counter | Signup attempts, by result |
| `signup_duration_seconds` | Summary | Avg signup time (no percentiles — deliberately shows the Python Summary limitation) |
| `demo_requests_total` | Counter | Demo-only, used for the cardinality experiment |

## Logs

Every scan/signup/check-in/check-out event is written as one JSON line to
`logs/app.log`:

```json
{"time": "2026-09-23T00:19:02.323261+00:00", "service": "attendance-app", "severity": "INFO", "message": "checkin_success method=voice status=on_time duration_ms=338.8", "request_id": "98c40af6-182c-48ad-a747-b9a5893ac984"}
```

Fields: `time`, `service`, `severity`, `message`, `request_id`. No user ID,
name, or other personal information is ever logged.

Filebeat's `json.keys_under_root: true` setting promotes these fields to
the top level of each Elasticsearch document, so in Kibana you can filter
directly on `severity: ERROR` or search a specific `request_id` to find
everything about one request.

## Part E experiments

**1. Latency injection**

```bash
# Baseline
python traffic_generator.py --requests 100 --rate 5

# Inject: 500ms delay on every 5th request
python traffic_generator.py --requests 100 --rate 5 --inject-delay

# Recovery: baseline again
python traffic_generator.py --requests 100 --rate 5
```

Watch the "HTTP Latency p95 / p99" panel in Grafana across all three
stages — p99 should spike during the injected stage and recover after.

**2. Cardinality explosion**

```bash
python traffic_generator.py --cardinality-demo
```

Sends exactly 100 requests to `/api/demo/scan`, each with a unique
`request_id` label. In Prometheus or Grafana, run:

```
count(demo_requests_total)
```

It should read exactly 100 — one time series per unique label value. This
demonstrates why request IDs (or any unbounded value) must never be used
as a metric label — they belong in logs instead, which is why
`request_id` appears in `logs/app.log` but nowhere in a real business
metric.

## Clean up

```bash
# Stop the Flask app: Ctrl+C in its terminal

# Stop and remove all containers:
docker compose down

# Also wipe stored metrics/logs/dashboards data:
docker compose down -v
```

## Notes

- App state (who's checked in) is in-memory and resets when `app.py`
  restarts.
- `docker-compose.yml` uses `host.docker.internal` so Prometheus (running
  in a container) can reach the Flask app (running on your host).
- Elasticsearch runs single-node with security disabled — fine for a local
  demo, not how you'd run it in production.
