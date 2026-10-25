"""Server interceptor: observability for EVERY RPC without touching handlers.

An interceptor wraps each handler. For every call it
  * binds request_id / trace_id (from gRPC metadata) so logs correlate,
  * measures duration at the SERVER and records the final status code,
  * records request/response message sizes,
  * writes one JSON log line per call.

Metric labels are bounded: grpc_method (one value today), grpc_code (<= 17
values), direction (2 values). Never a user, request or trace id.
"""

import time

import grpc
from prometheus_client import Counter, Histogram

from attendance_core import tracing
from attendance_core.logging_setup import log_event
from attendance_core.metrics import LATENCY_BUCKETS

GRPC_SERVER_HANDLED_TOTAL = Counter(
    "grpc_server_handled_total", "RPCs completed, by method and final status code",
    ["grpc_method", "grpc_code"])
GRPC_SERVER_HANDLING_SECONDS = Histogram(
    "grpc_server_handling_seconds", "Server-side RPC duration in seconds",
    ["grpc_method"], buckets=LATENCY_BUCKETS)
GRPC_SERVER_MESSAGE_SIZE_BYTES = Histogram(
    "grpc_server_message_size_bytes", "Serialized protobuf message size in bytes",
    ["grpc_method", "direction"], buckets=(16, 32, 64, 128, 256, 512, 1024, 2048, 4096))


def _metadata(context):
    return {k: v for k, v in (context.invocation_metadata() or [])}


class ObservabilityInterceptor(grpc.ServerInterceptor):
    def __init__(self, logger):
        self._logger = logger

    def intercept_service(self, continuation, handler_call_details):
        handler = continuation(handler_call_details)
        if handler is None or handler.unary_unary is None:
            return handler
        method = handler_call_details.method.rsplit("/", 1)[-1]     # e.g. "CheckIn"
        inner = handler.unary_unary
        logger = self._logger

        def wrapped(request, context):
            md = _metadata(context)
            incoming = tracing.parse_traceparent(md.get("traceparent"))
            trace_id = incoming[0] if incoming else tracing.new_trace_id()
            request_id = tracing.sanitize_request_id(md.get("x-request-id"))
            tokens = tracing.bind(request_id, trace_id)
            context.send_initial_metadata((
                ("x-request-id", request_id),
                ("traceparent", tracing.make_traceparent(trace_id, tracing.new_span_id())),
            ))
            start = time.perf_counter()
            code = grpc.StatusCode.OK
            GRPC_SERVER_MESSAGE_SIZE_BYTES.labels(method, "request").observe(request.ByteSize())
            try:
                response = inner(request, context)
                GRPC_SERVER_MESSAGE_SIZE_BYTES.labels(method, "response").observe(
                    response.ByteSize())
                remaining = context.time_remaining()
                if remaining is not None and remaining <= 0:    # client already gave up
                    code = grpc.StatusCode.DEADLINE_EXCEEDED
                return response
            except Exception:
                code = context.code() or grpc.StatusCode.UNKNOWN
                raise
            finally:
                elapsed = time.perf_counter() - start
                GRPC_SERVER_HANDLED_TOTAL.labels(method, code.name).inc()
                GRPC_SERVER_HANDLING_SECONDS.labels(method).observe(elapsed)
                log_event(logger, "INFO" if code == grpc.StatusCode.OK else "WARNING",
                          f"grpc_call method={method} code={code.name} "
                          f"duration_ms={round(elapsed * 1000, 1)}")
                tracing.reset(tokens)

        return grpc.unary_unary_rpc_method_handler(
            wrapped,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer)
