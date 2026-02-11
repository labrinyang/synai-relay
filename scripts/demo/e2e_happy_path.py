#!/usr/bin/env python3
"""E2E Happy Path: Full task lifecycle test.

Tests the complete lifecycle against a running server:
  register agents -> deposit -> create job -> fund -> claim -> submit -> confirm -> settle

Usage:
    python scripts/demo/e2e_happy_path.py
    SYNAI_URL=https://synai.shop python scripts/demo/e2e_happy_path.py
"""
import os
import sys
import json
import uuid
import time
import requests

BASE_URL = os.getenv("SYNAI_URL", "http://localhost:5005")

results = []


def step(name, condition, detail=""):
    """Record and print a single test assertion."""
    status = "PASS" if condition else "FAIL"
    results.append((name, status, detail))
    icon = "[PASS]" if condition else "[FAIL]"
    print(f"  {icon} {name}: {status}  {detail}")
    return condition


def api(method, path, json_body=None, expected_codes=None):
    """
    Make an HTTP request and return (response, ok).
    If the request raises a network error, returns (None, False).
    """
    url = f"{BASE_URL}{path}"
    expected_codes = expected_codes or [200, 201]
    try:
        resp = requests.request(method, url, json=json_body, timeout=15)
        ok = resp.status_code in expected_codes
        return resp, ok
    except requests.RequestException as exc:
        print(f"    [ERROR] {method} {path} -> {exc}")
        return None, False


def main():
    # Generate a short random suffix to avoid collisions on re-runs
    run_id = uuid.uuid4().hex[:6]
    boss_id = f"BOSS_HP_{run_id}"
    worker_id = f"WORKER_HP_{run_id}"

    print("=" * 60)
    print("E2E HAPPY PATH TEST")
    print(f"  Server : {BASE_URL}")
    print(f"  Boss   : {boss_id}")
    print(f"  Worker : {worker_id}")
    print(f"  Run ID : {run_id}")
    print("=" * 60)

    # ------------------------------------------------------------------
    # 1. Register BOSS agent
    # ------------------------------------------------------------------
    print("\n[Step 1] Register BOSS agent")
    resp, ok = api("POST", "/agents/register", {"agent_id": boss_id, "name": "Boss Happy Path"})
    step("Register BOSS", ok, f"status={resp.status_code if resp else 'N/A'}")
    if not ok:
        print("    Cannot continue without BOSS registration.")
        _print_summary()
        sys.exit(1)

    # ------------------------------------------------------------------
    # 2. Deposit 120 USDC into BOSS
    # ------------------------------------------------------------------
    print("\n[Step 2] Deposit 120 USDC into BOSS")
    resp, ok = api("POST", f"/agents/{boss_id}/deposit", {"amount": 120})
    boss_balance = None
    if ok:
        data = resp.json()
        boss_balance = data.get("balance")
    step("Deposit BOSS 120", ok, f"balance={boss_balance}")

    # ------------------------------------------------------------------
    # 3. Register WORKER agent
    # ------------------------------------------------------------------
    print("\n[Step 3] Register WORKER agent")
    resp, ok = api("POST", "/agents/register", {"agent_id": worker_id, "name": "Worker Happy Path"})
    step("Register WORKER", ok, f"status={resp.status_code if resp else 'N/A'}")
    if not ok:
        print("    Cannot continue without WORKER registration.")
        _print_summary()
        sys.exit(1)

    # ------------------------------------------------------------------
    # 4. Deposit 10 USDC into WORKER
    # ------------------------------------------------------------------
    print("\n[Step 4] Deposit 10 USDC into WORKER")
    resp, ok = api("POST", f"/agents/{worker_id}/deposit", {"amount": 10})
    worker_balance = None
    if ok:
        data = resp.json()
        worker_balance = data.get("balance")
    step("Deposit WORKER 10", ok, f"balance={worker_balance}")

    # ------------------------------------------------------------------
    # 5. Create task (price=100, empty verifiers_config for manual confirm)
    # ------------------------------------------------------------------
    print("\n[Step 5] Create task (price=100, manual confirm)")
    job_payload = {
        "title": "E2E Happy Path Test",
        "description": "Automated happy-path lifecycle test.",
        "terms": {"price": 100},
        "buyer_id": boss_id,
        "verifiers_config": [],
    }
    resp, ok = api("POST", "/jobs", job_payload)
    task_id = None
    if ok:
        task_id = resp.json().get("task_id")
    step("Create task", ok and task_id is not None, f"task_id={task_id}")
    if not task_id:
        print("    Cannot continue without a task_id.")
        _print_summary()
        sys.exit(1)

    # ------------------------------------------------------------------
    # 6. Fund task (off-chain mode)
    # ------------------------------------------------------------------
    print("\n[Step 6] Fund task (off-chain)")
    resp, ok = api("POST", f"/jobs/{task_id}/fund", {"escrow_tx_hash": "0xdemo_happy_path"})
    fund_status = None
    if ok:
        fund_status = resp.json().get("status")
    step("Fund task", ok and fund_status == "funded", f"status={fund_status}")

    # ------------------------------------------------------------------
    # 7. Worker claims task (stakes 5 USDC = 5% of 100)
    # ------------------------------------------------------------------
    print("\n[Step 7] Worker claims task (stake 5 USDC)")
    resp, ok = api("POST", f"/jobs/{task_id}/claim", {"agent_id": worker_id})
    claim_status = None
    if ok:
        claim_status = resp.json().get("status")
    step("Claim task", ok and claim_status == "claimed", f"status={claim_status}")

    # ------------------------------------------------------------------
    # 8. Verify worker locked_balance > 0
    # ------------------------------------------------------------------
    print("\n[Step 8] Verify worker locked_balance > 0")
    resp, ok = api("GET", f"/agents/{worker_id}")
    locked = None
    if ok:
        data = resp.json()
        locked = data.get("locked_balance")
    locked_float = float(locked) if locked is not None else 0.0
    step("Worker locked_balance > 0", ok and locked_float > 0, f"locked_balance={locked}")

    # ------------------------------------------------------------------
    # 9. Worker submits result
    # ------------------------------------------------------------------
    print("\n[Step 9] Worker submits result")
    submit_payload = {
        "agent_id": worker_id,
        "result": {"content": "Hello World", "source": "e2e_test"},
    }
    resp, ok = api("POST", f"/jobs/{task_id}/submit", submit_payload)
    submit_status = None
    if ok:
        submit_status = resp.json().get("status")
    step("Submit result", ok and submit_status == "submitted", f"status={submit_status}")

    # ------------------------------------------------------------------
    # 10. Verify job status is 'submitted' (awaiting manual confirm)
    # ------------------------------------------------------------------
    print("\n[Step 10] Verify job status is 'submitted'")
    resp, ok = api("GET", f"/jobs/{task_id}")
    job_status = None
    if ok:
        job_status = resp.json().get("status")
    step("Job status == submitted", ok and job_status == "submitted", f"status={job_status}")

    # ------------------------------------------------------------------
    # 11. Boss confirms task (triggers settlement)
    # ------------------------------------------------------------------
    print("\n[Step 11] Boss confirms task")
    confirm_payload = {"buyer_id": boss_id, "signature": "e2e_test_sig"}
    resp, ok = api("POST", f"/jobs/{task_id}/confirm", confirm_payload)
    confirm_status = None
    payout = None
    fee = None
    if ok:
        data = resp.json()
        confirm_status = data.get("status")
        payout = data.get("payout")
        fee = data.get("fee")
    step("Confirm task", ok and confirm_status == "settled", f"status={confirm_status}, payout={payout}, fee={fee}")

    # ------------------------------------------------------------------
    # 12. Verify job status is 'settled'
    # ------------------------------------------------------------------
    print("\n[Step 12] Verify job status is 'settled'")
    resp, ok = api("GET", f"/jobs/{task_id}")
    final_job_status = None
    if ok:
        final_job_status = resp.json().get("status")
    step("Job status == settled", ok and final_job_status == "settled", f"status={final_job_status}")

    # ------------------------------------------------------------------
    # 13. Verify worker balance increased
    #     Expected: 10 (deposit) - 5 (stake) + 5 (stake returned) + 80 (payout 80% of 100) = 90
    # ------------------------------------------------------------------
    print("\n[Step 13] Verify worker balance (expected ~90)")
    resp, ok = api("GET", f"/agents/{worker_id}")
    final_balance = None
    final_locked = None
    if ok:
        data = resp.json()
        final_balance = data.get("balance")
        final_locked = data.get("locked_balance")
    balance_val = float(final_balance) if final_balance is not None else -1
    locked_val = float(final_locked) if final_locked is not None else -1
    # Worker should have 90 USDC and 0 locked
    step(
        "Worker balance == 90",
        ok and abs(balance_val - 90.0) < 0.01,
        f"balance={final_balance}, locked={final_locked}",
    )
    step(
        "Worker locked_balance == 0",
        ok and abs(locked_val - 0.0) < 0.01,
        f"locked_balance={final_locked}",
    )

    # ------------------------------------------------------------------
    # 14. Verify ledger ranking endpoint returns data
    # ------------------------------------------------------------------
    print("\n[Step 14] Verify ledger ranking")
    resp, ok = api("GET", "/ledger/ranking")
    has_stats = False
    has_ranking = False
    if ok:
        data = resp.json()
        has_stats = "stats" in data
        has_ranking = "agent_ranking" in data
    step("Ledger ranking has stats", ok and has_stats, "")
    step("Ledger ranking has agent_ranking", ok and has_ranking, "")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    _print_summary()

    all_passed = all(s == "PASS" for _, s, _ in results)
    sys.exit(0 if all_passed else 1)


def _print_summary():
    """Print final results summary."""
    print("\n" + "=" * 60)
    passed = sum(1 for _, s, _ in results if s == "PASS")
    failed = sum(1 for _, s, _ in results if s == "FAIL")
    total = len(results)
    print(f"Results: {passed}/{total} passed, {failed} failed")
    if failed > 0:
        print("\nFailed steps:")
        for name, status, detail in results:
            if status == "FAIL":
                print(f"  - {name}: {detail}")
    print("=" * 60)


if __name__ == "__main__":
    main()
