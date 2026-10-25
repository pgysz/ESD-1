#!/usr/bin/env python3
"""Python gRPC client for the CheckIn RPC (no browser needed).

    python clients/grpc_client.py --user-id u1 --method face
    python clients/grpc_client.py --user-id u1 --method face --deadline 0.5

Sets a deadline on the call, forwards trace context in metadata, and reports
either the result or the gRPC status code + details.
"""

import argparse
import sys
import uuid
from pathlib import Path

import grpc

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from grpc_service import attendance_pb2 as pb              # noqa: E402
from grpc_service import attendance_pb2_grpc as pb_grpc    # noqa: E402

METHODS = {"face": pb.SCAN_METHOD_FACE, "fingerprint": pb.SCAN_METHOD_FINGERPRINT,
           "voice": pb.SCAN_METHOD_VOICE, "unspecified": pb.SCAN_METHOD_UNSPECIFIED}


def check_in(target, user_id, method, *, idempotency_key="", deadline=2.0, trace_id=None,
             inject_delay_ms=None):
    """Returns (response_or_None, grpc_code_name, details, initial_metadata, trailers)."""
    trace_id = trace_id or uuid.uuid4().hex
    metadata = [("traceparent", f"00-{trace_id}-{uuid.uuid4().hex[:16]}-01"),
                ("x-request-id", uuid.uuid4().hex[:16])]
    if inject_delay_ms:
        metadata.append(("x-inject-delay-ms", str(inject_delay_ms)))
    request = pb.CheckInRequest(user_id=user_id, method=METHODS[method],
                                idempotency_key=idempotency_key)
    with grpc.insecure_channel(target) as channel:
        stub = pb_grpc.AttendanceServiceStub(channel)
        try:
            # timeout= is the DEADLINE: the call is cancelled with DEADLINE_EXCEEDED after it.
            response, call = stub.CheckIn.with_call(request, timeout=deadline, metadata=metadata)
            return response, "OK", "", dict(call.initial_metadata()), dict(call.trailing_metadata())
        except grpc.RpcError as err:
            return (None, err.code().name, err.details(), dict(err.initial_metadata() or []),
                    dict(err.trailing_metadata() or []))


def main():
    parser = argparse.ArgumentParser(description="gRPC CheckIn client")
    parser.add_argument("--target", default="localhost:50051")
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--method", choices=list(METHODS), default="face")
    parser.add_argument("--idempotency-key", default="")
    parser.add_argument("--deadline", type=float, default=2.0, help="seconds")
    parser.add_argument("--trace-id", default=None, help="32 hex chars; new one if omitted")
    parser.add_argument("--inject-delay-ms", type=int, default=None,
                        help="test only: needs the server started with ENABLE_FAULT_INJECTION=1")
    args = parser.parse_args()

    response, code, details, initial, trailers = check_in(
        args.target, args.user_id, args.method, idempotency_key=args.idempotency_key,
        deadline=args.deadline, trace_id=args.trace_id, inject_delay_ms=args.inject_delay_ms)
    print(f"status      : {code}")
    if response is not None:
        print(f"session_id  : {response.session_id}")
        print(f"user_id     : {response.user_id}")
        print(f"attendance  : {pb.AttendanceStatus.Name(response.status)}")
        print(f"method      : {pb.ScanMethod.Name(response.method)}")
        print(f"checked_in  : {response.checked_in_at}")
        print(f"replayed    : {response.replayed}")
    else:
        print(f"details     : {details}")
        if "x-problem-type" in trailers:
            print(f"problem type: {trailers['x-problem-type']}")
    if "x-request-id" in initial:
        print(f"request_id  : {initial['x-request-id']}")
    sys.exit(0 if code == "OK" else 1)


if __name__ == "__main__":
    main()
