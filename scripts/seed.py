import argparse
import os
import random
import sys
from pathlib import Path
from typing import Any, Optional

# Ensure project root is in sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.config import DEMO_PHONE
from app.db import get_db_connection, init_db
from app.models import Customer, FailedPayment, FailureReason, Language, PaymentStatus


def generate_seed_data(demo_phone_override: Optional[str] = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Generate deterministic fictional customers and failed autopay payment records."""
    random.seed(42)

    demo_phone = (
        demo_phone_override
        if demo_phone_override is not None
        else os.getenv("DEMO_PHONE", DEMO_PHONE)
    ).strip()

    customers_raw = [
        {
            "id": "cust_001",
            "name": "Aarav Sharma",
            "phone": demo_phone or "+91 98765 00001",
            "language": Language.EN,
            "consent_to_call": 1,
            "call_window_start": "09:00",
            "call_window_end": "18:00",
            "timezone": "Asia/Kolkata",
        },
        {
            "id": "cust_002",
            "name": "Priya Patel",
            "phone": demo_phone or "+91 98765 00002",
            "language": Language.HI,
            "consent_to_call": 1,
            "call_window_start": "10:00",
            "call_window_end": "19:00",
            "timezone": "Asia/Kolkata",
        },
        {
            "id": "cust_003",
            "name": "Rohan Verma",
            "phone": "+91 98765 00003",
            "language": Language.EN,
            "consent_to_call": 0,  # Edge case: consent_to_call = 0 (skipped)
            "call_window_start": "09:00",
            "call_window_end": "18:00",
            "timezone": "Asia/Kolkata",
        },
        {
            "id": "cust_004",
            "name": "Ananya Iyer",
            "phone": "+91 98765 00004",
            "language": Language.EN,
            "consent_to_call": 1,
            "call_window_start": "10:00",  # Edge case: narrow window
            "call_window_end": "11:00",
            "timezone": "Asia/Kolkata",
        },
        {
            "id": "cust_005",
            "name": "Vikram Singh",
            "phone": "+91 98765 00005",
            "language": Language.HI,
            "consent_to_call": 1,
            "call_window_start": "09:30",
            "call_window_end": "18:30",
            "timezone": "Asia/Kolkata",
        },
        {
            "id": "cust_006",
            "name": "Sneha Reddy",
            "phone": "+91 98765 00006",
            "language": Language.EN,
            "consent_to_call": 1,
            "call_window_start": "09:00",
            "call_window_end": "17:00",
            "timezone": "Asia/Kolkata",
        },
        {
            "id": "cust_007",
            "name": "Arjun Mehta",
            "phone": "+91 98765 00007",
            "language": Language.HI,
            "consent_to_call": 1,
            "call_window_start": "11:00",
            "call_window_end": "20:00",
            "timezone": "Asia/Kolkata",
        },
        {
            "id": "cust_008",
            "name": "Pooja Nair",
            "phone": "+91 98765 00008",
            "language": Language.EN,
            "consent_to_call": 1,
            "call_window_start": "09:00",
            "call_window_end": "18:00",
            "timezone": "Asia/Kolkata",
        },
        {
            "id": "cust_009",
            "name": "Rajesh Gupta",
            "phone": "+91 98765 00009",
            "language": Language.HI,
            "consent_to_call": 1,
            "call_window_start": "10:00",
            "call_window_end": "19:00",
            "timezone": "Asia/Kolkata",
        },
        {
            "id": "cust_010",
            "name": "Kavita Joshi",
            "phone": "+91 98765 00010",
            "language": Language.EN,
            "consent_to_call": 1,
            "call_window_start": "09:00",
            "call_window_end": "18:00",
            "timezone": "Asia/Kolkata",
        },
    ]

    payments_raw = [
        {
            "id": "pay_001",
            "customer_id": "cust_001",
            "merchant_name": "StreamFlix",
            "plan_name": "Premium Ultra HD",
            "amount_inr": 799,
            "failure_reason": FailureReason.INSUFFICIENT_FUNDS,
            "failed_at": "2026-10-05T08:15:00Z",
            "retry_count": 0,
            "status": PaymentStatus.PENDING,
        },
        {
            "id": "pay_002",
            "customer_id": "cust_002",
            "merchant_name": "FitPulse Gym",
            "plan_name": "Monthly All-Access",
            "amount_inr": 2499,
            "failure_reason": FailureReason.CARD_EXPIRED,
            "failed_at": "2026-10-05T08:45:00Z",
            "retry_count": 0,
            "status": PaymentStatus.PENDING,
        },
        {
            "id": "pay_003",
            "customer_id": "cust_003",
            "merchant_name": "CloudMetrics SaaS",
            "plan_name": "Team Starter",
            "amount_inr": 1999,
            "failure_reason": FailureReason.MANDATE_EXPIRED,
            "failed_at": "2026-10-05T09:00:00Z",
            "retry_count": 0,
            "status": PaymentStatus.PENDING,
        },
        {
            "id": "pay_004",
            "customer_id": "cust_004",
            "merchant_name": "CareSecure Insurance",
            "plan_name": "Health Guard Monthly",
            "amount_inr": 3499,
            "failure_reason": FailureReason.BANK_DECLINED,
            "failed_at": "2026-10-05T09:30:00Z",
            "retry_count": 0,
            "status": PaymentStatus.PENDING,
        },
        {
            "id": "pay_005",
            "customer_id": "cust_005",
            "merchant_name": "TechFin Newsletter",
            "plan_name": "Annual Supporter",
            "amount_inr": 999,
            "failure_reason": FailureReason.OTP_NOT_COMPLETED,
            "failed_at": "2026-10-05T10:00:00Z",
            "retry_count": 2,  # Edge case: retry_count = 2 (near retry cap)
            "status": PaymentStatus.PENDING,
        },
        {
            "id": "pay_006",
            "customer_id": "cust_006",
            "merchant_name": "SkillCraft Learning",
            "plan_name": "Pro Monthly Membership",
            "amount_inr": 1499,
            "failure_reason": FailureReason.DAILY_LIMIT_EXCEEDED,
            "failed_at": "2026-10-05T10:30:00Z",
            "retry_count": 0,
            "status": PaymentStatus.PENDING,
        },
        {
            "id": "pay_007",
            "customer_id": "cust_007",
            "merchant_name": "BeatStream Music",
            "plan_name": "Family Duo",
            "amount_inr": 299,
            "failure_reason": FailureReason.INSUFFICIENT_FUNDS,
            "failed_at": "2026-10-05T11:00:00Z",
            "retry_count": 0,
            "status": PaymentStatus.PENDING,
        },
        {
            "id": "pay_008",
            "customer_id": "cust_008",
            "merchant_name": "WorkNest Coworking",
            "plan_name": "Flexi Desk Bundle",
            "amount_inr": 4999,
            "failure_reason": FailureReason.BANK_DECLINED,
            "failed_at": "2026-10-05T11:30:00Z",
            "retry_count": 0,
            "status": PaymentStatus.PENDING,
        },
        {
            "id": "pay_009",
            "customer_id": "cust_009",
            "merchant_name": "QuickBite Gourmet",
            "plan_name": "Gold Dining Pass",
            "amount_inr": 1299,
            "failure_reason": FailureReason.CARD_EXPIRED,
            "failed_at": "2026-10-05T12:00:00Z",
            "retry_count": 0,
            "status": PaymentStatus.PENDING,
        },
        {
            "id": "pay_010",
            "customer_id": "cust_010",
            "merchant_name": "MorningBrief Daily",
            "plan_name": "Digital Reader",
            "amount_inr": 199,
            "failure_reason": FailureReason.INSUFFICIENT_FUNDS,
            "failed_at": "2026-10-05T12:30:00Z",
            "retry_count": 0,
            "status": PaymentStatus.PENDING,
        },
    ]

    # Validate all raw entries against Pydantic models before inserting
    customers = [Customer(**c).model_dump() for c in customers_raw]
    payments = [FailedPayment(**p).model_dump() for p in payments_raw]

    return customers, payments


def seed_database(
    db_path: Optional[str | Path] = None,
    reset: bool = True,
    demo_phone_override: Optional[str] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Seed the SQLite database with 10 customers and failed payment records."""
    init_db(db_path=db_path, reset=reset)

    customers, payments = generate_seed_data(demo_phone_override=demo_phone_override)

    conn = get_db_connection(db_path)
    try:
        with conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO customers (
                    id, name, phone, language, consent_to_call,
                    call_window_start, call_window_end, timezone
                ) VALUES (
                    :id, :name, :phone, :language, :consent_to_call,
                    :call_window_start, :call_window_end, :timezone
                )
                """,
                customers,
            )
            conn.executemany(
                """
                INSERT OR REPLACE INTO failed_payments (
                    id, customer_id, merchant_name, plan_name,
                    amount_inr, failure_reason, failed_at, retry_count, status
                ) VALUES (
                    :id, :customer_id, :merchant_name, :plan_name,
                    :amount_inr, :failure_reason, :failed_at, :retry_count, :status
                )
                """,
                payments,
            )
    finally:
        conn.close()

    return customers, payments


def print_seeded_table(customers: list[dict[str, Any]], payments: list[dict[str, Any]]) -> None:
    """Print a clean formatted table summarizing seeded customers and failed payments."""
    payments_by_cust = {p["customer_id"]: p for p in payments}

    headers = [
        ("Cust ID", 8),
        ("Customer Name", 14),
        ("Phone Number", 17),
        ("Lang", 4),
        ("Consent", 7),
        ("Call Window", 13),
        ("Merchant", 20),
        ("Plan", 22),
        ("Amount", 8),
        ("Failure Reason", 21),
        ("Retries", 7),
    ]

    separator = "+" + "+".join("-" * (w + 2) for _, w in headers) + "+"
    header_row = (
        "| "
        + " | ".join(f"{name:<{w}}" for name, w in headers)
        + " |"
    )

    print("\n" + "=" * len(separator))
    print("SEEDED AUTOPAY RECOVERY RECORDS (10 FICTIONAL CUSTOMERS)".center(len(separator)))
    print("=" * len(separator))
    print(separator)
    print(header_row)
    print(separator)

    for c in customers:
        p = payments_by_cust.get(c["id"], {})
        amount_str = f"Rs {p.get('amount_inr', 0)}"
        consent_str = "Yes (1)" if c["consent_to_call"] == 1 else "No (0)"
        window_str = f"{c['call_window_start']}-{c['call_window_end']}"

        row_fields = [
            f"{c['id']:<8}",
            f"{c['name']:<14}",
            f"{c['phone']:<17}",
            f"{c['language']:<4}",
            f"{consent_str:<7}",
            f"{window_str:<13}",
            f"{p.get('merchant_name', ''):<20}",
            f"{p.get('plan_name', ''):<22}",
            f"{amount_str:<8}",
            f"{p.get('failure_reason', ''):<21}",
            f"{p.get('retry_count', 0):<7}",
        ]
        print("| " + " | ".join(row_fields) + " |")

    print(separator)
    print(f"Total seeded: {len(customers)} customers, {len(payments)} failed payment records.\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed database with fictional autopay recovery data.")
    parser.add_argument(
        "--reset",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Reset (drop and recreate) tables before seeding (default: True)",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="Custom SQLite database path (default: data/autopay.db)",
    )
    args = parser.parse_args()

    customers, payments = seed_database(db_path=args.db_path, reset=args.reset)
    print_seeded_table(customers, payments)


if __name__ == "__main__":
    main()
