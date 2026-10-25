"""Biometric Attendance Tracker -- HTTP API (Assignment 2 version).

This file is only the HTTP *transport*: routing, JSON parsing, status codes,
error format, rate limiting, metrics. All attendance rules live in
attendance_core (shared with the gRPC service).

Run:  python app.py        (see README.md for the full stack)
"""

import json
import math
import threading
import time
import uuid

from flask import Flask, g, jsonify, make_response, request, send_from_directory
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from werkzeug.exceptions import HTTPException, UnsupportedMediaType

from attendance_core import config, tracing
from attendance_core import metrics as biz
from attendance_core.errors import DomainError, RateLimitedError, ScanNotMatchedError, ValidationError
from attendance_core.logging_setup import configure_logging, log_event
from attendance_core.ratelimit import SlidingWindowRateLimiter, parse_limit
from attendance_core.service import AttendanceService
from attendance_core.store import Store

PROBLEM_BASE = "https://attendance.local/problems"

# ---------------------------------------------------------------------------
# HTTP (transport-level) Prometheus metrics
# ---------------------------------------------------------------------------
BUCKETS = biz.LATENCY_BUCKETS
SIZE_BUCKETS = (16, 32, 64, 128, 256, 512, 1024, 2048, 4096)

HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total", "Total HTTP requests received",
    ["endpoint", "method", "status_code"])
# A2: added the `method` label so POST (check-in) and GET (list) on the same
# path can be told apart; `endpoint` is now the route TEMPLATE, not the raw path.
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds", "HTTP request latency in seconds",
    ["endpoint", "method"], buckets=BUCKETS)
IN_FLIGHT_REQUESTS = Gauge(
    "in_flight_requests", "Number of requests currently being processed")
APP_ERRORS_TOTAL = Counter(
    "app_errors_total", "Total requests that resulted in a 4xx or 5xx response",
    ["endpoint", "status_code"])
# A2: new, same shape as the gRPC metric so the two can be compared.
HTTP_MESSAGE_SIZE_BYTES = Histogram(
    "http_message_size_bytes", "HTTP request/response body size in bytes",
    ["endpoint", "direction"], buckets=SIZE_BUCKETS)

# A1 cardinality-experiment counter: deliberately left in its 'fixed' state
# (no request_id label), exactly as submitted for Assignment 1.
DEMO_REQUESTS_TOTAL = Counter(
    "demo_requests_total",
    "Demo-only counter used to illustrate cardinality explosion via request_id labels")

# These paths are not application traffic: Prometheus scrapes /metrics every 5 s,
# which would otherwise dominate the request counts and the latency percentiles.
UNMETERED_PATHS = {"/metrics", "/api/health"}


def create_app(service=None, limiter=None):
    app = Flask(__name__, static_folder="static", static_url_path="")
    if service is None:
        logger = configure_logging("attendance-app", "app.log")
        service = AttendanceService(Store(), logger)
    else:
        logger = service.logger          # tests inject a service with a capturing logger
    if limiter is None:
        limit, window = parse_limit(config.signup_rate_limit())
        limiter = SlidingWindowRateLimiter(limit, window)
    svc = service
    fault_counts, fault_lock = {}, threading.Lock()

    if config.fault_injection_enabled():
        log_event(logger, "WARNING", "fault_injection_enabled via ENABLE_FAULT_INJECTION=1")

    # ------------------------------------------------------------------
    # Error format: RFC 9457 application/problem+json everywhere
    # ------------------------------------------------------------------
    def problem_response(status, slug, title, detail, *, errors=None, headers=None):
        body = {
            "type": f"{PROBLEM_BASE}/{slug}", "title": title, "status": status,
            "detail": detail, "instance": request.path,
            "request_id": g.get("request_id"), "trace_id": g.get("trace_id"),
        }
        if errors:
            body["errors"] = errors
        resp = make_response(json.dumps(body), status)
        resp.headers["Content-Type"] = "application/problem+json"
        for name, value in (headers or {}).items():
            resp.headers[name] = value
        return resp

    @app.errorhandler(DomainError)
    def _domain_error(exc):
        headers = dict(exc.headers)
        if isinstance(exc, ScanNotMatchedError):      # 401 requires WWW-Authenticate
            headers["WWW-Authenticate"] = 'Biometric realm="attendance"'
        return problem_response(exc.http_status, exc.slug, exc.title, exc.detail,
                                errors=exc.errors, headers=headers)

    @app.errorhandler(HTTPException)
    def _http_error(exc):
        slug = {404: "not-found", 405: "method-not-allowed",
                415: "unsupported-media-type"}.get(exc.code, "http-error")
        headers = {}
        if getattr(exc, "valid_methods", None):
            headers["Allow"] = ", ".join(exc.valid_methods)
        return problem_response(exc.code, slug, exc.name, exc.description, headers=headers)

    @app.errorhandler(Exception)
    def _unexpected(exc):
        # Never leak internals to the client; the details go to the log.
        logger.error("unhandled_exception", exc_info=exc)
        return problem_response(500, "internal-error", "Internal server error",
                                "An unexpected error occurred. Quote the request_id when reporting it.")

    # ------------------------------------------------------------------
    # Per-request plumbing: ids, fault injection, metrics
    # ------------------------------------------------------------------
    def maybe_inject_fault():
        """Test-only. Honoured only when ENABLE_FAULT_INJECTION=1.

        X-Inject-Delay: true               A1 experiment: +500 ms.
        X-Inject-Fault: delay=<ms>,status=<5xx>,times=<n>
            delay  sleep this long first (capped at 10 s)
            status then answer with this 5xx status without doing the work
            times  apply only to the first n requests sharing the same
                   Idempotency-Key (so a retry can succeed afterwards)
        """
        if not config.fault_injection_enabled():
            return None
        if request.headers.get("X-Inject-Delay") == "true":
            time.sleep(0.5)
            log_event(logger, "WARNING",
                      f"high_latency_injected endpoint={request.path} delay_ms=500")
        spec = request.headers.get("X-Inject-Fault")
        if not spec:
            return None
        try:
            opts = dict(part.split("=", 1) for part in spec.split(","))
            delay_ms = min(int(opts.get("delay", 0)), 10000)
            status = int(opts["status"]) if "status" in opts else None
            times = int(opts["times"]) if "times" in opts else None
        except (ValueError, KeyError):
            return problem_response(400, "validation-error", "Invalid request",
                                    "Malformed X-Inject-Fault header.")
        if times is not None:
            key = request.headers.get("Idempotency-Key") or g.request_id
            with fault_lock:
                fault_counts[key] = fault_counts.get(key, 0) + 1
                if fault_counts[key] > times:
                    return None
        if delay_ms:
            time.sleep(delay_ms / 1000)
        if status is not None and 500 <= status <= 599:
            log_event(logger, "WARNING", f"fault_injected status={status} endpoint={request.path}")
            return problem_response(status, "injected-fault", "Injected fault",
                                    "Failure injected by the X-Inject-Fault header (test only).")
        return None

    @app.before_request
    def _begin():
        g.start = time.perf_counter()
        g.metered = request.path not in UNMETERED_PATHS
        if g.metered:
            IN_FLIGHT_REQUESTS.inc()
        incoming = tracing.parse_traceparent(request.headers.get("traceparent"))
        g.trace_id = incoming[0] if incoming else tracing.new_trace_id()
        g.request_id = tracing.sanitize_request_id(request.headers.get("X-Request-ID"))
        g.ctx_tokens = tracing.bind(g.request_id, g.trace_id)
        g.extra_headers = {}
        return maybe_inject_fault()

    @app.after_request
    def _finish(response):
        response.headers["X-Request-ID"] = g.get("request_id", "")
        if g.get("trace_id"):
            response.headers["traceparent"] = tracing.make_traceparent(
                g.trace_id, tracing.new_span_id())
        for name, value in g.get("extra_headers", {}).items():
            response.headers.setdefault(name, value)
        if request.path.startswith("/api/"):
            # REST "cacheable" constraint: say explicitly that API data changes
            # constantly and must not be reused by caches.
            response.headers.setdefault("Cache-Control", "no-store")
        if g.get("metered"):
            # Route TEMPLATE (e.g. /api/v1/attendance/<session_id>), never the raw
            # path: raw paths would create one time series per id (cardinality!).
            endpoint = request.url_rule.rule if request.url_rule else "unmatched"
            elapsed = time.perf_counter() - g.start
            HTTP_REQUESTS_TOTAL.labels(endpoint, request.method, response.status_code).inc()
            HTTP_REQUEST_DURATION_SECONDS.labels(endpoint, request.method).observe(elapsed)
            if response.status_code >= 400:
                APP_ERRORS_TOTAL.labels(endpoint, response.status_code).inc()
            HTTP_MESSAGE_SIZE_BYTES.labels(endpoint, "request").observe(request.content_length or 0)
            size = 0 if response.direct_passthrough else len(response.get_data())
            HTTP_MESSAGE_SIZE_BYTES.labels(endpoint, "response").observe(size)
            IN_FLIGHT_REQUESTS.dec()
        return response

    @app.teardown_request
    def _teardown(_exc):
        tokens = g.get("ctx_tokens")
        if tokens:
            tracing.reset(tokens)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def json_body():
        if not request.is_json:
            raise UnsupportedMediaType("Send the body as application/json.")
        data = request.get_json(silent=True)
        if data is None:
            raise ValidationError("The request body is not valid JSON.",
                                  errors=[{"field": "body", "message": "malformed JSON"}])
        if not isinstance(data, dict):
            raise ValidationError("The JSON body must be an object.",
                                  errors=[{"field": "body", "message": "must be an object"}])
        return data

    def int_arg(name, default, lo, hi):
        raw = request.args.get(name)
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError:
            value = None
        if value is None or not lo <= value <= hi:
            raise ValidationError("One or more query parameters are invalid.",
                                  errors=[{"field": name, "message": f"must be an integer {lo}-{hi}"}])
        return value

    def enforce_rate_limit(endpoint_label):
        allowed, remaining, reset = limiter.check(request.remote_addr or "unknown")
        g.extra_headers.update({
            "RateLimit-Limit": str(limiter.limit),
            "RateLimit-Remaining": str(remaining),
            "RateLimit-Reset": str(math.ceil(reset)),
        })
        if not allowed:
            biz.RATE_LIMITED_TOTAL.labels(endpoint=endpoint_label).inc()
            retry_after = max(1, math.ceil(reset))
            log_event(logger, "WARNING", f"rate_limited endpoint={endpoint_label} "
                                         f"retry_after_s={retry_after}")
            raise RateLimitedError(
                f"Limit of {limiter.limit} requests per {int(limiter.window)} seconds "
                f"exceeded. Retry in {retry_after} s.",
                headers={"Retry-After": str(retry_after)})

    def created(body, location):
        resp = jsonify(body)
        resp.status_code = 201
        resp.headers["Location"] = location
        return resp

    # ------------------------------------------------------------------
    # Routes: users
    # ------------------------------------------------------------------
    @app.post("/api/v1/users")
    def create_user():
        enforce_rate_limit("/api/v1/users")
        user = svc.signup(json_body().get("name"))
        return created(user, f"/api/v1/users/{user['user_id']}")

    @app.get("/api/v1/users")
    def list_users():
        items = svc.list_users()
        return jsonify({"items": items, "count": len(items)})

    @app.get("/api/v1/users/<user_id>")
    def get_user(user_id):
        return jsonify(svc.get_user(user_id))

    # ------------------------------------------------------------------
    # Routes: attendance sessions (check-in creates one, check-out closes it)
    # ------------------------------------------------------------------
    @app.post("/api/v1/attendance")
    def check_in():
        body = json_body()
        session, replayed = svc.check_in(body.get("user_id"), body.get("method"),
                                         request.headers.get("Idempotency-Key"))
        resp = created(session, f"/api/v1/attendance/{session['id']}")
        if replayed:
            resp.headers["Idempotent-Replayed"] = "true"
        return resp

    @app.get("/api/v1/attendance")
    def list_attendance():
        items = svc.list_sessions(request.args.get("state"), request.args.get("user_id"),
                                  int_arg("limit", 100, 1, 500))
        return jsonify({"items": items, "count": len(items)})

    @app.get("/api/v1/attendance/<session_id>")
    def get_attendance(session_id):
        return jsonify(svc.get_session(session_id))

    @app.patch("/api/v1/attendance/<session_id>")
    def check_out(session_id):
        body = json_body()
        if body.get("state") != "closed":
            raise ValidationError("One or more fields are invalid.",
                                  errors=[{"field": "state", "message": "must be 'closed'"}])
        session, replayed = svc.check_out(session_id, body.get("method"))
        resp = jsonify(session)
        if replayed:
            resp.headers["Idempotent-Replayed"] = "true"
        return resp

    @app.get("/api/v1/events")
    def list_events():
        items = svc.recent_events(int_arg("limit", 20, 1, 100))
        return jsonify({"items": items, "count": len(items)})

    # ------------------------------------------------------------------
    # Routes: unchanged from A1 / operational
    # ------------------------------------------------------------------
    @app.post("/api/demo/scan")
    def demo_scan():
        """A1 cardinality experiment. Left exactly as submitted for A1."""
        data = request.get_json(force=True, silent=True) or {}
        request_id = data.get("request_id") or str(uuid.uuid4())
        DEMO_REQUESTS_TOTAL.inc()
        return jsonify({"request_id": request_id, "recorded": True})

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/metrics")
    def metrics_endpoint():
        return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}

    # ------------------------------------------------------------------
    # Docs + frontend
    # ------------------------------------------------------------------
    @app.get("/openapi.yaml")
    def openapi_spec():
        return send_from_directory(str(config.ROOT), "openapi.yaml", mimetype="application/yaml")

    @app.get("/docs")
    def swagger_ui():
        return ("<!DOCTYPE html><html><head><meta charset='utf-8'><title>API docs</title>"
                "<link rel='stylesheet' href='https://unpkg.com/swagger-ui-dist@5/swagger-ui.css'>"
                "</head><body><div id='ui'></div>"
                "<script src='https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js'></script>"
                "<script>SwaggerUIBundle({url: '/openapi.yaml', dom_id: '#ui'});</script>"
                "</body></html>")

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
