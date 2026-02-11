#!/usr/bin/env python3
"""
E2E Demo: Reject + Retry Flow
==============================
Tests the rejection, retry, and circuit-breaker flow against a running
relay server at http://localhost:5005.

Scenario A -- Reject then Retry (success):
  1. Register BOSS + WORKER agents
  2. Deposit funds
  3. Post task with webhook verifier (expected_payload = {"status":"ok"})
  4. Fund task
  5. WORKER claims task (stake locked)
  6. WORKER submits BAD result  -> webhook mismatch -> auto-reject
     - failure_count increments, status -> 'rejected', stake returned
  7. WORKER re-claims the rejected task (retry)
  8. WORKER submits GOOD result -> webhook match -> auto-settle
     - payout + stake returned
  9. Verify final balances

Scenario B -- Circuit Breaker (max_retries exhausted):
  1. Post task with max_retries=2
  2. Fund + claim + submit bad (reject #1)
  3. Re-claim + submit bad (reject #2) -> failure_count >= max_retries -> 'expired'
  4. Verify task cannot be claimed again

Scenario C -- Cancel pre-claim task:
  1. Post + fund a task
  2. Boss cancels before any worker claims
  3. Verify status = 'cancelled'
"""

import sys
import uuid
import time
import requests

BASE_URL = "http://localhost:5005"
SUFFIX = uuid.uuid4().hex[:6]
BOSS_ID = f"BOSS_RR_{SUFFIX}"
WORKER_ID = f"WORKER_RR_{SUFFIX}"

passed = 0
failed = 0


def step(label, fn):
    """Run a test step, track pass/fail, print result."""
    global passed, failed
    print(f"\n--- {label} ---")
    try:
        fn()
        passed += 1
        print(f"  [PASS] {label}")
    except AssertionError as e:
        failed += 1
        print(f"  [FAIL] {label}: {e}")
    except Exception as e:
        failed += 1
        print(f"  [ERROR] {label}: {e}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def register(agent_id, name=None):
    r = requests.post(f"{BASE_URL}/agents/register", json={
        "agent_id": agent_id,
        "name": name or agent_id,
    })
    assert r.status_code == 201, f"Register {agent_id} failed: {r.status_code} {r.text}"
    print(f"  Registered {agent_id}")
    return r.json()


def deposit(agent_id, amount):
    r = requests.post(f"{BASE_URL}/agents/{agent_id}/deposit", json={"amount": amount})
    assert r.status_code == 200, f"Deposit failed: {r.status_code} {r.text}"
    print(f"  Deposited {amount} USDC -> {agent_id} (balance: {r.json().get('balance')})")
    return r.json()


def get_balance(agent_id):
    r = requests.get(f"{BASE_URL}/ledger/{agent_id}")
    assert r.status_code == 200, f"Get balance failed: {r.status_code} {r.text}"
    return float(r.json()["balance"])


def get_agent(agent_id):
    r = requests.get(f"{BASE_URL}/agents/{agent_id}")
    assert r.status_code == 200, f"Get agent failed: {r.status_code} {r.text}"
    return r.json()


def post_task(buyer_id, price, max_retries=3, verifiers_config=None, title="Test Task"):
    payload = {
        "title": title,
        "description": f"E2E reject+retry test task ({SUFFIX})",
        "terms": {"price": price},
        "buyer_id": buyer_id,
        "max_retries": max_retries,
        "verifiers_config": verifiers_config if verifiers_config is not None else [],
    }
    r = requests.post(f"{BASE_URL}/jobs", json=payload)
    assert r.status_code == 201, f"Post task failed: {r.status_code} {r.text}"
    task_id = r.json()["task_id"]
    print(f"  Posted task {task_id} (price={price}, max_retries={max_retries})")
    return task_id


def fund_task(task_id):
    tx_hash = f"0x{uuid.uuid4().hex}"
    r = requests.post(f"{BASE_URL}/jobs/{task_id}/fund", json={"escrow_tx_hash": tx_hash})
    assert r.status_code == 200, f"Fund failed: {r.status_code} {r.text}"
    print(f"  Funded task {task_id}")
    return r.json()


def claim_task(task_id, agent_id):
    r = requests.post(f"{BASE_URL}/jobs/{task_id}/claim", json={"agent_id": agent_id})
    return r


def submit_task(task_id, agent_id, result):
    r = requests.post(f"{BASE_URL}/jobs/{task_id}/submit", json={
        "agent_id": agent_id,
        "result": result,
    })
    return r


def get_job(task_id):
    r = requests.get(f"{BASE_URL}/jobs/{task_id}")
    assert r.status_code == 200, f"Get job failed: {r.status_code} {r.text}"
    return r.json()


def cancel_task(task_id, buyer_id):
    r = requests.post(f"{BASE_URL}/jobs/{task_id}/cancel", json={"buyer_id": buyer_id})
    return r


# ---------------------------------------------------------------------------
# Webhook verifier config: expects {"status": "ok"}
# ---------------------------------------------------------------------------
WEBHOOK_VERIFIER = [
    {
        "type": "webhook",
        "weight": 1.0,
        "config": {"expected_payload": {"status": "ok"}},
    }
]

BAD_RESULT = {"webhook_payload": {"status": "bad"}}
GOOD_RESULT = {"webhook_payload": {"status": "ok"}}


# ===================================================================
# SCENARIO A: Reject then Retry (success)
# ===================================================================

task_a_id = None


def a1_register_agents():
    register(BOSS_ID, "Boss-RejectRetry")
    register(WORKER_ID, "Worker-RejectRetry")


def a2_deposit():
    deposit(BOSS_ID, 100)
    deposit(WORKER_ID, 10)
    bal_b = get_balance(BOSS_ID)
    bal_w = get_balance(WORKER_ID)
    assert bal_b == 100.0, f"Boss balance {bal_b} != 100"
    assert bal_w == 10.0, f"Worker balance {bal_w} != 10"


def a3_post_task():
    global task_a_id
    task_a_id = post_task(
        buyer_id=BOSS_ID,
        price=80,
        max_retries=3,
        verifiers_config=WEBHOOK_VERIFIER,
        title="Reject+Retry Task A",
    )


def a4_fund_task():
    fund_task(task_a_id)
    job = get_job(task_a_id)
    assert job["status"] == "funded", f"Expected funded, got {job['status']}"


def a5_claim_task():
    r = claim_task(task_a_id, WORKER_ID)
    assert r.status_code == 200, f"Claim failed: {r.status_code} {r.text}"
    print(f"  Worker {WORKER_ID} claimed task {task_a_id}")
    # Verify stake was locked (5% of 80 = 4 USDC)
    agent = get_agent(WORKER_ID)
    locked = float(agent.get("locked_balance", 0))
    print(f"  Locked balance: {locked}")
    assert locked == 4.0, f"Expected locked 4.0, got {locked}"


def a6_submit_bad_result():
    """Submit bad result -> webhook mismatch -> auto-reject."""
    r = submit_task(task_a_id, WORKER_ID, BAD_RESULT)
    assert r.status_code == 200, f"Submit failed: {r.status_code} {r.text}"
    data = r.json()
    print(f"  Submit response: status={data.get('status')}, settlement={data.get('settlement')}")
    # The submit endpoint auto-settles via _settle_job(success=False)
    # Status should be 'rejected'
    status = data.get("status")
    assert status == "rejected", f"Expected status 'rejected', got '{status}'"
    settlement = data.get("settlement", {})
    assert settlement.get("failure_count") == 1, f"Expected failure_count 1, got {settlement.get('failure_count')}"
    assert settlement.get("payout") == 0, f"Expected payout 0, got {settlement.get('payout')}"
    # Stake should be returned (no penalty on reject)
    assert settlement.get("stake_return") == 4.0, f"Expected stake_return 4.0, got {settlement.get('stake_return')}"
    # Verify worker balance: 10 (initial) - 4 (staked) + 4 (returned) = 10
    agent = get_agent(WORKER_ID)
    bal = float(agent["balance"])
    locked = float(agent.get("locked_balance", 0))
    print(f"  After reject: balance={bal}, locked={locked}")
    assert locked == 0.0, f"Expected locked 0 after reject, got {locked}"
    assert bal == 10.0, f"Expected balance 10.0 after reject, got {bal}"


def a7_retry_claim():
    """Re-claim the rejected task (retry)."""
    job = get_job(task_a_id)
    assert job["status"] == "rejected", f"Expected rejected, got {job['status']}"
    assert job["failure_count"] == 1, f"Expected failure_count 1, got {job['failure_count']}"
    r = claim_task(task_a_id, WORKER_ID)
    assert r.status_code == 200, f"Retry claim failed: {r.status_code} {r.text}"
    print(f"  Worker {WORKER_ID} re-claimed task {task_a_id} (retry)")
    # Verify stake is locked again
    agent = get_agent(WORKER_ID)
    locked = float(agent.get("locked_balance", 0))
    assert locked == 4.0, f"Expected locked 4.0 on retry, got {locked}"


def a8_submit_good_result():
    """Submit correct result -> webhook match -> auto-settle."""
    r = submit_task(task_a_id, WORKER_ID, GOOD_RESULT)
    assert r.status_code == 200, f"Submit failed: {r.status_code} {r.text}"
    data = r.json()
    print(f"  Submit response: status={data.get('status')}, settlement={data.get('settlement')}")
    assert data.get("status") == "settled", f"Expected settled, got {data.get('status')}"
    settlement = data.get("settlement", {})
    # Payout = 80 * 0.80 = 64 USDC
    assert settlement.get("payout") == 64.0, f"Expected payout 64.0, got {settlement.get('payout')}"
    # Fee = 80 * 0.20 = 16 USDC
    assert settlement.get("fee") == 16.0, f"Expected fee 16.0, got {settlement.get('fee')}"
    # Stake returned
    assert settlement.get("stake_return") == 4.0, f"Expected stake_return 4.0, got {settlement.get('stake_return')}"


def a9_verify_balances():
    """Final balance check after successful retry."""
    agent = get_agent(WORKER_ID)
    bal = float(agent["balance"])
    locked = float(agent.get("locked_balance", 0))
    # Worker: 10 (initial) - 4 (stake) + 4 (returned) + 64 (payout) = 74 USDC
    print(f"  Worker final: balance={bal}, locked={locked}")
    assert bal == 74.0, f"Expected worker balance 74.0, got {bal}"
    assert locked == 0.0, f"Expected locked 0 after settlement, got {locked}"
    # Job should be settled
    job = get_job(task_a_id)
    assert job["status"] == "settled", f"Expected settled, got {job['status']}"
    # Reliability metric should have net 0 change (+1 success, -1 from reject)
    # Actually: reject decrements by 1 (from 0 -> 0, clamped), success increments by 1 -> 1
    metrics = agent.get("metrics", {})
    print(f"  Worker metrics: {metrics}")


# ===================================================================
# SCENARIO B: Circuit Breaker (max_retries exhausted)
# ===================================================================

task_b_id = None
WORKER_B_ID = f"WORKER_CB_{SUFFIX}"


def b0_setup():
    register(WORKER_B_ID, "Worker-CircuitBreaker")
    deposit(WORKER_B_ID, 50)


def b1_post_task():
    global task_b_id
    task_b_id = post_task(
        buyer_id=BOSS_ID,
        price=20,
        max_retries=2,
        verifiers_config=WEBHOOK_VERIFIER,
        title="Circuit Breaker Task B",
    )


def b2_fund_task():
    fund_task(task_b_id)


def b3_first_bad_attempt():
    """Claim + submit bad -> reject #1."""
    r = claim_task(task_b_id, WORKER_B_ID)
    assert r.status_code == 200, f"Claim #1 failed: {r.status_code} {r.text}"
    r = submit_task(task_b_id, WORKER_B_ID, BAD_RESULT)
    assert r.status_code == 200, f"Submit #1 failed: {r.status_code} {r.text}"
    data = r.json()
    assert data.get("status") == "rejected", f"Expected rejected, got {data.get('status')}"
    fc = data.get("settlement", {}).get("failure_count", 0)
    assert fc == 1, f"Expected failure_count 1, got {fc}"
    print(f"  Reject #1 OK (failure_count={fc})")


def b4_second_bad_attempt():
    """Re-claim + submit bad -> reject #2 -> max_retries hit -> expired."""
    r = claim_task(task_b_id, WORKER_B_ID)
    assert r.status_code == 200, f"Retry claim failed: {r.status_code} {r.text}"
    r = submit_task(task_b_id, WORKER_B_ID, BAD_RESULT)
    assert r.status_code == 200, f"Submit #2 failed: {r.status_code} {r.text}"
    data = r.json()
    settlement = data.get("settlement", {})
    fc = settlement.get("failure_count", 0)
    status = settlement.get("status", data.get("status"))
    print(f"  Reject #2: failure_count={fc}, status={status}")
    assert fc == 2, f"Expected failure_count 2, got {fc}"
    # settle_reject sets status to 'expired' when failure_count >= max_retries
    assert status == "expired", f"Expected expired, got {status}"


def b5_claim_blocked():
    """Verify that claiming an expired task is rejected."""
    r = claim_task(task_b_id, WORKER_B_ID)
    assert r.status_code in (403, 410), f"Expected 403/410 for expired task, got {r.status_code}"
    print(f"  Claim correctly blocked: {r.status_code} {r.json().get('error', '')}")


def b6_verify_job_expired():
    job = get_job(task_b_id)
    assert job["status"] == "expired", f"Expected expired, got {job['status']}"
    assert job["failure_count"] == 2, f"Expected failure_count 2, got {job['failure_count']}"
    print(f"  Task {task_b_id} confirmed expired (failure_count={job['failure_count']})")


# ===================================================================
# SCENARIO C: Cancel pre-claim task
# ===================================================================

task_c_id = None


def c1_post_and_fund():
    global task_c_id
    task_c_id = post_task(
        buyer_id=BOSS_ID,
        price=30,
        verifiers_config=WEBHOOK_VERIFIER,
        title="Cancel Test Task C",
    )
    fund_task(task_c_id)


def c2_cancel():
    r = cancel_task(task_c_id, BOSS_ID)
    assert r.status_code == 200, f"Cancel failed: {r.status_code} {r.text}"
    data = r.json()
    assert data["status"] == "cancelled", f"Expected cancelled, got {data['status']}"
    print(f"  Task {task_c_id} cancelled by boss")


def c3_claim_blocked():
    """Cancelled tasks cannot be claimed."""
    r = claim_task(task_c_id, WORKER_ID)
    assert r.status_code in (403, 400), f"Expected 403/400 for cancelled task, got {r.status_code}"
    print(f"  Claim correctly blocked: {r.status_code} {r.json().get('error', '')}")


def c4_verify_status():
    job = get_job(task_c_id)
    assert job["status"] == "cancelled", f"Expected cancelled, got {job['status']}"
    print(f"  Task {task_c_id} confirmed cancelled")


# ===================================================================
# MAIN
# ===================================================================

def main():
    global passed, failed
    print("=" * 60)
    print("  E2E REJECT + RETRY DEMO")
    print(f"  Server: {BASE_URL}")
    print(f"  Boss:   {BOSS_ID}")
    print(f"  Worker: {WORKER_ID}")
    print("=" * 60)

    # Health check
    try:
        r = requests.get(f"{BASE_URL}/health", timeout=5)
        assert r.status_code == 200, f"Health check failed: {r.status_code}"
        print(f"\nServer healthy: {r.json()}")
    except Exception as e:
        print(f"\nServer not reachable at {BASE_URL}: {e}")
        print("Start the server with: python server.py")
        sys.exit(1)

    # Scenario A: Reject then Retry (success)
    print("\n" + "=" * 60)
    print("  SCENARIO A: Reject -> Retry -> Settle")
    print("=" * 60)
    step("A1. Register agents", a1_register_agents)
    step("A2. Deposit funds (BOSS=100, WORKER=10)", a2_deposit)
    step("A3. Post task (price=80, max_retries=3, webhook verifier)", a3_post_task)
    step("A4. Fund task", a4_fund_task)
    step("A5. Worker claims task (stake 4 USDC)", a5_claim_task)
    step("A6. Submit BAD result -> auto-reject (failure_count=1)", a6_submit_bad_result)
    step("A7. Worker re-claims rejected task (retry)", a7_retry_claim)
    step("A8. Submit GOOD result -> auto-settle (payout=64)", a8_submit_good_result)
    step("A9. Verify final balances", a9_verify_balances)

    # Scenario B: Circuit Breaker
    print("\n" + "=" * 60)
    print("  SCENARIO B: Circuit Breaker (max_retries=2)")
    print("=" * 60)
    step("B0. Setup worker for circuit breaker test", b0_setup)
    step("B1. Post task (price=20, max_retries=2)", b1_post_task)
    step("B2. Fund task", b2_fund_task)
    step("B3. Attempt #1: claim + submit bad -> reject", b3_first_bad_attempt)
    step("B4. Attempt #2: re-claim + submit bad -> expired", b4_second_bad_attempt)
    step("B5. Verify claim blocked on expired task", b5_claim_blocked)
    step("B6. Verify task status is expired", b6_verify_job_expired)

    # Scenario C: Cancel
    print("\n" + "=" * 60)
    print("  SCENARIO C: Cancel Pre-Claim Task")
    print("=" * 60)
    step("C1. Post + fund task", c1_post_and_fund)
    step("C2. Boss cancels task", c2_cancel)
    step("C3. Verify claim blocked on cancelled task", c3_claim_blocked)
    step("C4. Verify task status is cancelled", c4_verify_status)

    # Summary
    total = passed + failed
    print("\n" + "=" * 60)
    print(f"  SUMMARY: {passed}/{total} steps passed, {failed} failed")
    print("=" * 60)
    if failed:
        print("  RESULT: FAIL")
        sys.exit(1)
    else:
        print("  RESULT: ALL PASSED")
        sys.exit(0)


if __name__ == "__main__":
    main()
