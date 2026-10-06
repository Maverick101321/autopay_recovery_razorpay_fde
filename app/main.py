import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Generator, Optional
import sqlite3

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.config import (
    RETELL_API_KEY,
    REQUIRE_WEBHOOK_SIGNATURE,
    TOOL_SHARED_SECRET,
)
from app.db import get_db_connection
from app.rules import next_retry_time
from app.utils import verify_retell_webhook_signature

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("autopay.tools")

app = FastAPI(title="Autopay Recovery Agent")

FAILURE_REASON_PHRASES: dict[str, str] = {
    "insufficient_funds": "the bank reported that the account balance was too low at that time",
    "card_expired": "the card on file has expired",
    "mandate_expired": "the autopay authorization has expired and needs to be renewed",
    "bank_declined": "the bank declined the transaction",
    "otp_not_completed": "the verification code step was not completed",
    "daily_limit_exceeded": "the transaction went over the bank's daily limit",
}


def get_db() -> Generator[sqlite3.Connection, None, None]:
    """FastAPI dependency providing an active SQLite connection."""
    conn = get_db_connection()
    try:
        yield conn
    finally:
        conn.close()


def verify_tool_secret(x_tool_secret: Optional[str] = Header(None, alias="x-tool-secret")) -> None:
    """Require header x-tool-secret equal to TOOL_SHARED_SECRET; otherwise return 401."""
    expected = os.getenv("TOOL_SHARED_SECRET", TOOL_SHARED_SECRET).strip()
    if not expected or x_tool_secret != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: invalid or missing tool secret",
        )


def extract_args(payload: Any) -> dict[str, Any]:
    """Extract tool arguments whether wrapped in {"args": {...}} or passed directly."""
    if isinstance(payload, dict):
        if "args" in payload and isinstance(payload["args"], dict):
            return payload["args"]
        return payload
    return {}


tools_router = APIRouter(prefix="/tools", dependencies=[Depends(verify_tool_secret)])


@app.get("/health")
def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


@tools_router.post("/get_payment_context")
async def get_payment_context(request: Request, db: sqlite3.Connection = Depends(get_db)) -> dict[str, str]:
    """Retrieve customer and failed payment details for the voice agent."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    args = extract_args(body)
    logger.info("get_payment_context called with args: %s", args)

    failed_payment_id = args.get("failed_payment_id")
    if not failed_payment_id:
        return {"result": "I couldn't find that account"}

    payment = db.execute("SELECT * FROM failed_payments WHERE id = ?", (failed_payment_id,)).fetchone()
    if not payment:
        return {"result": "I couldn't find that account"}

    customer = db.execute("SELECT * FROM customers WHERE id = ?", (payment["customer_id"],)).fetchone()
    if not customer:
        return {"result": "I couldn't find that account"}

    first_name = customer["name"].split()[0]
    merchant = payment["merchant_name"]
    plan = payment["plan_name"]
    amount = payment["amount_inr"]
    raw_reason = payment["failure_reason"]
    phrase = FAILURE_REASON_PHRASES.get(raw_reason, str(raw_reason).replace("_", " "))
    retries = payment["retry_count"]

    result = (
        f"The customer is {first_name}. A {amount} rupee payment to {merchant} "
        f"for the {plan} plan failed because {phrase}. This is attempt number {retries + 1}."
    )
    return {"result": result}


@tools_router.post("/create_payment_link")
async def create_payment_link(request: Request, db: sqlite3.Connection = Depends(get_db)) -> dict[str, str]:
    """Generate or retrieve a mock payment link and simulate sending via SMS."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    args = extract_args(body)
    logger.info("create_payment_link called with args: %s", args)

    failed_payment_id = args.get("failed_payment_id")
    if not failed_payment_id:
        return {"result": "I couldn't find that account"}

    payment = db.execute("SELECT * FROM failed_payments WHERE id = ?", (failed_payment_id,)).fetchone()
    if not payment:
        return {"result": "I couldn't find that account"}

    existing = db.execute(
        "SELECT url FROM payment_links WHERE failed_payment_id = ? AND status = 'created' ORDER BY created_at DESC LIMIT 1",
        (failed_payment_id,),
    ).fetchone()
    if existing:
        return {"result": f"Payment link is active: {existing['url']}"}

    link_id = f"link_{uuid.uuid4().hex[:8]}"
    url = f"https://pay.example.test/r/{link_id}"
    now_iso = datetime.now(timezone.utc).isoformat()

    with db:
        db.execute(
            "INSERT INTO payment_links (id, failed_payment_id, url, created_at, status) VALUES (?, ?, ?, ?, 'created')",
            (link_id, failed_payment_id, url, now_iso),
        )

        msg_id = f"msg_{uuid.uuid4().hex[:8]}"
        sms_body = f"Please complete your payment of INR {payment['amount_inr']} for {payment['merchant_name']}: {url}"
        db.execute(
            "INSERT INTO outbound_messages (id, failed_payment_id, channel, body, created_at) VALUES (?, ?, 'sms', ?, ?)",
            (msg_id, failed_payment_id, sms_body, now_iso),
        )

    return {"result": f"Payment link created and sent via SMS: {url}"}


@tools_router.post("/log_outcome")
async def log_outcome(request: Request, db: sqlite3.Connection = Depends(get_db)) -> dict[str, str]:
    """Log the call outcome, update payment status, and update the latest call attempt."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    args = extract_args(body)
    logger.info("log_outcome called with args: %s", args)

    failed_payment_id = args.get("failed_payment_id")
    outcome = args.get("outcome")
    notes = args.get("notes") or ""

    if not failed_payment_id:
        return {"result": "I couldn't find that account"}

    payment = db.execute("SELECT * FROM failed_payments WHERE id = ?", (failed_payment_id,)).fetchone()
    if not payment:
        return {"result": "I couldn't find that account"}

    if outcome == "promised_to_pay":
        new_status = "promised"
        new_retry = payment["retry_count"]
    elif outcome == "paid_now":
        new_status = "recovered"
        new_retry = payment["retry_count"]
    elif outcome == "do_not_call":
        new_status = "do_not_call"
        new_retry = payment["retry_count"]
    else:
        new_status = payment["status"]
        new_retry = payment["retry_count"] + 1

    with db:
        db.execute(
            "UPDATE failed_payments SET status = ?, retry_count = ? WHERE id = ?",
            (new_status, new_retry, failed_payment_id),
        )

        latest_attempt = db.execute(
            "SELECT id FROM call_attempts WHERE failed_payment_id = ? ORDER BY id DESC LIMIT 1",
            (failed_payment_id,),
        ).fetchone()

        if latest_attempt:
            db.execute(
                "UPDATE call_attempts SET outcome = ?, notes = ? WHERE id = ?",
                (outcome, notes, latest_attempt["id"]),
            )
        else:
            db.execute(
                "INSERT INTO call_attempts (failed_payment_id, started_at, outcome, notes) VALUES (?, ?, ?, ?)",
                (failed_payment_id, datetime.now(timezone.utc).isoformat(), outcome, notes),
            )

    return {"result": f"Outcome logged as {outcome}."}


@tools_router.post("/schedule_retry")
async def schedule_retry(request: Request, db: sqlite3.Connection = Depends(get_db)) -> dict[str, str]:
    """Schedule the next retry time inside customer's call window."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    args = extract_args(body)
    logger.info("schedule_retry called with args: %s", args)

    failed_payment_id = args.get("failed_payment_id")
    if not failed_payment_id:
        return {"result": "I couldn't find that account"}

    payment = db.execute("SELECT * FROM failed_payments WHERE id = ?", (failed_payment_id,)).fetchone()
    if not payment:
        return {"result": "I couldn't find that account"}

    customer = db.execute("SELECT * FROM customers WHERE id = ?", (payment["customer_id"],)).fetchone()
    if not customer:
        return {"result": "I couldn't find that account"}

    now_utc = datetime.now(timezone.utc)
    retry_dt = next_retry_time(now_utc, customer)
    retry_iso = retry_dt.isoformat()

    with db:
        db.execute(
            "UPDATE failed_payments SET next_retry_at = ? WHERE id = ?",
            (retry_iso, failed_payment_id),
        )

    return {"result": f"Retry scheduled for {retry_iso}."}


@tools_router.post("/escalate_to_human")
async def escalate_to_human(request: Request, db: sqlite3.Connection = Depends(get_db)) -> dict[str, str]:
    """Escalate payment issue to human support."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    args = extract_args(body)
    logger.info("escalate_to_human called with args: %s", args)

    failed_payment_id = args.get("failed_payment_id")
    reason = args.get("reason") or "Customer requested escalation"

    if not failed_payment_id:
        return {"result": "I couldn't find that account"}

    payment = db.execute("SELECT * FROM failed_payments WHERE id = ?", (failed_payment_id,)).fetchone()
    if not payment:
        return {"result": "I couldn't find that account"}

    note_text = f"Escalated to human: {reason}"
    with db:
        db.execute("UPDATE failed_payments SET status = 'escalated' WHERE id = ?", (failed_payment_id,))

        latest_attempt = db.execute(
            "SELECT id, notes FROM call_attempts WHERE failed_payment_id = ? ORDER BY id DESC LIMIT 1",
            (failed_payment_id,),
        ).fetchone()

        if latest_attempt:
            combined_notes = f"{latest_attempt['notes']}; {note_text}" if latest_attempt["notes"] else note_text
            db.execute("UPDATE call_attempts SET notes = ?, outcome = 'escalated' WHERE id = ?", (combined_notes, latest_attempt["id"]))
        else:
            db.execute(
                "INSERT INTO call_attempts (failed_payment_id, started_at, outcome, notes) VALUES (?, ?, 'escalated', ?)",
                (failed_payment_id, datetime.now(timezone.utc).isoformat(), note_text),
            )

    return {"result": f"Call escalated to human support: {reason}."}


app.include_router(tools_router)


@app.post("/webhooks/retell")
async def retell_webhook(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Any:
    """Receive call events from Retell AI with HMAC-SHA256 signature verification."""
    raw_body_bytes = await request.body()
    raw_body_str = raw_body_bytes.decode("utf-8")

    require_sig_env = os.getenv("REQUIRE_WEBHOOK_SIGNATURE")
    require_sig = (
        require_sig_env.lower() in ("true", "1", "yes")
        if require_sig_env is not None
        else REQUIRE_WEBHOOK_SIGNATURE
    )

    if require_sig:
        signature = request.headers.get("x-retell-signature")
        api_key = os.getenv("RETELL_API_KEY", RETELL_API_KEY)
        if not verify_retell_webhook_signature(raw_body_str, api_key, signature):
            logger.warning("Rejected webhook: invalid or missing x-retell-signature")
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Unauthorized: invalid webhook signature"},
            )

    try:
        payload = json.loads(raw_body_str) if raw_body_str else {}
    except Exception:
        payload = {}

    event = payload.get("event", "")
    call = payload.get("call") or {}
    logger.info("Retell webhook received: event=%s, call_id=%s", event, call.get("call_id"))

    call_id = call.get("call_id")
    metadata = call.get("metadata") or call.get("customer_metadata") or {}
    failed_payment_id = metadata.get("failed_payment_id") or call.get("failed_payment_id")

    if event == "call_started" and call_id and failed_payment_id:
        started_at = call.get("start_timestamp") or datetime.now(timezone.utc).isoformat()
        if isinstance(started_at, (int, float)):
            started_at = datetime.fromtimestamp(started_at / 1000, tz=timezone.utc).isoformat()

        with db:
            existing = db.execute("SELECT id FROM call_attempts WHERE retell_call_id = ?", (call_id,)).fetchone()
            if not existing:
                db.execute(
                    "INSERT INTO call_attempts (failed_payment_id, retell_call_id, started_at) VALUES (?, ?, ?)",
                    (failed_payment_id, call_id, str(started_at)),
                )

    elif event == "call_ended" and call_id:
        transcript = call.get("transcript") or ""
        ended_at = call.get("end_timestamp") or datetime.now(timezone.utc).isoformat()
        if isinstance(ended_at, (int, float)):
            ended_at = datetime.fromtimestamp(ended_at / 1000, tz=timezone.utc).isoformat()

        with db:
            existing = db.execute("SELECT id FROM call_attempts WHERE retell_call_id = ?", (call_id,)).fetchone()
            if existing:
                db.execute(
                    "UPDATE call_attempts SET transcript = ?, ended_at = ? WHERE retell_call_id = ?",
                    (transcript, str(ended_at), call_id),
                )
            elif failed_payment_id:
                started_at = call.get("start_timestamp") or ended_at
                if isinstance(started_at, (int, float)):
                    started_at = datetime.fromtimestamp(started_at / 1000, tz=timezone.utc).isoformat()
                db.execute(
                    "INSERT INTO call_attempts (failed_payment_id, retell_call_id, started_at, ended_at, transcript) VALUES (?, ?, ?, ?, ?)",
                    (failed_payment_id, call_id, str(started_at), str(ended_at), transcript),
                )

    return {"status": "ok"}


@app.get("/admin/state")
def get_admin_state(db: sqlite3.Connection = Depends(get_db)) -> list[dict[str, Any]]:
    """Return all failed payment records with current state and latest outcome."""
    rows = db.execute("""
        SELECT 
            p.id,
            p.customer_id,
            p.merchant_name,
            p.plan_name,
            p.amount_inr,
            p.failure_reason,
            p.failed_at,
            p.retry_count,
            p.status,
            p.next_retry_at,
            (SELECT ca.outcome FROM call_attempts ca WHERE ca.failed_payment_id = p.id ORDER BY ca.id DESC LIMIT 1) AS latest_outcome
        FROM failed_payments p
        ORDER BY p.id
    """).fetchall()
    return [dict(r) for r in rows]
