"""gRPC tests: success/failure codes, idempotency, deadline, correlation, metrics,
and -- most importantly -- that HTTP and gRPC share rules and source of truth."""
import time

import grpc
import pytest
from prometheus_client import REGISTRY

from app import create_app
from attendance_core.ratelimit import SlidingWindowRateLimiter
from grpc_service import attendance_pb2 as pb

TRACE = "0af7651916cd43dd8448eb211c80319c"


def call(stub, user="u1", method=pb.SCAN_METHOD_FACE, key="", **kw):
    return stub.CheckIn(pb.CheckInRequest(user_id=user, method=method, idempotency_key=key),
                        timeout=kw.pop("timeout", 2), **kw)


def code_of(stub, **kw):
    with pytest.raises(grpc.RpcError) as err:
        call(stub, **kw)
    return err.value


def test_successful_check_in(grpc_env):
    stub, _svc, store = grpc_env
    resp = call(stub)
    assert resp.user_id == "u1" and resp.status in (pb.ATTENDANCE_STATUS_ON_TIME, pb.ATTENDANCE_STATUS_LATE)
    assert resp.method == pb.SCAN_METHOD_FACE and not resp.replayed
    assert store.get_open_session_for_user("u1")["session_id"] == resp.session_id


@pytest.mark.parametrize("kwargs,expected", [
    (dict(user="nobody"), grpc.StatusCode.NOT_FOUND),
    (dict(method=pb.SCAN_METHOD_UNSPECIFIED), grpc.StatusCode.INVALID_ARGUMENT),
    (dict(user=""), grpc.StatusCode.INVALID_ARGUMENT),
    (dict(user="../x"), grpc.StatusCode.INVALID_ARGUMENT),
])
def test_failed_requests_map_to_grpc_codes(grpc_env, kwargs, expected):
    err = code_of(grpc_env[0], **kwargs)
    assert err.code() == expected and err.details()


def test_already_checked_in_is_already_exists_with_problem_type_trailer(grpc_env):
    stub = grpc_env[0]
    call(stub)
    err = code_of(stub)
    assert err.code() == grpc.StatusCode.ALREADY_EXISTS
    assert dict(err.trailing_metadata())["x-problem-type"] == "already-checked-in"


def test_failed_scan_is_unauthenticated(grpc_env, store, logger):
    import grpc as g
    from grpc_service.server import build_server
    from tests.conftest import make_service
    from grpc_service import attendance_pb2_grpc as pb_grpc
    server, port = build_server(make_service(store, logger, scan_failure_rate=1.0), logger, "localhost:0")
    server.start()
    try:
        stub = pb_grpc.AttendanceServiceStub(g.insecure_channel(f"localhost:{port}"))
        assert code_of(stub, user="u2").code() == grpc.StatusCode.UNAUTHENTICATED
    finally:
        server.stop(0)


def test_idempotent_replay_and_key_reuse(grpc_env):
    stub = grpc_env[0]
    first, again = call(stub, key="g-key"), call(stub, key="g-key")
    assert again.replayed and again.session_id == first.session_id and not first.replayed
    assert code_of(stub, user="u2", key="g-key").code() == grpc.StatusCode.INVALID_ARGUMENT


# ----------------------------------------------------------- shared source of truth

def test_http_checkin_is_visible_to_grpc_and_vice_versa(grpc_env, make_client):
    stub, _svc, _store = grpc_env
    http = make_client()
    assert http.post("/api/v1/attendance", json={"user_id": "u1", "method": "face"}).status_code == 201
    assert code_of(stub, user="u1").code() == grpc.StatusCode.ALREADY_EXISTS     # gRPC sees HTTP's write
    call(stub, user="u2")
    assert http.post("/api/v1/attendance", json={"user_id": "u2", "method": "face"}).status_code == 409
    assert http.get("/api/v1/attendance?state=open").get_json()["count"] == 2    # HTTP sees gRPC's write


@pytest.mark.parametrize("http_body,grpc_kwargs", [
    ({"user_id": "nobody", "method": "face"}, dict(user="nobody")),
    ({"user_id": "u1", "method": "retina"}, dict(method=pb.SCAN_METHOD_UNSPECIFIED)),
])
def test_same_input_same_rule_different_status_vocabulary(grpc_env, make_client, http_body, grpc_kwargs):
    """The two transports reject the same inputs for the same reason."""
    stub = grpc_env[0]
    http_status = make_client().post("/api/v1/attendance", json=http_body).status_code
    grpc_code = code_of(stub, **grpc_kwargs).code()
    assert (http_status, grpc_code) in {(404, grpc.StatusCode.NOT_FOUND), (400, grpc.StatusCode.INVALID_ARGUMENT)}


# --------------------------------------------------------- correlation across the boundary

def test_trace_context_crosses_the_grpc_boundary_into_logs(grpc_env, log_capture):
    stub = grpc_env[0]
    resp, call_obj = stub.CheckIn.with_call(
        pb.CheckInRequest(user_id="u3", method=pb.SCAN_METHOD_VOICE), timeout=2,
        metadata=(("traceparent", f"00-{TRACE}-00f067aa0ba902b7-01"), ("x-request-id", "grpc-req-9")))
    initial = dict(call_obj.initial_metadata())
    assert initial["x-request-id"] == "grpc-req-9" and initial["traceparent"].split("-")[1] == TRACE
    records = log_capture.records()
    assert len(records) >= 2                                    # service line + interceptor line
    assert all(r["trace_id"] == TRACE and r["request_id"] == "grpc-req-9" for r in records)
    assert any(r["message"].startswith("grpc_call method=CheckIn code=OK") for r in records)


def test_same_trace_id_links_an_http_call_and_a_grpc_call(grpc_env, make_client, log_capture):
    stub = grpc_env[0]
    http = make_client()
    http.post("/api/v1/attendance", json={"user_id": "u1", "method": "face"},
              headers={"traceparent": f"00-{TRACE}-00f067aa0ba902b7-01"})
    stub.CheckIn(pb.CheckInRequest(user_id="u2", method=pb.SCAN_METHOD_FACE), timeout=2,
                 metadata=(("traceparent", f"00-{TRACE}-00f067aa0ba902b7-01"),))
    assert {r["trace_id"] for r in log_capture.records()} == {TRACE}
    assert len({r["request_id"] for r in log_capture.records()}) == 2    # but two distinct requests


# ---------------------------------------------------------------- deadline + metrics

def test_deadline_exceeded_and_no_work_done_after_the_client_gave_up(grpc_env, monkeypatch):
    stub, _svc, store = grpc_env
    monkeypatch.setenv("ENABLE_FAULT_INJECTION", "1")
    before = REGISTRY.get_sample_value("grpc_server_handled_total",
                                       {"grpc_method": "CheckIn", "grpc_code": "DEADLINE_EXCEEDED"}) or 0
    with pytest.raises(grpc.RpcError) as err:
        call(stub, user="u4", timeout=0.15, metadata=(("x-inject-delay-ms", "500"),))
    assert err.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED
    time.sleep(0.8)                                             # let the slow handler wake up
    assert store.get_open_session_for_user("u4") is None        # cooperative cancellation
    after = REGISTRY.get_sample_value("grpc_server_handled_total",
                                      {"grpc_method": "CheckIn", "grpc_code": "DEADLINE_EXCEEDED"})
    assert after == before + 1


def test_fault_injection_metadata_is_ignored_unless_enabled(grpc_env):
    assert call(grpc_env[0], user="u5", timeout=0.3, metadata=(("x-inject-delay-ms", "2000"),)).session_id


def test_grpc_metrics_cover_calls_codes_duration_and_sizes(grpc_env):
    stub = grpc_env[0]

    def sample(name, **labels):
        return REGISTRY.get_sample_value(name, {"grpc_method": "CheckIn", **labels}) or 0

    ok0, nf0 = sample("grpc_server_handled_total", grpc_code="OK"), sample("grpc_server_handled_total", grpc_code="NOT_FOUND")
    dur0 = sample("grpc_server_handling_seconds_count")
    req0 = sample("grpc_server_message_size_bytes_count", direction="request")
    resp0 = sample("grpc_server_message_size_bytes_count", direction="response")
    call(stub, user="u6"); code_of(stub, user="nobody")
    assert sample("grpc_server_handled_total", grpc_code="OK") == ok0 + 1
    assert sample("grpc_server_handled_total", grpc_code="NOT_FOUND") == nf0 + 1
    assert sample("grpc_server_handling_seconds_count") == dur0 + 2
    assert sample("grpc_server_message_size_bytes_count", direction="request") == req0 + 2
    assert sample("grpc_server_message_size_bytes_count", direction="response") == resp0 + 1   # only the success has a response message


def test_grpc_metric_labels_are_bounded(grpc_env):
    stub = grpc_env[0]
    for i in range(5):
        try:
            call(stub, user=f"user-{i}")
        except grpc.RpcError:
            pass
    from prometheus_client import generate_latest
    text = generate_latest().decode()
    assert "user-" not in "".join(l for l in text.splitlines() if l.startswith("grpc_"))
