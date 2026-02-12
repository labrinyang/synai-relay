#!/usr/bin/env python3
"""
E2E V2 Multi-Worker: Tests multi-worker competition on a single job.

Two workers claim and submit to the same job. The oracle judges both.
At most one submission should pass (atomic resolve), and the winner takes all.

Usage:
    python scripts/demo/e2e_v2_multiworker.py
"""

import os
import sys
import time
import uuid
import requests

BASE_URL = os.getenv("SYNAI_URL", "http://localhost:5005")
POLL_INTERVAL = 2  # seconds
POLL_TIMEOUT = 120  # seconds

# Unique suffixes to avoid collisions
_uid = uuid.uuid4().hex[:8]
BOSS_ID = f"boss_mw_{_uid}"
WORKER_A_ID = f"worker_a_{_uid}"
WORKER_B_ID = f"worker_b_{_uid}"

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


def poll_submission(submission_id: str) -> dict:
    """Poll a submission until it leaves 'judging' status or timeout."""
    start = time.time()
    while time.time() - start < POLL_TIMEOUT:
        r = requests.get(f"{BASE_URL}/submissions/{submission_id}")
        if r.status_code != 200:
            return {"status": "error", "http_status": r.status_code}
        data = r.json()
        if data.get("status") != "judging":
            return data
        time.sleep(POLL_INTERVAL)
    return {"status": "timeout"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def main():
    global passed, failed

    print(f"{BOLD}SYNAI Relay V2 - E2E Multi-Worker Competition{RESET}")
    print(f"Server:   {BASE_URL}")
    print(f"Boss:     {BOSS_ID}")
    print(f"Worker A: {WORKER_A_ID}")
    print(f"Worker B: {WORKER_B_ID}")

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
    # 1. Register boss + worker_a + worker_b
    # ------------------------------------------------------------------
    section("1. Register Agents")
    for label, agent_id, wallet in [
        ("boss", BOSS_ID, "0xBOSS_MW"),
        ("worker_a", WORKER_A_ID, "0xWORKER_A"),
        ("worker_b", WORKER_B_ID, "0xWORKER_B"),
    ]:
        r = requests.post(f"{BASE_URL}/agents", json={
            "agent_id": agent_id,
            "name": label,
            "wallet_address": wallet,
        })
        check(f"Register {label} returns 201", r.status_code == 201, f"got {r.status_code}")

    # ------------------------------------------------------------------
    # 2. Create + fund job
    # ------------------------------------------------------------------
    section("2. Create + Fund Job")
    r = requests.post(f"{BASE_URL}/jobs", json={
        "title": "Multi-Worker Test",
        "description": "Provide an answer. Two workers will compete.",
        "price": 1.0,
        "buyer_id": BOSS_ID,
    })
    check("Create job returns 201", r.status_code == 201, f"got {r.status_code}")
    task_id = r.json().get("task_id", "")
    check("Got task_id", bool(task_id))
    print(f"    task_id = {task_id}")

    r = requests.post(f"{BASE_URL}/jobs/{task_id}/fund", json={
        "tx_hash": f"0xfake_mw_deposit_{_uid}",
        "buyer_id": BOSS_ID,
    })
    check("Fund job returns 200", r.status_code == 200, f"got {r.status_code}")

    # ------------------------------------------------------------------
    # 3. Both workers claim
    # ------------------------------------------------------------------
    section("3. Both Workers Claim")
    for label, worker_id in [("worker_a", WORKER_A_ID), ("worker_b", WORKER_B_ID)]:
        r = requests.post(f"{BASE_URL}/jobs/{task_id}/claim", json={
            "worker_id": worker_id,
        })
        check(f"{label} claim returns 200", r.status_code == 200, f"got {r.status_code}")

    # ------------------------------------------------------------------
    # 4 & 5. Both workers submit
    # ------------------------------------------------------------------
    section("4. Worker A Submits")
    r = requests.post(f"{BASE_URL}/jobs/{task_id}/submit", json={
        "worker_id": WORKER_A_ID,
        "content": {"answer": "from worker A"},
    })
    check("Worker A submit returns 202", r.status_code == 202, f"got {r.status_code}")
    sub_a_id = r.json().get("submission_id", "")
    check("Got submission_id for A", bool(sub_a_id))
    print(f"    submission_id (A) = {sub_a_id}")

    section("5. Worker B Submits")
    r = requests.post(f"{BASE_URL}/jobs/{task_id}/submit", json={
        "worker_id": WORKER_B_ID,
        "content": {"answer": "from worker B"},
    })
    check("Worker B submit returns 202", r.status_code == 202, f"got {r.status_code}")
    sub_b_id = r.json().get("submission_id", "")
    check("Got submission_id for B", bool(sub_b_id))
    print(f"    submission_id (B) = {sub_b_id}")

    # ------------------------------------------------------------------
    # 6. Poll both submissions until done
    # ------------------------------------------------------------------
    section("6. Poll Both Submissions")
    print(f"    Polling submission A ({sub_a_id[:8]}...) ...")
    data_a = poll_submission(sub_a_id)
    status_a = data_a.get("status", "unknown")
    print(f"    Submission A final status: {status_a}")
    check(
        "Submission A reached terminal status",
        status_a in ("passed", "failed"),
        f"got '{status_a}'",
    )

    print(f"    Polling submission B ({sub_b_id[:8]}...) ...")
    data_b = poll_submission(sub_b_id)
    status_b = data_b.get("status", "unknown")
    print(f"    Submission B final status: {status_b}")
    check(
        "Submission B reached terminal status",
        status_b in ("passed", "failed"),
        f"got '{status_b}'",
    )

    # ------------------------------------------------------------------
    # 7. At most one submission passed (atomic resolve)
    # ------------------------------------------------------------------
    section("7. Verify At-Most-One Winner")
    pass_count = sum(1 for s in (status_a, status_b) if s == "passed")
    check("At most one submission passed", pass_count <= 1, f"got {pass_count} passed")

    # ------------------------------------------------------------------
    # 8. If either passed, verify job is resolved with winner
    # ------------------------------------------------------------------
    section("8. Check Job Resolution")
    r = requests.get(f"{BASE_URL}/jobs/{task_id}")
    check("GET /jobs/<id> returns 200", r.status_code == 200)
    job_data = r.json()
    job_status = job_data.get("status", "")
    winner_id = job_data.get("winner_id")
    print(f"    job status = {job_status}")
    print(f"    winner_id  = {winner_id}")

    if pass_count == 1:
        check("Job is resolved", job_status == "resolved", f"got '{job_status}'")
        check("winner_id is set", winner_id is not None)
        # Verify winner matches the passed submission
        if status_a == "passed":
            check("Winner is worker_a", winner_id == WORKER_A_ID, f"got '{winner_id}'")
        else:
            check("Winner is worker_b", winner_id == WORKER_B_ID, f"got '{winner_id}'")
    else:
        # Both failed -- job should still be funded
        check("Job still funded (no winner)", job_status == "funded", f"got '{job_status}'")

    # ------------------------------------------------------------------
    # 9. List submissions for this task
    # ------------------------------------------------------------------
    section("9. List Submissions for Task")
    r = requests.get(f"{BASE_URL}/jobs/{task_id}/submissions")
    check("GET /jobs/<id>/submissions returns 200", r.status_code == 200)
    subs = r.json()
    check("Response is a list", isinstance(subs, list))
    check("Contains 2 submissions", len(subs) == 2, f"got {len(subs)}")

    worker_ids_in_subs = {s.get("worker_id") for s in subs}
    check("Both workers represented", worker_ids_in_subs == {WORKER_A_ID, WORKER_B_ID})

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
