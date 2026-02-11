#!/usr/bin/env python3
"""E2E Expiry: Lazy expiry + refund flow test."""
import os, sys, json, time, uuid, requests

BASE_URL = os.getenv("SYNAI_URL", "http://localhost:5005")
results = []


def step(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    results.append((name, status, detail))
    icon = "\u2705" if condition else "\u274c"
    print(f"  {icon} {name}: {status} {detail}")
    return condition


def main():
    suffix = uuid.uuid4().hex[:8]
    boss_id = f"BOSS_EX_{suffix}"

    print("=" * 60)
    print("E2E EXPIRY TEST")
    print("=" * 60)
    print(f"  Server : {BASE_URL}")
    print(f"  Boss ID: {boss_id}")
    print()

    # ------------------------------------------------------------------
    # Step 1: Register the BOSS_EX agent
    # ------------------------------------------------------------------
    print("[1] Register agent")
    r = requests.post(f"{BASE_URL}/agents/register", json={
        "agent_id": boss_id,
        "name": "Expiry Test Boss",
    })
    step("Register agent", r.status_code == 201, f"HTTP {r.status_code}")

    # ------------------------------------------------------------------
    # Step 2: Deposit 50 USDC
    # ------------------------------------------------------------------
    print("[2] Deposit 50 USDC")
    r = requests.post(f"{BASE_URL}/agents/{boss_id}/deposit", json={
        "amount": 50,
    })
    step("Deposit 50 USDC", r.status_code == 200, f"HTTP {r.status_code}")

    # ------------------------------------------------------------------
    # Step 3: Post task with expiry = now + 3 seconds
    # ------------------------------------------------------------------
    expiry_ts = int(time.time()) + 3
    print(f"[3] Post task  (expiry unix={expiry_ts})")
    r = requests.post(f"{BASE_URL}/jobs", json={
        "title": "E2E Expiry Test",
        "description": "Task that should expire in ~3 seconds",
        "buyer_id": boss_id,
        "terms": {"price": 30},
        "verifiers_config": [],
        "expiry": expiry_ts,
    })
    ok = r.status_code == 201
    task_id = r.json().get("task_id") if ok else None
    step("Post task", ok, f"task_id={task_id}")
    if not task_id:
        print("  FATAL: cannot continue without task_id")
        _print_summary()
        sys.exit(1)

    # ------------------------------------------------------------------
    # Step 4: Fund the task
    # ------------------------------------------------------------------
    print("[4] Fund task")
    r = requests.post(f"{BASE_URL}/jobs/{task_id}/fund", json={
        "escrow_tx_hash": "0xdemo_expiry",
    })
    step("Fund task", r.status_code == 200, f"HTTP {r.status_code}")

    # ------------------------------------------------------------------
    # Step 5: Verify status is "funded"
    # ------------------------------------------------------------------
    print("[5] Verify status == funded")
    r = requests.get(f"{BASE_URL}/jobs/{task_id}")
    job = r.json()
    step("Status is funded", job.get("status") == "funded",
         f"status={job.get('status')}")

    # ------------------------------------------------------------------
    # Step 6: Wait for expiry
    # ------------------------------------------------------------------
    wait_secs = 4
    print(f"[6] Sleeping {wait_secs}s for expiry window...")
    time.sleep(wait_secs)

    # ------------------------------------------------------------------
    # Step 7: GET /jobs/:id  -- lazy expiry should transition to "expired"
    # ------------------------------------------------------------------
    print("[7] GET job (lazy expiry check)")
    r = requests.get(f"{BASE_URL}/jobs/{task_id}")
    job = r.json()
    step("Status is expired", job.get("status") == "expired",
         f"status={job.get('status')}")

    # ------------------------------------------------------------------
    # Step 8: POST /jobs/:id/refund -- transition to "refunded"
    # ------------------------------------------------------------------
    print("[8] Refund expired task")
    r = requests.post(f"{BASE_URL}/jobs/{task_id}/refund", json={
        "buyer_id": boss_id,
    })
    ok = r.status_code == 200
    body = r.json()
    step("Refund succeeds", ok, f"HTTP {r.status_code}")
    step("Status is refunded", body.get("status") == "refunded",
         f"status={body.get('status')}")
    step("Refund amount matches", body.get("amount") == 30,
         f"amount={body.get('amount')}")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    _print_summary()


def _print_summary():
    print()
    print("=" * 60)
    passed = sum(1 for _, s, _ in results if s == "PASS")
    total = len(results)
    print(f"Results: {passed}/{total} passed")
    print("=" * 60)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
