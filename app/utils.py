import hashlib
import hmac
import json
import logging
import re
import time
from typing import Any, Optional

logger = logging.getLogger("autopay.webhook_auth")


def normalize_e164(raw: str) -> str:
    """Normalize phone numbers to E.164 format without spaces or dashes.

    - Strips spaces and dashes.
    - If 10 digits, prefixes '+91'.
    - If 12 digits starting with '91', prefixes '+'.
    - If already '+91' followed by 10 digits, returns it.
    - Raises ValueError otherwise.
    """
    if not isinstance(raw, str):
        raise ValueError(f"Invalid phone number: {raw}")

    cleaned = re.sub(r"[\s\-]", "", raw.strip())

    if cleaned.startswith("+"):
        digits = cleaned[1:]
        if len(digits) == 12 and digits.startswith("91") and digits.isdigit():
            return cleaned
        raise ValueError(f"Invalid phone number: {raw}")

    if len(cleaned) == 10 and cleaned.isdigit():
        return f"+91{cleaned}"

    if len(cleaned) == 12 and cleaned.startswith("91") and cleaned.isdigit():
        return f"+{cleaned}"

    raise ValueError(f"Invalid phone number: {raw}")


def normalize_failed_payment_id(raw_id: Any) -> str:
    """Normalize failed_payment_id by stripping whitespace and lowercasing.

    If it consists only of digits, formats as 'pay_' followed by zero-padded 3 digits
    (e.g., '1' or '001' -> 'pay_001').
    """
    if raw_id is None:
        return ""
    cleaned = str(raw_id).strip().lower()
    if not cleaned:
        return ""
    if cleaned.isdigit():
        return f"pay_{int(cleaned):03d}"
    return cleaned


def compute_retell_webhook_signature(
    raw_body: str | bytes,
    api_key: str,
    timestamp_ms: Optional[int] = None,
    use_compact_json: bool = False,
) -> str:
    """Compute Retell webhook signature header value formatted as: v={timestamp},d={hex_digest}."""
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)

    body_str = raw_body.decode("utf-8") if isinstance(raw_body, bytes) else raw_body
    if use_compact_json:
        parsed = json.loads(body_str)
        body_str = json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)

    to_sign = f"{body_str}{timestamp_ms}".encode("utf-8")
    digest = hmac.new(api_key.encode("utf-8"), to_sign, hashlib.sha256).hexdigest()
    return f"v={timestamp_ms},d={digest}"


def verify_retell_webhook_signature(
    raw_body: str | bytes,
    api_key: str,
    signature_header: Optional[str],
    tolerance_ms: int = 5 * 60 * 1000,
    current_time_ms: Optional[int] = None,
) -> bool:
    """Verify Retell webhook request signature per Retell security specification.

    Header format: v={timestamp_ms},d={hex_digest}
    Signed content: raw_body + timestamp_ms or compact_json + timestamp_ms
    Algorithm: HMAC-SHA256 with api_key as secret.
    Matches against EITHER raw request body or compact JSON.
    Logs one WARNING on failure with diagnostic fields (no secrets or digests).
    """
    body_str = raw_body.decode("utf-8") if isinstance(raw_body, bytes) else (raw_body or "")
    body_len = len(body_str)
    header_present = bool(signature_header and signature_header.strip())
    parsed_timestamp: Optional[int] = None
    skew_ms: Optional[int] = None
    variants_tried: list[str] = []

    def log_failure() -> bool:
        logger.warning(
            "Webhook signature verification failed: header_present=%s, parsed_timestamp=%s, skew_ms=%s, body_length=%d, variants_tried=%s",
            header_present,
            parsed_timestamp,
            skew_ms,
            body_len,
            variants_tried,
        )
        return False

    if not header_present or not api_key:
        return log_failure()

    v_str: Optional[str] = None
    d_str: Optional[str] = None

    for part in signature_header.strip().split(","):
        part = part.strip()
        if part.startswith("v="):
            v_str = part[2:].strip()
        elif part.startswith("d="):
            d_str = part[2:].strip()

    if not v_str or not d_str:
        return log_failure()

    try:
        parsed_timestamp = int(v_str)
    except ValueError:
        return log_failure()

    now_ms = current_time_ms if current_time_ms is not None else int(time.time() * 1000)
    skew_ms = abs(now_ms - parsed_timestamp)

    if skew_ms > tolerance_ms:
        return log_failure()

    # Variant 1: raw body bytes/string
    variants_tried.append("raw_body")
    to_sign_raw = f"{body_str}{v_str}".encode("utf-8")
    expected_digest_raw = hmac.new(api_key.encode("utf-8"), to_sign_raw, hashlib.sha256).hexdigest()
    if hmac.compare_digest(expected_digest_raw, d_str):
        return True

    # Variant 2: compact JSON
    try:
        parsed_json = json.loads(body_str)
        compact_json_str = json.dumps(parsed_json, separators=(",", ":"), ensure_ascii=False)
        variants_tried.append("compact_json")
        to_sign_compact = f"{compact_json_str}{v_str}".encode("utf-8")
        expected_digest_compact = hmac.new(api_key.encode("utf-8"), to_sign_compact, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected_digest_compact, d_str):
            return True
    except Exception:
        pass

    return log_failure()


def number_to_words(n: int) -> str:
    """Convert an integer to spoken English words for numbers up to 999,999."""
    if n == 0:
        return "zero"
    ones = [
        "", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
        "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
        "seventeen", "eighteen", "nineteen",
    ]
    tens = [
        "", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety",
    ]

    parts: list[str] = []
    if n >= 1000:
        thousands = n // 1000
        parts.append(f"{number_to_words(thousands)} thousand")
        n %= 1000
    if n >= 100:
        hundreds = n // 100
        parts.append(f"{ones[hundreds]} hundred")
        n %= 100
    if n >= 20:
        ten_part = tens[n // 10]
        one_part = ones[n % 10]
        parts.append(f"{ten_part} {one_part}".strip())
    elif n > 0:
        parts.append(ones[n])
    return " ".join(parts).strip()


def format_spoken_amount(amount_inr: int) -> str:
    """Format an amount in INR into a spoken-friendly string (e.g. 'seven hundred ninety nine rupees')."""
    words = number_to_words(int(amount_inr))
    return f"{words} rupees"
