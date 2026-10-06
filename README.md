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

## Assumptions
Zero-cost local execution for initial development with SQLite and simulated customer data.

## Limitations
Voice calls and external network interactions with Retell AI are not yet integrated in this phase.
