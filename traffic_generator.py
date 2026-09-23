#!/usr/bin/env python3
"""
traffic_generator.py
---------------------
Drives fake traffic against the attendance app's API so Prometheus has
data to scrape and Elasticsearch has logs to index.

Normal mode: randomly picks a user, a scan method (face/fingerprint/voice),
and an action (checkin/checkout/signup), and fires requests at a controlled
rate for a fixed number of requests.

Two special modes support the Part E experiments:

  --inject-delay
      Adds a client-side 500ms delay before every 5th request, simulating
      a slow biometric matching pass. Run once WITHOUT this flag to
      capture a baseline, then once WITH it to see p95/p99 latency spike
      in Grafana, then WITHOUT it again to show recovery.

  --cardinality-demo
      Hits the dedicated /api/demo/scan endpoint with a unique request_id
      on every call, hard-stopping at exactly 100 requests. This is what
      makes demo_requests_total explode into 100 distinct time series in
      Prometheus -- deliberately the WRONG pattern, used only to
      demonstrate why request IDs must never be used as metric labels.

Usage examples:
    python traffic_generator.py --requests 200 --rate 5
    python traffic_generator.py --requests 200 --rate 5 --inject-delay
    python traffic_generator.py --cardinality-demo
"""

import argparse
import random
import time
import uuid

import requests

BASE_URL = "http://localhost:5000"
METHODS = ["face", "fingerprint", "voice"]
USER_IDS = [f"u{i}" for i in range(1, 21)]  # matches the 20 seeded users
ACTIONS = ["checkin", "checkout", "signup"]

# Track who we've "checked in" locally so checkout calls are more likely to
# succeed rather than constantly hitting "not_checked_in".
_checked_in = set()


def do_checkin(session, method):
    user_id = random.choice(USER_IDS)
    resp = session.post(
        f"{BASE_URL}/api/checkin",
        json={"user_id": user_id, "method": method},
        timeout=5,
    )
    if resp.ok and resp.json().get("success"):
        _checked_in.add(user_id)
    return resp


def do_checkout(session, method):
    if _checked_in:
        user_id = random.choice(list(_checked_in))
    else:
        user_id = random.choice(USER_IDS)
    resp = session.post(
        f"{BASE_URL}/api/checkout",
        json={"user_id": user_id, "method": method},
        timeout=5,
    )
    if resp.ok and resp.json().get("success"):
        _checked_in.discard(user_id)
    return resp


def do_signup(session, method):
    name = f"Fake User {random.randint(1000, 9999)}"
    return session.post(f"{BASE_URL}/api/signup", json={"name": name}, timeout=5)


def run_normal_mode(total_requests, rate, inject_delay):
    session = requests.Session()
    delay_between = 1.0 / rate if rate > 0 else 0
    print(
        f"Sending {total_requests} requests at ~{rate} req/s "
        f"(inject_delay={inject_delay})..."
    )

    action_fns = {"checkin": do_checkin, "checkout": do_checkout, "signup": do_signup}

    for i in range(1, total_requests + 1):
        action = random.choice(ACTIONS)
        method = random.choice(METHODS)

        # Simulate a slow biometric matching pass on every 5th request when
        # --inject-delay is set. This is a CLIENT-side delay added before
        # the request is even sent, standing in for what a genuinely slower
        # server-side matching algorithm would look like.
        if inject_delay and i % 5 == 0:
            session.headers["X-Inject-Delay"] = "true"
        else:
            session.headers.pop("X-Inject-Delay", None)

        try:
            resp = action_fns[action](session, method)
            status = resp.status_code
        except requests.RequestException as e:
            status = f"ERROR: {e}"

        if i % 20 == 0 or i == total_requests:
            print(f"  [{i}/{total_requests}] last action={action} method={method} status={status}")

        if delay_between:
            time.sleep(delay_between)

    print("Done.")


def run_cardinality_demo():
    """Hits /api/demo/scan with a unique request_id each time, hard-stopping
    at exactly 100 requests -- demonstrates cardinality explosion without
    letting it run away and hurt the local Prometheus instance.
    """
    session = requests.Session()
    LIMIT = 100
    print(f"Cardinality demo: sending exactly {LIMIT} uniquely-labeled requests...")

    for i in range(1, LIMIT + 1):
        request_id = str(uuid.uuid4())
        resp = session.post(
            f"{BASE_URL}/api/demo/scan", json={"request_id": request_id}, timeout=5
        )
        if i % 10 == 0 or i == LIMIT:
            print(f"  [{i}/{LIMIT}] request_id={request_id} status={resp.status_code}")
        time.sleep(0.05)

    print(
        f"Done. Hard-stopped at exactly {LIMIT} requests.\n"
        f"Check in Grafana/Prometheus: count(demo_requests_total) should now be {LIMIT}."
    )


def main():
    parser = argparse.ArgumentParser(description="Fake traffic generator for the attendance app.")
    parser.add_argument(
        "--requests", type=int, default=100, help="Total requests to send in normal mode (default: 100)"
    )
    parser.add_argument(
        "--rate", type=float, default=3.0, help="Target requests per second in normal mode (default: 3.0)"
    )
    parser.add_argument(
        "--inject-delay",
        action="store_true",
        help="Add a 500ms delay before every 5th request, simulating slow biometric matching (Part E).",
    )
    parser.add_argument(
        "--cardinality-demo",
        action="store_true",
        help="Run the cardinality-explosion demo instead of normal traffic: exactly 100 uniquely-labeled requests, then stop.",
    )
    args = parser.parse_args()

    if args.cardinality_demo:
        run_cardinality_demo()
    else:
        run_normal_mode(args.requests, args.rate, args.inject_delay)


if __name__ == "__main__":
    main()
