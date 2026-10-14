from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pytest

from app.rules import MAX_RETRIES, can_call, is_dialable, next_retry_time


def test_is_dialable_logic(monkeypatch: pytest.MonkeyPatch):
    """Assert is_dialable accepts only DEMO_PHONE and rejects fake numbers."""
    demo_phone = "+919876500099"
    fake_phone = "+919876500003"

    # When DEMO_PHONE is unset
    monkeypatch.delenv("DEMO_PHONE", raising=False)
    assert not is_dialable(demo_phone)
    assert not is_dialable(fake_phone)

    # When DEMO_PHONE is set
    monkeypatch.setenv("DEMO_PHONE", demo_phone)
    assert is_dialable(demo_phone)
    assert not is_dialable(fake_phone)


def test_cust_003_to_cust_010_never_dialable(monkeypatch: pytest.MonkeyPatch):
    """Assert that with DEMO_PHONE set, cust_003 through cust_010 are all not dialable."""
    monkeypatch.setenv("DEMO_PHONE", "+919999900001")

    for i in range(3, 11):
        fake_phone = f"+9198765{i:05d}"
        assert not is_dialable(fake_phone), f"Customer {i} phone {fake_phone} should not be dialable"


def test_can_call_terminal_status():
    """Assert terminal payment statuses block calling."""
    customer = {
        "id": "cust_001",
        "phone": "+919876500001",
        "consent_to_call": 1,
        "call_window_start": "09:00",
        "call_window_end": "18:00",
        "timezone": "Asia/Kolkata",
    }
    tz = ZoneInfo("Asia/Kolkata")
    now = datetime(2026, 10, 6, 10, 30, tzinfo=tz)

    for term_status in ("recovered", "escalated", "do_not_call", "failed"):
        payment = {"id": "pay_001", "status": term_status, "retry_count": 0}
        ok, reason = can_call(customer, payment, now)
        assert not ok
        assert reason == "terminal_status"


def test_can_call_no_consent():
    """Assert consent_to_call == 0 blocks calling."""
    customer = {
        "id": "cust_003",
        "phone": "+919876500003",
        "consent_to_call": 0,
        "call_window_start": "09:00",
        "call_window_end": "18:00",
        "timezone": "Asia/Kolkata",
    }
    payment = {"id": "pay_003", "status": "pending", "retry_count": 0}
    now = datetime(2026, 10, 6, 11, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    ok, reason = can_call(customer, payment, now)
    assert not ok
    assert reason == "no_consent"


def test_can_call_retry_cap():
    """Assert retry_count >= MAX_RETRIES blocks calling."""
    customer = {
        "id": "cust_005",
        "phone": "+919876500005",
        "consent_to_call": 1,
        "call_window_start": "09:00",
        "call_window_end": "18:00",
        "timezone": "Asia/Kolkata",
    }
    payment = {"id": "pay_005", "status": "pending", "retry_count": MAX_RETRIES}
    now = datetime(2026, 10, 6, 11, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    ok, reason = can_call(customer, payment, now)
    assert not ok
    assert reason == "retry_cap"


def test_can_call_outside_window():
    """Assert calling cust_004 (window 10:00-11:00) at 15:00 IST blocks calling."""
    customer = {
        "id": "cust_004",
        "phone": "+919876500004",
        "consent_to_call": 1,
        "call_window_start": "10:00",
        "call_window_end": "11:00",
        "timezone": "Asia/Kolkata",
    }
    payment = {"id": "pay_004", "status": "pending", "retry_count": 0}
    now_1500_ist = datetime(2026, 10, 6, 15, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    ok, reason = can_call(customer, payment, now_1500_ist)
    assert not ok
    assert reason == "outside_window"


def test_can_call_exclusive_end_boundary(monkeypatch: pytest.MonkeyPatch):
    """Assert call window end is EXCLUSIVE (start <= t < end): exactly end is blocked, 1 sec before is ok."""
    demo_phone = "+919876500099"
    monkeypatch.setenv("DEMO_PHONE", demo_phone)

    customer = {
        "id": "cust_001",
        "phone": demo_phone,
        "consent_to_call": 1,
        "call_window_start": "09:00",
        "call_window_end": "18:00",
        "timezone": "Asia/Kolkata",
    }
    payment = {"id": "pay_001", "status": "pending", "retry_count": 0}
    tz = ZoneInfo("Asia/Kolkata")

    # Boundary 1: Exactly at window start (09:00:00) -> Allowed
    at_start = datetime(2026, 10, 6, 9, 0, 0, tzinfo=tz)
    ok_start, reason_start = can_call(customer, payment, at_start)
    assert ok_start
    assert reason_start == "ok"

    # Boundary 2: One second before window end (17:59:59) -> Allowed
    before_end = datetime(2026, 10, 6, 17, 59, 59, tzinfo=tz)
    ok_before, reason_before = can_call(customer, payment, before_end)
    assert ok_before
    assert reason_before == "ok"

    # Boundary 3: Exactly at window end (18:00:00) -> Blocked (outside_window)
    at_end = datetime(2026, 10, 6, 18, 0, 0, tzinfo=tz)
    ok_end, reason_end = can_call(customer, payment, at_end)
    assert not ok_end
    assert reason_end == "outside_window"


def test_can_call_not_allowlisted(monkeypatch: pytest.MonkeyPatch):
    """Assert non-allowlisted phones are blocked when within window and consent given."""
    monkeypatch.delenv("DEMO_PHONE", raising=False)
    customer = {
        "id": "cust_001",
        "phone": "+919876500001",
        "consent_to_call": 1,
        "call_window_start": "09:00",
        "call_window_end": "18:00",
        "timezone": "Asia/Kolkata",
    }
    payment = {"id": "pay_001", "status": "pending", "retry_count": 0}
    now = datetime(2026, 10, 6, 11, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    ok, reason = can_call(customer, payment, now)
    assert not ok
    assert reason == "not_allowlisted"


def test_can_call_ok_when_demo_phone_set(monkeypatch: pytest.MonkeyPatch):
    """Assert can_call returns (True, 'ok') when DEMO_PHONE is set and all conditions pass."""
    demo_phone = "+919876500099"
    monkeypatch.setenv("DEMO_PHONE", demo_phone)

    customer = {
        "id": "cust_001",
        "phone": demo_phone,
        "consent_to_call": 1,
        "call_window_start": "09:00",
        "call_window_end": "18:00",
        "timezone": "Asia/Kolkata",
    }
    payment = {"id": "pay_001", "status": "pending", "retry_count": 0}
    now = datetime(2026, 10, 6, 11, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    ok, reason = can_call(customer, payment, now)
    assert ok
    assert reason == "ok"


def test_can_call_priority_order(monkeypatch: pytest.MonkeyPatch):
    """Assert precedence: terminal status > no consent > retry cap > outside window > not allowlisted."""
    monkeypatch.delenv("DEMO_PHONE", raising=False)

    # Both terminal and no_consent: terminal status must take precedence
    cust = {
        "id": "c1",
        "phone": "+919876500001",
        "consent_to_call": 0,
        "call_window_start": "09:00",
        "call_window_end": "18:00",
        "timezone": "Asia/Kolkata",
    }
    pay = {"id": "p1", "status": "recovered", "retry_count": 5}
    now_outside = datetime(2026, 10, 6, 23, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    ok, reason = can_call(cust, pay, now_outside)
    assert not ok
    assert reason == "terminal_status"

    # No consent + retry cap: no_consent must take precedence
    pay["status"] = "pending"
    ok, reason = can_call(cust, pay, now_outside)
    assert not ok
    assert reason == "no_consent"

    # Retry cap + outside window: retry_cap must take precedence
    cust["consent_to_call"] = 1
    ok, reason = can_call(cust, pay, now_outside)
    assert not ok
    assert reason == "retry_cap"

    # Outside window + not allowlisted: outside_window must take precedence
    pay["retry_count"] = 0
    ok, reason = can_call(cust, pay, now_outside)
    assert not ok
    assert reason == "outside_window"


def test_next_retry_time_calculation():
    """Assert next_retry_time calculates slot >= 4h later within call window."""
    customer = {
        "call_window_start": "10:00",
        "call_window_end": "18:00",
        "timezone": "Asia/Kolkata",
    }
    tz = ZoneInfo("Asia/Kolkata")

    # Case 1: now at 09:00 -> 4h later is 13:00 (inside window)
    now1 = datetime(2026, 10, 6, 9, 0, tzinfo=tz)
    retry1 = next_retry_time(now1, customer)
    assert retry1 == datetime(2026, 10, 6, 13, 0, tzinfo=tz)
    assert retry1 >= now1 + timedelta(hours=4)

    # Case 2: now at 15:00 -> 4h later is 19:00 (after window closes at 18:00) -> moves to tomorrow 10:00
    now2 = datetime(2026, 10, 6, 15, 0, tzinfo=tz)
    retry2 = next_retry_time(now2, customer)
    assert retry2 == datetime(2026, 10, 7, 10, 0, tzinfo=tz)
    assert retry2 >= now2 + timedelta(hours=4)

    # Case 3: now at 02:00 -> 4h later is 06:00 (before window opens at 10:00) -> moves to today 10:00
    now3 = datetime(2026, 10, 6, 2, 0, tzinfo=tz)
    retry3 = next_retry_time(now3, customer)
    assert retry3 == datetime(2026, 10, 6, 10, 0, tzinfo=tz)
    assert retry3 >= now3 + timedelta(hours=4)
