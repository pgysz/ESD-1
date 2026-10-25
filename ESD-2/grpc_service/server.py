"""gRPC server for the CheckIn operation.

Call path:  client stub -> HTTP/2 -> grpc runtime -> ObservabilityInterceptor
            -> AttendanceServicer.CheckIn (generated base class) ->
            AttendanceService.check_in (shared with the HTTP API) -> SQLite.

Run (from the project root):  python -m grpc_service.server
"""

import os
import signal
import time
from concurrent import futures

import grpc
from prometheus_client import start_http_server

from attendance_core import config
from attendance_core.errors import DomainError
from attendance_core.logging_setup import configure_logging
from attendance_core.service import AttendanceService
from attendance_core.store import Store
from attendance_core import tracing

from . import attendance_pb2 as pb
from . import attendance_pb2_grpc as pb_grpc
from .interceptors import ObservabilityInterceptor

METHOD_FROM_PB = {pb.SCAN_METHOD_FACE: "face", pb.SCAN_METHOD_FINGERPRINT: "fingerprint",
                  pb.SCAN_METHOD_VOICE: "voice"}
METHOD_TO_PB = {v: k for k, v in METHOD_FROM_PB.items()}
STATUS_TO_PB = {"on_time": pb.ATTENDANCE_STATUS_ON_TIME, "late": pb.ATTENDANCE_STATUS_LATE}


class AttendanceServicer(pb_grpc.AttendanceServiceServicer):
    """The server-side handler: translates protobuf <-> AttendanceService."""

    def __init__(self, service):
        self._service = service

    def CheckIn(self, request, context):
        try:
            self._maybe_inject_delay(context)
            if not context.is_active():
                # The deadline passed while we were delayed: do not start work the
                # client has already given up on.
                context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "deadline exceeded")
            # An unset/unknown enum value maps to None, which validation rejects.
            session, replayed = self._service.check_in(
                request.user_id, METHOD_FROM_PB.get(request.method),
                request.idempotency_key or None)
        except DomainError as exc:
            # Same rule, different wire format: the DomainError carries its gRPC code.
            context.set_trailing_metadata((
                ("x-problem-type", exc.slug),
                ("x-request-id", tracing.current_request_id() or ""),
            ))
            context.abort(grpc.StatusCode[exc.grpc_code], exc.detail)
        return pb.CheckInResponse(
            session_id=session["id"], user_id=session["user_id"],
            status=STATUS_TO_PB[session["status"]],
            method=METHOD_TO_PB[session["check_in_method"]],
            checked_in_at=session["checked_in_at"], replayed=replayed)

    @staticmethod
    def _maybe_inject_delay(context):
        """Test-only (ENABLE_FAULT_INJECTION=1): metadata x-inject-delay-ms."""
        if not config.fault_injection_enabled():
            return
        for key, value in context.invocation_metadata() or []:
            if key == "x-inject-delay-ms":
                time.sleep(min(int(value), 10000) / 1000)


def build_server(service, logger, address="[::]:50051", max_workers=10):
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers),
                         interceptors=[ObservabilityInterceptor(logger)])
    pb_grpc.add_AttendanceServiceServicer_to_server(AttendanceServicer(service), server)
    port = server.add_insecure_port(address)
    return server, port


def serve():
    logger = configure_logging("attendance-grpc", "grpc.log")
    service = AttendanceService(Store(), logger)
    grpc_port = int(os.environ.get("GRPC_PORT", "50051"))
    metrics_port = int(os.environ.get("GRPC_METRICS_PORT", "9102"))
    start_http_server(metrics_port)       # gRPC is not HTTP/1, so metrics get their own port
    server, _ = build_server(service, logger, f"[::]:{grpc_port}")
    server.start()
    print(f"gRPC AttendanceService on :{grpc_port}, Prometheus metrics on :{metrics_port}/metrics")
    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    while not stop["flag"]:
        time.sleep(0.2)
    server.stop(grace=5).wait()


if __name__ == "__main__":
    serve()
