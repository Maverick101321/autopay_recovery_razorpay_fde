# Three-minute demo shot list

This is a short reviewer walkthrough. Keep secrets, credentials, public tunnel URLs, phone numbers, and payment-link URLs off screen.

| Time | Screen / action | What to say |
|---|---|---|
| 0:00–0:25 | Run `python scripts/dial.py --plan`. | “The plan is a zero-call safety view. It applies consent, call windows, retry caps, terminal states, and the verified-destination allowlist.” |
| 0:25–0:45 | Run `python scripts/setup_retell.py --dry-run`. | “This produces the Retell LLM and agent payloads without network calls. The tool schema includes first-name binding, and masked headers remain masked.” |
| 0:45–1:15 | Show the Retell call page and recording for the labeled live phone call. | “This was a trial SIP-trunk call to the author’s verified destination. Point out the disclosure recovery, name re-check, unverified-payment handling, and link offer. Do not reveal credentials or phone numbers.” |
| 1:15–1:35 | Show `GET /admin/calls` with the tool-secret header configured locally. | “The list view exposes summaries, not raw events. The detail route is for the sanitized event history.” |
| 1:35–2:20 | Open `reports/eval_report.md`. | “Separate SIMULATED funnel figures from MEASURED transcript and cost data. Show valid, superseded, ghost, and invalid-test handling; then show the per-channel Retell and measured telephony cost lines.” |
| 2:20–3:00 | Run `python -m pytest -q`. | “The suite covers API behavior, policy rules, dialing safety, schema migration, sanitizer behavior, outcome binding, and the offline evaluator.” |

## Presenter checklist

- Use the activated virtual environment and `python` commands from the README.
- Do not place a call during the recording; use the stored call page/recording only.
- Keep dynamic-variable values, public URLs, phone numbers, credentials, and headers out of the capture.
- State plainly that the funnel is simulated and the payment link/SMS are mocks.
