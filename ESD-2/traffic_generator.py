#!/usr/bin/env python3
"""
traffic_generator.py  (updated for the Assignment 2 API)
--------------------------------------------------------
Drives fake traffic against the attendance API so Prometheus has data to scrape
and Elasticsearch has logs to index.

Normal mode picks a random action (check-in / check-out / signup) and scan
method and sends requests at a controlled rate.

Experiment flags kept from Assignment 1:

  --inject-delay      every 5th request carries `X-Inject-Delay: true`; the
                      server adds 500 ms. The server must be started with
                      ENABLE_FAULT_INJECTION=1 (A2 made fault injection opt-in).
  --cardinality-demo  exactly 100 requests to /api/demo/scan with a unique
                      request_id each, then stop (A1 cardinality experiment).

Examples:
    python traffic_generator.py --requests 200 --rate 5
    ENABLE_FAULT_INJECTION=1 python app.py   # server side, then:
    python traffic_generator.py --requests 200 --rate 5 --inject-delay
    python traffic_generator.py --cardinality-demo
"""

import argparse
import collections
import random
import time
import uuid

import requests

BASE_URL = "http://localhost:5000"
METHODS = ["face", "fingerprint", "voice"]
USER_IDS = [f"u{i}" for i in range(1, 21)]          # the 20 seeded users
ACTIONS = ["checkin"] * 9 + ["checkout"] * 9 + ["signup"] * 2   # weighted: signup is rate limited

_open_sessions = {}          # user_id -> session id of their open session
_signup_blocked_until = 0.0  # set when the server answers 429 with Retry-After
_statuses = collections.Counter()


def sync_open_sessions(session):
    """The database now persists across runs, so learn who is already checked in."""
    resp = session.get(f"{BASE_URL}/api/v1/attendance",
                       params={"state": "open", "limit": 500}, timeout=5)
    for item in resp.json()["items"]:
        _open_sessions[item["user_id"]] = item["id"]


def do_checkin(session, method, headers):
    user_id = random.choice(USER_IDS)
    resp = session.post(f"{BASE_URL}/api/v1/attendance",
                        json={"user_id": user_id, "method": method},
                        headers={**headers, "Idempotency-Key": str(uuid.uuid4())}, timeout=5)
    if resp.status_code == 201:
        _open_sessions[user_id] = resp.json()["id"]
    elif resp.status_code == 409:
        sync_open_sessions(session)
    return resp


def do_checkout(session, method, headers):
    if not _open_sessions:
        return do_checkin(session, method, headers)
    user_id = random.choice(list(_open_sessions))
    resp = session.patch(f"{BASE_URL}/api/v1/attendance/{_open_sessions[user_id]}",
                         json={"state": "closed", "method": method}, headers=headers, timeout=5)
    if resp.status_code == 200:
        _open_sessions.pop(user_id, None)
    return resp


def do_signup(session, method, headers):
    global _signup_blocked_until
    if time.monotonic() < _signup_blocked_until:      # respect the earlier Retry-After
        return do_checkin(session, method, headers)
    resp = session.post(f"{BASE_URL}/api/v1/users",
                        json={"name": f"Fake User {random.randint(1000, 9999)}"},
                        headers=headers, timeout=5)
    if resp.status_code == 429:
        _signup_blocked_until = time.monotonic() + float(resp.headers.get("Retry-After", "1"))
    return resp


def run_normal_mode(total_requests, rate, inject_delay):
    session = requests.Session()
    sync_open_sessions(session)
    pause = 1.0 / rate if rate > 0 else 0
    print(f"Sending {total_requests} requests at ~{rate} req/s (inject_delay={inject_delay})...")
    actions = {"checkin": do_checkin, "checkout": do_checkout, "signup": do_signup}
    for i in range(1, total_requests + 1):
        action, method = random.choice(ACTIONS), random.choice(METHODS)
        headers = {"X-Inject-Delay": "true"} if inject_delay and i % 5 == 0 else {}
        try:
            resp = actions[action](session, method, headers)
            status = resp.status_code
        except requests.RequestException as exc:
            status = f"ERROR: {exc}"
        _statuses[status] += 1
        if i % 20 == 0 or i == total_requests:
            print(f"  [{i}/{total_requests}] last action={action} method={method} status={status}")
        if pause:
            time.sleep(pause)
    print("Done. Status codes seen:", dict(sorted(_statuses.items(), key=lambda kv: str(kv[0]))))


def run_cardinality_demo():
    """Exactly 100 uniquely-labelled requests, then stop (A1 experiment)."""
    session, limit = requests.Session(), 100
    print(f"Cardinality demo: sending exactly {limit} uniquely-labeled requests...")
    for i in range(1, limit + 1):
        request_id = str(uuid.uuid4())
        resp = session.post(f"{BASE_URL}/api/demo/scan", json={"request_id": request_id}, timeout=5)
        if i % 10 == 0 or i == limit:
            print(f"  [{i}/{limit}] request_id={request_id} status={resp.status_code}")
        time.sleep(0.05)
    print(f"Done. Hard-stopped at exactly {limit} requests.\n"
          f"Check in Grafana/Prometheus: count(demo_requests_total).")


def main():
    parser = argparse.ArgumentParser(description="Fake traffic generator for the attendance app.")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--rate", type=float, default=3.0, help="requests per second")
    parser.add_argument("--inject-delay", action="store_true",
                        help="500 ms server-side delay on every 5th request (needs ENABLE_FAULT_INJECTION=1)")
    parser.add_argument("--cardinality-demo", action="store_true")
    args = parser.parse_args()
    if args.cardinality_demo:
        run_cardinality_demo()
    else:
        run_normal_mode(args.requests, args.rate, args.inject_delay)


if __name__ == "__main__":
    main()
