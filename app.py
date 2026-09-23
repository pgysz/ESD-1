"""
Biometric Attendance Tracking System (simulated)
-------------------------------------------------
A minimal Flask app that simulates face / fingerprint / voice scans to
mark employees present, late, or absent, and lets them check out again.

The "biometric matching" itself is fully mocked: a scan has a random
chance of failing (simulating a bad read) and an artificial processing
delay (simulating model inference time). No real biometric data is
collected or stored.

This app is the base on which Prometheus metrics and structured JSON
logging will be layered in later steps.
"""

import json
import logging
import os
import random
import time
import uuid
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request, send_from_directory, g
from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    Summary,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

app = Flask(__name__, static_folder="static", static_url_path="")

# ---------------------------------------------------------------------------
# Structured JSON logging
# ---------------------------------------------------------------------------
# Every log line is a single JSON object written to logs/app.log, with no
# user-identifying information at all (no user_id, no name) — only what is
# needed to diagnose a request: time, service, severity, message, request_id.

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "app.log")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "time": datetime.now(timezone.utc).isoformat(),
            "app_name": "attendance-app",
            "severity": record.levelname,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
        }
        return json.dumps(payload)


app_logger = logging.getLogger("attendance")
app_logger.setLevel(logging.INFO)
_file_handler = logging.FileHandler(LOG_FILE)
_file_handler.setFormatter(JsonFormatter())
app_logger.addHandler(_file_handler)
app_logger.propagate = False


def log_event(severity, message, request_id=None):
    app_logger.log(
        getattr(logging, severity.upper(), logging.INFO),
        message,
        extra={"request_id": request_id},
    )

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
# App metrics — generic to every request, regardless of what the endpoint does.

HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total HTTP requests received",
    ["endpoint", "method", "status_code"],
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["endpoint"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5),
)

IN_FLIGHT_REQUESTS = Gauge(
    "in_flight_requests",
    "Number of requests currently being processed",
)

APP_ERRORS_TOTAL = Counter(
    "app_errors_total",
    "Total requests that resulted in a 4xx or 5xx response",
    ["endpoint", "status_code"],
)

# Business metrics — specific to the attendance domain.

SCAN_ATTEMPTS_TOTAL = Counter(
    "scan_attempts_total",
    "Total biometric scan attempts (check-in or check-out)",
    ["scan_method", "action", "result"],
)

SCAN_DURATION_SECONDS = Histogram(
    "scan_duration_seconds",
    "Simulated biometric scan/match duration in seconds",
    ["scan_method"],
    buckets=(0.02, 0.05, 0.1, 0.2, 0.3, 0.5, 1),
)

CHECKINS_TOTAL = Counter(
    "checkins_total",
    "Total successful check-ins",
    ["status"],
)

CHECKOUTS_TOTAL = Counter(
    "checkouts_total",
    "Total successful check-outs",
)

CURRENTLY_CHECKED_IN = Gauge(
    "currently_checked_in",
    "Number of people currently checked in",
)

SIGNUPS_TOTAL = Counter(
    "signups_total",
    "Total new-user signup attempts",
    ["result"],
)

# Summary (not histogram) on purpose: with prometheus_client's Python
# implementation, Summary only gives count/sum (i.e. an average), not real
# quantiles. This is intentional so the report can show that limitation.
SIGNUP_DURATION_SECONDS = Summary(
    "signup_duration_seconds",
    "Simulated signup/enrollment duration in seconds",
)

# Dedicated demo counter for the cardinality-explosion experiment (Part E).
# NEVER label real business metrics with request_id — this counter exists
# purely to demonstrate why that is a bad idea, and is not used elsewhere.
DEMO_REQUESTS_TOTAL = Counter(
    "demo_requests_total",
    "Demo-only counter used to illustrate cardinality explosion via request_id labels",
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCAN_METHODS = ["face", "fingerprint", "voice"]

# Chance that a scan attempt fails to match (simulated bad read), per method.
# Kept different per method so the demo has some natural variety.
SCAN_FAILURE_RATE = {
    "face": 0.08,
    "fingerprint": 0.05,
    "voice": 0.12,
}

# Simulated processing delay per method, in seconds (min, max).
SCAN_DELAY_RANGE = {
    "face": (0.05, 0.25),
    "fingerprint": (0.02, 0.10),
    "voice": (0.10, 0.40),
}

# Shift start time and grace period used to decide "on time" vs "late".
SHIFT_START_HOUR = 9
SHIFT_START_MINUTE = 0
GRACE_PERIOD_MINUTES = 10

# A starting set of "enrolled" users for the demo. In a real system these
# would come from a database with actual enrolled biometric templates.
# New users can also be added at runtime via POST /api/signup.
ENROLLED_USERS = {
    "u1": "Ali Raza",
    "u2": "Sara Khan",
    "u3": "Bilal Ahmed",
    "u4": "Ayesha Noor",
    "u5": "Hamza Tariq",
    "u6": "Zainab Malik",
    "u7": "Usman Sheikh",
    "u8": "Mahnoor Aziz",
    "u9": "Fahad Iqbal",
    "u10": "Hira Baig",
    "u11": "Omar Farooq",
    "u12": "Sana Yousuf",
    "u13": "Danish Rehman",
    "u14": "Iqra Siddiqui",
    "u15": "Kashif Nawaz",
    "u16": "Rabia Hassan",
    "u17": "Junaid Aslam",
    "u18": "Nida Chaudhry",
    "u19": "Waqas Anwar",
    "u20": "Mehreen Qureshi",
}

# Counter used to generate new user IDs at signup time, kept in sync with
# whatever the highest existing "uN" id is so it never collides.
_next_user_number = max(int(uid[1:]) for uid in ENROLLED_USERS) + 1

# Simulated signup latency, in seconds (min, max) — e.g. writing a new
# enrollment record / biometric template to storage.
SIGNUP_DELAY_RANGE = (0.05, 0.20)

# Chance a signup attempt fails (e.g. duplicate/invalid data, simulated
# storage error). Kept low but non-zero so it is a meaningful metric.
SIGNUP_FAILURE_RATE = 0.05

# ---------------------------------------------------------------------------
# In-memory state (fine for a demo app; not persisted across restarts)
# ---------------------------------------------------------------------------

# user_id -> {"check_in": datetime, "status": "on_time"/"late", "method": str}
currently_checked_in = {}

# Flat list of every event (check-in attempts, check-outs, failures) so the
# frontend can show a simple activity feed. Newest first.
event_log = []

MAX_EVENT_LOG = 200


def _record_event(event):
    event["timestamp"] = event["timestamp"].isoformat()
    event_log.insert(0, event)
    del event_log[MAX_EVENT_LOG:]

    # Mirror every in-memory event as a structured JSON log line too. No
    # user-identifying fields are forwarded (user_id/name are dropped) —
    # only the event type, request_id, and a human-readable message.
    severity = "ERROR" if event["type"].endswith("failed") else "INFO"
    detail_bits = []
    if "method" in event:
        detail_bits.append(f"method={event['method']}")
    if "status" in event:
        detail_bits.append(f"status={event['status']}")
    if "reason" in event:
        detail_bits.append(f"reason={event['reason']}")
    if "duration_ms" in event:
        detail_bits.append(f"duration_ms={event['duration_ms']}")
    message = f"{event['type']} " + " ".join(detail_bits)
    log_event(severity, message.strip(), request_id=event.get("request_id"))


# ---------------------------------------------------------------------------
# Generic per-request instrumentation (applies to every route)
# ---------------------------------------------------------------------------


@app.before_request
def _before_request():
    g._start_time = time.time()
    IN_FLIGHT_REQUESTS.inc()
    if request.headers.get("X-Inject-Delay") == "true":
        time.sleep(0.5)
        log_event("WARNING", f"high_latency_injected endpoint={request.path} delay_ms=500")
@app.after_request
def _after_request(response):
    # Group dynamic/unknown paths under request.path is fine here since this
    # is a small, fixed set of routes (no unbounded IDs in the URL path).
    endpoint = request.path
    duration = time.time() - getattr(g, "_start_time", time.time())

    HTTP_REQUESTS_TOTAL.labels(
        endpoint=endpoint, method=request.method, status_code=response.status_code
    ).inc()
    HTTP_REQUEST_DURATION_SECONDS.labels(endpoint=endpoint).observe(duration)

    if response.status_code >= 400:
        APP_ERRORS_TOTAL.labels(endpoint=endpoint, status_code=response.status_code).inc()

    IN_FLIGHT_REQUESTS.dec()
    return response


@app.route("/metrics")
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}


def _simulate_scan(method, action):
    """Simulate a biometric scan attempt.

    Returns (success: bool, duration_seconds: float). Also records the
    scan_duration_seconds histogram and scan_attempts_total counter.
    """
    lo, hi = SCAN_DELAY_RANGE[method]
    duration = random.uniform(lo, hi)
    time.sleep(duration)
    success = random.random() > SCAN_FAILURE_RATE[method]

    SCAN_DURATION_SECONDS.labels(scan_method=method).observe(duration)
    SCAN_ATTEMPTS_TOTAL.labels(
        scan_method=method, action=action, result="success" if success else "failure"
    ).inc()

    return success, duration


def _attendance_status_for(check_in_time):
    """Return 'on_time' or 'late' based on the shift start + grace period."""
    shift_start = check_in_time.replace(
        hour=SHIFT_START_HOUR, minute=SHIFT_START_MINUTE, second=0, microsecond=0
    )
    cutoff = shift_start + timedelta(minutes=GRACE_PERIOD_MINUTES)
    return "on_time" if check_in_time <= cutoff else "late"


# ---------------------------------------------------------------------------
# Routes: frontend
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---------------------------------------------------------------------------
# Routes: API
# ---------------------------------------------------------------------------


@app.route("/api/users", methods=["GET"])
def list_users():
    return jsonify(
        [{"user_id": uid, "name": name} for uid, name in ENROLLED_USERS.items()]
    )


@app.route("/api/signup", methods=["POST"])
def signup():
    """Enroll a new user (simulated). This is the runtime equivalent of a
    new employee registering their biometric templates for the first time.
    """
    global _next_user_number

    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    request_id = str(uuid.uuid4())
    now = datetime.now()

    if not name:
        _record_event(
            {
                "type": "signup_failed",
                "name": None,
                "reason": "missing_name",
                "request_id": request_id,
                "timestamp": now,
            }
        )
        return (
            jsonify({"error": "name is required", "request_id": request_id}),
            400,
        )

    # Simulate the work of writing a new enrollment record / template.
    lo, hi = SIGNUP_DELAY_RANGE
    duration = random.uniform(lo, hi)
    time.sleep(duration)
    success = random.random() > SIGNUP_FAILURE_RATE

    SIGNUP_DURATION_SECONDS.observe(duration)
    SIGNUPS_TOTAL.labels(result="success" if success else "failed").inc()

    if not success:
        _record_event(
            {
                "type": "signup_failed",
                "name": name,
                "reason": "enrollment_error",
                "request_id": request_id,
                "duration_ms": round(duration * 1000, 1),
                "timestamp": now,
            }
        )
        return (
            jsonify(
                {
                    "success": False,
                    "reason": "enrollment_error",
                    "duration_ms": round(duration * 1000, 1),
                    "request_id": request_id,
                }
            ),
            500,
        )

    user_id = f"u{_next_user_number}"
    _next_user_number += 1
    ENROLLED_USERS[user_id] = name

    _record_event(
        {
            "type": "signup_success",
            "user_id": user_id,
            "name": name,
            "request_id": request_id,
            "duration_ms": round(duration * 1000, 1),
            "timestamp": now,
        }
    )

    return jsonify(
        {
            "success": True,
            "user_id": user_id,
            "name": name,
            "duration_ms": round(duration * 1000, 1),
            "request_id": request_id,
        }
    )


@app.route("/api/checkin", methods=["POST"])
def checkin():
    data = request.get_json(force=True, silent=True) or {}
    user_id = data.get("user_id")
    method = data.get("method")
    request_id = str(uuid.uuid4())

    if user_id not in ENROLLED_USERS:
        return jsonify({"error": "unknown user_id", "request_id": request_id}), 404
    if method not in SCAN_METHODS:
        return jsonify({"error": "invalid method", "request_id": request_id}), 400

    success, duration = _simulate_scan(method, action="checkin")
    now = datetime.now()

    if not success:
        _record_event(
            {
                "type": "checkin_failed",
                "user_id": user_id,
                "name": ENROLLED_USERS[user_id],
                "method": method,
                "request_id": request_id,
                "duration_ms": round(duration * 1000, 1),
                "timestamp": now,
            }
        )
        return (
            jsonify(
                {
                    "success": False,
                    "reason": "scan_not_matched",
                    "method": method,
                    "duration_ms": round(duration * 1000, 1),
                    "request_id": request_id,
                }
            ),
            401,
        )

    if user_id in currently_checked_in:
        return (
            jsonify(
                {
                    "success": False,
                    "reason": "already_checked_in",
                    "request_id": request_id,
                }
            ),
            409,
        )

    status = _attendance_status_for(now)
    currently_checked_in[user_id] = {
        "check_in": now,
        "status": status,
        "method": method,
    }

    CHECKINS_TOTAL.labels(status=status).inc()
    CURRENTLY_CHECKED_IN.set(len(currently_checked_in))

    _record_event(
        {
            "type": "checkin_success",
            "user_id": user_id,
            "name": ENROLLED_USERS[user_id],
            "method": method,
            "status": status,
            "request_id": request_id,
            "duration_ms": round(duration * 1000, 1),
            "timestamp": now,
        }
    )

    return jsonify(
        {
            "success": True,
            "user_id": user_id,
            "name": ENROLLED_USERS[user_id],
            "status": status,
            "method": method,
            "duration_ms": round(duration * 1000, 1),
            "request_id": request_id,
        }
    )


@app.route("/api/checkout", methods=["POST"])
def checkout():
    data = request.get_json(force=True, silent=True) or {}
    user_id = data.get("user_id")
    method = data.get("method")
    request_id = str(uuid.uuid4())

    if user_id not in ENROLLED_USERS:
        return jsonify({"error": "unknown user_id", "request_id": request_id}), 404
    if method not in SCAN_METHODS:
        return jsonify({"error": "invalid method", "request_id": request_id}), 400
    if user_id not in currently_checked_in:
        return (
            jsonify(
                {
                    "success": False,
                    "reason": "not_checked_in",
                    "request_id": request_id,
                }
            ),
            409,
        )

    success, duration = _simulate_scan(method, action="checkout")
    now = datetime.now()

    if not success:
        _record_event(
            {
                "type": "checkout_failed",
                "user_id": user_id,
                "name": ENROLLED_USERS[user_id],
                "method": method,
                "request_id": request_id,
                "duration_ms": round(duration * 1000, 1),
                "timestamp": now,
            }
        )
        return (
            jsonify(
                {
                    "success": False,
                    "reason": "scan_not_matched",
                    "method": method,
                    "duration_ms": round(duration * 1000, 1),
                    "request_id": request_id,
                }
            ),
            401,
        )

    session = currently_checked_in.pop(user_id)
    worked_seconds = (now - session["check_in"]).total_seconds()

    CHECKOUTS_TOTAL.inc()
    CURRENTLY_CHECKED_IN.set(len(currently_checked_in))

    _record_event(
        {
            "type": "checkout_success",
            "user_id": user_id,
            "name": ENROLLED_USERS[user_id],
            "method": method,
            "request_id": request_id,
            "duration_ms": round(duration * 1000, 1),
            "session_seconds": round(worked_seconds, 1),
            "timestamp": now,
        }
    )

    return jsonify(
        {
            "success": True,
            "user_id": user_id,
            "name": ENROLLED_USERS[user_id],
            "method": method,
            "duration_ms": round(duration * 1000, 1),
            "session_seconds": round(worked_seconds, 1),
            "request_id": request_id,
        }
    )


@app.route("/api/status", methods=["GET"])
def status():
    checked_in_list = [
        {
            "user_id": uid,
            "name": ENROLLED_USERS[uid],
            "status": info["status"],
            "method": info["method"],
            "check_in": info["check_in"].isoformat(),
        }
        for uid, info in currently_checked_in.items()
    ]
    return jsonify(
        {
            "checked_in_count": len(currently_checked_in),
            "checked_in": checked_in_list,
            "recent_events": event_log[:20],
        }
    )


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/api/demo/scan", methods=["POST"])
def demo_scan():
    """Cardinality-explosion demo endpoint (Part E only).

    Increments demo_requests_total with whatever request_id the caller
    supplies. This is DELIBERATELY the wrong pattern — a real request_id
    should only ever go into logs, never into a metric label — used here
    to demonstrate why, not as something the real app relies on.
    """
    data = request.get_json(force=True, silent=True) or {}
    request_id = data.get("request_id") or str(uuid.uuid4())
    DEMO_REQUESTS_TOTAL.inc()
    return jsonify({"request_id": request_id, "recorded": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
