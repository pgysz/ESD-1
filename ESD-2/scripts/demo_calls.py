#!/usr/bin/env python3
"""Repeatable success + failure calls through BOTH the HTTP API and gRPC.

Populates the "HTTP vs gRPC" Grafana panels and prints a side-by-side table of
status codes for the report. Start both servers first, ideally with
SCAN_FAILURE_RATE=0 so the "success" calls always succeed:

    SCAN_FAILURE_RATE=0 python app.py
    SCAN_FAILURE_RATE=0 python -m grpc_service.server
    python scripts/demo_calls.py --rounds 5
"""
import argparse
import sys
import time
import uuid
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from clients.grpc_client import check_in as grpc_check_in   # noqa: E402

HTTP_USERS = ["u6", "u7", "u8"]
GRPC_USERS = ["u9", "u10", "u11"]


def http_check_in(base, user, method="face", key=None):
    headers = {"Idempotency-Key": key} if key else {}
    r = requests.post(f"{base}/api/v1/attendance", json={"user_id": user, "method": method},
                      headers=headers, timeout=5)
    return r.status_code, r.json()


def cleanup(base):
    for user in HTTP_USERS + GRPC_USERS:
        items = requests.get(f"{base}/api/v1/attendance",
                             params={"state": "open", "user_id": user}, timeout=5).json()["items"]
        for s in items:
            requests.patch(f"{base}/api/v1/attendance/{s['id']}",
                           json={"state": "closed", "method": "face"}, timeout=5)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default="http://localhost:5000")
    p.add_argument("--grpc-target", default="localhost:50051")
    p.add_argument("--rounds", type=int, default=5)
    a = p.parse_args()
    cleanup(a.base_url)
    rows = []
    for i in range(a.rounds):
        hu, gu = HTTP_USERS[i % 3], GRPC_USERS[i % 3]
        trace = uuid.uuid4().hex
        # success
        s, _ = http_check_in(a.base_url, hu)
        _, c, *_ = grpc_check_in(a.grpc_target, gu, "face", trace_id=trace)
        rows.append(("success", s, c))
        # failure: unknown user
        s, _ = http_check_in(a.base_url, "nobody")
        _, c, *_ = grpc_check_in(a.grpc_target, "nobody", "face", trace_id=trace)
        rows.append(("unknown user", s, c))
        # failure: invalid input (HTTP: bad method string; gRPC: unspecified enum)
        s, _ = http_check_in(a.base_url, hu, method="retina")
        _, c, *_ = grpc_check_in(a.grpc_target, gu, "unspecified", trace_id=trace)
        rows.append(("invalid input", s, c))
        # failure: already checked in (both users are open from the success above)
        s, _ = http_check_in(a.base_url, hu)
        _, c, *_ = grpc_check_in(a.grpc_target, gu, "face", trace_id=trace)
        rows.append(("already checked in", s, c))
        # idempotent repeat: same key twice -> the second call is a replay
        cleanup(a.base_url)
        key_h, key_g = f"http-{uuid.uuid4().hex[:8]}", f"grpc-{uuid.uuid4().hex[:8]}"
        http_check_in(a.base_url, hu, key=key_h)
        r = requests.post(f"{a.base_url}/api/v1/attendance", json={"user_id": hu, "method": "face"},
                          headers={"Idempotency-Key": key_h}, timeout=5)
        grpc_check_in(a.grpc_target, gu, "face", idempotency_key=key_g)
        resp, c, *_ = grpc_check_in(a.grpc_target, gu, "face", idempotency_key=key_g)
        rows.append(("repeat, same Idempotency-Key",
                     f"{r.status_code} (Idempotent-Replayed: {r.headers.get('Idempotent-Replayed')})",
                     f"{c} (replayed={resp.replayed if resp else '-'})"))
        cleanup(a.base_url)
        time.sleep(0.3)
    print(f"{'scenario':<30}{'HTTP':<40}gRPC")
    for scenario, http_status, grpc_code in rows[:5]:
        print(f"{scenario:<30}{str(http_status):<40}{grpc_code}")
    print(f"... ({a.rounds} rounds sent in total)")


if __name__ == "__main__":
    main()
