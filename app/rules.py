from datetime import datetime, time, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app.config import allowed_dial_numbers
from app.utils import normalize_e164

MAX_RETRIES: int = 3
TERMINAL_STATUSES: set[str] = {"recovered", "escalated", "do_not_call", "failed"}


def _get_val(obj: Any, key: str, default: Any = None) -> Any:
    """Helper to access dict keys or object attributes seamlessly."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def resolve_timezone(tz: Optional[str | ZoneInfo] = None, fallback: str = "Asia/Kolkata") -> ZoneInfo:
    """Resolve a timezone string or ZoneInfo object into a valid ZoneInfo."""
    if isinstance(tz, ZoneInfo):
        return tz
    if isinstance(tz, str) and tz.strip():
        return ZoneInfo(tz.strip())
    return ZoneInfo(fallback)


def is_dialable(phone: str) -> bool:
    """Check if a phone number is present in the dial allowlist."""
    try:
        normalized = normalize_e164(phone)
    except ValueError:
        return False
    return normalized in allowed_dial_numbers()


def can_call(
    customer: Any,
    payment: Any,
    now: datetime,
    tz: Optional[str | ZoneInfo] = None,
) -> tuple[bool, str]:
    """Evaluate business rules in priority order to decide if customer can be called.

    Checks:
    1. Payment status is terminal (recovered, escalated, do_not_call, failed) -> block "terminal_status"
    2. consent_to_call == 0 -> block "no_consent"
    3. retry_count >= MAX_RETRIES -> block "retry_cap"
    4. now is outside call window in customer's timezone (start <= t < end, end exclusive) -> block "outside_window"
    5. not is_dialable(phone) -> block "not_allowlisted"
    6. All checks pass -> return (True, "ok")
    """
    # 1. Check terminal payment status
    status = str(_get_val(payment, "status", "pending"))
    if status in TERMINAL_STATUSES:
        return False, "terminal_status"

    # 2. Check call consent
    consent = int(_get_val(customer, "consent_to_call", 1))
    if consent == 0:
        return False, "no_consent"

    # 3. Check retry cap
    retry_count = int(_get_val(payment, "retry_count", 0))
    if retry_count >= MAX_RETRIES:
        return False, "retry_cap"

    # 4. Check call window in customer's timezone (start <= t < end)
    cust_tz = resolve_timezone(tz or _get_val(customer, "timezone", "Asia/Kolkata"))
    local_now = now.replace(tzinfo=cust_tz) if now.tzinfo is None else now.astimezone(cust_tz)

    start_raw = _get_val(customer, "call_window_start", "09:00")
    end_raw = _get_val(customer, "call_window_end", "18:00")
    start_h, start_m = [int(p) for p in start_raw.split(":")]
    end_h, end_m = [int(p) for p in end_raw.split(":")]
    start_time = time(start_h, start_m)
    end_time = time(end_h, end_m)
    current_time = local_now.time()

    if not (start_time <= current_time < end_time):
        return False, "outside_window"

    # 5. Check dial allowlist
    phone = str(_get_val(customer, "phone", ""))
    if not is_dialable(phone):
        return False, "not_allowlisted"

    # 6. Eligible for calling
    return True, "ok"


def next_retry_time(now: datetime, customer: Any) -> datetime:
    """Calculate the next retry datetime inside the customer's call window, at least 4 hours after now."""
    cust_tz = resolve_timezone(_get_val(customer, "timezone", "Asia/Kolkata"))
    local_now = now.replace(tzinfo=cust_tz) if now.tzinfo is None else now.astimezone(cust_tz)

    earliest = local_now + timedelta(hours=4)

    start_raw = _get_val(customer, "call_window_start", "09:00")
    end_raw = _get_val(customer, "call_window_end", "18:00")
    start_h, start_m = [int(p) for p in start_raw.split(":")]
    end_h, end_m = [int(p) for p in end_raw.split(":")]
    start_t = time(start_h, start_m)
    end_t = time(end_h, end_m)

    window_start_dt = datetime.combine(earliest.date(), start_t, tzinfo=cust_tz)
    window_end_dt = datetime.combine(earliest.date(), end_t, tzinfo=cust_tz)

    if earliest < window_start_dt:
        return window_start_dt
    elif earliest < window_end_dt:
        return earliest
    else:
        next_day = earliest.date() + timedelta(days=1)
        return datetime.combine(next_day, start_t, tzinfo=cust_tz)
