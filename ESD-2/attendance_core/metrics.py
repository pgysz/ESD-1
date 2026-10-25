"""Business metrics shared by both processes.

Metric names/types are unchanged from Assignment 1 so the existing dashboard
keeps working. Each process (HTTP, gRPC) exports its own copy on its own
/metrics endpoint; Prometheus tells them apart by the `job` label.

New in A2 (see docs/A2_CHANGES.md, Part C):
  * currently_checked_in is now computed from the database at scrape time
  * idempotent_replays_total, store_operation_duration_seconds, rate_limited_total
  * finer duration buckets
All labels are bounded -- never a user, request or trace id.
"""

from prometheus_client import Counter, Gauge, Histogram, Summary

# Tuned to this system: scans take 20-400 ms (+500 ms when a fault is injected).
LATENCY_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 1, 2.5)

SCAN_ATTEMPTS_TOTAL = Counter(
    "scan_attempts_total",
    "Total biometric scan attempts (check-in or check-out)",
    ["scan_method", "action", "result"],
)

SCAN_DURATION_SECONDS = Histogram(
    "scan_duration_seconds",
    "Simulated biometric scan/match duration in seconds",
    ["scan_method"],
    buckets=LATENCY_BUCKETS,
)

CHECKINS_TOTAL = Counter("checkins_total", "Total successful check-ins", ["status"])
CHECKOUTS_TOTAL = Counter("checkouts_total", "Total successful check-outs")

# Computed from the shared database on every scrape (see bind_store()).
CURRENTLY_CHECKED_IN = Gauge(
    "currently_checked_in", "Number of people currently checked in (from the database)"
)

SIGNUPS_TOTAL = Counter("signups_total", "Total new-user signup attempts", ["result"])

# Summary on purpose (A1 decision): the Python client only exposes count/sum.
SIGNUP_DURATION_SECONDS = Summary(
    "signup_duration_seconds", "Simulated signup/enrollment duration in seconds"
)

# --- New in A2 -------------------------------------------------------------

IDEMPOTENT_REPLAYS_TOTAL = Counter(
    "idempotent_replays_total",
    "Repeated requests answered from a stored result instead of re-executing",
    ["operation"],
)

STORE_OPERATION_DURATION_SECONDS = Histogram(
    "store_operation_duration_seconds",
    "Duration of database operations in seconds",
    ["operation"],
    buckets=(0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25),
)

RATE_LIMITED_TOTAL = Counter(
    "rate_limited_total",
    "Requests rejected with 429 Too Many Requests",
    ["endpoint"],
)


def bind_store(store):
    """Make currently_checked_in read the database at scrape time."""
    CURRENTLY_CHECKED_IN.set_function(store.count_open)
