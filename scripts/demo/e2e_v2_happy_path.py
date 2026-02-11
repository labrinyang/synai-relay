#!/usr/bin/env python3
"""
E2E V2 Happy Path: Full job lifecycle test (off-chain / dev mode).

Tests the complete V2 lifecycle against a running server:
  register agents -> create job -> fund -> claim -> submit -> oracle judge -> check result

Usage:
    python scripts/demo/e2e_v2_happy_path.py
"""

import os
import sys
import time
import uuid
import requests

BASE_URL = os.getenv("SYNAI_URL", "http://localhost:5000")
POLL_INTERVAL = 2  # seconds
POLL_TIMEOUT = 120  # seconds

# Unique suffixes to avoid collisions with previous runs
_uid = uuid.uuid4().hex[:8]
BOSS_ID = f"boss_test_{_uid}"
WORKER_ID = f"worker_test_{_uid}"

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

    print(f"{BOLD}SYNAI Relay V2 - E2E Happy Path{RESET}")
    print(f"Server: {BASE_URL}")
    print(f"Boss:   {BOSS_ID}")
    print(f"Worker: {WORKER_ID}")

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
    check("GET /health returns 200", r.status_code == 200)
    check("Service is synai-relay-v2", r.json().get("service") == "synai-relay-v2")

    # ------------------------------------------------------------------
    # 1. Register boss agent
    # ------------------------------------------------------------------
    section("1. Register Boss Agent")
    r = requests.post(f"{BASE_URL}/agents", json={
        "agent_id": BOSS_ID,
        "name": "Test Boss",
        "wallet_address": "0xBOSS",
    })
    check("POST /agents (boss) returns 201", r.status_code == 201, f"got {r.status_code}")
    check("Response has status=registered", r.json().get("status") == "registered")

    # ------------------------------------------------------------------
    # 2. Register worker agent
    # ------------------------------------------------------------------
    section("2. Register Worker Agent")
    r = requests.post(f"{BASE_URL}/agents", json={
        "agent_id": WORKER_ID,
        "name": "Test Worker",
        "wallet_address": "0xWORKER",
    })
    check("POST /agents (worker) returns 201", r.status_code == 201, f"got {r.status_code}")
    check("Response has status=registered", r.json().get("status") == "registered")

    # ------------------------------------------------------------------
    # 3. Create job
    # ------------------------------------------------------------------
    section("3. Create Job")
    r = requests.post(f"{BASE_URL}/jobs", json={
        "title": "Test Task - Happy Path",
        "description": "Return a greeting message.",
        "price": 1.0,
        "buyer_id": BOSS_ID,
    })
    check("POST /jobs returns 201", r.status_code == 201, f"got {r.status_code}")
    body = r.json()
    task_id = body.get("task_id", "")
    check("Response has task_id", bool(task_id))
    check("Response status=open", body.get("status") == "open")
    print(f"    task_id = {task_id}")

    # ------------------------------------------------------------------
    # 4. Fund job (dev mode -- any tx_hash accepted)
    # ------------------------------------------------------------------
    section("4. Fund Job")
    r = requests.post(f"{BASE_URL}/jobs/{task_id}/fund", json={
        "tx_hash": f"0xfake_deposit_{_uid}",
        "buyer_id": BOSS_ID,
    })
    check("POST /jobs/<id>/fund returns 200", r.status_code == 200, f"got {r.status_code}")
    check("Response status=funded", r.json().get("status") == "funded")

    # ------------------------------------------------------------------
    # 5. Claim job
    # ------------------------------------------------------------------
    section("5. Claim Job")
    r = requests.post(f"{BASE_URL}/jobs/{task_id}/claim", json={
        "worker_id": WORKER_ID,
    })
    check("POST /jobs/<id>/claim returns 200", r.status_code == 200, f"got {r.status_code}")
    check("Response status=claimed", r.json().get("status") == "claimed")

    # ------------------------------------------------------------------
    # 6. Submit work
    # ------------------------------------------------------------------
    section("6. Submit Work")
    r = requests.post(f"{BASE_URL}/jobs/{task_id}/submit", json={
        "worker_id": WORKER_ID,
        "content": {"answer": "Hello World"},
    })
    check("POST /jobs/<id>/submit returns 202", r.status_code == 202, f"got {r.status_code}")
    body = r.json()
    submission_id = body.get("submission_id", "")
    check("Response status=judging", body.get("status") == "judging")
    check("Response has submission_id", bool(submission_id))
    print(f"    submission_id = {submission_id}")

    # ------------------------------------------------------------------
    # 7. Poll submission until oracle finishes
    # ------------------------------------------------------------------
    section("7. Poll Submission (waiting for oracle)")
    start = time.time()
    final_status = "judging"
    while time.time() - start < POLL_TIMEOUT:
        r = requests.get(f"{BASE_URL}/submissions/{submission_id}")
        if r.status_code != 200:
            break
        final_status = r.json().get("status", "")
        if final_status != "judging":
            break
        elapsed = int(time.time() - start)
        print(f"    ... still judging ({elapsed}s)", end="\r")
        time.sleep(POLL_INTERVAL)

    elapsed = round(time.time() - start, 1)
    print(f"    Oracle finished in {elapsed}s                    ")
    check(
        "Submission reached terminal status",
        final_status in ("passed", "failed"),
        f"got '{final_status}'",
    )
    print(f"    final_status = {final_status}")

    # ------------------------------------------------------------------
    # 8. Check final submission details
    # ------------------------------------------------------------------
    section("8. Check Submission Details")
    r = requests.get(f"{BASE_URL}/submissions/{submission_id}")
    check("GET /submissions/<id> returns 200", r.status_code == 200)
    sub_data = r.json()
    check("oracle_score is present", sub_data.get("oracle_score") is not None)
    check("oracle_reason is present", sub_data.get("oracle_reason") is not None)

    # ------------------------------------------------------------------
    # 9. Check job status
    # ------------------------------------------------------------------
    section("9. Check Job Status")
    r = requests.get(f"{BASE_URL}/jobs/{task_id}")
    check("GET /jobs/<id> returns 200", r.status_code == 200)
    job_data = r.json()
    job_status = job_data.get("status", "")
    print(f"    job status = {job_status}")
    if final_status == "passed":
        check("Job resolved (submission passed)", job_status == "resolved")
        check("winner_id is set", job_data.get("winner_id") == WORKER_ID)
    else:
        check("Job still funded (submission failed)", job_status == "funded")

    # ------------------------------------------------------------------
    # 10. Platform deposit-info
    # ------------------------------------------------------------------
    section("10. Platform Deposit Info")
    r = requests.get(f"{BASE_URL}/platform/deposit-info")
    check("GET /platform/deposit-info returns 200", r.status_code == 200)
    info = r.json()
    check("Response has operations_wallet", "operations_wallet" in info)
    check("Response has usdc_contract", "usdc_contract" in info)
    check("Response has chain_connected", "chain_connected" in info)

    # ------------------------------------------------------------------
    # 11. List jobs with status filter
    # ------------------------------------------------------------------
    section("11. List Jobs (status=funded)")
    r = requests.get(f"{BASE_URL}/jobs", params={"status": "funded"})
    check("GET /jobs?status=funded returns 200", r.status_code == 200)
    check("Response is a list", isinstance(r.json(), list))

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
