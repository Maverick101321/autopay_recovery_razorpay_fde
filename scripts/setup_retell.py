import argparse
import copy
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

import httpx

# Ensure project root is in sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.config import (
    BASE_DIR as CONFIG_BASE_DIR,
    PUBLIC_BASE_URL,
    RETELL_API_KEY,
    RETELL_VOICE_ID_EN,
    RETELL_VOICE_ID_HI,
    TOOL_SHARED_SECRET,
)

RETELL_API_BASE = "https://api.retellai.com"
ID_FILE_PATH = CONFIG_BASE_DIR / "data" / "retell_ids.json"
PROMPT_FILE_PATH = CONFIG_BASE_DIR / "prompts" / "agent_prompt.md"

# Model selection:
# 'gpt-4.1-mini' is the latest cost-optimized, low-latency (~550ms) model officially recommended by Retell
# to replace the deprecated 'gpt-4o-mini', providing high prompt compliance at lowest cost.
DEFAULT_MODEL = "gpt-4.1-mini"
DEFAULT_VOICE_EN = "11labs-Adrian"
DEFAULT_VOICE_HI = "11labs-Aditi"


def load_agent_prompt() -> str:
    """Read system prompt template from prompts/agent_prompt.md."""
    if not PROMPT_FILE_PATH.exists():
        raise FileNotFoundError(f"Agent prompt file not found at: {PROMPT_FILE_PATH}")
    return PROMPT_FILE_PATH.read_text(encoding="utf-8").strip()


def build_tools(public_base_url: str, tool_secret: str) -> list[dict[str, Any]]:
    """Construct general tools array containing 5 custom tools and built-in end_call."""
    base_url = public_base_url.rstrip("/")

    headers = {}
    if tool_secret:
        headers["x-tool-secret"] = tool_secret

    return [
        {
            "type": "custom",
            "name": "get_payment_context",
            "description": "Retrieve details of the customer's failed autopay payment including amount, plan, failure reason, and retry count.",
            "url": f"{base_url}/tools/get_payment_context",
            "method": "POST",
            "headers": headers,
            "parameters": {
                "type": "object",
                "properties": {
                    "failed_payment_id": {
                        "type": "string",
                        "description": "The unique identifier of the failed payment record.",
                    }
                },
                "required": ["failed_payment_id"],
            },
        },
        {
            "type": "custom",
            "name": "create_payment_link",
            "description": "Generate a secure payment recovery link and simulate sending it to customer's registered phone number via SMS.",
            "url": f"{base_url}/tools/create_payment_link",
            "method": "POST",
            "headers": headers,
            "parameters": {
                "type": "object",
                "properties": {
                    "failed_payment_id": {
                        "type": "string",
                        "description": "The unique identifier of the failed payment record.",
                    }
                },
                "required": ["failed_payment_id"],
            },
        },
        {
            "type": "custom",
            "name": "log_outcome",
            "description": "Log the call outcome and update payment status before ending the call.",
            "url": f"{base_url}/tools/log_outcome",
            "method": "POST",
            "headers": headers,
            "parameters": {
                "type": "object",
                "properties": {
                    "failed_payment_id": {
                        "type": "string",
                        "description": "The unique identifier of the failed payment record.",
                    },
                    "outcome": {
                        "type": "string",
                        "enum": [
                            "promised_to_pay",
                            "paid_now",
                            "claims_paid",
                            "refused",
                            "wrong_person",
                            "call_back_later",
                            "do_not_call",
                            "dispute",
                        ],
                        "description": "The outcome category of the call.",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Optional notes or summary of the conversation.",
                    },
                },
                "required": ["failed_payment_id", "outcome"],
            },
        },
        {
            "type": "custom",
            "name": "schedule_retry",
            "description": "Schedule a callback time within the customer's calling window.",
            "url": f"{base_url}/tools/schedule_retry",
            "method": "POST",
            "headers": headers,
            "parameters": {
                "type": "object",
                "properties": {
                    "failed_payment_id": {
                        "type": "string",
                        "description": "The unique identifier of the failed payment record.",
                    },
                    "preferred_time": {
                        "type": "string",
                        "description": "Optional customer preferred callback time or time of day.",
                    },
                },
                "required": ["failed_payment_id"],
            },
        },
        {
            "type": "custom",
            "name": "escalate_to_human",
            "description": "Escalate the call to human support when customer is angry, disputes the charge, or insists on speaking with a person.",
            "url": f"{base_url}/tools/escalate_to_human",
            "method": "POST",
            "headers": headers,
            "parameters": {
                "type": "object",
                "properties": {
                    "failed_payment_id": {
                        "type": "string",
                        "description": "The unique identifier of the failed payment record.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Reason for escalation.",
                    },
                },
                "required": ["failed_payment_id", "reason"],
            },
        },
        {
            "type": "end_call",
            "name": "end_call",
            "description": "End the call with user when the conversation is concluded.",
        },
    ]


def build_llm_payload(
    public_base_url: str,
    tool_secret: str,
    model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    """Build Retell LLM creation/update payload."""
    prompt = load_agent_prompt()
    tools = build_tools(public_base_url, tool_secret)

    return {
        "model": model,
        "start_speaker": "agent",
        "begin_message": "{{opening_line}}",
        "general_prompt": prompt,
        "general_tools": tools,
    }


def build_agent_payload(
    llm_id: str,
    agent_name: str,
    language: str,
    voice_id: str,
    public_base_url: str,
) -> dict[str, Any]:
    """Build Retell Agent payload referencing the shared LLM."""
    base_url = public_base_url.rstrip("/")
    return {
        "agent_name": agent_name,
        "language": language,
        "voice_id": voice_id,
        "response_engine": {
            "type": "retell-llm",
            "llm_id": llm_id,
        },
        "webhook_url": f"{base_url}/webhooks/retell",
        "webhook_events": ["call_started", "call_ended", "call_analyzed"],
    }


def check_response(resp: httpx.Response, action_description: str) -> None:
    """Verify HTTP response status, printing full Retell error body on failure."""
    if resp.status_code not in (200, 201):
        print(
            f"Error: {action_description} failed with status {resp.status_code}.\n"
            f"Response Body:\n{resp.text}",
            file=sys.stderr,
        )
        sys.exit(1)


def find_indian_voices(voices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter voices for Indian/Hindi accent or language."""
    indian_voices = []
    keywords = ["india", "hindi", "hi-in", "en-in"]
    for v in voices:
        lang = str(v.get("language") or "").lower()
        accent = str(v.get("accent") or "").lower()
        name = str(v.get("voice_name") or "").lower()
        if any(kw in lang or kw in accent or kw in name for kw in keywords):
            indian_voices.append(v)
    return indian_voices


def verify_voices_exist(
    client: httpx.Client,
    headers: dict[str, str],
    voice_id_en: str,
    voice_id_hi: str,
) -> None:
    """Verify that both English and Hindi voice IDs exist in Retell list-voices.
    If not, abort with a clear message and print available Indian/Hindi voices.
    """
    resp = client.get(f"{RETELL_API_BASE}/list-voices", headers=headers)
    check_response(resp, "Retrieving voice list from Retell")

    voices = resp.json()
    all_voice_ids = {v.get("voice_id") for v in voices if v.get("voice_id")}

    missing = []
    if voice_id_en not in all_voice_ids:
        missing.append(f"English voice ID '{voice_id_en}'")
    if voice_id_hi not in all_voice_ids:
        missing.append(f"Hindi voice ID '{voice_id_hi}'")

    if missing:
        msg = " and ".join(missing)
        print(f"Error: Voice ID verification failed. {msg} does not exist in Retell voices list.", file=sys.stderr)
        print("Do not silently default. Available Indian / Hindi-accent voices in Retell:", file=sys.stderr)
        indian_voices = find_indian_voices(voices)
        if indian_voices:
            print(f"{'VOICE ID':<35} | {'NAME':<20} | {'PROVIDER':<15} | {'LANG/ACCENT':<15}", file=sys.stderr)
            print("-" * 90, file=sys.stderr)
            for v in indian_voices:
                v_id = v.get("voice_id", "")
                v_name = v.get("voice_name", "")
                provider = v.get("provider", "")
                lang = v.get("language", "") or v.get("accent", "")
                print(f"{v_id:<35} | {v_name:<20} | {provider:<15} | {lang:<15}", file=sys.stderr)
        else:
            print("  No Indian / Hindi-accent voices found in Retell account.", file=sys.stderr)
        sys.exit(1)


def mask_payload_headers(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of payload with all tool header values masked as '***'."""
    masked = copy.deepcopy(payload)
    for tool in masked.get("general_tools", []):
        if "headers" in tool and isinstance(tool["headers"], dict):
            tool["headers"] = {k: "***" for k in tool["headers"]}
    return masked


def load_retell_ids(id_file_path: Path = ID_FILE_PATH) -> dict[str, str]:
    """Load stored Retell resource IDs from JSON file."""
    if id_file_path.exists():
        try:
            return json.loads(id_file_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_retell_ids(ids: dict[str, Any], id_file_path: Path = ID_FILE_PATH) -> None:
    """Immediately persist Retell resource IDs to JSON file."""
    id_file_path.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: v for k, v in ids.items() if v is not None}
    id_file_path.write_text(json.dumps(clean, indent=2), encoding="utf-8")


def list_voices(api_key: str) -> None:
    """List voices from Retell AI API and print id, name, provider, language."""
    if not api_key:
        print("Error: RETELL_API_KEY environment variable is required to list voices.", file=sys.stderr)
        sys.exit(1)

    url = f"{RETELL_API_BASE}/list-voices"
    headers = {"Authorization": f"Bearer {api_key}"}

    with httpx.Client(timeout=15.0) as client:
        resp = client.get(url, headers=headers)
        check_response(resp, "Listing Retell voices")

        voices = resp.json()
        print(f"\n{'VOICE ID':<35} | {'NAME':<20} | {'PROVIDER':<15} | {'LANGUAGE':<10}")
        print("-" * 88)
        for v in voices:
            v_id = v.get("voice_id", "")
            v_name = v.get("voice_name", "")
            provider = v.get("provider", "")
            lang = v.get("language", "") or v.get("accent", "")
            print(f"{v_id:<35} | {v_name:<20} | {provider:<15} | {lang:<10}")
        print(f"\nTotal voices listed: {len(voices)}")


def setup_retell(
    dry_run: bool = False,
    id_file_path: Optional[Path] = None,
) -> None:
    """Setup Retell LLM and Voice Agents, creating or updating idempotently."""
    api_key = os.getenv("RETELL_API_KEY", RETELL_API_KEY).strip()
    public_base_url = os.getenv("PUBLIC_BASE_URL", PUBLIC_BASE_URL).strip() or "https://demo.ngrok-free.app"
    tool_secret = os.getenv("TOOL_SHARED_SECRET", TOOL_SHARED_SECRET).strip()

    voice_id_en = os.getenv("RETELL_VOICE_ID_EN", RETELL_VOICE_ID_EN).strip() or DEFAULT_VOICE_EN
    voice_id_hi = os.getenv("RETELL_VOICE_ID_HI", RETELL_VOICE_ID_HI).strip() or DEFAULT_VOICE_HI

    target_id_path = id_file_path or ID_FILE_PATH

    print(f"Using Public Base URL: {public_base_url}")
    print(f"Selected English Voice: {voice_id_en}")
    print(f"Selected Hindi Voice:   {voice_id_hi}")

    # Check for existing IDs
    saved_ids = load_retell_ids(target_id_path)
    llm_id = saved_ids.get("llm_id")
    agent_id_en = saved_ids.get("agent_id_en")
    agent_id_hi = saved_ids.get("agent_id_hi")

    llm_payload = build_llm_payload(public_base_url, tool_secret)
    dummy_llm_id = llm_id or "llm_mock_123456"
    agent_en_payload = build_agent_payload(
        llm_id=dummy_llm_id,
        agent_name="Autopay Recovery Agent (EN)",
        language="en-IN",
        voice_id=voice_id_en,
        public_base_url=public_base_url,
    )
    agent_hi_payload = build_agent_payload(
        llm_id=dummy_llm_id,
        agent_name="Autopay Recovery Agent (HI)",
        language="hi-IN",
        voice_id=voice_id_hi,
        public_base_url=public_base_url,
    )

    if dry_run:
        print("\n=== DRY RUN MODE: No network calls will be made ===\n")
        masked_llm_payload = mask_payload_headers(llm_payload)
        print("1. Retell LLM Payload (Target: " + (f"PATCH /update-retell-llm/{llm_id}" if llm_id else "POST /create-retell-llm") + "):")
        print(json.dumps(masked_llm_payload, indent=2))

        print("\n2. English Agent Payload (Target: " + (f"PATCH /update-agent/{agent_id_en}" if agent_id_en else "POST /create-agent") + "):")
        print(json.dumps(agent_en_payload, indent=2))

        print("\n3. Hindi Agent Payload (Target: " + (f"PATCH /update-agent/{agent_id_hi}" if agent_id_hi else "POST /create-agent") + "):")
        print(json.dumps(agent_hi_payload, indent=2))
        return

    if not api_key:
        print("Error: RETELL_API_KEY environment variable is required to provision resources.", file=sys.stderr)
        sys.exit(1)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=30.0) as client:
        # 1. Provision LLM
        if llm_id:
            print(f"Updating existing Retell LLM: {llm_id}...")
            resp = client.patch(f"{RETELL_API_BASE}/update-retell-llm/{llm_id}", headers=headers, json=llm_payload)
            check_response(resp, f"Updating Retell LLM ({llm_id})")
        else:
            print("Creating new Retell LLM...")
            resp = client.post(f"{RETELL_API_BASE}/create-retell-llm", headers=headers, json=llm_payload)
            check_response(resp, "Creating Retell LLM")
            llm_id = resp.json().get("llm_id")
            print(f"Created LLM: {llm_id}")
            saved_ids["llm_id"] = llm_id
            save_retell_ids(saved_ids, target_id_path)

        # 2. Before creating agents, verify voice IDs exist in Retell
        verify_voices_exist(client, headers, voice_id_en, voice_id_hi)

        # Update agent payloads with real LLM ID
        agent_en_payload["response_engine"]["llm_id"] = llm_id
        agent_hi_payload["response_engine"]["llm_id"] = llm_id

        # 3. Provision English Agent
        if agent_id_en:
            print(f"Updating English Agent: {agent_id_en}...")
            resp = client.patch(f"{RETELL_API_BASE}/update-agent/{agent_id_en}", headers=headers, json=agent_en_payload)
            check_response(resp, f"Updating English Agent ({agent_id_en})")
        else:
            print("Creating English Agent (en-IN)...")
            resp = client.post(f"{RETELL_API_BASE}/create-agent", headers=headers, json=agent_en_payload)
            check_response(resp, "Creating English Agent")
            agent_id_en = resp.json().get("agent_id")
            print(f"Created English Agent: {agent_id_en}")
            saved_ids["agent_id_en"] = agent_id_en
            save_retell_ids(saved_ids, target_id_path)

        # 4. Provision Hindi Agent
        if agent_id_hi:
            print(f"Updating Hindi Agent: {agent_id_hi}...")
            resp = client.patch(f"{RETELL_API_BASE}/update-agent/{agent_id_hi}", headers=headers, json=agent_hi_payload)
            check_response(resp, f"Updating Hindi Agent ({agent_id_hi})")
        else:
            print("Creating Hindi Agent (hi-IN)...")
            resp = client.post(f"{RETELL_API_BASE}/create-agent", headers=headers, json=agent_hi_payload)
            check_response(resp, "Creating Hindi Agent")
            agent_id_hi = resp.json().get("agent_id")
            print(f"Created Hindi Agent: {agent_id_hi}")
            saved_ids["agent_id_hi"] = agent_id_hi
            save_retell_ids(saved_ids, target_id_path)

    print(f"\nRetell setup complete! Saved IDs to: {target_id_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Retell AI provisioning script for Autopay Recovery Voice Agent")
    parser.add_argument("--dry-run", action="store_true", help="Print exact payloads without making any network calls")
    parser.add_argument("--list-voices", action="store_true", help="List available Retell voices")
    args = parser.parse_args()

    if args.list_voices:
        api_key = os.getenv("RETELL_API_KEY", RETELL_API_KEY).strip()
        list_voices(api_key)
    else:
        setup_retell(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
