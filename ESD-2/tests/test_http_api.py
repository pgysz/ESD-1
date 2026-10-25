"""HTTP API tests: valid request, invalid input, repeated request, error format,
rate limiting, correlation ids, fault injection, metrics hygiene, OpenAPI parity."""
import re
import threading

import pytest
import yaml
from prometheus_client import REGISTRY, generate_latest

from app import create_app
from attendance_core.config import ROOT
from attendance_core.ratelimit import SlidingWindowRateLimiter
from tests.conftest import make_service

SPEC = yaml.safe_load(open(ROOT / "openapi.yaml"))
# Endpoints deliberately not in the OpenAPI document (operational / docs / frontend).
NOT_IN_SPEC = {"/api/health", "/metrics", "/openapi.yaml", "/docs", "/", "/<path:filename>"}


def documented(client, method, path, template, **kwargs):
    """Make a request AND assert its status code is documented in openapi.yaml."""
    resp = getattr(client, method)(path, **kwargs)
    declared = SPEC["paths"][template][method]["responses"]
    assert str(resp.status_code) in declared, (
        f"{method.upper()} {template} returned undocumented status {resp.status_code}")
    return resp


def check_in(client, user="u1", key=None, method="face"):
    headers = {"Idempotency-Key": key} if key else {}
    return documented(client, "post", "/api/v1/attendance", "/api/v1/attendance",
                      json={"user_id": user, "method": method}, headers=headers)


# ------------------------------------------------------------------ OpenAPI

def test_every_route_is_in_the_spec_and_vice_versa(client):
    app = client.application
    flask_routes = {(r.rule.replace("<", "{").replace(">", "}"), m.lower())
                    for r in app.url_map.iter_rules() if r.rule not in NOT_IN_SPEC
                    for m in r.methods if m not in ("HEAD", "OPTIONS")}
    spec_routes = {(path, m) for path, ops in SPEC["paths"].items()
                   for m in ops if m in ("get", "post", "patch", "put", "delete")}
    assert flask_routes == spec_routes


# --------------------------------------------------------------- valid requests

def test_signup_valid_returns_201_location_and_body(client):
    r = documented(client, "post", "/api/v1/users", "/api/v1/users", json={"name": "  Talal Ahmed "})
    assert r.status_code == 201
    body = r.get_json()
    assert body["name"] == "Talal Ahmed" and r.headers["Location"] == f"/api/v1/users/{body['user_id']}"
    assert client.get(r.headers["Location"]).get_json() == body


def test_checkin_then_checkout_flow(client):
    r = check_in(client)
    assert r.status_code == 201 and r.get_json()["state"] == "open"
    sid = r.headers["Location"].rsplit("/", 1)[-1]
    out = documented(client, "patch", f"/api/v1/attendance/{sid}", "/api/v1/attendance/{session_id}",
                     json={"state": "closed", "method": "voice"})
    assert out.status_code == 200 and out.get_json()["state"] == "closed"
    assert client.get("/api/v1/attendance?state=open").get_json()["count"] == 0


# ------------------------------------------------------------------ invalid input

@pytest.mark.parametrize("payload,field", [
    ({}, "name"), ({"name": ""}, "name"), ({"name": "   "}, "name"),
    ({"name": 123}, "name"), ({"name": "x" * 81}, "name"), ({"name": "bad\x00name"}, "name"),
])
def test_signup_invalid_input_returns_400_with_field_errors(client, payload, field):
    r = documented(client, "post", "/api/v1/users", "/api/v1/users", json=payload)
    assert r.status_code == 400 and r.content_type == "application/problem+json"
    assert r.get_json()["errors"][0]["field"] == field


@pytest.mark.parametrize("payload,field", [
    ({"user_id": "u1", "method": "retina"}, "method"),
    ({"user_id": "u1"}, "method"), ({"method": "face"}, "user_id"),
    ({"user_id": "../etc", "method": "face"}, "user_id"),
])
def test_checkin_invalid_input_returns_400(client, payload, field):
    r = documented(client, "post", "/api/v1/attendance", "/api/v1/attendance", json=payload)
    assert r.status_code == 400 and r.get_json()["errors"][0]["field"] == field


def test_malformed_and_non_object_json_and_wrong_content_type(client):
    bad = client.post("/api/v1/users", data="{not json", content_type="application/json")
    assert bad.status_code == 400
    arr = client.post("/api/v1/users", json=["a"])
    assert arr.status_code == 400
    wrong = documented(client, "post", "/api/v1/users", "/api/v1/users",
                       data="name=x", content_type="text/plain")
    assert wrong.status_code == 415


def test_unknown_resources_return_404(client):
    assert check_in(client, user="nobody").status_code == 404
    assert documented(client, "get", "/api/v1/users/zzz", "/api/v1/users/{user_id}").status_code == 404
    assert documented(client, "get", "/api/v1/attendance/nope", "/api/v1/attendance/{session_id}").status_code == 404


def test_bad_query_parameters_return_400(client):
    assert documented(client, "get", "/api/v1/attendance?state=weird", "/api/v1/attendance").status_code == 400
    assert documented(client, "get", "/api/v1/events?limit=0", "/api/v1/events").status_code == 400


# ----------------------------------------------- repeated requests (idempotency)

def test_repeat_with_same_key_replays_and_creates_one_session(client):
    first = check_in(client, key="key-1")
    again = check_in(client, key="key-1")
    assert (first.status_code, again.status_code) == (201, 201)
    assert again.headers["Idempotent-Replayed"] == "true" and "Idempotent-Replayed" not in first.headers
    assert again.get_json() == first.get_json()
    assert client.get("/api/v1/attendance?state=open").get_json()["count"] == 1


def test_replay_is_counted_in_metrics(client):
    before = REGISTRY.get_sample_value("idempotent_replays_total", {"operation": "check_in"}) or 0
    check_in(client, key="k-metric"); check_in(client, key="k-metric")
    after = REGISTRY.get_sample_value("idempotent_replays_total", {"operation": "check_in"})
    assert after == before + 1


def test_same_key_different_body_is_rejected_422(client):
    check_in(client, user="u1", key="k")
    r = check_in(client, user="u2", key="k")
    assert r.status_code == 422 and r.get_json()["type"].endswith("idempotency-key-reuse")


def test_repeat_without_key_is_a_409_conflict_not_a_duplicate(client):
    check_in(client)
    r = check_in(client)
    assert r.status_code == 409
    assert client.get("/api/v1/attendance?state=open").get_json()["count"] == 1


def test_repeating_checkout_is_safe(client):
    sid = check_in(client).headers["Location"].rsplit("/", 1)[-1]
    url = f"/api/v1/attendance/{sid}"
    body = {"state": "closed", "method": "face"}
    first = client.patch(url, json=body)
    again = client.patch(url, json=body)
    assert first.status_code == again.status_code == 200
    assert again.headers["Idempotent-Replayed"] == "true"
    assert first.get_json()["checked_out_at"] == again.get_json()["checked_out_at"]


def test_checkout_rejects_wrong_state_value(client):
    sid = check_in(client).headers["Location"].rsplit("/", 1)[-1]
    r = documented(client, "patch", f"/api/v1/attendance/{sid}", "/api/v1/attendance/{session_id}",
                   json={"state": "open", "method": "face"})
    assert r.status_code == 400


def test_concurrent_checkins_for_one_user_create_exactly_one_session(client):
    results = []

    def worker(i):
        results.append(client.post("/api/v1/attendance", json={"user_id": "u3", "method": "face"},
                                   headers={"Idempotency-Key": f"race-{i}"}).status_code)
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert sorted(results) == [201] + [409] * 5            # the database enforces it


# ------------------------------------------------------------------ scan failure

def test_failed_scan_is_401_and_creates_nothing(store, logger, make_client):
    c = make_client(service=make_service(store, logger, scan_failure_rate=1.0))
    r = check_in(c)
    assert r.status_code == 401 and "WWW-Authenticate" in r.headers
    assert c.get("/api/v1/attendance?state=open").get_json()["count"] == 0


def test_already_checked_in_is_409_even_if_scans_would_fail(store, logger, make_client):
    """Rule order: the conflict is detected BEFORE a scan is attempted (A1 did it the other way round)."""
    make_client()                                          # build an app on the shared store
    make_service(store, logger).check_in("u1", "face")
    c = make_client(service=make_service(store, logger, scan_failure_rate=1.0))
    assert check_in(c).status_code == 409


# ------------------------------------------------------------------ error format

@pytest.mark.parametrize("call", [
    lambda c: c.get("/api/v1/nope"),
    lambda c: c.delete("/api/v1/users"),
    lambda c: c.post("/api/v1/users", data="x", content_type="text/plain"),
    lambda c: c.post("/api/v1/users", json={}),
    lambda c: c.get("/api/v1/users/zzz"),
])
def test_every_error_uses_the_same_problem_json_shape(client, call):
    r = call(client)
    assert r.status_code >= 400 and r.content_type == "application/problem+json"
    body = r.get_json()
    assert {"type", "title", "status", "detail", "instance", "request_id"} <= set(body)
    assert body["status"] == r.status_code


def test_unexpected_exception_returns_500_problem_without_leaking_details(store, logger, make_client):
    svc = make_service(store, logger)
    svc.list_users = lambda: (_ for _ in ()).throw(RuntimeError("secret database path /etc/x"))
    r = make_client(service=svc).get("/api/v1/users")
    assert r.status_code == 500 and "secret" not in r.get_data(as_text=True)


# ------------------------------------------------------------------ rate limiting

def test_signup_rate_limit_429_retry_after_and_recovery(store, logger, make_client):
    class Clock:
        t = 0.0
        def __call__(self): return self.t
    clock = Clock()
    c = make_client(limiter=SlidingWindowRateLimiter(3, 60, clock=clock))
    codes = [c.post("/api/v1/users", json={"name": f"User {i}"}).status_code for i in range(3)]
    assert codes == [201, 201, 201]
    blocked = documented(c, "post", "/api/v1/users", "/api/v1/users", json={"name": "Too many"})
    assert blocked.status_code == 429 and blocked.headers["Retry-After"] == "60"
    assert blocked.headers["RateLimit-Remaining"] == "0" and blocked.headers["RateLimit-Limit"] == "3"
    assert c.get("/api/v1/users").status_code == 200       # other endpoints are NOT limited
    clock.t = 59
    assert c.post("/api/v1/users", json={"name": "Early"}).headers["Retry-After"] == "1"
    clock.t = 60                                           # oldest request left the window
    assert c.post("/api/v1/users", json={"name": "Allowed again"}).status_code == 201


def test_rate_limit_hits_are_counted(store, logger, make_client):
    c = make_client(limiter=SlidingWindowRateLimiter(1, 60))
    before = REGISTRY.get_sample_value("rate_limited_total", {"endpoint": "/api/v1/users"}) or 0
    c.post("/api/v1/users", json={"name": "a"}); c.post("/api/v1/users", json={"name": "b"})
    assert REGISTRY.get_sample_value("rate_limited_total", {"endpoint": "/api/v1/users"}) == before + 1


# ------------------------------------------------------- correlation + logging

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"


def test_trace_and_request_id_are_echoed_and_logged(client, log_capture):
    r = client.post("/api/v1/attendance", json={"user_id": "u1", "method": "face"},
                    headers={"traceparent": f"00-{TRACE}-00f067aa0ba902b7-01", "X-Request-ID": "req-abc-1"})
    assert r.headers["X-Request-ID"] == "req-abc-1"
    assert r.headers["traceparent"].split("-")[1] == TRACE
    lines = log_capture.records()
    assert lines and all(l["trace_id"] == TRACE and l["request_id"] == "req-abc-1" for l in lines)


def test_ids_are_generated_when_absent_and_garbage_is_ignored(client):
    r = client.get("/api/v1/users", headers={"traceparent": "garbage", "X-Request-ID": "bad id!!"})
    assert re.fullmatch(r"[0-9a-f-]{36}", r.headers["X-Request-ID"])
    assert re.fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-01", r.headers["traceparent"])


def test_logs_contain_the_agreed_fields_and_no_personal_data(client, log_capture):
    client.post("/api/v1/users", json={"name": "Zorro Quux"})
    check_in(client, user="u1")
    records = log_capture.records()
    assert records
    for rec in records:
        assert set(rec) == {"time", "service", "severity", "message", "request_id", "trace_id"}
    text = " ".join(log_capture.lines)
    assert "Zorro" not in text and '"u1"' not in text and "user_id" not in text


# --------------------------------------------------------------- fault injection

def test_fault_injection_headers_are_ignored_unless_enabled(client):
    r = client.get("/api/v1/users", headers={"X-Inject-Fault": "status=503"})
    assert r.status_code == 200


def test_fault_injection_when_enabled_times_limits_the_failures(client, monkeypatch):
    monkeypatch.setenv("ENABLE_FAULT_INJECTION", "1")
    h = {"X-Inject-Fault": "status=503,times=2", "Idempotency-Key": "f-1"}
    codes = [client.post("/api/v1/attendance", json={"user_id": "u5", "method": "face"},
                         headers=h).status_code for _ in range(3)]
    assert codes == [503, 503, 201]
    assert client.get("/api/v1/users", headers={"X-Inject-Fault": "status=200"}).status_code == 200


# --------------------------------------------------------------- metrics hygiene

def test_metrics_use_route_templates_not_raw_ids(client):
    for i in range(5):
        client.get(f"/api/v1/attendance/session-number-{i}")
        client.get(f"/api/v1/users/user-number-{i}")
    text = client.get("/metrics").get_data(as_text=True)
    assert "session-number-" not in text and "user-number-" not in text
    assert 'endpoint="/api/v1/attendance/<session_id>"' in text


def test_metrics_and_health_scrapes_are_not_counted_as_traffic(client):
    client.get("/metrics"); client.get("/api/health")
    text = client.get("/metrics").get_data(as_text=True)
    assert 'endpoint="/metrics"' not in text and 'endpoint="/api/health"' not in text


def test_currently_checked_in_gauge_reads_the_database(client, store):
    check_in(client, user="u1"); check_in(client, user="u2")
    assert REGISTRY.get_sample_value("currently_checked_in") == 2
    store.close_session(store.get_open_session_for_user("u1")["session_id"], "face", 1.0)
    assert REGISTRY.get_sample_value("currently_checked_in") == 1   # no restart/refresh needed


def test_api_responses_declare_themselves_non_cacheable(client):
    assert client.get("/api/v1/users").headers["Cache-Control"] == "no-store"
    assert client.get("/api/v1/users/zzz").headers["Cache-Control"] == "no-store"   # errors too
