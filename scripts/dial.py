#!/usr/bin/env python3
"""Dialing script for autopay-recovery-agent.

Evaluates dialability across accounts and executes or previews phone calls via Retell AI.
Supports --plan (default), --payment-id with dry-run, and --live call placement.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

from app.config import (
    DEMO_PHONE,
    RETELL_API_KEY,
    RETELL_FROM_NUMBER,
    get_resolved_db_path,
)
from app.db import get_db_connection
from app.rules import can_call, is_dialable
from app.utils import format_spoken_amount, normalize_e164, normalize_failed_payment_id

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("autopay.dial")


def load_retell_ids(ids_path: Optional[Path] = None) -> dict[str, str]:
    """Load agent and LLM IDs from retell_ids.json."""
    path = ids_path or (Path(__file__).resolve().parent.parent / "data" / "retell_ids.json")
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Could not read retell_ids.json at %s: %s", path, e)
        return {}


def print_plan_table(conn, now: Optional[datetime] = None) -> None:
    """Print an evaluation table of all 10 payments evaluating can_call in customer timezone."""
    now_utc = now or datetime.now(timezone.utc)
    payments = conn.execute("SELECT * FROM failed_payments ORDER BY id").fetchall()

    header = (
        f"{'Payment ID':<10} | {'Customer':<16} | {'Phone':<14} | {'Merchant':<18} | "
        f"{'Amount':<8} | {'Lang':<5} | {'Can Call?':<10} | {'Reason'}"
    )
    divider = "-" * len(header)
    print(divider)
    print(header)
    print(divider)

    dialable_count = 0
    blocked_count = 0
    for p in payments:
        cust = conn.execute("SELECT * FROM customers WHERE id = ?", (p["customer_id"],)).fetchone()
        if not cust:
            continue
        can, reason = can_call(cust, p, now=now_utc)
        if can:
            dialable_count += 1
            can_str = "YES"
        else:
            blocked_count += 1
            can_str = "NO"
        amt_str = f"INR {p['amount_inr']}"
        print(
            f"{p['id']:<10} | {cust['name']:<16} | {cust['phone']:<14} | {p['merchant_name']:<18} | "
            f"{amt_str:<8} | {cust['language']:<5} | {can_str:<10} | {reason}"
        )

    print(divider)
    print(
        f"Summary: {len(payments)} payments evaluated at {now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')} | "
        f"Dialable: {dialable_count} | Blocked: {blocked_count}"
    )


def build_call_payload(
    payment: Any,
    customer: Any,
    from_number: str,
    agent_id: str,
) -> dict[str, Any]:
    """Build exact Retell create-phone-call request payload with spoken amount and dynamic variables."""
    to_number = normalize_e164(customer["phone"])
    first_name = customer["name"].split()[0]

    if customer["language"] == "hi":
        opening_line = (
            f"Namaste, main {payment['merchant_name']} ki taraf se Riya bol rahi hoon, ek automated AI assistant. "
            f"Kya meri baat {first_name} ji se ho rahi hai?"
        )
    else:
        opening_line = (
            f"Hello, this is Riya, an automated AI assistant calling on behalf of {payment['merchant_name']}. "
            f"Am I speaking with {first_name}?"
        )

    spoken_amount = format_spoken_amount(payment["amount_inr"])

    return {
        "from_number": from_number,
        "to_number": to_number,
        "override_agent_id": agent_id,
        "retell_llm_dynamic_variables": {
            "customer_first_name": first_name,
            "merchant_name": payment["merchant_name"],
            "plan_name": payment["plan_name"],
            "amount_inr": str(payment["amount_inr"]),
            "amount_spoken": spoken_amount,
            "failed_payment_id": payment["id"],
            "language": customer["language"],
            "opening_line": opening_line,
        },
        "metadata": {
            "failed_payment_id": payment["id"],
        },
    }


def check_cooldown(conn, payment_id: str, cooldown_seconds: int = 600) -> Optional[str]:
    """Check if the payment had a call attempt started within the last cooldown_seconds (default 10 mins).

    Returns error string if in cooldown, None otherwise.
    """
    latest_attempt = conn.execute(
        "SELECT started_at FROM call_attempts WHERE failed_payment_id = ? ORDER BY id DESC LIMIT 1",
        (payment_id,),
    ).fetchone()

    if latest_attempt and latest_attempt["started_at"]:
        try:
            started_raw = latest_attempt["started_at"]
            if isinstance(started_raw, (int, float)):
                started_dt = datetime.fromtimestamp(started_raw / 1000.0, tz=timezone.utc)
            else:
                started_dt = datetime.fromisoformat(started_raw)
            if started_dt.tzinfo is None:
                started_dt = started_dt.replace(tzinfo=timezone.utc)
            now_utc = datetime.now(timezone.utc)
            elapsed = (now_utc - started_dt).total_seconds()
            if 0 <= elapsed < cooldown_seconds:
                remaining = int(cooldown_seconds - elapsed)
                return (
                    f"Cooldown active: a call attempt for {payment_id} was started {int(elapsed)}s ago. "
                    f"Please wait {remaining}s before retrying."
                )
        except Exception as e:
            logger.warning("Could not parse started_at '%s' for cooldown check: %s", latest_attempt["started_at"], e)

    return None


def run_dial(
    payment_id_arg: Optional[str] = None,
    plan_mode: bool = False,
    live_mode: bool = False,
    limit: int = 1,
    db_path: Optional[str] = None,
    ids_path: Optional[Path] = None,
    mock_input: Optional[str] = None,
    now: Optional[datetime] = None,
) -> int:
    """Core dialing routine returning process exit code."""
    if limit > 3:
        print(f"Error: --limit cannot exceed 3 (hard limit). Specified limit: {limit}", file=sys.stderr)
        return 1

    conn = get_db_connection(db_path)
    try:
        # Default or explicit plan mode
        if plan_mode or not payment_id_arg:
            print_plan_table(conn, now=now)
            return 0

        # Payment ID specified
        failed_payment_id = normalize_failed_payment_id(payment_id_arg)
        payment = conn.execute("SELECT * FROM failed_payments WHERE id = ?", (failed_payment_id,)).fetchone()
        if not payment:
            print(f"Error: Payment {failed_payment_id} not found in database.", file=sys.stderr)
            return 1

        customer = conn.execute("SELECT * FROM customers WHERE id = ?", (payment["customer_id"],)).fetchone()
        if not customer:
            print(f"Error: Customer {payment['customer_id']} not found in database.", file=sys.stderr)
            return 1

        try:
            to_number = normalize_e164(customer["phone"])
        except ValueError as e:
            print(f"Safety refusal: Invalid customer phone number: {e}", file=sys.stderr)
            return 1

        # Safety Check 1: Must equal DEMO_PHONE
        demo_phone_raw = os.getenv("DEMO_PHONE", DEMO_PHONE)
        if not demo_phone_raw:
            print("Safety refusal: DEMO_PHONE environment variable is not configured.", file=sys.stderr)
            return 1

        try:
            allowed_demo = normalize_e164(demo_phone_raw)
        except ValueError:
            print(f"Safety refusal: DEMO_PHONE '{demo_phone_raw}' is not a valid E.164 number.", file=sys.stderr)
            return 1

        if to_number != allowed_demo:
            print(
                f"Safety refusal: Destination phone {to_number} does not match DEMO_PHONE {allowed_demo}. "
                "Calls to unverified numbers are strictly blocked.",
                file=sys.stderr,
            )
            return 1

        # Safety Check 2: is_dialable & can_call
        if not is_dialable(to_number):
            print(f"Safety refusal: Phone {to_number} is not in the dial allowlist.", file=sys.stderr)
            return 1

        now_utc = datetime.now(timezone.utc)
        allowed, reason_code = can_call(customer, payment, now=now_utc)
        if not allowed:
            print(f"Safety refusal: can_call check blocked dialing with reason: '{reason_code}'.", file=sys.stderr)
            return 1

        # Safety Check 3: Cooldown (10 minutes)
        cooldown_err = check_cooldown(conn, failed_payment_id, cooldown_seconds=600)
        if cooldown_err:
            print(f"Safety refusal: {cooldown_err}", file=sys.stderr)
            return 1

        # Agent lookup by language
        retell_ids = load_retell_ids(ids_path)
        lang = customer["language"]
        agent_key = "agent_id_hi" if lang == "hi" else "agent_id_en"
        agent_id = retell_ids.get(agent_key)
        if not agent_id:
            print(
                f"Error: Missing {agent_key} in retell_ids.json. Please run scripts/setup_retell.py first.",
                file=sys.stderr,
            )
            return 1

        from_number = os.getenv("RETELL_FROM_NUMBER") or RETELL_FROM_NUMBER
        if not from_number:
            print(
                "Error: RETELL_FROM_NUMBER is not set. Please set RETELL_FROM_NUMBER in your environment or .env file.",
                file=sys.stderr,
            )
            return 1

        payload = build_call_payload(
            payment=payment,
            customer=customer,
            from_number=from_number,
            agent_id=agent_id,
        )

        # Dry-run mode: print exact JSON payload and exit
        if not live_mode:
            print(json.dumps(payload, indent=2))
            return 0

        # Live mode: interactive confirmation prompt
        print("================ LIVE CALL SAFETY CONFIRMATION ================")
        print(f"Customer:           {customer['name']} ({customer['id']})")
        print(f"To Number:          {to_number}")
        print(f"Language:           {customer['language'].upper()}")
        print(f"Agent ID:           {agent_id}")
        print(f"Merchant & Plan:    {payment['merchant_name']} - {payment['plan_name']} (INR {payment['amount_inr']})")
        print("Estimated Max Cost: ~$0.30 USD (or ~INR 25)")
        print("===============================================================")

        if mock_input is not None:
            confirmation = mock_input.strip()
        else:
            try:
                confirmation = input("Type YES to place this live phone call: ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nCall aborted by user.", file=sys.stderr)
                return 1

        if confirmation != "YES":
            print(f"Safety refusal: Call aborted. Expected 'YES' but got '{confirmation}'.", file=sys.stderr)
            return 1

        # Call Retell API
        api_key = os.getenv("RETELL_API_KEY", RETELL_API_KEY)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                "https://api.retellai.com/v2/create-phone-call",
                headers=headers,
                json=payload,
            )
            if resp.status_code not in (200, 201):
                print(
                    f"Retell call creation failed with status {resp.status_code}:\n{resp.text}",
                    file=sys.stderr,
                )
                return 1

            resp_data = resp.json()
            call_id = resp_data.get("call_id")
            print(f"Call successfully placed! Retell Call ID: {call_id}")

            with conn:
                conn.execute(
                    "INSERT INTO call_attempts (failed_payment_id, retell_call_id, started_at) VALUES (?, ?, ?)",
                    (payment["id"], call_id, datetime.now(timezone.utc).isoformat()),
                )

        return 0
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Autopay Recovery Agent Dialing CLI")
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Print a table of all 10 payments evaluating dialability (default mode).",
    )
    parser.add_argument(
        "--payment-id",
        type=str,
        default=None,
        help="Specify the failed payment ID to evaluate, dry-run, or call.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Execute live phone call via Retell AI API (requires interactive YES confirmation).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1,
        help="Maximum calls to place per run (default: 1, hard max: 3).",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="Custom SQLite database file path.",
    )
    parser.add_argument(
        "--ids-path",
        type=str,
        default=None,
        help="Custom path to retell_ids.json.",
    )

    args = parser.parse_args()
    ids_path_obj = Path(args.ids_path) if args.ids_path else None

    exit_code = run_dial(
        payment_id_arg=args.payment_id,
        plan_mode=args.plan,
        live_mode=args.live,
        limit=args.limit,
        db_path=args.db_path,
        ids_path=ids_path_obj,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
