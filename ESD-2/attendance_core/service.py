"""AttendanceService: the business rules, shared by HTTP and gRPC.

Rules (identical whichever transport calls them):
  * input is validated here, so both transports reject the same bad input;
  * unknown user -> NotFound; user already checked in -> AlreadyCheckedIn
    (both checked BEFORE a scan is attempted -- fixes the A1 ordering bug where
    a duplicate check-in burned a scan first);
  * a failed biometric scan -> ScanNotMatched;
  * check-in with an Idempotency-Key is safe to repeat: the stored result is
    replayed and nothing is executed twice;
  * checking out an already-closed session returns that closed session (the
    operation is naturally idempotent) instead of acting a second time.
"""

import hashlib
import json
import random
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

from . import config, metrics, tracing
from .errors import (AlreadyCheckedInError, EnrollmentUnavailableError,
                     IdempotencyKeyReuseError, NotFoundError, ScanNotMatchedError,
                     ValidationError)
from .logging_setup import log_event
from .store import AlreadyOpenSessionError

NAME_MAX_LENGTH = 80
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


# --- validation (shared by both transports) --------------------------------

def _invalid(field, message):
    return ValidationError("One or more fields are invalid.",
                           errors=[{"field": field, "message": message}])


def validate_name(value):
    if value is None:
        raise _invalid("name", "is required")
    if not isinstance(value, str):
        raise _invalid("name", "must be a string")
    value = value.strip()
    if not value:
        raise _invalid("name", "must not be empty")
    if len(value) > NAME_MAX_LENGTH:
        raise _invalid("name", f"must be at most {NAME_MAX_LENGTH} characters")
    if any(ord(ch) < 32 for ch in value):
        raise _invalid("name", "must not contain control characters")
    return value


def validate_method(value):
    if value is None:
        raise _invalid("method", "is required")
    if value not in config.SCAN_METHODS:
        raise _invalid("method", f"must be one of {', '.join(config.SCAN_METHODS)}")
    return value


def validate_id(value, field):
    if value is None or value == "":
        raise _invalid(field, "is required")
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise _invalid(field, "must be 1-64 characters: letters, digits, '_' or '-'")
    return value


def validate_idempotency_key(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _KEY_RE.match(value):
        raise _invalid("Idempotency-Key", "must be 1-128 characters: letters, digits, . _ : -")
    return value


def _iso(epoch):
    return None if epoch is None else datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def session_repr(row):
    """Database row -> the representation returned by the API."""
    return {
        "id": row["session_id"],
        "user_id": row["user_id"],
        "state": row["state"],
        "status": row["status"],
        "check_in_method": row["check_in_method"],
        "checked_in_at": _iso(row["checked_in_at"]),
        "checked_out_at": _iso(row["checked_out_at"]),
        "check_out_method": row["check_out_method"],
    }


class AttendanceService:
    def __init__(self, store, logger, *, scan_failure_rate=None, signup_failure_rate=None,
                 delay_scale=1.0, sleep=time.sleep, rng=None, clock=time.time):
        """The keyword arguments exist so tests can make the simulation
        deterministic (no sleeping, forced success/failure)."""
        self.store = store
        self.logger = logger
        self._scan_failure_rate = scan_failure_rate
        self._signup_failure_rate = signup_failure_rate
        self._delay_scale = delay_scale
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._clock = clock
        metrics.bind_store(store)

    # -- helpers ------------------------------------------------------------

    def _log(self, severity, message):
        log_event(self.logger, severity, message)

    def _scan_failure(self, method):
        if self._scan_failure_rate is not None:
            return self._scan_failure_rate
        override = config.scan_failure_override()
        return override if override is not None else config.SCAN_FAILURE_RATE[method]

    def _signup_failure(self):
        if self._signup_failure_rate is not None:
            return self._signup_failure_rate
        override = config.signup_failure_override()
        return override if override is not None else config.SIGNUP_FAILURE_RATE

    def _simulate_scan(self, method, action):
        lo, hi = config.SCAN_DELAY_RANGE[method]
        duration = self._rng.uniform(lo, hi) * self._delay_scale
        self._sleep(duration)
        success = self._rng.random() >= self._scan_failure(method)
        metrics.SCAN_DURATION_SECONDS.labels(scan_method=method).observe(duration)
        metrics.SCAN_ATTEMPTS_TOTAL.labels(
            scan_method=method, action=action,
            result="success" if success else "failure").inc()
        return success, duration

    @staticmethod
    def _attendance_status(epoch):
        local = datetime.fromtimestamp(epoch)          # shift times are local time
        shift_start = local.replace(hour=config.SHIFT_START_HOUR,
                                    minute=config.SHIFT_START_MINUTE, second=0, microsecond=0)
        cutoff = shift_start + timedelta(minutes=config.GRACE_PERIOD_MINUTES)
        return "on_time" if local <= cutoff else "late"

    @staticmethod
    def _hash(payload):
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def _replay_if_known(self, key, request_hash, operation):
        """Return the stored response for this key, or None if the key is new."""
        stored = self.store.get_idempotency(key)
        if stored is None:
            return None
        if stored["request_hash"] != request_hash:
            raise IdempotencyKeyReuseError(
                "This Idempotency-Key was already used with a different request body.")
        metrics.IDEMPOTENT_REPLAYS_TOTAL.labels(operation).inc()
        self._log("INFO", f"{operation}_replayed reason=idempotency_key")
        return stored["response"]

    # -- users --------------------------------------------------------------

    def list_users(self):
        return self.store.list_users()

    def get_user(self, user_id):
        user_id = validate_id(user_id, "user_id")
        user = self.store.get_user(user_id)
        if user is None:
            raise NotFoundError(f"User '{user_id}' does not exist.")
        return user

    def signup(self, name):
        name = validate_name(name)
        lo, hi = config.SIGNUP_DELAY_RANGE
        duration = self._rng.uniform(lo, hi) * self._delay_scale
        self._sleep(duration)
        success = self._rng.random() >= self._signup_failure()
        metrics.SIGNUP_DURATION_SECONDS.observe(duration)
        metrics.SIGNUPS_TOTAL.labels(result="success" if success else "failed").inc()
        now = self._clock()
        if not success:
            self.store.add_event("signup_failed", now, reason="enrollment_error",
                                 request_id=tracing.current_request_id())
            self._log("ERROR", f"signup_failed reason=enrollment_error "
                               f"duration_ms={round(duration * 1000, 1)}")
            raise EnrollmentUnavailableError(
                "Enrollment storage is temporarily unavailable. Please retry later.")
        user = self.store.create_user(name, now)
        self.store.add_event("signup_success", now, user_id=user["user_id"],
                             request_id=tracing.current_request_id())
        self._log("INFO", f"signup_success duration_ms={round(duration * 1000, 1)}")
        return user

    # -- check-in -----------------------------------------------------------

    def check_in(self, user_id, method, idempotency_key=None):
        """Returns (session_representation, replayed: bool)."""
        user_id = validate_id(user_id, "user_id")
        method = validate_method(method)
        key = validate_idempotency_key(idempotency_key)
        request_hash = self._hash({"user_id": user_id, "method": method})

        if key:                                   # a repeat of a finished request?
            replayed = self._replay_if_known(key, request_hash, "check_in")
            if replayed is not None:
                return replayed, True

        if self.store.get_user(user_id) is None:
            raise NotFoundError(f"User '{user_id}' does not exist.")
        if self.store.get_open_session_for_user(user_id) is not None:
            self._log("WARNING", f"checkin_rejected reason=already_checked_in method={method}")
            raise AlreadyCheckedInError("This user is already checked in.")

        success, duration = self._simulate_scan(method, "checkin")
        duration_ms = round(duration * 1000, 1)
        if not success:
            self.store.add_event("checkin_failed", self._clock(), user_id=user_id,
                                 method=method, reason="scan_not_matched",
                                 request_id=tracing.current_request_id())
            self._log("ERROR", f"checkin_failed method={method} reason=scan_not_matched "
                               f"duration_ms={duration_ms}")
            raise ScanNotMatchedError("The biometric scan did not match. Please scan again.")

        now = self._clock()
        status = self._attendance_status(now)
        session_id = str(uuid.uuid4())
        replayed = None
        try:
            with self.store.transaction():
                if key:                           # re-check: a concurrent twin may have won
                    replayed = self._replay_if_known(key, request_hash, "check_in")
                if replayed is None:
                    self.store.insert_session(session_id, user_id, status, method, now)
                    body = session_repr(self.store.get_session(session_id))
                    if key:
                        self.store.save_idempotency(key, request_hash, body, now)
                    self.store.add_event("checkin_success", now, user_id=user_id,
                                         method=method, status=status,
                                         request_id=tracing.current_request_id())
        except AlreadyOpenSessionError:
            raise AlreadyCheckedInError("This user is already checked in.")
        if replayed is not None:
            return replayed, True

        metrics.CHECKINS_TOTAL.labels(status=status).inc()
        self._log("INFO", f"checkin_success method={method} status={status} "
                          f"duration_ms={duration_ms}")
        return body, False

    # -- check-out ----------------------------------------------------------

    def check_out(self, session_id, method):
        """Returns (session_representation, replayed: bool)."""
        session_id = validate_id(session_id, "session_id")
        method = validate_method(method)
        row = self.store.get_session(session_id)
        if row is None:
            raise NotFoundError(f"Attendance session '{session_id}' does not exist.")
        if row["state"] == "closed":              # repeating a finished checkout
            metrics.IDEMPOTENT_REPLAYS_TOTAL.labels("check_out").inc()
            self._log("INFO", "check_out_replayed reason=already_closed")
            return session_repr(row), True

        success, duration = self._simulate_scan(method, "checkout")
        duration_ms = round(duration * 1000, 1)
        if not success:
            self.store.add_event("checkout_failed", self._clock(), user_id=row["user_id"],
                                 method=method, reason="scan_not_matched",
                                 request_id=tracing.current_request_id())
            self._log("ERROR", f"checkout_failed method={method} reason=scan_not_matched "
                               f"duration_ms={duration_ms}")
            raise ScanNotMatchedError("The biometric scan did not match. Please scan again.")

        now = self._clock()
        with self.store.transaction():
            closed_by_us = self.store.close_session(session_id, method, now)
            if closed_by_us:
                self.store.add_event("checkout_success", now, user_id=row["user_id"],
                                     method=method, request_id=tracing.current_request_id())
        updated = session_repr(self.store.get_session(session_id))
        if not closed_by_us:                      # a concurrent twin closed it first
            metrics.IDEMPOTENT_REPLAYS_TOTAL.labels("check_out").inc()
            return updated, True
        metrics.CHECKOUTS_TOTAL.inc()
        self._log("INFO", f"checkout_success method={method} duration_ms={duration_ms}")
        return updated, False

    # -- reads --------------------------------------------------------------

    def get_session(self, session_id):
        session_id = validate_id(session_id, "session_id")
        row = self.store.get_session(session_id)
        if row is None:
            raise NotFoundError(f"Attendance session '{session_id}' does not exist.")
        return session_repr(row)

    def list_sessions(self, state=None, user_id=None, limit=100):
        if state not in (None, "open", "closed"):
            raise _invalid("state", "must be 'open' or 'closed'")
        if user_id is not None:
            user_id = validate_id(user_id, "user_id")
        return [session_repr(r) for r in self.store.list_sessions(state, user_id, limit)]

    def recent_events(self, limit=20):
        return [{
            "type": e["type"], "user_id": e["user_id"], "method": e["method"],
            "status": e["status"], "reason": e["reason"],
            "timestamp": _iso(e["ts"]),
        } for e in self.store.recent_events(limit)]
