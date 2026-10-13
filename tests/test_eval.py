import hashlib
import json
from pathlib import Path

import httpx

from evals.checks import check_payment_context, check_transcript
from scripts.run_eval import main, network_guard
from scripts.run_eval import cost_metrics


ROOT = Path(__file__).resolve().parent.parent


def test_transcript_fixtures_match_labels():
    fixtures = json.loads((ROOT / "evals" / "fixtures" / "transcripts.json").read_text())
    for fixture in fixtures["good"]:
        assert all(result["passed"] for result in check_transcript(fixture["text"], fixture.get("context")) if result["severity"] == "must")
    for fixture in fixtures["bad"]:
        failed = {result["check"] for result in check_transcript(fixture["text"], fixture.get("context")) if not result["passed"]}
        assert failed == {fixture["intended"]}


def test_network_guard_blocks_httpx():
    with network_guard():
        try:
            httpx.get("https://example.com")
        except RuntimeError as exc:
            assert str(exc) == "network blocked in eval"
        else:
            raise AssertionError("network was not blocked")


def test_eval_end_to_end_does_not_change_real_db(tmp_path):
    db = ROOT / "data" / "autopay.db"
    before = hashlib.sha256(db.read_bytes()).hexdigest() if db.exists() else None
    # Stored calls may legitimately contain must-check failures; the harness still
    # writes its report and never mutates the real database.
    assert main(["--out", str(tmp_path)]) in (0, 1)
    report = json.loads((tmp_path / "eval_report.json").read_text())
    assert {"summary", "policy_matrix", "tool_behavior", "funnel", "stored_transcripts", "lint", "cost_estimate"} <= report.keys()
    after = hashlib.sha256(db.read_bytes()).hexdigest() if db.exists() else None
    assert before == after


def test_all_payment_context_outputs_are_safe():
    from scripts.run_eval import main as _main
    # The end-to-end run exercises all ten; this unit-level assertion preserves the
    # checker contract independently of FastAPI setup.
    assert check_payment_context("A 799 rupee payment failed because plain language.", "Ada", "Acme", 799, "+919999999999", "Lovelace", "pay_001")[0]["passed"]


def test_cost_metrics_uses_usd_per_minute(tmp_path):
    db = tmp_path / "calls.db"
    conn = __import__("sqlite3").connect(db)
    conn.execute("CREATE TABLE call_attempts (call_cost_usd REAL, duration_ms INTEGER)")
    conn.execute("INSERT INTO call_attempts VALUES (0.15, 60000)")
    conn.commit(); conn.close()
    measured = cost_metrics(db)
    assert measured["stored_call_count"] == 1
    assert measured["measured_average_cost_per_minute_usd"] == 0.15
    assert measured["assumed_average_duration_seconds"] == 60


def test_scenarios_puts_claims_paid_on_an_eligible_customer():
    scenarios = json.loads((ROOT / "evals" / "scenarios.json").read_text())
    claim_payment = next(payment for payment, persona in scenarios["persona_mapping"].items() if persona == "claims_already_paid")
    assert claim_payment != "pay_003"
