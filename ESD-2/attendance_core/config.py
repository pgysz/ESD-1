"""Configuration shared by the HTTP and gRPC processes.

Simulation constants are unchanged from Assignment 1. New in Assignment 2:
environment-variable overrides so demos and tests are repeatable (for example
SCAN_FAILURE_RATE=0 makes every scan succeed instead of failing at random).
"""

import os
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent

SCAN_METHODS = ("face", "fingerprint", "voice")

# Chance a scan fails to match (simulated bad read), per method. (A1 values.)
SCAN_FAILURE_RATE = {"face": 0.08, "fingerprint": 0.05, "voice": 0.12}

# Simulated processing delay per method, in seconds (min, max). (A1 values.)
SCAN_DELAY_RANGE = {
    "face": (0.05, 0.25),
    "fingerprint": (0.02, 0.10),
    "voice": (0.10, 0.40),
}

SIGNUP_DELAY_RANGE = (0.05, 0.20)
SIGNUP_FAILURE_RATE = 0.05

# "On time" vs "late" rule. (A1 values.)
SHIFT_START_HOUR = 9
SHIFT_START_MINUTE = 0
GRACE_PERIOD_MINUTES = 10

# The 20 users seeded into an empty database (same people as Assignment 1).
SEED_USERS = [
    ("u1", "Ali Raza"), ("u2", "Sara Khan"), ("u3", "Bilal Ahmed"),
    ("u4", "Ayesha Noor"), ("u5", "Hamza Tariq"), ("u6", "Zainab Malik"),
    ("u7", "Usman Sheikh"), ("u8", "Mahnoor Aziz"), ("u9", "Fahad Iqbal"),
    ("u10", "Hira Baig"), ("u11", "Omar Farooq"), ("u12", "Sana Yousuf"),
    ("u13", "Danish Rehman"), ("u14", "Iqra Siddiqui"), ("u15", "Kashif Nawaz"),
    ("u16", "Rabia Hassan"), ("u17", "Junaid Aslam"), ("u18", "Nida Chaudhry"),
    ("u19", "Waqas Anwar"), ("u20", "Mehreen Qureshi"),
]


def _env_float(name: str) -> Optional[float]:
    value = os.environ.get(name)
    return float(value) if value not in (None, "") else None


def db_path() -> str:
    """Location of the shared SQLite database (the single source of truth)."""
    return os.environ.get("ATTENDANCE_DB", str(ROOT / "data" / "attendance.db"))


def log_dir() -> str:
    return os.environ.get("ATTENDANCE_LOG_DIR", str(ROOT / "logs"))


def fault_injection_enabled() -> bool:
    """Fault-injection headers/metadata are ignored unless this is set to 1."""
    return os.environ.get("ENABLE_FAULT_INJECTION", "0") == "1"


def scan_failure_override() -> Optional[float]:
    return _env_float("SCAN_FAILURE_RATE")


def signup_failure_override() -> Optional[float]:
    return _env_float("SIGNUP_FAILURE_RATE")


def signup_rate_limit() -> str:
    """'<max requests>/<window seconds>' for POST /api/v1/users."""
    return os.environ.get("RATE_LIMIT_SIGNUP", "10/60")
