"""Business-rule tests that do not need any transport."""
import threading

import pytest

from attendance_core.errors import (AlreadyCheckedInError, NotFoundError, ScanNotMatchedError,
                                    ValidationError)
from attendance_core.store import Store
from tests.conftest import make_service


def test_two_store_objects_on_one_file_see_the_same_data(tmp_path):
    """Stand-in for 'two processes': they only share the database file."""
    a, b = Store(str(tmp_path / "x.db")), Store(str(tmp_path / "x.db"))
    a.create_user("Shared Person", 1.0)
    assert any(u["name"] == "Shared Person" for u in b.list_users())
    a.insert_session("s1", "u1", "on_time", "face", 1.0)
    assert b.get_open_session_for_user("u1")["session_id"] == "s1"


def test_database_forbids_a_second_open_session(store):
    store.insert_session("s1", "u1", "on_time", "face", 1.0)
    from attendance_core.store import AlreadyOpenSessionError
    with pytest.raises(AlreadyOpenSessionError):
        store.insert_session("s2", "u1", "late", "voice", 2.0)


def test_conflict_is_checked_before_scanning(store, logger):
    """A1 scanned first, so a duplicate check-in could fail with a scan error."""
    make_service(store, logger).check_in("u1", "face")
    scans = []
    svc = make_service(store, logger, scan_failure_rate=1.0, sleep=scans.append)
    with pytest.raises(AlreadyCheckedInError):
        svc.check_in("u1", "face")
    assert scans == []                                          # no scan was attempted


def test_unknown_user_is_checked_before_scanning(store, logger):
    scans = []
    svc = make_service(store, logger, sleep=scans.append)
    with pytest.raises(NotFoundError):
        svc.check_in("ghost", "face")
    assert scans == []


def test_failed_scan_raises_and_records_event(store, logger):
    svc = make_service(store, logger, scan_failure_rate=1.0)
    with pytest.raises(ScanNotMatchedError):
        svc.check_in("u1", "face")
    assert svc.recent_events(1)[0]["type"] == "checkin_failed"


def test_on_time_vs_late_uses_shift_start_plus_grace(store, logger):
    from datetime import datetime
    on_time = datetime(2026, 10, 20, 9, 10).timestamp()
    late = datetime(2026, 10, 20, 9, 11).timestamp()
    assert make_service(store, logger, clock=lambda: on_time).check_in("u1", "face")[0]["status"] == "on_time"
    assert make_service(store, logger, clock=lambda: late).check_in("u2", "face")[0]["status"] == "late"


def test_idempotency_key_is_validated(service):
    with pytest.raises(ValidationError):
        service.check_in("u1", "face", "bad key with spaces")


def test_signup_assigns_next_id_and_persists(service, store):
    new = service.signup("New Person")
    assert new["user_id"] == "u21" and store.get_user("u21")["name"] == "New Person"


def test_race_between_two_threads_with_same_key_executes_once(store, logger):
    """Two concurrent identical requests: one executes, the other replays."""
    svc = make_service(store, logger)
    outcomes = []

    def worker():
        outcomes.append(svc.check_in("u7", "face", "same-key"))
    threads = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert sorted(replayed for _body, replayed in outcomes) == [False, True, True, True]
    assert len({body["id"] for body, _ in outcomes}) == 1
    assert len(store.list_sessions(state="open")) == 1
