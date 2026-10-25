"""Shared fixtures: a throw-away database, a deterministic service, a log
capturer, a Flask test client and an in-process gRPC server."""
import json
import logging
import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from attendance_core.logging_setup import JsonFormatter          # noqa: E402
from attendance_core.ratelimit import SlidingWindowRateLimiter    # noqa: E402
from attendance_core.service import AttendanceService             # noqa: E402
from attendance_core.store import Store                           # noqa: E402


class ListHandler(logging.Handler):
    """Collects formatted JSON log lines in memory."""

    def __init__(self, service_name):
        super().__init__()
        self.lines = []
        self.setFormatter(JsonFormatter(service_name))

    def emit(self, record):
        self.lines.append(self.format(record))

    def records(self):
        return [json.loads(line) for line in self.lines]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for var in ("ENABLE_FAULT_INJECTION", "SCAN_FAILURE_RATE", "SIGNUP_FAILURE_RATE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ATTENDANCE_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("ATTENDANCE_LOG_DIR", str(tmp_path / "logs"))


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "shared.db"))


@pytest.fixture
def log_capture():
    return ListHandler("test-service")


@pytest.fixture
def logger(log_capture):
    lg = logging.getLogger(f"test.{uuid.uuid4().hex}")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    lg.addHandler(log_capture)
    return lg


def make_service(store, logger, **kwargs):
    defaults = dict(scan_failure_rate=0, signup_failure_rate=0, delay_scale=0,
                    sleep=lambda _s: None)
    defaults.update(kwargs)
    return AttendanceService(store, logger, **defaults)


@pytest.fixture
def service(store, logger):
    return make_service(store, logger)


@pytest.fixture
def make_client(store, logger):
    """Factory so a test can build a client around a custom service/limiter."""
    from app import create_app

    def _make(service=None, limiter=None):
        svc = service or make_service(store, logger)
        lim = limiter or SlidingWindowRateLimiter(1000, 60)
        return create_app(svc, lim).test_client()
    return _make


@pytest.fixture
def client(make_client):
    return make_client()


@pytest.fixture
def grpc_env(store, logger):
    """In-process gRPC server on a random port, sharing `store` with HTTP."""
    import grpc
    from grpc_service import attendance_pb2_grpc as pb_grpc
    from grpc_service.server import build_server

    svc = make_service(store, logger)
    server, port = build_server(svc, logger, "localhost:0")
    server.start()
    channel = grpc.insecure_channel(f"localhost:{port}")
    yield pb_grpc.AttendanceServiceStub(channel), svc, store
    channel.close()
    server.stop(0)
