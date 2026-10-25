"""Request-id and W3C trace-context helpers.

A1 only had a per-request request_id. A2 adds a trace_id taken from the W3C
`traceparent` header (HTTP) / metadata (gRPC) so one logical operation can be
followed across the HTTP and gRPC services in Kibana.

    traceparent = 00-<32 hex trace id>-<16 hex parent span id>-<2 hex flags>
"""

import contextvars
import re
import secrets
import uuid

_request_id = contextvars.ContextVar("request_id", default=None)
_trace_id = contextvars.ContextVar("trace_id", default=None)

_TRACEPARENT_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def new_trace_id() -> str:
    return secrets.token_hex(16)


def new_span_id() -> str:
    return secrets.token_hex(8)


def make_traceparent(trace_id: str, span_id: str, flags: str = "01") -> str:
    return f"00-{trace_id}-{span_id}-{flags}"


def parse_traceparent(value):
    """Return (trace_id, parent_span_id, flags) or None if invalid."""
    if not value:
        return None
    match = _TRACEPARENT_RE.match(value.strip().lower())
    if not match:
        return None
    trace_id, span_id, flags = match.groups()
    if set(trace_id) == {"0"} or set(span_id) == {"0"}:   # all-zero ids are invalid
        return None
    return trace_id, span_id, flags


def sanitize_request_id(value) -> str:
    """Accept a caller-supplied request id only if it is short and harmless."""
    if value and _REQUEST_ID_RE.match(value):
        return value
    return str(uuid.uuid4())


def bind(request_id, trace_id):
    """Bind ids to the current execution context; returns tokens for reset()."""
    return _request_id.set(request_id), _trace_id.set(trace_id)


def reset(tokens):
    _request_id.reset(tokens[0])
    _trace_id.reset(tokens[1])


def current_request_id():
    return _request_id.get()


def current_trace_id():
    return _trace_id.get()
