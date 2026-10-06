import hashlib
import hmac
import re
import time
from typing import Optional


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


def compute_retell_webhook_signature(
    raw_body: str | bytes,
    api_key: str,
    timestamp_ms: Optional[int] = None,
) -> str:
    """Compute Retell webhook signature header value formatted as: v={timestamp},d={hex_digest}."""
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)

    body_str = raw_body.decode("utf-8") if isinstance(raw_body, bytes) else raw_body
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
    Signed content: raw_body + timestamp_ms
    Algorithm: HMAC-SHA256 with api_key as secret.
    """
    if not signature_header or not api_key:
        return False

    v_str: Optional[str] = None
    d_str: Optional[str] = None

    for part in signature_header.strip().split(","):
        part = part.strip()
        if part.startswith("v="):
            v_str = part[2:]
        elif part.startswith("d="):
            d_str = part[2:]

    if not v_str or not d_str:
        return False

    try:
        timestamp_ms = int(v_str)
    except ValueError:
        return False

    now_ms = current_time_ms if current_time_ms is not None else int(time.time() * 1000)
    if abs(now_ms - timestamp_ms) > tolerance_ms:
        return False

    body_str = raw_body.decode("utf-8") if isinstance(raw_body, bytes) else raw_body
    to_sign = f"{body_str}{v_str}".encode("utf-8")
    expected_digest = hmac.new(api_key.encode("utf-8"), to_sign, hashlib.sha256).hexdigest()

    return hmac.compare_digest(expected_digest, d_str)
