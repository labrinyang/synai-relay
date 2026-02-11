# V2 Code Audit Findings

> **Audit Date:** 2026-02-12
> **Reviewers:** 8 independent reviewers (2 per area: Security, Design Compliance, Oracle & Services, API & State Machine)
> **Codebase:** feature/v2-refactor branch

## CRITICAL (5 items — fund safety / blocking defects)

### C1: Double Refund — No Idempotency Guard
- **File:** `server.py:604-618`
- **Confirmed by:** 6/8 reviewers
- **Description:** The refund endpoint does not check `job.refund_tx_hash` before issuing a new refund. Combined with C4 (status overwrite), a buyer can call refund repeatedly on the same job, draining the operations wallet.
- **Fix:** Add `if job.refund_tx_hash: return 409 "Already refunded"` before the wallet call.

### C2: Cancel Only Allows `open` State — Design Says `open` OR `funded`
- **File:** `server.py:569`
- **Confirmed by:** 8/8 reviewers
- **Description:** `if job.status != 'open'` rejects cancellation of funded jobs. The design doc (Section 4) explicitly states: "cancelled only from open or funded". A poster who funded a task cannot cancel it, locking funds permanently if no expiry is set.
- **Fix:** Change to `if job.status not in ('open', 'funded')`. For funded jobs, also check no submissions are in `judging` state. If cancelling a funded job, mark pending/judging submissions as `failed`.

### C3: No 50KB Content Size Limit on Submissions
- **File:** `server.py:450-513`
- **Confirmed by:** 6/8 reviewers
- **Description:** Design doc (Section 5) specifies "Max submission content size: 50KB". No validation exists. Attackers can submit multi-MB payloads causing DB bloat, CPU DoS (regex on huge strings), and LLM cost amplification.
- **Fix:** Add `if len(json.dumps(content)) > 50_000: return 400 "Content exceeds 50KB limit"` before creating the Submission.

### C4: Refund Overwrites Status — `expired` Becomes `cancelled`
- **File:** `server.py:618`
- **Confirmed by:** 6/8 reviewers
- **Description:** `job.status = 'cancelled'` is set unconditionally after refund. If the job was `expired`, this overwrites the terminal state. Additionally, since `cancelled` is in the allowed refund states, this enables the double-refund in C1.
- **Fix:** Do not change status on refund. Just record `refund_tx_hash`. The job stays in its current terminal state (expired or cancelled).

### C5: Oracle Thread Has No Exception Handling — Submissions Stuck in `judging` Forever
- **File:** `server.py:43-123`
- **Confirmed by:** 4/8 reviewers
- **Description:** If `_run_oracle` throws any unhandled exception (JSON parse error, network timeout, KeyError), the background thread crashes silently. The submission remains in `judging` status permanently with no recovery mechanism.
- **Fix:** Wrap the entire `_run_oracle` body in `try/except Exception`, catching all errors and setting `sub.status = 'failed'` with `sub.oracle_reason = f"Internal error: {str(e)}"`.

---

## HIGH (10 items — security / correctness)

### H1: Non-Atomic Payout — Partial Payment Risk
- **File:** `wallet_service.py:130-138`
- **Confirmed by:** 4/8 reviewers
- **Description:** `payout()` sends two independent transactions (80% worker, 20% fee). If the first succeeds but the second fails, the worker gets paid but the platform loses the fee. The exception is caught silently in server.py.
- **Fix:** Record `payout_tx_hash` immediately after the first send succeeds (before the fee send). Wrap each transfer separately. Add a recovery mechanism or at least detailed logging for partial failures.

### H2: LLM Guard Fails Open on Any Exception
- **File:** `oracle_guard.py:93-94`
- **Confirmed by:** 4/8 reviewers
- **Description:** If the LLM API call fails (network error, JSON parse error, rate limit), the guard returns `blocked: False`, silently disabling injection protection.
- **Fix:** Change to fail-closed: return `blocked: True` with reason "Guard error — blocked by default" on exception.

### H3: Oracle Guard Regex Bypass via Unicode
- **File:** `oracle_guard.py:12-31`
- **Confirmed by:** 2/8 reviewers
- **Description:** Regex patterns use ASCII only. Bypass via Unicode homoglyphs (Cyrillic letters), zero-width characters, full-width characters.
- **Fix:** Add NFKC normalization + zero-width character stripping before scanning: `text = unicodedata.normalize('NFKC', text)` then strip `\u200b\u200c\u200d\ufeff`.

### H4: `</SUBMISSION>` Delimiter Escape Attack
- **File:** `oracle_prompts.py` (all templates)
- **Confirmed by:** 2/8 reviewers
- **Description:** If user submission contains `</SUBMISSION>`, it can break out of the delimiter sandwich in prompts, injecting instructions that appear outside the user data block.
- **Fix:** Escape or strip `<SUBMISSION>` and `</SUBMISSION>` tags from submission content before inserting into prompts.

### H5: Nonce Race Condition in Concurrent Transactions
- **File:** `wallet_service.py:116`
- **Confirmed by:** 3/8 reviewers
- **Description:** Multiple oracle threads calling `send_usdc` concurrently get the same nonce from `get_transaction_count`, causing one transaction to fail.
- **Fix:** Add `threading.Lock()` around nonce acquisition + transaction submission.

### H6: Concurrent Claim — participants JSON Read-Modify-Write Race
- **File:** `server.py:428-436`
- **Confirmed by:** 2/8 reviewers
- **Description:** Two concurrent claim requests can both read the same participants list, both pass the duplicate check, and one write overwrites the other.
- **Fix:** Use `db.session.execute(select(Job).filter_by(task_id=...).with_for_update())` or wrap in a transaction with proper locking.

### H7: Submissions Not Discarded on Task Resolve
- **File:** `server.py:83-123`
- **Confirmed by:** 4/8 reviewers
- **Description:** Design spec: "When task resolves/cancels, all pending/judging submissions are discarded." After atomic resolve, other in-flight submissions continue processing, wasting LLM resources.
- **Fix:** After `if updated:`, add bulk update: `Submission.query.filter(task_id=..., id!=sub.id, status.in_(['pending','judging'])).update({'status': 'failed'})`.

### H8: `verdict` vs `passed` Field Contradiction
- **File:** `oracle_service.py:142-150` + `server.py:83`
- **Confirmed by:** 2/8 reviewers
- **Description:** If LLM returns `verdict: "RESOLVED"` but `score < pass_threshold`, `_build_result` sets `passed: False` but `verdict: "RESOLVED"`. Server checks `verdict` not `passed`, so it resolves the task incorrectly.
- **Fix:** In `_build_result`, override verdict based on score: if `score < pass_threshold`, force `verdict = "REJECTED"`.

### H9: Duplicate tx_hash Causes Unhandled IntegrityError (500)
- **File:** `server.py:373`
- **Confirmed by:** 1/8 reviewers
- **Description:** `deposit_tx_hash` has a unique constraint. Reusing the same tx_hash for two jobs throws an uncaught `IntegrityError`, returning a raw 500 error.
- **Fix:** Wrap `db.session.commit()` in `try/except IntegrityError` and return 409 "Transaction already used".

### H10: max_submissions Race Condition
- **File:** `server.py:478-479`
- **Confirmed by:** 2/8 reviewers
- **Description:** Count check is non-atomic. Two concurrent submissions can both read count=19 (max=20) and both proceed.
- **Fix:** Use a DB-level check or wrap in a serializable transaction.

---

## MEDIUM (13 items — correctness / robustness)

### M1: Reputation Denominator Wrong — Submissions vs Claims
- **File:** `agent_service.py:37-43`
- **Description:** `total_claims` counts distinct task_ids from Submission table. Design says denominator should be total claims (tasks in participants). Workers who claim but never submit don't hurt reputation.
- **Fix:** Count tasks where agent appears in `Job.participants` as denominator.

### M2: `metrics.reliability` Never Updated
- **File:** `agent_service.py`
- **Description:** Design says `metrics.reliability` incremented on pass, decremented on fail. Never implemented.
- **Fix:** Add reliability update in `update_reputation()`.

### M3: Reputation Only Updated on Pass, Not Fail
- **File:** `server.py:115`
- **Description:** `update_reputation()` only called in the success branch of `_run_oracle`.
- **Fix:** Also call `update_reputation(sub.worker_id)` in the failure branch.

### M4: Dev Mode Deposit Bypass Without Explicit Flag
- **File:** `server.py:370`
- **Description:** When `is_connected()` is False, any tx_hash accepted. No explicit DEV_MODE flag.
- **Fix:** Add `DEV_MODE` env var. In production, reject funding if chain not connected.

### M5: Oracle Steps Fully Exposed in API
- **File:** `server.py:140`
- **Description:** Full oracle_steps (including prompt details) exposed to all callers. Attackers can reverse-engineer guard patterns.
- **Fix:** Filter oracle_steps to only show step name + pass/fail status, not full LLM outputs.

### M6: wallet_address Not Validated on Registration
- **File:** `agent_service.py:11`
- **Description:** No format validation. Invalid address stored, fails at payout time.
- **Fix:** Validate with `Web3.is_address()` or regex `^0x[0-9a-fA-F]{40}$`.

### M7: Step 4 Quality Prompt Missing Description
- **File:** `oracle_prompts.py:82`
- **Description:** STEP4_QUALITY template only includes `{title}`, not `{description}`. Quality assessment misses context.
- **Fix:** Add `{description}` to the template and pass it in oracle_service.py.

### M8: Error Messages Leak Internal Details
- **Files:** `server.py:616`, `server.py:367`, `oracle_guard.py:94`, `wallet_service.py:103`
- **Description:** Raw exception strings returned to client, revealing RPC URLs and internal paths.
- **Fix:** Return generic errors to client, log details server-side.

### M9: list_jobs Doesn't Trigger Expiry Check
- **File:** `job_service.py:31-40`
- **Description:** `list_jobs()` returns expired tasks as `funded`. Only `get_job()` triggers lazy expiry.
- **Fix:** Add expiry check in `list_jobs` or in `to_dict`.

### M10: No HTTP Status Check Before LLM Response Parsing
- **File:** `oracle_service.py:37`
- **Description:** `resp.json()` called without checking status_code. 429/500 responses cause KeyError.
- **Fix:** Add `resp.raise_for_status()` or check `resp.ok` before parsing.

### M11: max_tokens=1000 May Truncate Complex Steps
- **File:** `oracle_service.py:33`
- **Description:** Steps 3 (completeness) and 5 (devil's advocate) may need >1000 tokens for complex rubrics.
- **Fix:** Make max_tokens configurable per step, or increase default for detailed steps.

### M12: JSON contains() Not Portable (SQLite vs PostgreSQL)
- **File:** `job_service.py:39`
- **Description:** `Job.participants.contains(worker_id)` behavior varies by DB engine. May do string-contains in SQLite instead of array-element-contains.
- **Fix:** Load job and check in Python, or use a proper join table.

### M13: No Chain Confirmation Count for Deposit Verification
- **File:** `wallet_service.py:82`
- **Description:** `get_transaction_receipt` doesn't check block confirmations. Recent txs vulnerable to chain reorg.
- **Fix:** Check `current_block - receipt_block >= 12` before accepting deposit.

---

## LOW (12 items — improvements)

| # | Issue | File |
|---|-------|------|
| L1 | Submission skips `pending` → directly `judging` | server.py:495 |
| L2 | Missing endpoints: `POST /agents/adopt`, `GET /ledger/ranking` | server.py |
| L3 | `/platform/deposit-info` missing `chain` + `min_amount` fields | server.py:165 |
| L4 | `to_dict` missing `fee_tx_hash` + `depositor_address` | job_service.py |
| L5 | `debug=True` hardcoded in entrypoint | server.py:636 |
| L6 | No pagination on list endpoints | server.py |
| L7 | `buyer_id` has no FK constraint to agents | models.py:44 |
| L8 | Services not singletons, re-read env vars per call | multiple |
| L9 | Gas limit hardcoded 100k + legacy gasPrice (not EIP-1559) | wallet_service.py |
| L10 | `check_expiry` commits inside static method, breaking caller txn | job_service.py:26 |
| L11 | No crash recovery for `judging` submissions on server restart | server.py |
| L12 | No endpoint authentication (MVP phase) | server.py global |
