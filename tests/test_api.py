import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch
import pytest
import httpx
from fastapi.testclient import TestClient

from app.db import get_db_connection
from app.main import app, get_db
from app.utils import compute_retell_webhook_signature, normalize_failed_payment_id
from scripts.seed import seed_database
from scripts.setup_retell import build_agent_payload, build_llm_payload, check_response, setup_retell

TEST_TOOL_SECRET = "test_shared_secret_xyz"
TEST_API_KEY = "test_retell_api_key_abc"


@pytest.fixture
def api_test_ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[tuple[TestClient, str], None, None]:
    """Provide isolated TestClient and temporary database for each test with test env vars."""
    db_file = str(tmp_path / "test_api.db")
    seed_database(db_path=db_file, reset=True, demo_phone_override="")

    monkeypatch.setenv("TOOL_SHARED_SECRET", TEST_TOOL_SECRET)
    monkeypatch.setenv("RETELL_API_KEY", TEST_API_KEY)
    monkeypatch.setenv("REQUIRE_WEBHOOK_SIGNATURE", "true")

    def override_get_db():
        conn = get_db_connection(db_file)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    try:
        yield client, db_file
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def tool_headers() -> dict[str, str]:
    return {"x-tool-secret": TEST_TOOL_SECRET}


def test_health_endpoint(api_test_ctx):
    """Health check endpoint must be open without tool secret."""
    client, _ = api_test_ctx
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_tool_shared_secret_enforcement(api_test_ctx):
    """All /tools/* endpoints require x-tool-secret header; reject with 401 otherwise."""
    client, _ = api_test_ctx

    # 1. Missing header -> 401
    resp_missing = client.post("/tools/get_payment_context", json={"args": {"failed_payment_id": "pay_001"}})
    assert resp_missing.status_code == 401
    assert "detail" in resp_missing.json()

    # 2. Invalid header -> 401
    resp_invalid = client.post(
        "/tools/get_payment_context",
        headers={"x-tool-secret": "wrong_secret"},
        json={"args": {"failed_payment_id": "pay_001"}},
    )
    assert resp_invalid.status_code == 401

    # 3. Valid header -> 200
    resp_valid = client.post(
        "/tools/get_payment_context",
        headers={"x-tool-secret": TEST_TOOL_SECRET},
        json={"args": {"failed_payment_id": "pay_001"}},
    )
    assert resp_valid.status_code == 200


def test_get_payment_context_voice_friendly_phrases_and_format(api_test_ctx, tool_headers):
    """Assert voice-friendly text format, phrase mappings, retry count+1, and removal of raw enums."""
    client, _ = api_test_ctx

    # pay_001: insufficient_funds, retry_count=0 -> attempt number 1
    resp1 = client.post(
        "/tools/get_payment_context",
        headers=tool_headers,
        json={"args": {"failed_payment_id": "pay_001"}},
    )
    assert resp1.status_code == 200
    res1 = resp1.json()["result"]
    expected1 = (
        "The customer is Aarav. A 799 rupee payment to StreamFlix for the Premium Ultra HD plan "
        "failed because the bank reported that the account balance was too low at that time. This is attempt number 1."
    )
    assert res1 == expected1
    assert "insufficient_funds" not in res1
    assert "Preferred language" not in res1

    # pay_002: card_expired, retry_count=0 -> attempt number 1
    resp2 = client.post(
        "/tools/get_payment_context",
        headers=tool_headers,
        json={"failed_payment_id": "pay_002"},
    )
    assert resp2.status_code == 200
    res2 = resp2.json()["result"]
    expected2 = (
        "The customer is Priya. A 2499 rupee payment to FitPulse Gym for the Monthly All-Access plan "
        "failed because the card on file has expired. This is attempt number 1."
    )
    assert res2 == expected2
    assert "card_expired" not in res2

    # pay_005: otp_not_completed, retry_count=2 -> attempt number 3
    resp3 = client.post(
        "/tools/get_payment_context",
        headers=tool_headers,
        json={"failed_payment_id": "pay_005"},
    )
    assert resp3.status_code == 200
    res3 = resp3.json()["result"]
    expected3 = (
        "The customer is Vikram. A 999 rupee payment to TechFin Newsletter for the Annual Supporter plan "
        "failed because the verification code step was not completed. This is attempt number 3."
    )
    assert res3 == expected3
    assert "otp_not_completed" not in res3


def test_get_payment_context_unknown_id(api_test_ctx, tool_headers):
    client, _ = api_test_ctx

    resp = client.post(
        "/tools/get_payment_context",
        headers=tool_headers,
        json={"args": {"failed_payment_id": "unknown_pay"}},
    )
    assert resp.status_code == 200
    assert resp.json() == {"result": "I couldn't find that account"}


def test_create_payment_link_and_idempotency(api_test_ctx, tool_headers):
    client, db_file = api_test_ctx

    # First call creates the link and writes outbound sms message
    resp1 = client.post(
        "/tools/create_payment_link",
        headers=tool_headers,
        json={"args": {"failed_payment_id": "pay_001"}},
    )
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert "https://pay.example.test/r/" in data1["result"]
    url1 = data1["result"].split("SMS: ")[1].strip()

    conn = get_db_connection(db_file)
    try:
        links1 = conn.execute("SELECT * FROM payment_links WHERE failed_payment_id = 'pay_001'").fetchall()
        msgs1 = conn.execute("SELECT * FROM outbound_messages WHERE failed_payment_id = 'pay_001'").fetchall()
        assert len(links1) == 1
        assert links1[0]["url"] == url1
        assert len(msgs1) == 1
        assert msgs1[0]["channel"] == "sms"
        assert url1 in msgs1[0]["body"]

        # Second call with flat payload: must be idempotent and return active link
        resp2 = client.post(
            "/tools/create_payment_link",
            headers=tool_headers,
            json={"failed_payment_id": "pay_001"},
        )
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert url1 in data2["result"]
        assert "Payment link is active" in data2["result"]
    finally:
        conn.close()


def test_create_payment_link_unknown_id(api_test_ctx, tool_headers):
    client, _ = api_test_ctx
    resp = client.post(
        "/tools/create_payment_link",
        headers=tool_headers,
        json={"args": {"failed_payment_id": "nonexistent"}},
    )
    assert resp.status_code == 200
    assert resp.json() == {"result": "I couldn't find that account"}


def test_log_outcome_status_transitions(api_test_ctx, tool_headers):
    client, db_file = api_test_ctx

    # 1. promised_to_pay -> status becomes 'promised'
    resp1 = client.post(
        "/tools/log_outcome",
        headers=tool_headers,
        json={
            "args": {
                "failed_payment_id": "pay_001",
                "outcome": "promised_to_pay",
                "notes": "Customer promised to clear dues by Friday",
            }
        },
    )
    assert resp1.status_code == 200
    assert resp1.json() == {"result": "Outcome logged as promised_to_pay."}

    # 2. paid_now -> status becomes 'recovered'
    resp2 = client.post(
        "/tools/log_outcome",
        headers=tool_headers,
        json={"failed_payment_id": "pay_002", "outcome": "paid_now", "notes": "Paid during call"},
    )
    assert resp2.status_code == 200
    assert resp2.json() == {"result": "Outcome logged as paid_now."}

    # 3. do_not_call -> status becomes 'do_not_call'
    resp3 = client.post(
        "/tools/log_outcome",
        headers=tool_headers,
        json={
            "args": {
                "failed_payment_id": "pay_003",
                "outcome": "do_not_call",
                "notes": "Customer opted out of calls",
            }
        },
    )
    assert resp3.status_code == 200
    assert resp3.json() == {"result": "Outcome logged as do_not_call."}

    # 4. other outcomes (refused) -> status unchanged, retry_count incremented
    resp4 = client.post(
        "/tools/log_outcome",
        headers=tool_headers,
        json={"failed_payment_id": "pay_004", "outcome": "refused", "notes": "Refused to pay"},
    )
    assert resp4.status_code == 200
    assert resp4.json() == {"result": "Outcome logged as refused."}

    conn = get_db_connection(db_file)
    try:
        p1 = conn.execute("SELECT status, retry_count FROM failed_payments WHERE id = 'pay_001'").fetchone()
        assert p1["status"] == "promised"
        assert p1["retry_count"] == 0

        p2 = conn.execute("SELECT status, retry_count FROM failed_payments WHERE id = 'pay_002'").fetchone()
        assert p2["status"] == "recovered"
        assert p2["retry_count"] == 0

        p3 = conn.execute("SELECT status, retry_count FROM failed_payments WHERE id = 'pay_003'").fetchone()
        assert p3["status"] == "do_not_call"
        assert p3["retry_count"] == 0

        p4 = conn.execute("SELECT status, retry_count FROM failed_payments WHERE id = 'pay_004'").fetchone()
        assert p4["status"] == "pending"
        assert p4["retry_count"] == 1
    finally:
        conn.close()


def test_log_outcome_unknown_id(api_test_ctx, tool_headers):
    client, _ = api_test_ctx
    resp = client.post(
        "/tools/log_outcome",
        headers=tool_headers,
        json={"args": {"failed_payment_id": "bad_id", "outcome": "refused"}},
    )
    assert resp.status_code == 200
    assert resp.json() == {"result": "I couldn't find that account"}


def test_schedule_retry(api_test_ctx, tool_headers):
    client, db_file = api_test_ctx

    resp = client.post(
        "/tools/schedule_retry",
        headers=tool_headers,
        json={"args": {"failed_payment_id": "pay_001", "preferred_time": "after lunch"}},
    )
    assert resp.status_code == 200
    assert "Retry scheduled for" in resp.json()["result"]

    conn = get_db_connection(db_file)
    try:
        p = conn.execute("SELECT next_retry_at FROM failed_payments WHERE id = 'pay_001'").fetchone()
        assert p["next_retry_at"] is not None
        dt = datetime.fromisoformat(p["next_retry_at"])
        assert dt is not None
    finally:
        conn.close()


def test_schedule_retry_unknown_id(api_test_ctx, tool_headers):
    client, _ = api_test_ctx
    resp = client.post(
        "/tools/schedule_retry",
        headers=tool_headers,
        json={"args": {"failed_payment_id": "not_there"}},
    )
    assert resp.status_code == 200
    assert resp.json() == {"result": "I couldn't find that account"}


def test_escalate_to_human(api_test_ctx, tool_headers):
    client, db_file = api_test_ctx

    resp = client.post(
        "/tools/escalate_to_human",
        headers=tool_headers,
        json={"args": {"failed_payment_id": "pay_001", "reason": "Customer is frustrated with double deduction"}},
    )
    assert resp.status_code == 200
    assert "Call escalated to human support" in resp.json()["result"]

    conn = get_db_connection(db_file)
    try:
        p = conn.execute("SELECT status FROM failed_payments WHERE id = 'pay_001'").fetchone()
        assert p["status"] == "escalated"
        att = conn.execute("SELECT * FROM call_attempts WHERE failed_payment_id = 'pay_001' ORDER BY id DESC LIMIT 1").fetchone()
        assert "double deduction" in att["notes"]
        assert att["outcome"] == "escalated"
    finally:
        conn.close()


def test_escalate_to_human_unknown_id(api_test_ctx, tool_headers):
    client, _ = api_test_ctx
    resp = client.post(
        "/tools/escalate_to_human",
        headers=tool_headers,
        json={"failed_payment_id": "unknown_id"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"result": "I couldn't find that account"}


def test_normalize_failed_payment_id_unit():
    """Unit tests for normalize_failed_payment_id helper."""
    assert normalize_failed_payment_id("1") == "pay_001"
    assert normalize_failed_payment_id("001") == "pay_001"
    assert normalize_failed_payment_id(" 002 ") == "pay_002"
    assert normalize_failed_payment_id("PAY_003") == "pay_003"
    assert normalize_failed_payment_id(" pay_004 ") == "pay_004"
    assert normalize_failed_payment_id(5) == "pay_005"
    assert normalize_failed_payment_id("10") == "pay_010"
    assert normalize_failed_payment_id("") == ""
    assert normalize_failed_payment_id(None) == ""


def test_tool_endpoints_id_tolerance(api_test_ctx, tool_headers):
    """All /tools/* handlers must accept numeric IDs (e.g. '1', '001') and whitespace/casing variations."""
    client, db_file = api_test_ctx

    # 1. get_payment_context with '1', '001', and casing/spaces
    resp1 = client.post(
        "/tools/get_payment_context",
        headers=tool_headers,
        json={"args": {"failed_payment_id": "1"}},
    )
    assert resp1.status_code == 200
    assert "The customer is Aarav." in resp1.json()["result"]

    resp2 = client.post(
        "/tools/get_payment_context",
        headers=tool_headers,
        json={"failed_payment_id": "001"},
    )
    assert resp2.status_code == 200
    assert "The customer is Aarav." in resp2.json()["result"]

    resp3 = client.post(
        "/tools/get_payment_context",
        headers=tool_headers,
        json={"failed_payment_id": " PAY_002 "},
    )
    assert resp3.status_code == 200
    assert "The customer is Priya." in resp3.json()["result"]

    # 2. create_payment_link with '1'
    resp_link = client.post(
        "/tools/create_payment_link",
        headers=tool_headers,
        json={"args": {"failed_payment_id": "1"}},
    )
    assert resp_link.status_code == 200
    assert "Payment link created and sent via SMS:" in resp_link.json()["result"]

    # 3. log_outcome with '1'
    resp_outcome = client.post(
        "/tools/log_outcome",
        headers=tool_headers,
        json={"args": {"failed_payment_id": "1", "outcome": "paid_now", "notes": "Paid"}},
    )
    assert resp_outcome.status_code == 200
    assert resp_outcome.json() == {"result": "Outcome logged as paid_now."}

    conn = get_db_connection(db_file)
    try:
        p1 = conn.execute("SELECT status FROM failed_payments WHERE id = 'pay_001'").fetchone()
        assert p1["status"] == "recovered"
    finally:
        conn.close()

    # 4. schedule_retry with '2'
    resp_retry = client.post(
        "/tools/schedule_retry",
        headers=tool_headers,
        json={"args": {"failed_payment_id": "2"}},
    )
    assert resp_retry.status_code == 200
    assert "Retry scheduled for" in resp_retry.json()["result"]

    conn = get_db_connection(db_file)
    try:
        p2 = conn.execute("SELECT next_retry_at FROM failed_payments WHERE id = 'pay_002'").fetchone()
        assert p2["next_retry_at"] is not None
    finally:
        conn.close()

    # 5. escalate_to_human with '3'
    resp_esc = client.post(
        "/tools/escalate_to_human",
        headers=tool_headers,
        json={"args": {"failed_payment_id": " 3 ", "reason": "Customer angry"}},
    )
    assert resp_esc.status_code == 200
    assert "Call escalated to human support: Customer angry." in resp_esc.json()["result"]

    conn = get_db_connection(db_file)
    try:
        p3 = conn.execute("SELECT status FROM failed_payments WHERE id = 'pay_003'").fetchone()
        assert p3["status"] == "escalated"
    finally:
        conn.close()


def test_webhook_signature_verification_valid_and_invalid(api_test_ctx, caplog):
    """Test webhook authentication accepting both raw body and compact JSON signature variants,
    rejecting bad signatures, and verifying diagnostic warning logs without credential leakage.
    """
    client, db_file = api_test_ctx
    call_id = "call_webhook_sig_test"

    payload = {
        "event": "call_started",
        "call": {
            "call_id": call_id,
            "metadata": {"failed_payment_id": "pay_001"},
            "start_timestamp": 1775470000000,
        },
    }

    # 1. Missing signature header -> 401 and warning logged
    raw_body = json.dumps(payload)
    with caplog.at_level("WARNING"):
        caplog.clear()
        resp_missing = client.post(
            "/webhooks/retell",
            content=raw_body,
            headers={"content-type": "application/json"},
        )
    assert resp_missing.status_code == 401
    assert "detail" in resp_missing.json()
    assert "header_present=False" in caplog.text

    # 2. Invalid signature header -> 401 and diagnostic warning logged
    current_ms = int(time.time() * 1000)
    with caplog.at_level("WARNING"):
        caplog.clear()
        resp_invalid = client.post(
            "/webhooks/retell",
            content=raw_body,
            headers={
                "content-type": "application/json",
                "x-retell-signature": f"v={current_ms},d=0000000000000000000000000000000000000000000000000000000000000000",
            },
        )
    assert resp_invalid.status_code == 401
    assert "header_present=True" in caplog.text
    assert f"parsed_timestamp={current_ms}" in caplog.text
    assert "skew_ms=" in caplog.text
    assert f"body_length={len(raw_body)}" in caplog.text
    assert "variants_tried=['raw_body', 'compact_json']" in caplog.text
    # Ensure neither key nor any digest appears in log
    assert TEST_API_KEY not in caplog.text
    assert "00000000000000000000000000000000" not in caplog.text

    # 3. Expired timestamp (> 5 minutes ago) -> 401
    old_time_ms = int(time.time() * 1000) - (10 * 60 * 1000)
    expired_sig = compute_retell_webhook_signature(raw_body, TEST_API_KEY, timestamp_ms=old_time_ms)
    resp_expired = client.post(
        "/webhooks/retell",
        content=raw_body,
        headers={
            "content-type": "application/json",
            "x-retell-signature": expired_sig,
        },
    )
    assert resp_expired.status_code == 401

    # 4. Valid signature header - Variant 1 (raw string body) -> 200
    valid_sig_raw = compute_retell_webhook_signature(raw_body, TEST_API_KEY, use_compact_json=False)
    resp_valid_raw = client.post(
        "/webhooks/retell",
        content=raw_body,
        headers={
            "content-type": "application/json",
            "x-retell-signature": valid_sig_raw,
        },
    )
    assert resp_valid_raw.status_code == 200
    assert resp_valid_raw.json() == {"status": "ok"}

    # 5. Valid signature header - Variant 2 (compact JSON signature with formatted raw body) -> 200
    pretty_body = json.dumps(payload, indent=2)
    assert pretty_body != json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    sig_compact = compute_retell_webhook_signature(pretty_body, TEST_API_KEY, use_compact_json=True)
    resp_valid_compact = client.post(
        "/webhooks/retell",
        content=pretty_body,
        headers={
            "content-type": "application/json",
            "x-retell-signature": sig_compact,
        },
    )
    assert resp_valid_compact.status_code == 200
    assert resp_valid_compact.json() == {"status": "ok"}

    conn = get_db_connection(db_file)
    try:
        att = conn.execute("SELECT * FROM call_attempts WHERE retell_call_id = ?", (call_id,)).fetchone()
        assert att is not None
        assert att["failed_payment_id"] == "pay_001"
    finally:
        conn.close()


def test_retell_webhook_call_flow(api_test_ctx):
    """Test full retell webhook lifecycle: call_started creates attempt, call_ended stores transcript and ended_at."""
    client, db_file = api_test_ctx
    call_id = "call_test_12345"

    # 1. call_started event
    start_payload = {
        "event": "call_started",
        "call": {
            "call_id": call_id,
            "metadata": {"failed_payment_id": "pay_001"},
            "start_timestamp": 1775470000000,
        },
    }
    raw_start = json.dumps(start_payload)
    sig_start = compute_retell_webhook_signature(raw_start, TEST_API_KEY)
    resp1 = client.post(
        "/webhooks/retell",
        content=raw_start,
        headers={"content-type": "application/json", "x-retell-signature": sig_start},
    )
    assert resp1.status_code == 200
    assert resp1.json() == {"status": "ok"}

    conn = get_db_connection(db_file)
    try:
        row1 = conn.execute("SELECT * FROM call_attempts WHERE retell_call_id = ?", (call_id,)).fetchone()
        assert row1 is not None
        assert row1["failed_payment_id"] == "pay_001"
        assert row1["retell_call_id"] == call_id

        # 2. call_ended event with transcript
        transcript_text = "Agent: Hello Aarav. Customer: I will pay tomorrow."
        end_payload = {
            "event": "call_ended",
            "call": {
                "call_id": call_id,
                "transcript": transcript_text,
                "end_timestamp": 1775470120000,
            },
        }
        raw_end = json.dumps(end_payload)
        sig_end = compute_retell_webhook_signature(raw_end, TEST_API_KEY)
        resp2 = client.post(
            "/webhooks/retell",
            content=raw_end,
            headers={"content-type": "application/json", "x-retell-signature": sig_end},
        )
        assert resp2.status_code == 200
        assert resp2.json() == {"status": "ok"}

        row2 = conn.execute("SELECT * FROM call_attempts WHERE retell_call_id = ?", (call_id,)).fetchone()
        assert row2["transcript"] == transcript_text
        assert row2["ended_at"] is not None
    finally:
        conn.close()


def test_admin_state_endpoint(api_test_ctx):
    """Admin state endpoint returns all payments with status, retry_count, and next_retry_at."""
    client, _ = api_test_ctx

    resp = client.get("/admin/state")
    assert resp.status_code == 200
    records = resp.json()
    assert len(records) == 10

    for rec in records:
        assert "id" in rec
        assert "status" in rec
        assert "retry_count" in rec
        assert "next_retry_at" in rec
        assert "latest_outcome" in rec


def test_setup_retell_dry_run_payload_and_no_network_calls():
    """Assert setup_retell --dry-run builds 5 tools + end_call, correct URLs, both agents, and makes no network calls."""
    base_url = "https://test.example.ngrok-free.app"
    tool_secret = "secret_xyz"

    llm_payload = build_llm_payload(public_base_url=base_url, tool_secret=tool_secret)
    assert llm_payload["begin_message"] == "{{opening_line}}"
    tools = llm_payload["general_tools"]

    # 1. Assert 5 custom tools + 1 end_call
    assert len(tools) == 6
    custom_tools = [t for t in tools if t.get("type") == "custom"]
    end_call_tools = [t for t in tools if t.get("type") == "end_call"]
    assert len(custom_tools) == 5
    assert len(end_call_tools) == 1

    tool_names = {t["name"] for t in tools}
    expected_names = {
        "get_payment_context",
        "create_payment_link",
        "log_outcome",
        "schedule_retry",
        "escalate_to_human",
        "end_call",
    }
    assert tool_names == expected_names

    log_outcome_tool = next(t for t in custom_tools if t["name"] == "log_outcome")
    outcome_enum = log_outcome_tool["parameters"]["properties"]["outcome"]["enum"]
    assert "claims_paid" in outcome_enum

    # 2. Check URLs and headers
    for ct in custom_tools:
        name = ct["name"]
        assert ct["url"] == f"{base_url}/tools/{name}"
        assert ct["method"] == "POST"
        assert ct["headers"].get("x-tool-secret") == tool_secret

    # 3. Check both agents
    agent_en = build_agent_payload(
        llm_id="llm_123",
        agent_name="Autopay Recovery Agent (EN)",
        language="en-IN",
        voice_id="11labs-Adrian",
        public_base_url=base_url,
    )
    assert agent_en["language"] == "en-IN"
    assert agent_en["webhook_url"] == f"{base_url}/webhooks/retell"
    assert agent_en["webhook_events"] == ["call_started", "call_ended", "call_analyzed"]

    agent_hi = build_agent_payload(
        llm_id="llm_123",
        agent_name="Autopay Recovery Agent (HI)",
        language="hi-IN",
        voice_id="11labs-Aditi",
        public_base_url=base_url,
    )
    assert agent_hi["language"] == "hi-IN"
    assert agent_hi["webhook_url"] == f"{base_url}/webhooks/retell"

    # 4. Mock httpx and assert it is NEVER called when running setup_retell(dry_run=True)
    with patch("httpx.Client") as mock_client:
        setup_retell(dry_run=True)
        mock_client.assert_not_called()


def test_setup_retell_dry_run_masks_headers(capsys):
    """In --dry-run output, verify that all header values like x-tool-secret are masked as '***'."""
    setup_retell(dry_run=True)
    captured = capsys.readouterr().out
    assert '"x-tool-secret": "***"' in captured
    assert TEST_TOOL_SECRET not in captured


def test_setup_retell_saves_ids_incrementally_and_reuses_on_rerun(tmp_path, monkeypatch):
    """Save ids to retell_ids.json immediately after EACH successful create.
    On rerun, reuse stored ids and PATCH instead of creating.
    A failed HI agent must not cause new LLMs to be created.
    """
    id_file = tmp_path / "retell_ids.json"
    created_calls = []

    monkeypatch.setenv("RETELL_API_KEY", "test_key_123")
    monkeypatch.setenv("RETELL_VOICE_ID_EN", "11labs-Adrian")
    monkeypatch.setenv("RETELL_VOICE_ID_HI", "11labs-Aditi")

    mock_voices_resp = MagicMock()
    mock_voices_resp.status_code = 200
    mock_voices_resp.json.return_value = [
        {"voice_id": "11labs-Adrian", "voice_name": "Adrian", "provider": "elevenlabs", "accent": "indian"},
        {"voice_id": "11labs-Aditi", "voice_name": "Aditi", "provider": "elevenlabs", "language": "Hindi"},
    ]

    mock_llm_post = MagicMock()
    mock_llm_post.status_code = 201
    mock_llm_post.json.return_value = {"llm_id": "llm_first_run"}

    mock_agent_en_post = MagicMock()
    mock_agent_en_post.status_code = 201
    mock_agent_en_post.json.return_value = {"agent_id": "agent_en_1"}

    mock_agent_hi_fail = MagicMock()
    mock_agent_hi_fail.status_code = 400
    mock_agent_hi_fail.text = '{"error": "Failed to create Hindi voice agent"}'

    def mock_request_run1(method, url, **kwargs):
        created_calls.append((method, url))
        if "list-voices" in url:
            return mock_voices_resp
        if "create-retell-llm" in url:
            return mock_llm_post
        if "create-agent" in url and kwargs.get("json", {}).get("language") == "en-IN":
            return mock_agent_en_post
        if "create-agent" in url and kwargs.get("json", {}).get("language") == "hi-IN":
            return mock_agent_hi_fail
        return MagicMock(status_code=404, text="Not Found")

    with patch("httpx.Client") as mock_client_cls:
        client_instance = mock_client_cls.return_value.__enter__.return_value
        client_instance.get.side_effect = lambda url, **kwargs: mock_request_run1("GET", url, **kwargs)
        client_instance.post.side_effect = lambda url, **kwargs: mock_request_run1("POST", url, **kwargs)
        client_instance.patch.side_effect = lambda url, **kwargs: mock_request_run1("PATCH", url, **kwargs)

        with pytest.raises(SystemExit) as exc_info:
            setup_retell(dry_run=False, id_file_path=id_file)
        assert exc_info.value.code == 1

    # After run 1 (which failed on HI agent), check id_file:
    # llm_id and agent_id_en must ALREADY be saved on disk!
    assert id_file.exists()
    ids_after_run1 = json.loads(id_file.read_text(encoding="utf-8"))
    assert ids_after_run1.get("llm_id") == "llm_first_run"
    assert ids_after_run1.get("agent_id_en") == "agent_en_1"
    assert "agent_id_hi" not in ids_after_run1

    # Run 2: HI agent now succeeds
    run2_calls = []
    mock_llm_patch = MagicMock(status_code=200, json=lambda: {"llm_id": "llm_first_run"})
    mock_agent_en_patch = MagicMock(status_code=200, json=lambda: {"agent_id": "agent_en_1"})
    mock_agent_hi_post = MagicMock(status_code=201, json=lambda: {"agent_id": "agent_hi_1"})

    def mock_request_run2(method, url, **kwargs):
        run2_calls.append((method, url))
        if "list-voices" in url:
            return mock_voices_resp
        if f"update-retell-llm/{ids_after_run1['llm_id']}" in url:
            return mock_llm_patch
        if f"update-agent/{ids_after_run1['agent_id_en']}" in url:
            return mock_agent_en_patch
        if "create-agent" in url and kwargs.get("json", {}).get("language") == "hi-IN":
            return mock_agent_hi_post
        return MagicMock(status_code=404, text="Not Found")

    with patch("httpx.Client") as mock_client_cls:
        client_instance = mock_client_cls.return_value.__enter__.return_value
        client_instance.get.side_effect = lambda url, **kwargs: mock_request_run2("GET", url, **kwargs)
        client_instance.post.side_effect = lambda url, **kwargs: mock_request_run2("POST", url, **kwargs)
        client_instance.patch.side_effect = lambda url, **kwargs: mock_request_run2("PATCH", url, **kwargs)

        setup_retell(dry_run=False, id_file_path=id_file)

    # In run 2, NO create-retell-llm was called (it reused stored ID and called PATCH)
    assert not any("create-retell-llm" in url for _, url in run2_calls)
    assert any("update-retell-llm/llm_first_run" in url for method, url in run2_calls if method == "PATCH")
    assert any("update-agent/agent_en_1" in url for method, url in run2_calls if method == "PATCH")
    assert any("create-agent" in url for method, url in run2_calls if method == "POST")

    # Final IDs file has all 3 IDs
    final_ids = json.loads(id_file.read_text(encoding="utf-8"))
    assert final_ids == {
        "llm_id": "llm_first_run",
        "agent_id_en": "agent_en_1",
        "agent_id_hi": "agent_hi_1",
    }


def test_setup_retell_aborts_on_missing_voice_and_prints_indian_voices(tmp_path, monkeypatch, capsys):
    """Before creating agents, verify voice ids exist; if not, abort with clear message and print Indian voices."""
    id_file = tmp_path / "retell_ids.json"
    monkeypatch.setenv("RETELL_VOICE_ID_EN", "nonexistent-voice-123")

    mock_voices_resp = MagicMock()
    mock_voices_resp.status_code = 200
    mock_voices_resp.json.return_value = [
        {"voice_id": "voice_in_1", "voice_name": "Kavita", "provider": "elevenlabs", "accent": "Indian English"},
        {"voice_id": "voice_us_1", "voice_name": "John", "provider": "elevenlabs", "language": "English"},
    ]

    mock_llm_post = MagicMock(status_code=201, json=lambda: {"llm_id": "llm_voice_test"})

    with patch("httpx.Client") as mock_client_cls:
        client_instance = mock_client_cls.return_value.__enter__.return_value
        client_instance.get.return_value = mock_voices_resp
        client_instance.post.return_value = mock_llm_post

        with pytest.raises(SystemExit) as exc_info:
            setup_retell(dry_run=False, id_file_path=id_file)
        assert exc_info.value.code == 1

    captured_err = capsys.readouterr().err
    assert "Voice ID verification failed" in captured_err
    assert "nonexistent-voice-123" in captured_err
    assert "Available Indian / Hindi-accent voices in Retell" in captured_err
    assert "voice_in_1" in captured_err
    assert "Kavita" in captured_err


def test_check_response_prints_error_body_on_failure(capsys):
    """check_response must print full error body and exit with status 1 on non-200/201 response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 422
    resp.text = '{"error": "invalid_parameters", "message": "Field plan_name is required"}'

    with pytest.raises(SystemExit) as exc:
        check_response(resp, "Creating Retell LLM")
    assert exc.value.code == 1

    captured_err = capsys.readouterr().err
    assert "Creating Retell LLM failed with status 422" in captured_err
    assert '{"error": "invalid_parameters", "message": "Field plan_name is required"}' in captured_err


def test_log_outcome_claims_paid(api_test_ctx, tool_headers):
    """Test claims_paid outcome marks payment as promised and appends note without incrementing retry_count."""
    client, db_file = api_test_ctx

    resp = client.post(
        "/tools/log_outcome",
        headers=tool_headers,
        json={"failed_payment_id": "pay_001", "outcome": "claims_paid", "notes": "Customer says UPI was debited"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"result": "Outcome logged as claims_paid."}

    conn = get_db_connection(db_file)
    try:
        p1 = conn.execute("SELECT status, retry_count FROM failed_payments WHERE id = 'pay_001'").fetchone()
        assert p1["status"] == "promised"
        assert p1["retry_count"] == 0
        att = conn.execute("SELECT * FROM call_attempts WHERE failed_payment_id = 'pay_001' ORDER BY id DESC LIMIT 1").fetchone()
        assert "customer claims paid; unverified" in att["notes"]
        assert att["outcome"] == "claims_paid"
    finally:
        conn.close()


def test_admin_confirm_payment_endpoint(api_test_ctx, tool_headers):
    """POST /admin/confirm_payment requires x-tool-secret, updates status to recovered, and returns 404 for unknown IDs."""
    client, db_file = api_test_ctx

    # 1. Missing secret header -> 401
    resp_unauth = client.post("/admin/confirm_payment", json={"failed_payment_id": "pay_001"})
    assert resp_unauth.status_code == 401

    # 2. Unknown ID -> 404
    resp_unknown = client.post(
        "/admin/confirm_payment",
        headers=tool_headers,
        json={"failed_payment_id": "nonexistent_pay"},
    )
    assert resp_unknown.status_code == 404

    # 3. Valid call -> 200 and payment status recovered
    conn = get_db_connection(db_file)
    try:
        with conn:
            conn.execute(
                "INSERT INTO call_attempts (failed_payment_id, retell_call_id, started_at, outcome) VALUES (?, ?, ?, ?)",
                ("pay_001", "call_test_confirm", "2026-10-08T10:00:00Z", "call_back_later"),
            )
    finally:
        conn.close()

    resp_valid = client.post(
        "/admin/confirm_payment",
        headers=tool_headers,
        json={"failed_payment_id": "1", "notes": "Confirmed bank settlement via Razorpay webhook"},
    )
    assert resp_valid.status_code == 200
    data = resp_valid.json()
    assert data["status"] == "recovered"
    assert data["failed_payment_id"] == "pay_001"

    conn = get_db_connection(db_file)
    try:
        p = conn.execute("SELECT status FROM failed_payments WHERE id = 'pay_001'").fetchone()
        assert p["status"] == "recovered"
        att = conn.execute("SELECT * FROM call_attempts WHERE failed_payment_id = 'pay_001' ORDER BY id DESC LIMIT 1").fetchone()
        assert att is not None
        assert att["outcome"] == "recovered"
    finally:
        conn.close()


def test_admin_calls_endpoint(api_test_ctx, tool_headers):
    """GET /admin/calls requires x-tool-secret and returns all attempts in descending order."""
    client, db_file = api_test_ctx

    # Missing secret -> 401
    resp_unauth = client.get("/admin/calls")
    assert resp_unauth.status_code == 401

    # Insert test call attempts
    conn = get_db_connection(db_file)
    try:
        with conn:
            conn.execute(
                "INSERT INTO call_attempts (failed_payment_id, retell_call_id, started_at, outcome) VALUES (?, ?, ?, ?)",
                ("pay_001", "call_first", "2026-10-08T10:00:00Z", "call_back_later"),
            )
            conn.execute(
                "INSERT INTO call_attempts (failed_payment_id, retell_call_id, started_at, outcome) VALUES (?, ?, ?, ?)",
                ("pay_002", "call_second", "2026-10-08T11:00:00Z", "promised_to_pay"),
            )
    finally:
        conn.close()

    resp_valid = client.get("/admin/calls", headers=tool_headers)
    assert resp_valid.status_code == 200
    calls = resp_valid.json()
    assert len(calls) >= 2
    assert calls[0]["retell_call_id"] == "call_second"
    assert calls[1]["retell_call_id"] == "call_first"


def test_webhook_multi_event_upsert_and_replay_deduplication(api_test_ctx):
    """Test webhook across call_started, call_ended, call_analyzed, fallback resolution of failed_payment_id,
    capping raw_events at 100 KB, and verifying replay does not duplicate rows.
    """
    client, db_file = api_test_ctx
    call_id = "call_multi_event_test_001"

    # Event 1: call_started
    p1 = {
        "event": "call_started",
        "call": {
            "call_id": call_id,
            "metadata": {"failed_payment_id": "pay_001"},
            "start_timestamp": 1775470000000,
        },
    }
    raw1 = json.dumps(p1)
    sig1 = compute_retell_webhook_signature(raw1, TEST_API_KEY)
    r1 = client.post("/webhooks/retell", content=raw1, headers={"content-type": "application/json", "x-retell-signature": sig1})
    assert r1.status_code == 200

    conn = get_db_connection(db_file)
    try:
        rows = conn.execute("SELECT * FROM call_attempts WHERE retell_call_id = ?", (call_id,)).fetchall()
        assert len(rows) == 1
        assert rows[0]["failed_payment_id"] == "pay_001"
        assert rows[0]["duration_ms"] is None

        # Event 2: call_ended with duration, disconnection reason, transcript
        p2 = {
            "event": "call_ended",
            "call": {
                "call_id": call_id,
                "metadata": {"failed_payment_id": "pay_001"},
                "start_timestamp": 1775470000000,
                "end_timestamp": 1775470045000,
                "duration_ms": 45000,
                "disconnection_reason": "user_hangup",
                "transcript": "User: Yes speaking. Agent: Your payment failed.",
            },
        }
        raw2 = json.dumps(p2)
        sig2 = compute_retell_webhook_signature(raw2, TEST_API_KEY)
        r2 = client.post("/webhooks/retell", content=raw2, headers={"content-type": "application/json", "x-retell-signature": sig2})
        assert r2.status_code == 200

        rows2 = conn.execute("SELECT * FROM call_attempts WHERE retell_call_id = ?", (call_id,)).fetchall()
        assert len(rows2) == 1  # No duplicate rows
        assert rows2[0]["duration_ms"] == 45000
        assert rows2[0]["disconnection_reason"] == "user_hangup"
        assert "Your payment failed" in rows2[0]["transcript"]

        # Event 3: call_analyzed with call_cost and call_analysis
        p3 = {
            "event": "call_analyzed",
            "call": {
                "call_id": call_id,
                "metadata": {"failed_payment_id": "pay_001"},
                "call_cost": {"combined_cost": 0.045},
                "call_analysis": {
                    "call_summary": "Customer verified identity and agreed to link",
                    "user_sentiment": "Positive",
                },
            },
        }
        raw3 = json.dumps(p3)
        sig3 = compute_retell_webhook_signature(raw3, TEST_API_KEY)
        r3 = client.post("/webhooks/retell", content=raw3, headers={"content-type": "application/json", "x-retell-signature": sig3})
        assert r3.status_code == 200

        rows3 = conn.execute("SELECT * FROM call_attempts WHERE retell_call_id = ?", (call_id,)).fetchall()
        assert len(rows3) == 1
        assert rows3[0]["call_cost"] == 0.045
        assert "call_summary" in rows3[0]["call_analysis"]
        assert rows3[0]["duration_ms"] == 45000  # Preserved from call_ended

        # Replay event 3: Ensure still exactly 1 row
        r3_replay = client.post("/webhooks/retell", content=raw3, headers={"content-type": "application/json", "x-retell-signature": sig3})
        assert r3_replay.status_code == 200
        rows3_replay = conn.execute("SELECT * FROM call_attempts WHERE retell_call_id = ?", (call_id,)).fetchall()
        assert len(rows3_replay) == 1

        # Test fallback resolution from retell_llm_dynamic_variables when metadata is absent
        call_id_fallback = "call_fallback_dyn_vars"
        p_fallback = {
            "event": "call_started",
            "call": {
                "call_id": call_id_fallback,
                "retell_llm_dynamic_variables": {"failed_payment_id": "pay_002"},
            },
        }
        raw_fb = json.dumps(p_fallback)
        sig_fb = compute_retell_webhook_signature(raw_fb, TEST_API_KEY)
        r_fb = client.post("/webhooks/retell", content=raw_fb, headers={"content-type": "application/json", "x-retell-signature": sig_fb})
        assert r_fb.status_code == 200
        fb_row = conn.execute("SELECT * FROM call_attempts WHERE retell_call_id = ?", (call_id_fallback,)).fetchone()
        assert fb_row is not None
        assert fb_row["failed_payment_id"] == "pay_002"

        # Test call without any payment ID (e.g. web test call): stored with failed_payment_id NULL
        call_id_none = "call_no_payment_id"
        p_none = {
            "event": "call_started",
            "call": {
                "call_id": call_id_none,
            },
        }
        raw_none = json.dumps(p_none)
        sig_none = compute_retell_webhook_signature(raw_none, TEST_API_KEY)
        r_none = client.post("/webhooks/retell", content=raw_none, headers={"content-type": "application/json", "x-retell-signature": sig_none})
        assert r_none.status_code == 200
        none_row = conn.execute("SELECT * FROM call_attempts WHERE retell_call_id = ?", (call_id_none,)).fetchone()
        assert none_row is not None
        assert none_row["failed_payment_id"] is None
    finally:
        conn.close()

