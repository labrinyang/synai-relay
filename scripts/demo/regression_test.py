#!/usr/bin/env python3
"""
Regression test: verify all API endpoints of the SYNAI Relay Protocol.

Runs against a live server (default: http://localhost:5005).
Override with SYNAI_URL env var.

Usage:
    python scripts/demo/regression_test.py
    SYNAI_URL=http://staging:5005 python scripts/demo/regression_test.py
"""
import os
import sys
import json
import time
import uuid
import requests

BASE_URL = os.getenv("SYNAI_URL", "http://localhost:5005")
results = []
RUN_ID = uuid.uuid4().hex[:8]

# Unique identifiers scoped to this run to avoid conflicts
BOSS_ID = f"boss_{RUN_ID}"
WORKER_ID = f"worker_{RUN_ID}"
WORKER_NAME = f"TestWorker_{RUN_ID}"
ADOPTER_HANDLE = f"@tester_{RUN_ID}"


def step(name, condition, detail=""):
    """Record and print a single test result."""
    status = "PASS" if condition else "FAIL"
    results.append((name, status, detail))
    icon = "+" if condition else "!"
    print(f"  [{icon}] [{len(results):02d}] {name}: {status}  {detail}")
    return condition


def safe_request(method, path, **kwargs):
    """Perform an HTTP request, catching network errors gracefully."""
    url = f"{BASE_URL}{path}"
    try:
        resp = getattr(requests, method)(url, timeout=15, **kwargs)
        return resp
    except requests.ConnectionError:
        return None
    except requests.Timeout:
        return None
    except Exception:
        return None


def main():
    print("=" * 64)
    print(f"  SYNAI RELAY REGRESSION TEST  (run: {RUN_ID})")
    print(f"  Target: {BASE_URL}")
    print("=" * 64)

    # ------------------------------------------------------------------
    # Connectivity pre-check
    # ------------------------------------------------------------------
    print("\n--- Connectivity ---")
    r = safe_request("get", "/health")
    if r is None:
        print(f"  [!] Cannot reach {BASE_URL}. Is the server running?")
        sys.exit(2)

    # ==================================================================
    # 1. Health check
    # ==================================================================
    print("\n--- Basic Endpoints ---")
    r = safe_request("get", "/health")
    step("Health check (GET /health)",
         r is not None and r.status_code == 200,
         f"status={r.status_code if r else 'N/A'}")

    # ==================================================================
    # 2. Landing page
    # ==================================================================
    r = safe_request("get", "/")
    step("Landing page (GET /)",
         r is not None and r.status_code == 200
         and "text/html" in r.headers.get("Content-Type", ""),
         f"status={r.status_code if r else 'N/A'}")

    # ==================================================================
    # 3. Dashboard
    # ==================================================================
    r = safe_request("get", "/dashboard")
    step("Dashboard (GET /dashboard)",
         r is not None and r.status_code == 200
         and "text/html" in r.headers.get("Content-Type", ""),
         f"status={r.status_code if r else 'N/A'}")

    # ==================================================================
    # 4. Register agent (worker)
    # ==================================================================
    print("\n--- Agent Registration ---")
    r = safe_request("post", "/agents/register",
                     json={"agent_id": WORKER_ID, "name": WORKER_NAME})
    reg_ok = (r is not None and r.status_code == 201)
    body = r.json() if reg_ok else {}
    step("Register agent (POST /agents/register)",
         reg_ok and "wallet_address" in body,
         f"status={r.status_code if r else 'N/A'} wallet={'yes' if 'wallet_address' in body else 'no'}")

    # ==================================================================
    # 5. Duplicate register
    # ==================================================================
    r = safe_request("post", "/agents/register",
                     json={"agent_id": WORKER_ID, "name": WORKER_NAME})
    step("Duplicate register (POST /agents/register same id)",
         r is not None and r.status_code == 409,
         f"status={r.status_code if r else 'N/A'}")

    # ==================================================================
    # 6. Get agent profile
    # ==================================================================
    print("\n--- Agent Profile ---")
    r = safe_request("get", f"/agents/{WORKER_ID}")
    profile_ok = (r is not None and r.status_code == 200)
    body = r.json() if profile_ok else {}
    step("Get agent profile (GET /agents/:id)",
         profile_ok and "balance" in body,
         f"status={r.status_code if r else 'N/A'} balance={body.get('balance', '?')}")

    # ==================================================================
    # 7. Get unknown agent
    # ==================================================================
    r = safe_request("get", "/agents/nonexistent_xyz")
    step("Get unknown agent (GET /agents/nonexistent_xyz)",
         r is not None and r.status_code == 404,
         f"status={r.status_code if r else 'N/A'}")

    # ==================================================================
    # 8. Deposit funds
    # ==================================================================
    print("\n--- Deposits ---")
    r = safe_request("post", f"/agents/{WORKER_ID}/deposit",
                     json={"amount": 100})
    deposit_ok = (r is not None and r.status_code == 200)
    body = r.json() if deposit_ok else {}
    step("Deposit funds (POST /agents/:id/deposit)",
         deposit_ok and body.get("status") == "deposited",
         f"status={r.status_code if r else 'N/A'}")

    # Also register and fund the boss so they can post jobs
    safe_request("post", "/agents/register",
                 json={"agent_id": BOSS_ID, "name": f"Boss_{RUN_ID}"})
    safe_request("post", f"/agents/{BOSS_ID}/deposit",
                 json={"amount": 1000})

    # ==================================================================
    # 9. Invalid deposit (negative amount)
    # ==================================================================
    r = safe_request("post", f"/agents/{WORKER_ID}/deposit",
                     json={"amount": -1})
    step("Invalid deposit (amount=-1)",
         r is not None and r.status_code == 400,
         f"status={r.status_code if r else 'N/A'}")

    # ==================================================================
    # 10. Post job
    # ==================================================================
    print("\n--- Job Lifecycle ---")
    r = safe_request("post", "/jobs", json={
        "title": f"Test Task {RUN_ID}",
        "description": "Regression test task",
        "buyer_id": BOSS_ID,
        "terms": {"price": 10},
        "artifact_type": "CODE",
    })
    post_ok = (r is not None and r.status_code == 201)
    body = r.json() if post_ok else {}
    TASK_ID = body.get("task_id", "")
    step("Post job (POST /jobs)",
         post_ok and body.get("status") == "created",
         f"status={r.status_code if r else 'N/A'} task_id={TASK_ID[:12]}...")

    # ==================================================================
    # 11. List jobs
    # ==================================================================
    r = safe_request("get", "/jobs")
    list_ok = (r is not None and r.status_code == 200)
    body = r.json() if list_ok else []
    step("List jobs (GET /jobs)",
         list_ok and isinstance(body, list),
         f"status={r.status_code if r else 'N/A'} count={len(body)}")

    # ==================================================================
    # 12. Filter jobs by status
    # ==================================================================
    r = safe_request("get", "/jobs?status=created")
    filter_ok = (r is not None and r.status_code == 200)
    body = r.json() if filter_ok else []
    all_created = all(j.get("status") == "created" for j in body) if body else True
    step("Filter jobs by status (GET /jobs?status=created)",
         filter_ok and isinstance(body, list) and all_created,
         f"status={r.status_code if r else 'N/A'} count={len(body)}")

    # ==================================================================
    # 13. Get single job
    # ==================================================================
    r = safe_request("get", f"/jobs/{TASK_ID}")
    get_ok = (r is not None and r.status_code == 200)
    body = r.json() if get_ok else {}
    step("Get single job (GET /jobs/:id)",
         get_ok and body.get("task_id") == TASK_ID,
         f"status={r.status_code if r else 'N/A'}")

    # ==================================================================
    # 14. Get unknown job
    # ==================================================================
    r = safe_request("get", "/jobs/nonexistent")
    step("Get unknown job (GET /jobs/nonexistent)",
         r is not None and r.status_code == 404,
         f"status={r.status_code if r else 'N/A'}")

    # ==================================================================
    # 15. Fund job
    # ==================================================================
    print("\n--- Fund / Claim / Submit / Confirm ---")
    r = safe_request("post", f"/jobs/{TASK_ID}/fund",
                     json={"escrow_tx_hash": f"0xfake_{RUN_ID}"})
    fund_ok = (r is not None and r.status_code == 200)
    body = r.json() if fund_ok else {}
    step("Fund job (POST /jobs/:id/fund)",
         fund_ok and body.get("status") == "funded",
         f"status={r.status_code if r else 'N/A'}")

    # ==================================================================
    # 16. Fund already-funded job
    # ==================================================================
    r = safe_request("post", f"/jobs/{TASK_ID}/fund",
                     json={"escrow_tx_hash": f"0xfake2_{RUN_ID}"})
    step("Fund already-funded job (POST /jobs/:id/fund again)",
         r is not None and r.status_code == 400,
         f"status={r.status_code if r else 'N/A'}")

    # ==================================================================
    # 17. Claim job
    # ==================================================================
    r = safe_request("post", f"/jobs/{TASK_ID}/claim",
                     json={"agent_id": WORKER_ID})
    claim_ok = (r is not None and r.status_code == 200)
    body = r.json() if claim_ok else {}
    step("Claim job (POST /jobs/:id/claim)",
         claim_ok and body.get("status") == "claimed",
         f"status={r.status_code if r else 'N/A'}")

    # ==================================================================
    # 18. Claim with unregistered agent (needs a separate funded job)
    # ==================================================================
    r2 = safe_request("post", "/jobs", json={
        "title": f"UnregTest {RUN_ID}", "terms": {"price": 10}, "buyer_id": BOSS_ID})
    unreg_task = r2.json().get("task_id") if r2 and r2.status_code == 201 else None
    if unreg_task:
        safe_request("post", f"/jobs/{unreg_task}/fund",
                     json={"escrow_tx_hash": f"0xunreg_{RUN_ID}"})
    r = safe_request("post", f"/jobs/{unreg_task}/claim",
                     json={"agent_id": f"ghost_{RUN_ID}_unknown"}) if unreg_task else None
    step("Claim with unregistered agent",
         r is not None and r.status_code == 400,
         f"status={r.status_code if r else 'N/A'}")

    # ==================================================================
    # 19. Submit result
    # ==================================================================
    r = safe_request("post", f"/jobs/{TASK_ID}/submit", json={
        "agent_id": WORKER_ID,
        "result": {"code": "print('hello')", "status": "complete"},
    })
    submit_ok = (r is not None and r.status_code == 200)
    body = r.json() if submit_ok else {}
    step("Submit result (POST /jobs/:id/submit)",
         submit_ok and body.get("status") in ("submitted", "settled", "rejected"),
         f"status={r.status_code if r else 'N/A'} job_status={body.get('status', '?')}")

    # ==================================================================
    # 20. Confirm job (manual confirmation)
    # ==================================================================
    # Confirm only works if status == 'submitted' (no auto-verify)
    # If status became 'settled' from auto-verify, we need a fresh task
    r_check = safe_request("get", f"/jobs/{TASK_ID}")
    current_status = r_check.json().get("status") if r_check and r_check.status_code == 200 else ""

    if current_status == "submitted":
        # Confirm the existing task
        r = safe_request("post", f"/jobs/{TASK_ID}/confirm", json={
            "buyer_id": BOSS_ID,
            "signature": f"sig_{RUN_ID}",
        })
        confirm_ok = (r is not None and r.status_code == 200)
        body = r.json() if confirm_ok else {}
        step("Confirm job (POST /jobs/:id/confirm)",
             confirm_ok and body.get("status") == "settled",
             f"status={r.status_code if r else 'N/A'}")
    else:
        # The submit already settled the job via auto-verify. Create a fresh task for confirm.
        r2 = safe_request("post", "/jobs", json={
            "title": f"Confirm Test {RUN_ID}",
            "description": "Task for manual confirm test",
            "buyer_id": BOSS_ID,
            "terms": {"price": 5},
        })
        confirm_task_id = r2.json().get("task_id", "") if r2 and r2.status_code == 201 else ""
        # Fund, claim, submit (no verifiers), then confirm
        safe_request("post", f"/jobs/{confirm_task_id}/fund",
                     json={"escrow_tx_hash": f"0xconfirm_{RUN_ID}"})
        # Need to deposit more to cover stake
        safe_request("post", f"/agents/{WORKER_ID}/deposit", json={"amount": 50})
        safe_request("post", f"/jobs/{confirm_task_id}/claim",
                     json={"agent_id": WORKER_ID})
        safe_request("post", f"/jobs/{confirm_task_id}/submit", json={
            "agent_id": WORKER_ID,
            "result": {"output": "done"},
        })
        r = safe_request("post", f"/jobs/{confirm_task_id}/confirm", json={
            "buyer_id": BOSS_ID,
            "signature": f"sig_{RUN_ID}",
        })
        confirm_ok = (r is not None and r.status_code == 200)
        body = r.json() if confirm_ok else {}
        step("Confirm job (POST /jobs/:id/confirm)",
             confirm_ok and body.get("status") == "settled",
             f"status={r.status_code if r else 'N/A'} (fresh task)")

    # ==================================================================
    # 21. Ledger ranking
    # ==================================================================
    print("\n--- Ledger ---")
    r = safe_request("get", "/ledger/ranking")
    rank_ok = (r is not None and r.status_code == 200)
    body = r.json() if rank_ok else {}
    step("Ledger ranking (GET /ledger/ranking)",
         rank_ok and "agent_ranking" in body,
         f"status={r.status_code if r else 'N/A'}")

    # ==================================================================
    # 22. Agent ledger (balance)
    # ==================================================================
    r = safe_request("get", f"/ledger/{WORKER_ID}")
    ledger_ok = (r is not None and r.status_code == 200)
    body = r.json() if ledger_ok else {}
    step("Agent ledger (GET /ledger/:agent_id)",
         ledger_ok and "balance" in body,
         f"status={r.status_code if r else 'N/A'} balance={body.get('balance', '?')}")

    # ==================================================================
    # 23. Cancel job (new task, pre-claim)
    # ==================================================================
    print("\n--- Cancel / Refund / Verdict / Withdraw ---")
    r = safe_request("post", "/jobs", json={
        "title": f"Cancel Test {RUN_ID}",
        "description": "To be cancelled",
        "buyer_id": BOSS_ID,
        "terms": {"price": 3},
    })
    cancel_task_id = ""
    if r and r.status_code == 201:
        cancel_task_id = r.json().get("task_id", "")

    r = safe_request("post", f"/jobs/{cancel_task_id}/cancel",
                     json={"buyer_id": BOSS_ID})
    cancel_ok = (r is not None and r.status_code == 200)
    body = r.json() if cancel_ok else {}
    step("Cancel job (POST /jobs/:id/cancel)",
         cancel_ok and body.get("status") == "cancelled",
         f"status={r.status_code if r else 'N/A'}")

    # ==================================================================
    # 24. Cancel non-cancellable (settled job)
    # ==================================================================
    # Use the main TASK_ID which should be settled by now
    r = safe_request("post", f"/jobs/{TASK_ID}/cancel",
                     json={"buyer_id": BOSS_ID})
    step("Cancel non-cancellable (settled job)",
         r is not None and r.status_code == 400,
         f"status={r.status_code if r else 'N/A'}")

    # ==================================================================
    # 25. Refund job (post + fund + expire + refund)
    # ==================================================================
    # Create a task with 2-second expiry
    expiry_ts = int(time.time()) + 2  # 2 seconds from now
    r = safe_request("post", "/jobs", json={
        "title": f"Refund Test {RUN_ID}",
        "description": "Short expiry for refund test",
        "buyer_id": BOSS_ID,
        "terms": {"price": 2},
        "expiry": expiry_ts,
    })
    refund_task_id = ""
    if r and r.status_code == 201:
        refund_task_id = r.json().get("task_id", "")

    # Fund it
    safe_request("post", f"/jobs/{refund_task_id}/fund",
                 json={"escrow_tx_hash": f"0xrefund_{RUN_ID}"})

    # Wait for expiry (lazy check triggers on next access)
    print("    ... waiting 3s for task expiry ...")
    time.sleep(3)

    # Access the job to trigger lazy expiry check
    safe_request("get", f"/jobs/{refund_task_id}")

    # Request refund
    r = safe_request("post", f"/jobs/{refund_task_id}/refund",
                     json={"buyer_id": BOSS_ID})
    refund_ok = (r is not None and r.status_code == 200)
    body = r.json() if refund_ok else {}
    step("Refund expired job (POST /jobs/:id/refund)",
         refund_ok and body.get("status") == "refunded",
         f"status={r.status_code if r else 'N/A'}")

    # ==================================================================
    # 26. Verdict (no verdict data)
    # ==================================================================
    # Use a fresh task that has no verdict
    r = safe_request("post", "/jobs", json={
        "title": f"Verdict Test {RUN_ID}",
        "description": "No verdict expected",
        "buyer_id": BOSS_ID,
        "terms": {"price": 1},
    })
    verdict_task_id = ""
    if r and r.status_code == 201:
        verdict_task_id = r.json().get("task_id", "")

    r = safe_request("get", f"/jobs/{verdict_task_id}/verdict")
    step("Verdict (no verdict data)",
         r is not None and r.status_code == 404,
         f"status={r.status_code if r else 'N/A'}")

    # ==================================================================
    # 27. Withdraw (no chain bridge)
    # ==================================================================
    r = safe_request("post", f"/agents/{WORKER_ID}/withdraw",
                     json={"amount": 1})
    step("Withdraw (no chain bridge)",
         r is not None and r.status_code == 503,
         f"status={r.status_code if r else 'N/A'}")

    # ==================================================================
    # 28. Adopt agent
    # ==================================================================
    r = safe_request("post", "/agents/adopt", json={
        "agent_id": WORKER_ID,
        "twitter_handle": ADOPTER_HANDLE,
        "tweet_url": f"https://twitter.com/test/status/{RUN_ID}",
    })
    adopt_ok = (r is not None and r.status_code == 200)
    body = r.json() if adopt_ok else {}
    step("Adopt agent (POST /agents/adopt)",
         adopt_ok and body.get("status") == "success",
         f"status={r.status_code if r else 'N/A'}")

    # ==================================================================
    # Summary
    # ==================================================================
    print("\n" + "=" * 64)
    passed = sum(1 for _, s, _ in results if s == "PASS")
    failed = sum(1 for _, s, _ in results if s == "FAIL")
    print(f"  Results: {passed} passed, {failed} failed, {len(results)} total")
    if failed > 0:
        print("\n  Failed tests:")
        for name, status, detail in results:
            if status == "FAIL":
                print(f"    - {name}  ({detail})")
    print("=" * 64)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
