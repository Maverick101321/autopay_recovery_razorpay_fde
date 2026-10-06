import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from app.db import get_db_connection
from app.main import app, get_db
from app.utils import compute_retell_webhook_signature
from scripts.seed import seed_database
from scripts.setup_retell import build_agent_payload, build_llm_payload, setup_retell

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


def test_webhook_signature_verification_valid_and_invalid(api_test_ctx):
    """Test webhook authentication with HMAC-SHA256 signature verification."""
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
    raw_body = json.dumps(payload)

    # 1. Missing signature header -> 401
    resp_missing = client.post(
        "/webhooks/retell",
        content=raw_body,
        headers={"content-type": "application/json"},
    )
    assert resp_missing.status_code == 401
    assert "detail" in resp_missing.json()

    # 2. Invalid signature header -> 401
    resp_invalid = client.post(
        "/webhooks/retell",
        content=raw_body,
        headers={
            "content-type": "application/json",
            "x-retell-signature": "v=1775470000000,d=invalid_hex_digest_12345",
        },
    )
    assert resp_invalid.status_code == 401

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

    # 4. Valid signature header -> 200
    valid_sig = compute_retell_webhook_signature(raw_body, TEST_API_KEY)
    resp_valid = client.post(
        "/webhooks/retell",
        content=raw_body,
        headers={
            "content-type": "application/json",
            "x-retell-signature": valid_sig,
        },
    )
    assert resp_valid.status_code == 200
    assert resp_valid.json() == {"status": "ok"}

    conn = get_db_connection(db_file)
    try:
        att = conn.execute("SELECT * FROM call_attempts WHERE retell_call_id = ?", (call_id,)).fetchone()
        assert att is not None
        assert att["failed_payment_id"] == "pay_001"
    finally:
        conn.close()


def test_setup_retell_dry_run_payload_and_no_network_calls():
    """Assert setup_retell --dry-run builds 5 tools + end_call, correct URLs, both agents, and makes no network calls."""
    base_url = "https://test.example.ngrok-free.app"
    tool_secret = "secret_xyz"

    llm_payload = build_llm_payload(public_base_url=base_url, tool_secret=tool_secret)
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
