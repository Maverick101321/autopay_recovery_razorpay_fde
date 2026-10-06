import os
import re
from datetime import datetime
from pathlib import Path

import pytest

from app.db import get_db_connection
from app.models import Customer, FailedPayment, FailureReason, Language, PaymentStatus
from scripts.seed import generate_seed_data, seed_database


@pytest.fixture
def temp_db(tmp_path: Path) -> str:
    """Create a unique temporary database file for test isolation."""
    db_file = tmp_path / "test_autopay.db"
    return str(db_file)


def test_seed_creates_10_customers_and_payments(temp_db: str):
    """Assert exactly 10 customers and 10 failed payment records are created."""
    customers, payments = seed_database(db_path=temp_db, reset=True)

    assert len(customers) == 10
    assert len(payments) == 10

    conn = get_db_connection(temp_db)
    try:
        cust_rows = conn.execute("SELECT * FROM customers").fetchall()
        pay_rows = conn.execute("SELECT * FROM failed_payments").fetchall()

        assert len(cust_rows) == 10
        assert len(pay_rows) == 10
    finally:
        conn.close()


def test_customer_fields_and_edge_cases(temp_db: str):
    """Assert customer fields match specifications and edge cases are present."""
    seed_database(db_path=temp_db, reset=True)
    conn = get_db_connection(temp_db)
    try:
        rows = conn.execute("SELECT * FROM customers ORDER BY id").fetchall()
        customers = [Customer.model_validate(dict(r)) for r in rows]

        assert len(customers) == 10

        # Customer IDs format cust_001 .. cust_010
        expected_ids = [f"cust_{i:03d}" for i in range(1, 11)]
        assert [c.id for c in customers] == expected_ids

        # Phone numbers match fake pattern +91 98765 000NN
        fake_pattern = re.compile(r"^\+91 98765 000(0[1-9]|10)$")
        for c in customers:
            assert fake_pattern.match(c.phone), f"Phone {c.phone} does not match fake pattern"

        # Language distribution: 6 english, 4 hindi
        en_count = sum(1 for c in customers if c.language == Language.EN)
        hi_count = sum(1 for c in customers if c.language == Language.HI)
        assert en_count == 6
        assert hi_count == 4

        # Timezone default Asia/Kolkata
        for c in customers:
            assert c.timezone == "Asia/Kolkata"

        # Edge case 1: exactly 1 customer with consent_to_call = 0
        no_consent = [c for c in customers if c.consent_to_call == 0]
        assert len(no_consent) == 1
        assert no_consent[0].id == "cust_003"

        # Edge case 2: 1 customer with narrow window (10:00-11:00)
        narrow_window = [c for c in customers if c.call_window_start == "10:00" and c.call_window_end == "11:00"]
        assert len(narrow_window) == 1
        assert narrow_window[0].id == "cust_004"
    finally:
        conn.close()


def test_failed_payments_fields_and_edge_cases(temp_db: str):
    """Assert failed payments match specifications and edge cases are present."""
    seed_database(db_path=temp_db, reset=True)
    conn = get_db_connection(temp_db)
    try:
        rows = conn.execute("SELECT * FROM failed_payments ORDER BY id").fetchall()
        payments = [FailedPayment.model_validate(dict(r)) for r in rows]

        assert len(payments) == 10

        # Edge case 3: 1 customer with retry_count = 2 (near retry cap)
        retried = [p for p in payments if p.retry_count == 2]
        assert len(retried) == 1
        assert retried[0].customer_id == "cust_005"

        # Amounts between 199 and 4999 INR inclusive
        for p in payments:
            assert 199 <= p.amount_inr <= 4999

        # At least one failure_reason of each enum value across the 10 records
        all_reasons = {reason.value for reason in FailureReason}
        observed_reasons = {p.failure_reason.value for p in payments}
        assert all_reasons.issubset(observed_reasons)
        assert len(observed_reasons) == 6

        # Status defaults to pending
        for p in payments:
            assert p.status == PaymentStatus.PENDING

        # Valid ISO timestamps for failed_at
        for p in payments:
            dt = datetime.fromisoformat(p.failed_at.replace("Z", "+00:00"))
            assert dt is not None
    finally:
        conn.close()


def test_deterministic_seed_output(temp_db: str):
    """Assert repeated seed generation produces deterministic and identical output."""
    cust_1, pay_1 = generate_seed_data()
    cust_2, pay_2 = generate_seed_data()

    assert cust_1 == cust_2
    assert pay_1 == pay_2

    # Idempotent database seeding with reset
    seed_database(db_path=temp_db, reset=True)
    seed_database(db_path=temp_db, reset=True)

    conn = get_db_connection(temp_db)
    try:
        cust_count = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
        pay_count = conn.execute("SELECT COUNT(*) FROM failed_payments").fetchone()[0]
        assert cust_count == 10
        assert pay_count == 10
    finally:
        conn.close()


def test_demo_phone_handling(temp_db: str, monkeypatch: pytest.MonkeyPatch):
    """Assert cust_001 and cust_002 take DEMO_PHONE when set, and fall back when unset."""
    fake_demo_number = "+91 98765 00099"

    # Test with DEMO_PHONE set
    monkeypatch.setenv("DEMO_PHONE", fake_demo_number)
    custs_with_demo, _ = seed_database(db_path=temp_db, reset=True)
    assert custs_with_demo[0]["phone"] == fake_demo_number
    assert custs_with_demo[1]["phone"] == fake_demo_number
    assert custs_with_demo[2]["phone"] == "+91 98765 00003"

    # Test with DEMO_PHONE unset
    monkeypatch.delenv("DEMO_PHONE", raising=False)
    custs_without_demo, _ = seed_database(db_path=temp_db, reset=True, demo_phone_override="")
    assert custs_without_demo[0]["phone"] == "+91 98765 00001"
    assert custs_without_demo[1]["phone"] == "+91 98765 00002"
    assert custs_without_demo[2]["phone"] == "+91 98765 00003"
