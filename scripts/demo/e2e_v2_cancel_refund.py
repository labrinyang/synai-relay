#!/usr/bin/env python3
"""
E2E V2 Cancel & Refund: Tests job cancellation and refund flows.

Scenarios:
  A. Cancel an open (unfunded) job -- should succeed.
  B. Attempt to cancel a funded job -- should fail.
  C. Refund a funded job -- requires expired/cancelled state first;
     we test that refund on a funded job returns an error.

Usage:
    python scripts/demo/e2e_v2_cancel_refund.py
"""

import os
import sys
import uuid
import requests

BASE_URL = os.getenv("SYNAI_URL", "http://localhost:5005")

# Unique suffixes to avoid collisions
_uid = uuid.uuid4().hex[:8]
BOSS_ID = f"boss_cr_{_uid}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GREEN = "\033[92m"
RED = "\033[91m"
BOLD = "\033[1m"
RESET = "\033[0m"

passed = 0
failed = 0


def check(label: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  {GREEN}\u2713{RESET} {label}")
    else:
        failed += 1
        msg = f"  {RED}\u2717{RESET} {label}"
        if detail:
            msg += f"  -- {detail}"
        print(msg)


def section(title: str):
    print(f"\n{BOLD}--- {title} ---{RESET}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def main():
    global passed, failed

    print(f"{BOLD}SYNAI Relay V2 - E2E Cancel & Refund{RESET}")
    print(f"Server: {BASE_URL}")
    print(f"Boss:   {BOSS_ID}")

    # ------------------------------------------------------------------
    # 0. Health check
    # ------------------------------------------------------------------
    section("Health Check")
    try:
        r = requests.get(f"{BASE_URL}/health", timeout=5)
    except requests.ConnectionError:
        print(f"\n{RED}ERROR: Cannot connect to server at {BASE_URL}{RESET}")
        print("Make sure the server is running (python server.py)")
        sys.exit(1)
    check("Server is healthy", r.status_code == 200)

    # ------------------------------------------------------------------
    # 1. Register boss
    # ------------------------------------------------------------------
    section("1. Register Boss Agent")
    r = requests.post(f"{BASE_URL}/agents", json={
        "agent_id": BOSS_ID,
        "name": "Cancel Test Boss",
        "wallet_address": "0xBOSS_CR",
    })
    check("Register boss returns 201", r.status_code == 201, f"got {r.status_code}")

    # ==================================================================
    # SCENARIO A: Cancel an open (unfunded) job
    # ==================================================================
    section("2. Create Job (for cancel test)")
    r = requests.post(f"{BASE_URL}/jobs", json={
        "title": "Cancel Test Job",
        "description": "This job will be cancelled before funding.",
        "price": 1.0,
        "buyer_id": BOSS_ID,
    })
    check("Create job returns 201", r.status_code == 201, f"got {r.status_code}")
    cancel_task_id = r.json().get("task_id", "")
    check("Got task_id", bool(cancel_task_id))
    print(f"    task_id = {cancel_task_id}")

    section("3. Cancel Open Job")
    r = requests.post(f"{BASE_URL}/jobs/{cancel_task_id}/cancel", json={
        "buyer_id": BOSS_ID,
    })
    check("POST /jobs/<id>/cancel returns 200", r.status_code == 200, f"got {r.status_code}")
    check("Response status=cancelled", r.json().get("status") == "cancelled")

    # Verify job status via GET
    r = requests.get(f"{BASE_URL}/jobs/{cancel_task_id}")
    check("GET /jobs/<id> returns 200", r.status_code == 200)
    check("Job status is cancelled", r.json().get("status") == "cancelled")

    # ==================================================================
    # SCENARIO B: Create + fund a job, then try to cancel (should fail)
    # ==================================================================
    section("4. Create + Fund Another Job")
    r = requests.post(f"{BASE_URL}/jobs", json={
        "title": "Funded Job (cancel should fail)",
        "description": "This funded job should not be cancellable.",
        "price": 2.0,
        "buyer_id": BOSS_ID,
    })
    check("Create job returns 201", r.status_code == 201, f"got {r.status_code}")
    funded_task_id = r.json().get("task_id", "")
    check("Got task_id", bool(funded_task_id))
    print(f"    task_id = {funded_task_id}")

    section("5. Fund the Job")
    r = requests.post(f"{BASE_URL}/jobs/{funded_task_id}/fund", json={
        "tx_hash": f"0xfake_cr_deposit_{_uid}",
        "buyer_id": BOSS_ID,
    })
    check("Fund returns 200", r.status_code == 200, f"got {r.status_code}")
    check("Response status=funded", r.json().get("status") == "funded")

    section("6. Cancel Funded Job (C2: now allowed)")
    r = requests.post(f"{BASE_URL}/jobs/{funded_task_id}/cancel", json={
        "buyer_id": BOSS_ID,
    })
    check("Cancel funded job returns 200", r.status_code == 200, f"got {r.status_code}")
    check("Response status=cancelled", r.json().get("status") == "cancelled")

    # Verify job status via GET
    r = requests.get(f"{BASE_URL}/jobs/{funded_task_id}")
    check("GET /jobs/<id> returns 200", r.status_code == 200)
    check("Job status is cancelled", r.json().get("status") == "cancelled")

    # ==================================================================
    # SCENARIO C: Refund on the cancelled funded job
    #   Now that the funded job is cancelled, refund should succeed
    # ==================================================================
    section("7. Refund Cancelled (Funded) Job")
    r = requests.post(f"{BASE_URL}/jobs/{funded_task_id}/refund", json={
        "buyer_id": BOSS_ID,
    })
    check("Refund on cancelled funded job returns 200", r.status_code == 200, f"got {r.status_code}")
    refund_data = r.json()
    check("Response has refund info", "status" in refund_data)

    # ==================================================================
    # SCENARIO D: Refund on the already-cancelled (unfunded) job
    #   The cancel_task_id job is cancelled. Even though it was never funded,
    #   the refund endpoint should accept the state (cancelled is valid for
    #   refund), though there's nothing to actually refund on-chain.
    # ==================================================================
    section("8. Refund on Cancelled (Unfunded) Job")
    r = requests.post(f"{BASE_URL}/jobs/{cancel_task_id}/refund", json={
        "buyer_id": BOSS_ID,
    })
    check("Refund on cancelled job returns 200", r.status_code == 200, f"got {r.status_code}")
    refund_data = r.json()
    check("Response status=refunded", refund_data.get("status") == "refunded")
    check("Response includes amount", "amount" in refund_data)

    # ==================================================================
    # SCENARIO E: Verify idempotent state -- cancel an already-cancelled job
    #   should fail because status is now 'cancelled', not 'open'
    # ==================================================================
    section("9. Re-cancel Already Cancelled Job (should fail)")
    r = requests.post(f"{BASE_URL}/jobs/{cancel_task_id}/cancel", json={
        "buyer_id": BOSS_ID,
    })
    check("Re-cancel returns 400", r.status_code == 400, f"got {r.status_code}")

    # ==================================================================
    # SCENARIO F: Ownership enforcement -- someone else tries to cancel
    # ==================================================================
    section("10. Ownership Enforcement")
    # Create a fresh job and try to cancel with wrong buyer_id
    r = requests.post(f"{BASE_URL}/jobs", json={
        "title": "Ownership Test",
        "description": "Only the creator can cancel.",
        "price": 1.0,
        "buyer_id": BOSS_ID,
    })
    own_task_id = r.json().get("task_id", "")
    check("Create job for ownership test", r.status_code == 201)

    r = requests.post(f"{BASE_URL}/jobs/{own_task_id}/cancel", json={
        "buyer_id": "imposter_agent",
    })
    check("Cancel by non-owner returns 403", r.status_code == 403, f"got {r.status_code}")

    # Clean up: cancel it properly
    requests.post(f"{BASE_URL}/jobs/{own_task_id}/cancel", json={"buyer_id": BOSS_ID})

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    total = passed + failed
    print(f"\n{BOLD}{'=' * 50}{RESET}")
    print(f"{BOLD}Results: {passed}/{total} passed{RESET}", end="")
    if failed:
        print(f"  ({RED}{failed} failed{RESET})")
    else:
        print(f"  ({GREEN}all passed{RESET})")
    print()

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
