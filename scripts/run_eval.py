#!/usr/bin/env python3
"""Offline, deterministic evaluation harness. It blocks every outbound connection."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import socket
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@contextmanager
def network_guard():
    """Block actual networking while preserving TestClient's in-process ASGI transport."""
    def blocked(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("network blocked in eval")
    original_send = httpx.Client.send
    def guarded_send(client: httpx.Client, *args: Any, **kwargs: Any) -> Any:
        if client.__class__.__module__.startswith("starlette.testclient"):
            return original_send(client, *args, **kwargs)
        raise RuntimeError("network blocked in eval")
    with patch.object(httpx.Client, "send", guarded_send), patch.object(httpx.AsyncClient, "send", blocked), patch.object(socket, "create_connection", blocked):
        yield


def tally(results: list[dict[str, Any]]) -> dict[str, int]:
    return {severity: sum(r["severity"] == severity and r["passed"] for r in results) for severity in ("must", "should")}


def add(results: list[dict[str, Any]], name: str, passed: bool, severity: str = "must", evidence: str = "") -> None:
    results.append({"check": name, "passed": bool(passed), "severity": severity, "evidence": evidence})


def run_policy(conn: sqlite3.Connection, rules: Any) -> tuple[list[dict[str, Any]], dict[str, int]]:
    customers = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM customers")}
    payments = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM failed_payments")}
    ist = ZoneInfo("Asia/Kolkata")
    base = datetime(2026, 1, 15, 10, 5, tzinfo=ist)
    cases: list[tuple[str, dict[str, Any], dict[str, Any], datetime, str]] = []
    cases.append(("no_consent", customers["cust_003"], payments["pay_003"], base, "no_consent"))
    retry = dict(payments["pay_001"]); retry["retry_count"] = rules.MAX_RETRIES
    cases.append(("retry_cap", customers["cust_001"], retry, base, "retry_cap"))
    for status in sorted(rules.TERMINAL_STATUSES):
        payment = dict(payments["pay_001"]); payment["status"] = status
        cases.append((f"terminal_{status}", customers["cust_001"], payment, base, "terminal_status"))
    c4, p4 = dict(customers["cust_004"]), payments["pay_004"]
    c4["phone"] = os.environ["DEMO_PHONE"]
    for label, minute, expected in (("window_in", (10, 0), "ok"), ("window_before", (9, 59), "outside_window"), ("window_end", (11, 0), "outside_window"), ("window_after", (11, 1), "outside_window")):
        cases.append((label, c4, p4, datetime(2026, 1, 15, *minute, tzinfo=ist), expected))
    bad_phone = dict(customers["cust_001"]); bad_phone["phone"] = "+919999999999"
    cases.append(("not_allowlisted", bad_phone, payments["pay_001"], base, "not_allowlisted"))
    good_customer = dict(customers["cust_001"]); good_customer["phone"] = os.environ["DEMO_PHONE"]
    cases.append(("happy", good_customer, payments["pay_001"], base, "ok"))
    priority_c = dict(good_customer); priority_c["consent_to_call"] = 0
    priority_p = dict(payments["pay_001"]); priority_p["status"] = "recovered"; priority_p["retry_count"] = 99
    cases.append(("priority", priority_c, priority_p, base, "terminal_status"))
    results: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for name, customer, payment, now, expected in cases:
        allowed, actual = rules.can_call(customer, payment, now)
        add(results, f"policy:{name}", actual == expected, "must", f"expected={expected}, actual={actual}, allowed={allowed}")
        counts[actual] = counts.get(actual, 0) + 1
    return results, counts


def run_tools(client: Any, conn: sqlite3.Connection, secret: str, checks: Any) -> tuple[list[dict[str, Any]], list[str]]:
    headers = {"x-tool-secret": secret}; results: list[dict[str, Any]] = []; findings: list[str] = []
    rows = conn.execute("SELECT p.*, c.name, c.phone FROM failed_payments p JOIN customers c ON c.id=p.customer_id ORDER BY p.id").fetchall()
    for row in rows:
        response = client.post("/tools/get_payment_context", headers=headers, json={"failed_payment_id": row["id"], "customer_first_name": row["name"].split()[0]})
        output = response.json().get("result", "") if response.status_code == 200 else ""
        last_name = row["name"].split()[-1]
        for result in checks.check_payment_context(output, row["name"].split()[0], row["merchant_name"], row["amount_inr"], row["phone"], last_name, row["id"]):
            add(results, f"{result['check']}:{row['id']}", result["passed"], result["severity"], result["evidence"])
    unknown = client.post("/tools/get_payment_context", headers=headers, json={"failed_payment_id": "unknown"})
    add(results, "tool:unknown_id", unknown.status_code == 200 and "couldn't find" in unknown.json().get("result", "").lower(), evidence=str(unknown.text))
    add(results, "tool:secret_required", client.post("/tools/get_payment_context", json={}).status_code == 401)
    a = client.post("/tools/create_payment_link", headers=headers, json={"failed_payment_id": "pay_001"}).json()["result"]
    b = client.post("/tools/create_payment_link", headers=headers, json={"failed_payment_id": "pay_001"}).json()["result"]
    msg_count = conn.execute("SELECT count(*) FROM outbound_messages WHERE failed_payment_id='pay_001'").fetchone()[0]
    add(results, "tool:link_idempotent", a.split()[-1] == b.split()[-1] and msg_count == 1, evidence=f"messages={msg_count}")
    client.post("/tools/log_outcome", headers=headers, json={"failed_payment_id":"pay_002", "outcome":"claims_paid"})
    status = conn.execute("SELECT status FROM failed_payments WHERE id='pay_002'").fetchone()[0]
    add(results, "tool:claims_paid_promised", status == "promised", evidence=status)
    retry = client.post("/tools/schedule_retry", headers=headers, json={"failed_payment_id":"pay_004"}).json()["result"]
    retry_at = conn.execute("SELECT next_retry_at FROM failed_payments WHERE id='pay_004'").fetchone()[0]
    add(results, "tool:retry_in_window", bool(retry_at and retry), evidence=str(retry_at))
    calls = client.get("/admin/calls", headers=headers)
    add(results, "tool:admin_calls_sanitized", not any(x in calls.text.lower() for x in ("access_token", "credential", "ice_servers")), evidence=calls.text[:120])
    client.post("/tools/log_outcome", headers=headers, json={"failed_payment_id":"pay_005", "outcome":"paid_now"})
    paid_status = conn.execute("SELECT status FROM failed_payments WHERE id='pay_005'").fetchone()[0]
    add(results, "tool:paid_now_normalized", paid_status == "promised", evidence=paid_status)
    return results, findings


def run_funnel(client: Any, conn: sqlite3.Connection, secret: str, scenarios: dict[str, Any], rules: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    headers = {"x-tool-secret": secret}; results: list[dict[str, Any]] = []; mapping = scenarios["persona_mapping"]
    metrics: dict[str, Any] = {"customers_total": 10, "eligible": 0, "attempted": 0, "link_sent": 0, "promised": 0, "confirmed_recovered": 0, "blocked_by_reason": {}, "outcomes": {}, "real_allowlist_blocked": 0, "customers": []}
    original = rules.is_dialable; rules.is_dialable = lambda _phone: True
    try:
        for payment_id, persona_name in mapping.items():
            p = conn.execute("SELECT * FROM failed_payments WHERE id=?", (payment_id,)).fetchone(); c = conn.execute("SELECT * FROM customers WHERE id=?", (p["customer_id"],)).fetchone()
            hour, minute = map(int, c["call_window_start"].split(":")); now = datetime(2026, 1, 15, hour, minute + 5, tzinfo=ZoneInfo(c["timezone"]))
            allowed, reason = rules.can_call(c, p, now); metrics["eligible"] += int(allowed)
            if not original(c["phone"]): metrics["real_allowlist_blocked"] += 1
            if not allowed:
                metrics["blocked_by_reason"][reason] = metrics["blocked_by_reason"].get(reason, 0) + 1
                metrics["customers"].append({"customer": c["name"], "persona": persona_name, "eligible": False, "reason": reason, "final_status": p["status"], "outcome": None})
                continue
            persona = scenarios["personas"][persona_name]; metrics["attempted"] += 1
            for step in persona["steps"]:
                if step == "context": client.post("/tools/get_payment_context", headers=headers, json={"failed_payment_id":payment_id, "customer_first_name":c["name"].split()[0]})
                elif step == "link": client.post("/tools/create_payment_link", headers=headers, json={"failed_payment_id":payment_id}); metrics["link_sent"] += 1
                elif step == "retry": client.post("/tools/schedule_retry", headers=headers, json={"failed_payment_id":payment_id})
                elif step == "escalate": client.post("/tools/escalate_to_human", headers=headers, json={"failed_payment_id":payment_id,"reason":"simulated"})
                elif step == "lookup_failure": client.post("/tools/get_payment_context", headers=headers, json={"failed_payment_id":"unknown"})
                elif step == "confirm": client.post("/admin/confirm_payment", headers=headers, json={"failed_payment_id":payment_id,"notes":"simulated confirmation"})
                else: client.post("/tools/log_outcome", headers=headers, json={"failed_payment_id":payment_id,"outcome": {"promised":"promised_to_pay","claims":"claims_paid"}.get(step, step)})
            row = conn.execute("SELECT status FROM failed_payments WHERE id=?", (payment_id,)).fetchone(); outcome = conn.execute("SELECT outcome FROM call_attempts WHERE failed_payment_id=? ORDER BY id DESC LIMIT 1", (payment_id,)).fetchone()
            actual_outcome = outcome[0] if outcome else None
            add(results, f"funnel:{payment_id}:{persona_name}", row["status"] == persona["status"] and actual_outcome == persona["outcome"], evidence=f"status={row['status']} outcome={actual_outcome}")
            metrics["outcomes"][actual_outcome] = metrics["outcomes"].get(actual_outcome, 0) + 1
            metrics["promised"] += int(row["status"] == "promised"); metrics["confirmed_recovered"] += int(row["status"] == "recovered")
            metrics["customers"].append({"customer": c["name"], "persona": persona_name, "eligible": True, "reason": "ok", "final_status": row["status"], "outcome": actual_outcome})
    finally:
        rules.is_dialable = original
    add(results, "funnel:attempted_lte_eligible", metrics["attempted"] <= metrics["eligible"], evidence=f"{metrics['attempted']} <= {metrics['eligible']}")
    metrics["commitments"] = sum(1 for row in metrics["customers"] if row["outcome"] in {"promised_to_pay", "recovered"})
    metrics["commitment_rate"] = metrics["commitments"] / metrics["attempted"] if metrics["attempted"] else 0
    metrics["confirmed_recovery_rate"] = metrics["confirmed_recovered"] / metrics["attempted"] if metrics["attempted"] else 0
    add(results, "funnel:recovery_lte_commitment", metrics["confirmed_recovery_rate"] <= metrics["commitment_rate"], evidence=f"{metrics['confirmed_recovery_rate']:.4f} <= {metrics['commitment_rate']:.4f}")
    return results, metrics


def stored_transcripts(checks: Any, enabled: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not enabled: return [], [], [], [], [], []
    path = ROOT / "data" / "autopay.db"
    if not path.exists(): return [], [{"note":"No real data/autopay.db file exists."}], [], [], [], []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True); conn.row_factory = sqlite3.Row
        rows = conn.execute("""SELECT ca.retell_call_id, ca.transcript, ca.outcome, ca.raw_events, ca.ended_at, ca.started_at,
            ca.failed_payment_id, c.name, p.merchant_name FROM call_attempts ca
            LEFT JOIN failed_payments p ON p.id = ca.failed_payment_id LEFT JOIN customers c ON c.id = p.customer_id""").fetchall()
        other_rows = conn.execute("SELECT c.name, p.merchant_name FROM customers c JOIN failed_payments p ON p.customer_id=c.id").fetchall(); conn.close()
    except sqlite3.Error as exc: return [], [{"note":f"Stored-call check skipped: {exc}"}], [], [], [], []
    labels = json.loads((ROOT / "evals" / "call_labels.json").read_text()).get("labels", {})
    reports = []; results: list[dict[str, Any]] = []; invalid: list[dict[str, Any]] = []; notes: list[str] = []; ghosts: list[dict[str, Any]] = []; superseded: list[dict[str, Any]] = []
    for row in rows:
        label = labels.get(row["retell_call_id"], {})
        if label.get("status") == "superseded":
            superseded.append({"call_id": row["retell_call_id"], "label": label.get("label", ""), "note": label.get("note", "")}); continue
        if label.get("status") == "ghost":
            ghosts.append({"call_id": row["retell_call_id"], "reason": "ghost (no end event)", "note": label.get("note", "")}); continue
        if not row["ended_at"] and not row["transcript"] and row["started_at"]:
            try:
                if (datetime.now(timezone.utc) - datetime.fromisoformat(row["started_at"]).astimezone(timezone.utc)).total_seconds() > 1800:
                    ghosts.append({"call_id":row["retell_call_id"], "reason":"ghost (no end event)", "note":label.get("note", "")}); continue
            except ValueError: pass
        if row["transcript"]:
            variables = None
            try:
                events = json.loads(row["raw_events"] or "[]")
                for event in reversed(events if isinstance(events, list) else [events]):
                    variables = (event.get("call") or {}).get("retell_llm_dynamic_variables")
                    if variables: break
            except (ValueError, TypeError): pass
            has_identity_variables = isinstance(variables, dict) and {"customer_first_name", "merchant_name"} <= variables.keys()
            if has_identity_variables and ((variables.get("customer_first_name") or "").casefold() != (row["name"] or "").split()[0].casefold() or (variables.get("merchant_name") or "").casefold() != (row["merchant_name"] or "").casefold()):
                invalid.append({"call_id":row["retell_call_id"], "reason":"INVALID TEST (variables do not match record)"}); continue
            if not has_identity_variables: notes.append(f"{row['retell_call_id']}: variables unavailable; identity binding check skipped.")
            context = {"outcome":row["outcome"], "customer_first_name":(row["name"] or "").split()[0], "merchant_name":row["merchant_name"] or "", "other_names":[x["name"].split()[0] for x in other_rows] + [x["merchant_name"] for x in other_rows]}
            checked = checks.check_transcript(row["transcript"], context)
            channel = "unknown"
            try:
                event_call = (events[-1].get("call") or {}) if events else {}
                channel = event_call.get("call_type") or "unknown"
            except (IndexError, AttributeError): pass
            reports.append({"call_id":row["retell_call_id"], "payment_id":row["failed_payment_id"], "scenario":label.get("scenario", ""), "channel":channel, "results":checked}); results.extend(checked)
    return results, reports or [{"note":"No stored calls with transcripts."}], invalid, notes, ghosts, superseded


def cost_metrics(db_path: Path | None = None) -> dict[str, Any]:
    """Read cost and duration samples from the real DB without opening it writable."""
    path = db_path or ROOT / "data" / "autopay.db"
    fallback = {"stored_call_count": 0, "measured_average_cost_per_minute_usd": 0.0, "assumed_average_duration_seconds": 70.0}
    if not path.exists(): return fallback
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT call_cost_usd, duration_ms, raw_events FROM call_attempts WHERE call_cost_usd IS NOT NULL AND duration_ms > 0").fetchall()
        except sqlite3.OperationalError:
            rows = [(cost, duration, None) for cost, duration in conn.execute("SELECT call_cost_usd, duration_ms FROM call_attempts WHERE call_cost_usd IS NOT NULL AND duration_ms > 0").fetchall()]
        conn.close()
    except sqlite3.Error:
        return fallback
    if not rows: return fallback
    rates = [float(row[0]) / (float(row[1]) / 60000) for row in rows]
    by_channel: dict[str, list[float]] = {}
    for cost, duration, raw_events in rows:
        channel = "unknown"
        try:
            events = json.loads(raw_events or "[]")
            channel = ((events[-1].get("call") or {}).get("call_type")) or "unknown"
        except (ValueError, TypeError, IndexError, AttributeError): pass
        by_channel.setdefault(channel, []).append(float(cost) / (float(duration) / 60000))
    return {"stored_call_count": len(rows), "measured_average_cost_per_minute_usd": sum(rates) / len(rates), "assumed_average_duration_seconds": sum(float(row[1]) / 1000 for row in rows) / len(rows), "retell_cost_per_minute_by_channel": {channel: sum(values) / len(values) for channel, values in by_channel.items()}}


def telephony_cost(duration_seconds: float, measured: dict[str, Any]) -> float:
    """Price a call using the measured provider billing increment."""
    billed_minutes = math.ceil(duration_seconds / measured["billing_increment_seconds"])
    return billed_minutes * measured["rate_usd_per_billed_minute"]


def render_markdown(report: dict[str, Any]) -> str:
    s = report["summary"]
    lines = ["# Evaluation report", f"Run timestamp: {report['run_timestamp']}", "", "## Summary", "**SIMULATION NOTICE:** Funnel values are simulations of scripted backend behavior, not real-world recovery measurements.", f"Must-pass: {s['must_passed']}/{s['must_total']}; should-pass: {s['should_passed']}/{s['should_total']}", "", "## 1 Policy matrix (SIMULATED)"]
    lines += [f"- {r['check']}: {'PASS' if r['passed'] else 'FAIL'} — {r['evidence']}" for r in report["policy_matrix"]]
    lines += ["", "## 2 Tool behavior (SIMULATED)"] + [f"- {r['check']}: {'PASS' if r['passed'] else 'FAIL'}" for r in report["tool_behavior"]]
    f = report["funnel"]
    lines += ["", "## 3 Simulated funnel", "**SIMULATED:** allowlist bypassed only inside this harness; real `dial.py` still enforces it. Persona assignment determines the recovery number, so it is not a prediction.", f"Commitment rate: ({f['promised']} promised + {f['confirmed_recovered']} recovered) / {f['attempted']} attempted = {f['commitments']}/{f['attempted']} ({f['commitment_rate']:.1%}); confirmed recovery rate: {f['confirmed_recovered']}/{f['attempted']} ({f['confirmed_recovery_rate']:.1%}).", "", "| Customer | Persona | Eligible | Final status | Outcome |", "|---|---|---|---|---|"]
    lines += [f"| {row['customer']} | {row['persona']} | {'yes' if row['eligible'] else 'no: ' + row['reason']} | {row['final_status']} | {row['outcome'] or ''} |" for row in f["customers"]]
    lines += ["", "## 4 Transcript checks on stored calls (MEASURED)"]
    if report.get("invalid_tests"):
        lines += ["### Invalid tests (excluded from pass counts)"] + [f"- {row['call_id']}: {row['reason']}" for row in report["invalid_tests"]]
    if report.get("transcript_notes"):
        lines += ["### Identity-binding notes"] + [f"- {note}" for note in report["transcript_notes"]]
    if report.get("ghost_calls"):
        lines += ["### Ghost calls (excluded from pass counts)"] + [f"- {row['call_id']}: {row['reason']}{'; ' + row['note'] if row.get('note') else ''}" for row in report["ghost_calls"]]
    if report.get("superseded_calls"):
        lines += ["### Superseded calls (excluded from pass counts)"] + [f"- {row['call_id']} ({row['label']}): {row['note']}" for row in report["superseded_calls"]]
    calls = [row for row in report["stored_transcripts"] if "results" in row]
    if not calls: lines.append("No stored calls with transcripts.")
    else:
        names = [item["check"] for item in calls[0]["results"]]
        lines += ["| Call | Payment ID | Scenario | Channel | " + " | ".join(f"{name} ({next(x['severity'] for x in calls[0]['results'] if x['check'] == name)})" for name in names) + " |", "|---|---|---|---|" + "|".join("---" for _ in names) + "|"]
        for call in calls:
            statuses = {x["check"]: "PASS" if x["passed"] else "FAIL" for x in call["results"]}
            lines.append("| " + str(call["call_id"]) + f" | {call.get('payment_id','')} | {call.get('scenario','')} | {call.get('channel','')} | " + " | ".join(statuses.get(name, "—") for name in names) + " |")
        failures = [(call["call_id"], check) for call in calls for check in call["results"] if not check["passed"]]
        if failures: lines += ["Failures:"] + [f"- {call_id}: {check['check']} — {check['evidence']}" for call_id, check in failures]
    lines += ["", "## 5 Lint (SIMULATED)"]
    lines += [f"- {r['check']}: {'PASS' if r['passed'] else 'FAIL'}" for r in report["lint"]]
    cost = report["cost_estimate"]
    simulated_recovery_cost = cost["simulated_cost_per_confirmed_recovery_usd"]
    lines += ["", "## 6 Cost estimate (ESTIMATE)",
              f"- Retell (MEASURED, n={cost['stored_call_count']}): ${cost['measured_average_cost_per_minute_usd']:.4f}/minute; ${cost['retell_cost_per_attempt_usd']:.4f} per attempt at {cost['average_duration_seconds']:.1f}s average duration.",
              f"- Telephony (MEASURED, n=1): ${cost['telephony_measured']['price_usd']:.4f} for {cost['telephony_measured']['duration_seconds']}s from {cost['telephony_measured']['source']}; ${cost['telephony_cost_per_attempt_usd']:.4f} per attempt at the average duration using {cost['telephony_measured']['billing_increment_seconds']}s billing increments.",
              f"- Total estimated cost per attempt: ${cost['total_cost_per_attempt_usd']:.4f} (Retell ${cost['retell_cost_per_attempt_usd']:.4f} + telephony ${cost['telephony_cost_per_attempt_usd']:.4f}).",
              f"- Telephony (ASSUMED comparison only): ${cost['telephony_assumed_usd_per_minute']:.2f}/minute; ${cost['telephony_assumed_cost_per_attempt_usd']:.4f} per attempt.",
              f"- SIMULATED funnel cost per confirmed recovery: {f'${simulated_recovery_cost:.4f}' if simulated_recovery_cost is not None else 'N/A'} ({cost['confirmed_recovered']}/{cost['attempted']} confirmed recoveries).",
              "- Retell measured cost per minute by channel: " + ", ".join(f"{channel} ${rate:.4f}" for channel, rate in cost["retell_cost_per_minute_by_channel"].items()),
              "", "## 7 Limitations", "- LLM behavior is non-deterministic and only spot-checked on stored calls.", "- Personas are scripted; no real payments occurred.", "- Allowlist bypass applies only inside the simulation.", "- Hindi quality is not evaluated automatically.", "- Heuristic checks can miss paraphrases."]
    if report["findings"]: lines += ["", "## Findings"] + [f"- {x}" for x in report["findings"]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--out", default=str(ROOT / "reports")); parser.add_argument("--no-db-transcripts", action="store_true"); parser.add_argument("--json-only", action="store_true"); args = parser.parse_args(argv)
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("autopay.tools").setLevel(logging.WARNING)
    os.environ.update({"TOOL_SHARED_SECRET":"eval_secret", "RETELL_API_KEY":"eval_key", "DEMO_PHONE":"+919876500001", "PUBLIC_BASE_URL":"https://eval.local", "REQUIRE_WEBHOOK_SIGNATURE":"true"})
    with tempfile.TemporaryDirectory(prefix="autopay-eval-") as temp, network_guard():
        os.environ["DB_PATH"] = str(Path(temp) / "eval.db")
        from fastapi.testclient import TestClient
        from app.db import get_db_connection
        from app.main import app
        from app import rules
        from scripts.seed import seed_database
        from scripts.setup_retell import build_agent_payload, build_llm_payload, mask_payload_headers
        from scripts.dial import build_call_payload
        from evals import checks
        scenarios = json.loads((ROOT / "evals" / "scenarios.json").read_text())
        seed_database(reset=True, demo_phone_override=os.environ["DEMO_PHONE"]); conn = get_db_connection()
        with TestClient(app) as client:
            policy, _ = run_policy(conn, rules); tools, findings = run_tools(client, conn, os.environ["TOOL_SHARED_SECRET"], checks)
            seed_database(reset=True, demo_phone_override=os.environ["DEMO_PHONE"]); conn.close(); conn = get_db_connection()
            funnel_results, funnel = run_funnel(client, conn, os.environ["TOOL_SHARED_SECRET"], scenarios, rules)
            prompt = (ROOT / "prompts" / "agent_prompt.md").read_text(); lint = checks.lint_prompt(prompt)
            llm = build_llm_payload(os.environ["PUBLIC_BASE_URL"], os.environ["TOOL_SHARED_SECRET"]); en = build_agent_payload("x","en","en-IN","voice",os.environ["PUBLIC_BASE_URL"]); hi = build_agent_payload("x","hi","hi-IN","voice",os.environ["PUBLIC_BASE_URL"])
            add(lint, "payload", llm["begin_message"] == "{{opening_line}}" and len(llm["general_tools"]) == 6 and all(t.get("url", "").startswith(os.environ["PUBLIC_BASE_URL"]) for t in llm["general_tools"] if t["type"] == "custom") and en["language"] != hi["language"] and en["interruption_sensitivity"] == 0.5 and "***" in json.dumps(mask_payload_headers(llm)), evidence="dry-run payload")
            sample_p = conn.execute("SELECT * FROM failed_payments WHERE id='pay_001'").fetchone(); sample_c = conn.execute("SELECT * FROM customers WHERE id='cust_001'").fetchone(); opening = build_call_payload(sample_p, sample_c, "+1", "a")["retell_llm_dynamic_variables"]["opening_line"]
            add(lint, "opening_line", next(x for x in checks.check_transcript("Agent: "+opening+"\nUser: Yes.") if x["check"] == "ai_disclosure")["passed"])
        conn.close()
        transcript_results, transcript_report, invalid_tests, transcript_notes, ghost_calls, superseded_calls = stored_transcripts(checks, not args.no_db_transcripts)
    all_results = policy + tools + funnel_results + lint + transcript_results
    must_total = sum(x["severity"] == "must" for x in all_results); must_passed = sum(x["severity"] == "must" and x["passed"] for x in all_results); should_total = sum(x["severity"] == "should" for x in all_results); should_passed = sum(x["severity"] == "should" and x["passed"] for x in all_results)
    measured = cost_metrics()
    average_duration = measured["assumed_average_duration_seconds"]
    telephony_measured = scenarios["telephony_measured"]
    retell_per_attempt = measured["measured_average_cost_per_minute_usd"] * average_duration / 60
    telephony_per_attempt = telephony_cost(average_duration, telephony_measured)
    total_per_attempt = retell_per_attempt + telephony_per_attempt
    assumed_telephony_per_attempt = scenarios["telephony_assumed_usd_per_minute"] * average_duration / 60
    report = {"run_timestamp":datetime.now(timezone.utc).isoformat(), "summary":{"must_passed":must_passed,"must_total":must_total,"should_passed":should_passed,"should_total":should_total}, "policy_matrix":policy, "tool_behavior":tools, "funnel":funnel, "stored_transcripts":transcript_report, "invalid_tests":invalid_tests, "transcript_notes":transcript_notes, "ghost_calls":ghost_calls, "superseded_calls":superseded_calls, "lint":lint, "findings":findings, "cost_estimate":{"label":"ESTIMATE", **measured, "average_duration_seconds":average_duration, "telephony_measured":telephony_measured, "telephony_assumed_usd_per_minute":scenarios["telephony_assumed_usd_per_minute"], "retell_cost_per_attempt_usd":round(retell_per_attempt,4), "telephony_cost_per_attempt_usd":round(telephony_per_attempt,4), "total_cost_per_attempt_usd":round(total_per_attempt,4), "telephony_assumed_cost_per_attempt_usd":round(assumed_telephony_per_attempt,4), "attempted":funnel["attempted"], "confirmed_recovered":funnel["confirmed_recovered"], "simulated_cost_per_confirmed_recovery_usd":round(total_per_attempt / (funnel["confirmed_recovered"] / funnel["attempted"]),4) if funnel["confirmed_recovered"] and funnel["attempted"] else None}}
    (out / "eval_report.json").write_text(json.dumps(report, indent=2) + "\n"); (out / "eval_report.md").write_text(render_markdown(report))
    if not args.json_only: print(f"Evaluation complete: must {must_passed}/{must_total}, should {should_passed}/{should_total}; reports: {out}")
    return 0 if must_passed == must_total else 1

if __name__ == "__main__": raise SystemExit(main())
