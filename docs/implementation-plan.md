# Implementation Plan — Agent Lifecycle Gap Fill

> Phase 4: Detailed implementation specs for all P0 and P1 gaps
> Source: `docs/gap-analysis-final.md`

---

## P0 — Must Fix (7 gaps)

### G01: Authentication (API Key)

**What**: Add API key authentication to all mutating endpoints.

**Interface**:
- `POST /agents` remains unauthenticated (registration is open)
- All other endpoints require `Authorization: Bearer <api_key>` header
- `POST /agents` response returns `api_key` (generated on registration)
- Agent model gains `api_key_hash` column (bcrypt or sha256-hmac)

**Implementation**:
- Add `api_key_hash` column to `Agent` model
- Generate secure random API key on registration, return once, store hash
- Add `@require_auth` decorator checking `Authorization` header
- Apply decorator to: `/jobs` POST, `/jobs/<id>/fund`, `/jobs/<id>/claim`, `/jobs/<id>/submit`, `/jobs/<id>/cancel`, `/jobs/<id>/refund`, all agent-specific endpoints
- Auth middleware resolves `current_agent_id` from API key, replaces body-level `buyer_id`/`worker_id`

**Files**:
- `models.py` — add `api_key_hash` to Agent
- `services/auth_service.py` — new file: key generation, verification, decorator
- `server.py` — apply `@require_auth` decorator to endpoints

**Dependencies**: None (first to implement)

**Test cases**:
- Registration returns api_key
- Valid API key → 200
- Missing/invalid API key → 401
- Key from agent A cannot act as agent B (403 on buyer_id mismatch)

---

### G02: Agent Profile Update

**What**: Add `PATCH /agents/<agent_id>` endpoint.

**Interface**:
- `PATCH /agents/<agent_id>` — update mutable fields
- Request: `{ "name": "...", "wallet_address": "0x..." }`
- Response: `200 { agent profile }`
- Auth: must be the agent themselves

**Implementation**:
- Add route handler in `server.py`
- Validate wallet_address format (same regex as registration)
- Only allow updating: `name`, `wallet_address`
- Reject if agent not found (404)

**Files**: `server.py`
**Dependencies**: G01 (auth needed to verify caller is the agent)

**Test cases**:
- Update wallet_address successfully
- Update name successfully
- Invalid wallet format → 400
- Agent not found → 404
- Cannot update another agent's profile → 403

---

### G03: Job Search / Filtering / Pagination

**What**: Enhance `GET /jobs` with rich query params and pagination.

**Interface**:
- Query params: `status`, `buyer_id`, `worker_id`, `artifact_type`, `min_price`, `max_price`, `sort_by` (created_at|price|expiry), `sort_order` (asc|desc), `limit` (default 50, max 200), `offset` (default 0)
- Response adds: `{ "jobs": [...], "total": N, "limit": 50, "offset": 0 }`

**Implementation**:
- Refactor `JobService.list_jobs()` to build dynamic SQLAlchemy query
- Add DB-level filters for `artifact_type`, price range
- Add sorting support
- Add `LIMIT`/`OFFSET` to query
- Return total count with `db.func.count()`
- Also add pagination to `GET /jobs/<id>/submissions`

**Files**: `services/job_service.py`, `server.py`
**Dependencies**: G09 (indexes needed for performance), G10 (worker_id filter needs join table or index)

**Test cases**:
- Filter by status, buyer_id
- Filter by price range
- Pagination: limit/offset
- Sort by price ascending
- Default limit = 50, max = 200

---

### G04: Event Push (Webhooks)

**What**: Add webhook registration and job status change notifications.

**Interface**:
- `POST /agents/<agent_id>/webhooks` — register a webhook URL
  - Request: `{ "url": "https://...", "events": ["job.resolved", "job.expired", "submission.completed"] }`
  - Response: `201 { "webhook_id": "...", "url": "...", "events": [...] }`
- `GET /agents/<agent_id>/webhooks` — list registered webhooks
- `DELETE /agents/<agent_id>/webhooks/<webhook_id>` — remove webhook
- Webhook payload: `{ "event": "job.resolved", "task_id": "...", "data": {...}, "timestamp": "..." }`

**Implementation**:
- New model: `Webhook(id, agent_id, url, events, secret, active, created_at)`
- On job state change → query matching webhooks → POST to URL in background thread
- Include HMAC signature in `X-Webhook-Signature` header for verification
- Retry with exponential backoff (3 attempts)

**Files**:
- `models.py` — add Webhook model
- `services/webhook_service.py` — new file
- `server.py` — webhook CRUD endpoints + fire-and-forget on state changes

**Dependencies**: G01 (auth for webhook registration)

**Test cases**:
- Register webhook → 201
- Job resolves → webhook fired with correct payload
- Invalid URL → 400
- Delete webhook → 204

---

### G05: Unclaim / Withdraw

**What**: Allow workers to withdraw from claimed tasks.

**Interface**:
- `POST /jobs/<task_id>/unclaim` — worker withdraws
  - Request: `{ "worker_id": "<agent_id>" }`
  - Response: `200 { "status": "unclaimed", "task_id": "...", "worker_id": "..." }`
- Precondition: worker is in `participants[]`, no active `judging` submissions from this worker

**Implementation**:
- New route handler in `server.py`
- Remove worker_id from `participants[]` array
- Check no `judging` submissions from this worker (400 if any active)
- Cancel any `pending` submissions from this worker (set to `failed`)

**Files**: `server.py`
**Dependencies**: G01 (auth)

**Test cases**:
- Unclaim successfully → removed from participants
- Unclaim with judging submission → 400
- Unclaim from task not claimed → 404/400
- Cannot unclaim from resolved task → 400

---

### G06: Payout Failure Handling

**What**: Add explicit payout status tracking and retry mechanism.

**Interface**:
- Job model gains: `payout_status` (pending|success|failed|skipped)
- `POST /admin/jobs/<task_id>/retry-payout` — admin retry for failed payouts
- Worker-facing: `GET /jobs/<task_id>` now shows `payout_status`

**Implementation**:
- Add `payout_status` column to Job model
- In `_run_oracle()`: set `payout_status='pending'` before payout, `'success'` after, `'failed'` on exception, `'skipped'` if worker has no wallet
- Add retry endpoint (authenticated, admin-only for now)
- Also: warn at registration if wallet_address is missing (not blocking, just warning)

**Files**: `models.py`, `server.py`, `services/wallet_service.py`
**Dependencies**: G02 (agents can add wallet after registration)

**Test cases**:
- Successful payout → payout_status='success'
- Failed payout → payout_status='failed'
- Retry payout → success on second attempt
- No wallet → payout_status='skipped'

---

### G07: Oracle Timeout

**What**: Add timeout to oracle evaluation threads.

**Interface**:
- Config: `ORACLE_TIMEOUT_SECONDS` (default 120)
- After timeout: submission set to `failed` with reason "Evaluation timed out"

**Implementation**:
- Use `threading.Timer` or `concurrent.futures.ThreadPoolExecutor` with timeout
- In `_run_oracle()`: wrap LLM calls with per-call timeout (60s already set on requests)
- Add overall evaluation timeout (120s total)
- On timeout: set `submission.status = 'failed'`, `oracle_reason = 'Evaluation timed out'`
- Ensure the timed-out thread doesn't later write stale results (check submission status before commit)

**Files**: `server.py`, `config.py`
**Dependencies**: None

**Test cases**:
- Normal evaluation completes within timeout → passed/failed normally
- Simulated LLM hang → timeout → submission failed with timeout reason
- Timed-out thread doesn't overwrite post-timeout status

---

## P1 — Significant Limitations (17 gaps)

### G08: buyer_id Referential Integrity
- Add FK from `Job.buyer_id` → `Agent.agent_id`
- Validate buyer is registered agent at job creation time
- **Files**: `models.py`, `server.py` (validation in `_create_job()`)

### G09: Database Indexes
- Add indexes on: `jobs.status`, `jobs.buyer_id`, `submissions.task_id`, `submissions.worker_id`
- Add composite index: `submissions.(task_id, worker_id)`
- **Files**: `models.py`

### G10: Participants Join Table
- Replace `Job.participants` JSON array with `JobParticipant` model
- Columns: `id`, `task_id`, `worker_id`, `claimed_at`, `unclaimed_at`
- Migrate existing data (iterate jobs, expand JSON array)
- Update all references in `server.py`, `job_service.py`, `agent_service.py`
- **Files**: `models.py`, `server.py`, `services/job_service.py`, `services/agent_service.py`

### G11: Job Update Endpoint
- Add `PATCH /jobs/<task_id>` — update mutable fields
- Only mutable when status=`open`: title, description, rubric, expiry, max_submissions, max_retries, min_reputation
- When `funded`: only extend expiry (not shorten)
- Auth: buyer only
- **Files**: `server.py`

### G12: Proactive Expiry
- Add background expiry checker (runs every 60s via `threading.Timer` loop)
- Query `Job.status == 'funded' AND Job.expiry < now()`
- Transition to expired, cancel submissions, fire webhooks
- **Files**: `server.py` (startup), `services/job_service.py`

### G13: Rate Limiting
- Add simple in-memory rate limiter (per agent_id, per endpoint group)
- Default: 60 requests/minute for reads, 20/minute for writes
- Return `429 Too Many Requests` when exceeded
- Use `flask-limiter` or simple dict-based token bucket
- **Files**: `server.py` (middleware), `requirements.txt` (if flask-limiter)

### G14: Structured Logging
- Replace all `print()` with `logging` module
- JSON format: `{"timestamp", "level", "event", "task_id", "agent_id", "details"}`
- Add correlation ID per request
- **Files**: All files with `print()` statements

### G15: Migration System
- Add Alembic with Flask-Migrate
- Generate initial migration from current models
- **Files**: `requirements.txt`, new `migrations/` directory

### G16: Submission Content Privacy
- `GET /jobs/<id>/submissions` — only return content for the authenticated worker's own submissions
- Other submissions: show metadata (status, score, attempt) but redact `content`
- Add `GET /submissions?worker_id=<me>` for cross-job query
- **Files**: `server.py`

### G17: Idempotency on POST /jobs
- Accept `Idempotency-Key` header on `POST /jobs`
- Store key in new `IdempotencyKey` model (key, response, created_at, ttl=24h)
- On duplicate key → return cached response
- **Files**: `models.py`, `server.py`

### G18: Background Task Queue (Partial)
- For MVP: add error recovery and monitoring to existing thread model
- Add `ScheduledExecutor` wrapper with: timeout, retry on transient failure, dead letter logging
- Full Celery migration deferred to Phase 2
- **Files**: `server.py`

### G19: Fee Configurability
- Add `fee_bps` column to Job model (default 2000 = 20%)
- Add `fee_bps` param to `POST /jobs` (optional, default from config)
- Update `WalletService.payout()` to use job's fee_bps
- Add `PLATFORM_FEE_BPS` to Config (default 2000)
- **Files**: `models.py`, `config.py`, `server.py`, `services/wallet_service.py`

### G20: Wallet Warning at Registration
- In `POST /agents` response: add `warnings: ["wallet_address not set — payouts will be skipped"]` if wallet is empty
- In `POST /jobs/<id>/claim`: add warning if wallet not set
- **Files**: `server.py`, `services/agent_service.py`

### G21: Operations Wallet Solvency
- Add `GET /platform/solvency` endpoint (admin-only)
- Returns: wallet USDC balance, total outstanding liabilities (funded jobs), solvency ratio
- Add solvency check in `WalletService.payout()` and `refund()` — log warning if balance < 2x current operation
- **Files**: `services/wallet_service.py`, `server.py`

### G22: Overpayment Handling
- In `verify_deposit()`: if amount > expected_amount by > 10%, return warning in response
- Record `deposit_amount` on job for audit trail
- Refund mechanism for excess: add `POST /jobs/<id>/refund-excess` (future)
- **Files**: `models.py` (add `deposit_amount`), `services/wallet_service.py`, `server.py`

### G23: DEV_MODE Safety
- Log prominent `⚠️ DEV_MODE ENABLED` banner on startup
- Add `X-Dev-Mode: true` header on all responses when active
- `/health` includes `dev_mode: true`
- **Files**: `server.py`, `config.py`

### G24: Dispute Resolution (Stub)
- Add `POST /jobs/<task_id>/dispute` — file a dispute (buyer or worker)
- Add `disputes` table: task_id, filed_by, reason, status (open|resolved), resolution
- For MVP: just file and store. Resolution is manual.
- **Files**: `models.py`, `server.py`
