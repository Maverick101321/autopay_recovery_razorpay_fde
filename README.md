# Autopay Recovery Agent

## Overview
AI voice agent that recovers failed autopay payments for recurring transactions.

## Setup
Create a virtual environment and install dependencies:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Run
Seed the database and start the FastAPI local server:
```bash
# Seed the SQLite database with 10 fictional customers
python scripts/seed.py

# Run unit and integration tests
pytest -q

# Start the development server
uvicorn app.main:app --reload
```

## Retell Setup
To provision the Retell LLM response engine and voice agents:

1. **Configure Environment Variables**:
   Update `.env` with your Retell API key, public base URL, and tool shared secret:
   ```bash
   RETELL_API_KEY=your_retell_api_key_here
   DEMO_PHONE=+918433907272
   TOOL_SHARED_SECRET=your_random_secret_string
   REQUIRE_WEBHOOK_SIGNATURE=true
   ```
   > [!IMPORTANT]
   > **Webhook-Badge Key Requirement**: In your Retell AI dashboard (Settings -> API Keys), ensure that the API key specified in `RETELL_API_KEY` has the **Webhook** badge enabled. Retell signs all outbound webhook event payloads using this webhook-designated key. Configuring an unbadged API key will cause signature verification to fail with HTTP 401.

2. **Expose Local Server via ngrok**:
   Start an ngrok tunnel pointing to port 8000:
   ```bash
   ngrok http 8000
   ```
   Copy the assigned forwarding URL (e.g., `https://xyz.ngrok-free.app`) and set it in `.env`:
   ```bash
   PUBLIC_BASE_URL=https://xyz.ngrok-free.app
   ```

3. **Run Retell Provisioning**:
   ```bash
   # Preview JSON payloads without making network calls
   python scripts/setup_retell.py --dry-run

   # List available voices from Retell
   python scripts/setup_retell.py --list-voices

   # Create or idempotently update Retell LLM and agents
   python scripts/setup_retell.py
   ```
   Provisioned IDs are cached in `data/retell_ids.json`.

   > [!NOTE]
   > **Dashboard Web Test Calls**: When testing agents via the Retell web test simulator in your browser, ensure you define the `opening_line` variable in the dashboard's Dynamic Variables configuration modal (e.g. `Hello, this is Riya, an automated AI assistant calling on behalf of StreamFlix. Am I speaking with Aarav?`). The LLM prompt begins speaking using `{{opening_line}}`.

## Placing a Call
The dialing script (`scripts/dial.py`) manages call scheduling, safety checks, and API integration with Retell AI.

### Safety Guardrails
- **DEMO_PHONE Allowlist**: Live calls and dry runs are strictly limited to your verified `DEMO_PHONE` (`+918433907272`). Calls to fictional phone numbers (`cust_003`..`cust_010`) are rejected immediately.
- **Call Window & Consent**: `can_call()` enforces call consent and customer time windows (`09:00` - `18:00` IST) before dialing.
- **Rate Limit & Cooldown**: Default 1 call per run (hard limit of 3). A 10-minute cooldown prevents rapid repeat calls to the same payment record.
- **Interactive Confirmation**: Live calls require explicitly typing `YES` after reviewing customer, destination phone, agent ID, and estimated cost (~$0.30 USD).

### Example Commands
```bash
# 1. Preview dialability evaluation plan across all 10 payments (zero calls placed)
python scripts/dial.py --plan

# 2. Dry-run preview of exact Retell create-phone-call JSON payload (zero network calls)
python scripts/dial.py --payment-id pay_001

# 3. Place a live call via Retell AI (requires RETELL_FROM_NUMBER and typing YES)
python scripts/dial.py --payment-id pay_001 --live
```

## Admin Endpoints
Protected by the `x-tool-secret` header matching `TOOL_SHARED_SECRET`:
- `GET /admin/state`: View all payment statuses and retry metadata.
- `GET /admin/calls`: View call history, transcripts, costs, duration, and analysis.
- `POST /admin/confirm_payment`: Confirm external payment clearance (sets status to `recovered`).

## Assumptions
- Real phone calls are placed exclusively to the allowlisted `DEMO_PHONE` to eliminate unintended dials during development and demo.
- Spoken currency amounts are converted to plain English words (e.g., "seven hundred ninety nine rupees") for natural speech rendering.

## Limitations
- Live dialing requires an active Retell AI account with a provisioned phone number (`RETELL_FROM_NUMBER`).
- Live telephony charges (~$0.10 - $0.20/min) apply only when explicitly running with `--live` and confirming `YES`.
