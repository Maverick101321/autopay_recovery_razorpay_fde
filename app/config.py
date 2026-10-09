import os
from pathlib import Path
from dotenv import load_dotenv

from app.utils import normalize_e164

BASE_DIR: Path = Path(__file__).resolve().parent.parent

# Load environment variables from .env file at repo root if present
load_dotenv(dotenv_path=BASE_DIR / ".env")

RETELL_API_KEY: str = os.getenv("RETELL_API_KEY", "")
DEMO_PHONE: str = os.getenv("DEMO_PHONE", "")
PUBLIC_BASE_URL: str = os.getenv("PUBLIC_BASE_URL", "")
DB_PATH: str = os.getenv("DB_PATH", "data/autopay.db") or "data/autopay.db"
TOOL_SHARED_SECRET: str = os.getenv("TOOL_SHARED_SECRET", "")
REQUIRE_WEBHOOK_SIGNATURE: bool = os.getenv("REQUIRE_WEBHOOK_SIGNATURE", "true").lower() in ("true", "1", "yes")

RETELL_VOICE_ID_EN: str = os.getenv("RETELL_VOICE_ID_EN", "")
RETELL_VOICE_ID_HI: str = os.getenv("RETELL_VOICE_ID_HI", "")
RETELL_FROM_NUMBER: str = os.getenv("RETELL_FROM_NUMBER", "")


def allowed_dial_numbers() -> set[str]:
    """Return allowlist containing only normalize_e164(DEMO_PHONE) if set, else empty set."""
    raw = os.getenv("DEMO_PHONE", DEMO_PHONE).strip()
    if not raw:
        return set()
    try:
        return {normalize_e164(raw)}
    except ValueError:
        return set()


def get_resolved_db_path(db_path: str | Path | None = None) -> str:
    """Resolve database path relative to project root if not absolute or :memory:."""
    target = str(db_path if db_path is not None else os.getenv("DB_PATH", DB_PATH) or "data/autopay.db")
    if target == ":memory:" or Path(target).is_absolute():
        return target
    return str(BASE_DIR / target)
