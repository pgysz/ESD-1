#!/usr/bin/env python3
"""Show the rate limit on POST /api/v1/users: accepted calls, then 429 with
Retry-After, then accepted again once the window has moved on.

Tip: start the server with a small limit so the demo is quick, and with
SIGNUP_FAILURE_RATE=0 so no signup fails at random:
    RATE_LIMIT_SIGNUP=5/10 SIGNUP_FAILURE_RATE=0 python app.py
    python scripts/rate_limit_demo.py
Note: each accepted call really creates a user (named 'Rate Demo n').
"""
import argparse
import time

import requests


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default="http://localhost:5000")
    a = p.parse_args()
    url = f"{a.base_url}/api/v1/users"
    first = requests.post(url, json={"name": "Rate Demo 0"}, timeout=5)
    limit = int(first.headers["RateLimit-Limit"])
    print(f"limit = {limit} requests per window (per client IP)")
    print(f"call  1: {first.status_code}  remaining={first.headers['RateLimit-Remaining']}")
    retry_after = None
    for n in range(2, limit + 3):
        r = requests.post(url, json={"name": f"Rate Demo {n}"}, timeout=5)
        extra = f"remaining={r.headers.get('RateLimit-Remaining')}"
        if r.status_code == 429:
            retry_after = int(r.headers["Retry-After"])
            extra = f"Retry-After={retry_after}s  body.title={r.json()['title']!r}"
        print(f"call {n:>2}: {r.status_code}  {extra}")
    if retry_after:
        print(f"waiting {retry_after}s (the oldest call leaves the window) ...")
        time.sleep(retry_after + 0.5)
        r = requests.post(url, json={"name": "Rate Demo after wait"}, timeout=5)
        print(f"after wait: {r.status_code}  remaining={r.headers.get('RateLimit-Remaining')}")


if __name__ == "__main__":
    main()
