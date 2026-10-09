import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from app.db import get_db_connection
from scripts.dial import run_dial
from scripts.seed import seed_database

TEST_DEMO_PHONE = "+918433907272"
TEST_FROM_NUMBER = "+14155552671"


@pytest.fixture
def dial_ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Provide isolated SQLite database and mock retell_ids.json for dial tests."""
    db_file = str(tmp_path / "test_dial.db")
    seed_database(db_path=db_file, reset=True, demo_phone_override=TEST_DEMO_PHONE)

    ids_file = tmp_path / "retell_ids.json"
    ids_data = {
        "llm_id": "llm_dial_test",
        "agent_id_en": "agent_en_dial_test",
        "agent_id_hi": "agent_hi_dial_test",
    }
    ids_file.write_text(json.dumps(ids_data), encoding="utf-8")

    monkeypatch.setenv("DEMO_PHONE", TEST_DEMO_PHONE)
    monkeypatch.setenv("RETELL_FROM_NUMBER", TEST_FROM_NUMBER)
    monkeypatch.setenv("RETELL_API_KEY", "test_retell_key")

    return db_file, ids_file


def test_dial_plan_all_10_payments(dial_ctx, capsys):
    """--plan mode outputs a formatted table of all 10 payments with dialability and reason codes."""
    db_file, _ = dial_ctx

    exit_code = run_dial(plan_mode=True, db_path=db_file)
    assert exit_code == 0

    out = capsys.readouterr().out
    assert "Payment ID" in out
    assert "Customer" in out
    assert "Can Call?" in out
    assert "Reason" in out

    # Assert all 10 payments are listed
    for i in range(1, 11):
        assert f"pay_{i:03d}" in out

    assert "Summary: 10 payments evaluated" in out


def test_dial_plan_evaluation_at_1500_ist(dial_ctx, capsys):
    """--plan evaluation at 15:00 IST checks exact reasons per customer."""
    db_file, _ = dial_ctx
    # 15:00 IST = 09:30 UTC
    eval_time = datetime(2026, 10, 8, 9, 30, tzinfo=timezone.utc)

    exit_code = run_dial(plan_mode=True, db_path=db_file, now=eval_time)
    assert exit_code == 0

    out = capsys.readouterr().out
    lines = {line.split("|")[0].strip(): line for line in out.splitlines() if line.startswith("pay_")}

    # cust_001 and cust_002 have DEMO_PHONE and are in window
    assert "YES" in lines["pay_001"] and "ok" in lines["pay_001"]
    assert "YES" in lines["pay_002"] and "ok" in lines["pay_002"]

    # cust_003 has consent_to_call = 0
    assert "no_consent" in lines["pay_003"]

    # cust_004 call window is 10:00 - 11:00, so at 15:00 it is outside_window
    assert "outside_window" in lines["pay_004"]

    # cust_005 has retry_count = 2 (< 3 cap) and is in window (09:30-18:30), blocked because not allowlisted
    assert "not_allowlisted" in lines["pay_005"]

    # cust_006 through cust_010 are in window at 15:00 but fake numbers
    for p_id in ["pay_006", "pay_007", "pay_008", "pay_009", "pay_010"]:
        assert "not_allowlisted" in lines[p_id]


def test_dial_demo_phone_safety_refusal(dial_ctx, capsys):
    """Payments whose destination number does not equal DEMO_PHONE are strictly refused."""
    db_file, ids_file = dial_ctx

    # pay_003 belongs to Rohan Verma with fake phone +919876500003 != TEST_DEMO_PHONE
    exit_code = run_dial(payment_id_arg="pay_003", db_path=db_file, ids_path=ids_file)
    assert exit_code == 1

    err = capsys.readouterr().err
    assert "Safety refusal" in err
    assert "does not match DEMO_PHONE" in err


def test_dial_dry_run_payload_and_no_network_call(dial_ctx, capsys):
    """Dry-run prints exact create-phone-call request payload without placing network calls."""
    db_file, ids_file = dial_ctx

    # Make pay_001 call window always open for this test
    conn = get_db_connection(db_file)
    try:
        with conn:
            conn.execute("UPDATE customers SET call_window_start = '00:00', call_window_end = '23:59' WHERE id = 'cust_001'")
    finally:
        conn.close()

    with patch("httpx.Client") as mock_client:
        exit_code = run_dial(payment_id_arg="pay_001", live_mode=False, db_path=db_file, ids_path=ids_file)
        assert exit_code == 0
        mock_client.assert_not_called()

    captured = capsys.readouterr().out
    payload = json.loads(captured)

    assert payload["from_number"] == TEST_FROM_NUMBER
    assert payload["to_number"] == TEST_DEMO_PHONE
    assert payload["override_agent_id"] == "agent_en_dial_test"
    assert payload["metadata"]["failed_payment_id"] == "pay_001"

    dyn = payload["retell_llm_dynamic_variables"]
    assert dyn["customer_first_name"] == "Aarav"
    assert dyn["merchant_name"] == "StreamFlix"
    assert dyn["plan_name"] == "Premium Ultra HD"
    assert dyn["amount_inr"] == "799"
    assert dyn["amount_spoken"] == "seven hundred ninety nine rupees"
    assert dyn["failed_payment_id"] == "pay_001"
    assert dyn["language"] == "en"
    assert "Hello, this is Riya, an automated AI assistant calling on behalf of StreamFlix" in dyn["opening_line"]
    assert "Am I speaking with Aarav?" in dyn["opening_line"]


def test_dial_hindi_language_agent_mapping_and_dynamic_variables(dial_ctx, capsys):
    """Hindi customer uses agent_id_hi and Hindi opening line with spoken amount."""
    db_file, ids_file = dial_ctx

    # pay_002 is Hindi customer Priya with DEMO_PHONE
    conn = get_db_connection(db_file)
    try:
        with conn:
            conn.execute("UPDATE customers SET call_window_start = '00:00', call_window_end = '23:59' WHERE id = 'cust_002'")
    finally:
        conn.close()

    exit_code = run_dial(payment_id_arg="pay_002", live_mode=False, db_path=db_file, ids_path=ids_file)
    assert exit_code == 0

    captured = capsys.readouterr().out
    payload = json.loads(captured)

    assert payload["override_agent_id"] == "agent_hi_dial_test"
    dyn = payload["retell_llm_dynamic_variables"]
    assert dyn["customer_first_name"] == "Priya"
    assert dyn["language"] == "hi"
    assert dyn["amount_inr"] == "2499"
    assert dyn["amount_spoken"] == "two thousand four hundred ninety nine rupees"
    assert "Namaste, main FitPulse Gym ki taraf se Riya bol rahi hoon, ek automated AI assistant" in dyn["opening_line"]
    assert "Kya meri baat Priya ji se ho rahi hai?" in dyn["opening_line"]


def test_dial_live_without_yes_refusal(dial_ctx, capsys):
    """Live mode requires typing exact YES confirmation; aborts otherwise without network call."""
    db_file, ids_file = dial_ctx

    conn = get_db_connection(db_file)
    try:
        with conn:
            conn.execute("UPDATE customers SET call_window_start = '00:00', call_window_end = '23:59' WHERE id = 'cust_001'")
    finally:
        conn.close()

    with patch("httpx.Client") as mock_client:
        exit_code = run_dial(
            payment_id_arg="pay_001",
            live_mode=True,
            db_path=db_file,
            ids_path=ids_file,
            mock_input="no",
        )
        assert exit_code == 1
        mock_client.assert_not_called()

    err = capsys.readouterr().err
    assert "Safety refusal" in err
    assert "Expected 'YES' but got 'no'" in err


def test_dial_cooldown_refusal(dial_ctx, capsys):
    """Payments with a call attempt started within the last 10 minutes are refused due to cooldown."""
    db_file, ids_file = dial_ctx

    conn = get_db_connection(db_file)
    try:
        with conn:
            conn.execute("UPDATE customers SET call_window_start = '00:00', call_window_end = '23:59' WHERE id = 'cust_001'")
            conn.execute(
                "INSERT INTO call_attempts (failed_payment_id, retell_call_id, started_at) VALUES (?, ?, ?)",
                ("pay_001", "call_prev_recent", datetime.now(timezone.utc).isoformat()),
            )
    finally:
        conn.close()

    exit_code = run_dial(payment_id_arg="pay_001", live_mode=False, db_path=db_file, ids_path=ids_file)
    assert exit_code == 1

    err = capsys.readouterr().err
    assert "Safety refusal" in err
    assert "Cooldown active" in err


def test_dial_live_success_with_yes(dial_ctx, capsys):
    """Live mode with YES places call, prints call ID, and inserts call_attempt record into database."""
    db_file, ids_file = dial_ctx

    conn = get_db_connection(db_file)
    try:
        with conn:
            conn.execute("UPDATE customers SET call_window_start = '00:00', call_window_end = '23:59' WHERE id = 'cust_001'")
    finally:
        conn.close()

    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"call_id": "call_live_created_123"}

    with patch("httpx.Client") as mock_client_cls:
        client_instance = mock_client_cls.return_value.__enter__.return_value
        client_instance.post.return_value = mock_resp

        exit_code = run_dial(
            payment_id_arg="pay_001",
            live_mode=True,
            db_path=db_file,
            ids_path=ids_file,
            mock_input="YES",
        )
        assert exit_code == 0

    out = capsys.readouterr().out
    assert "Call successfully placed! Retell Call ID: call_live_created_123" in out

    # Verify call attempt is logged in DB
    conn = get_db_connection(db_file)
    try:
        row = conn.execute("SELECT * FROM call_attempts WHERE retell_call_id = 'call_live_created_123'").fetchone()
        assert row is not None
        assert row["failed_payment_id"] == "pay_001"
    finally:
        conn.close()


def test_dial_limit_exceeded(dial_ctx, capsys):
    """Limit greater than 3 is rejected by hard safety cap."""
    db_file, ids_file = dial_ctx
    exit_code = run_dial(payment_id_arg="pay_001", limit=4, db_path=db_file, ids_path=ids_file)
    assert exit_code == 1

    err = capsys.readouterr().err
    assert "--limit cannot exceed 3" in err
