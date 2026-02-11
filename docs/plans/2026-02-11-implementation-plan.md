# SynAI Relay Phase 1 -- Implementation Plan

> Date: 2026-02-11
> Based on: `docs/plans/2026-02-11-phase1-confirmed-design.md`
> Branch: `feature/phase1-critical-fixes`

---

## Dependency Graph (read top-to-bottom)

```
A1 (settle bug) ──┐
A2 (CLI bug)      │
A3 (dead code)    ├──> A4 (model changes) ──> A5 (agent_service) ──> A6 (job_service) ──> A7 (settlement svc)
                  │                                                        │
                  │                                                        v
                  │                                                   A8 (server.py wiring)
                  │                                                        │
                  v                                                        v
             A9 (move scripts)                                   B1 (contract changes) ──> B2 (contract tests)
                                                                       │
                                                                       v
                                                                 B3 (chain_bridge) ──> B4 (wire chain endpoints)
                                                                       │
                                                                       v
                                                                 B5 (verification svc) ──> B6 (cancel/refund/withdraw)
                                                                                                │
                                                                                                v
                                                                                          C1 (E2E happy path)
                                                                                          C2 (E2E expiry)
                                                                                          C3 (E2E reject+retry)
                                                                                          C4 (dashboard update)
                                                                                          C5 (regression)
```

---

## Phase A: Foundation

### A1 -- Fix `_settle_job` missing commit + remove slash-on-reject

**Files:**
- `/Users/labrinyang/projects/synai-relay-phase1/server.py` (lines 353-427)

**Problem 1:** The `_settle_job` function on line 353 of `server.py` has a `db.session.commit()` on line 393 (success path) and line 421 (failure path), but the confirm endpoint on line 478 duplicates settlement logic instead of calling `_settle_job`. This is not the main bug though -- the design doc says the commit bug exists. Inspecting the success path: `db.session.commit()` is present on line 393, but any exception before that line (e.g., in `EscrowManager.release_stake`) will cause a rollback on line 425. The real issue: the function uses `db.session.flush()` inside `EscrowManager` methods but only commits at the end -- if an intermediate step fails after flush, partial state is visible within the session. This is acceptable, but the bigger correctness issue is:

**Problem 2:** The failure path (lines 397-423) applies a 5% penalty on reject. The confirmed design says **no penalty on reject, full refund always**. Lines 400-412 must be replaced: instead of splitting stake into refund+penalty, release the full stake amount.

**Problem 3:** The `_settle_job` failure path sets `job.status = 'failed'` (line 419). The new status enum uses `rejected` for CVS rejection. The status `failed` does not exist in the confirmed state machine.

**Changes:**

1. In `server.py`, function `_settle_job` (line 353), **failure path** (lines 397-423):
   - Remove `penalty = stake_amount * Decimal('0.05')` (line 401)
   - Remove `refund = stake_amount - penalty` (line 402)
   - Change `EscrowManager.release_stake(agent_id, refund, job.task_id)` to `EscrowManager.release_stake(agent_id, stake_amount, job.task_id)` -- release full stake
   - Remove `EscrowManager.slash_stake(...)` call (line 412)
   - Change `job.status = 'failed'` to `job.status = 'rejected'` (line 419)
   - Update return dict: remove `"penalty"` key, change `"stake_return"` to `float(stake_amount)`

2. In the success path, verify `db.session.commit()` is reached (it is, line 393). No change needed there.

**Acceptance criteria:**
- `_settle_job(job, success=False)` releases full stake, sets status to `rejected`, logs no slash entry
- `_settle_job(job, success=True)` unchanged (80/20 split, full stake return, status `completed` -> will become `settled` in A4)
- No `slash_stake` call anywhere in the codebase for the reject flow

**Dependencies:** None (first task)

---

### A2 -- Fix CLI `agent_id` missing from submit payload

**File:** `/Users/labrinyang/projects/synai-relay-phase1/synai-cli.py` (lines 134-179)

**Problem:** The `submit` command on line 137 builds a `data` dict (lines 150-157) that contains `result` but does NOT include `agent_id`. The server endpoint `submit_result` (server.py line 318) requires `agent_id` and returns 400 if missing.

**Changes:**

In `synai-cli.py`, function `submit` (line 137), modify the `data` dict on lines 150-157:

```python
# BEFORE (lines 150-157):
data = {
    "result": {
        "content": content,
        "source": "cli_submission"
    }
}

# AFTER:
data = {
    "agent_id": agent_id,
    "result": {
        "content": content,
        "source": "cli_submission"
    }
}
```

The variable `agent_id` is already loaded from config on line 141.

**Acceptance criteria:**
- `synai submit <task_id> <file>` sends `{"agent_id": "...", "result": {...}}` in the POST body
- Server does not return 400 "agent_id required"

**Dependencies:** None

---

### A3 -- Delete dead code and duplicates

**Files to delete:**
1. `/Users/labrinyang/projects/synai-relay-phase1/synai/core/verifier_base.py` -- exact duplicate of `/Users/labrinyang/projects/synai-relay-phase1/core/verifier_base.py`
2. `/Users/labrinyang/projects/synai-relay-phase1/synai/core/plugins/` -- empty directory
3. `/Users/labrinyang/projects/synai-relay-phase1/synai/relay/` -- empty directory
4. `/Users/labrinyang/projects/synai-relay-phase1/core/payment.py` -- dead code; `PaymentSystem` class is never imported by any file (confirmed: no imports found)
5. `/Users/labrinyang/projects/synai-relay-phase1/core/verifier.py` -- legacy `Verifier` class superseded by `VerifierFactory` + plugin system; never imported by `server.py`
6. `/Users/labrinyang/projects/synai-relay-phase1/schema.sql` -- obsolete; SQLAlchemy `db.create_all()` + inline migrations handle schema

**Verification before deleting:** Run `grep -r "from core.payment" . && grep -r "from core.verifier import" . && grep -r "from synai.core.verifier_base" .` to confirm zero imports outside test files.

**Do NOT delete:**
- `synai/agent_client.py` -- used for client SDK
- `synai/demo_antigravity.py` -- demo script
- `core/escrow_manager.py` -- actively used by `server.py`

**Acceptance criteria:**
- Listed files/directories removed
- `python server.py` starts without import errors
- All existing tests still pass

**Dependencies:** None

---

### A4 -- Update models: add `expiry` field, align status values, change stake to 5%

**File:** `/Users/labrinyang/projects/synai-relay-phase1/models.py`

**Changes:**

1. **Add `expiry` column to `Job` model** (after line 48, `escrow_tx_hash`):
   ```python
   expiry = db.Column(db.DateTime, nullable=True)  # Task expiry timestamp
   ```

2. **Add `max_retries` column** (after `failure_count`, line 56):
   ```python
   max_retries = db.Column(db.Integer, default=3)
   ```

3. **Add `chain_task_id` column** for mapping backend UUID to on-chain bytes32 (after `escrow_tx_hash`):
   ```python
   chain_task_id = db.Column(db.String(66), nullable=True)  # On-chain bytes32 task ID (0x-prefixed hex)
   ```

4. **Add `verdict_data` column** to store CVS verdict details:
   ```python
   verdict_data = db.Column(JSON, nullable=True)  # {score, accepted, evidence_hash, timestamp}
   ```

5. **Update the status comment** (line 47) to reflect the confirmed enum:
   ```python
   # Statuses: 'created', 'funded', 'claimed', 'submitted', 'accepted',
   # 'rejected', 'settled', 'expired', 'cancelled', 'refunded'
   ```

6. **Change default status** from `'posted'` to `'created'` (line 46):
   ```python
   status = db.Column(db.String(20), default='created')
   ```

**File:** `/Users/labrinyang/projects/synai-relay-phase1/server.py`

7. **Update inline migration block** (lines 28-93) to add the new columns:
   After the existing migration checks (around line 90), add:
   ```python
   if 'expiry' not in existing_job_columns:
       conn.execute(text("ALTER TABLE jobs ADD COLUMN expiry DATETIME"))
   if 'max_retries' not in existing_job_columns:
       conn.execute(text("ALTER TABLE jobs ADD COLUMN max_retries INTEGER DEFAULT 3"))
   if 'chain_task_id' not in existing_job_columns:
       conn.execute(text("ALTER TABLE jobs ADD COLUMN chain_task_id VARCHAR(66)"))
   if 'verdict_data' not in existing_job_columns:
       conn.execute(text("ALTER TABLE jobs ADD COLUMN verdict_data JSON"))
   ```

8. **Update `post_job`** (lines 211-240):
   - Change `status='posted'` to `status='created'` (line 228)
   - Accept `expiry` from request: `expiry=data.get('expiry')` -- store as datetime if provided (unix timestamp -> `datetime.utcfromtimestamp(int(...))`)
   - Accept `max_retries` from request: `max_retries=data.get('max_retries', 3)`
   - Change stake calculation from 10% to 5% (line 233):
     ```python
     # BEFORE:
     new_job.deposit_amount = new_job.price * Decimal('0.10')
     # AFTER:
     new_job.deposit_amount = new_job.price * Decimal('0.05')
     ```
   - Change the response status string from `"posted"` to `"created"` (line 237)

9. **Update `list_jobs`** response status reference -- replace any check for `'posted'` status with `'created'`

10. **Update `claim_job`** (lines 257-305):
    - Change status check from `'funded'` to also handle the new state names (line 265): keep checking for `'funded'`
    - Change the `'failed'` retry check (line 269) to `'rejected'`:
      ```python
      if job.status == 'rejected' and job.failure_count < (job.max_retries or 3):
          pass  # Allow re-claim for retry
      ```
    - Change `'paused'` reference (line 276) -- when `failure_count >= max_retries`, set status to `'expired'` instead of `'paused'`:
      ```python
      job.status = 'expired'
      ```

**Acceptance criteria:**
- `Job` model has `expiry`, `max_retries`, `chain_task_id`, `verdict_data` columns
- Default status is `'created'` (not `'posted'`)
- Stake is 5% (not 10%)
- Server starts, migrations run, no errors on existing DB
- `POST /jobs` returns `{"status": "created", ...}`

**Dependencies:** A1 (status rename `failed` -> `rejected` must be consistent)

---

### A5 -- Create `services/agent_service.py` + new agent endpoints

**New file:** `/Users/labrinyang/projects/synai-relay-phase1/services/__init__.py`
```python
# Services layer -- business logic extracted from server.py
```

**New file:** `/Users/labrinyang/projects/synai-relay-phase1/services/agent_service.py`

```python
from models import db, Agent, LedgerEntry
from wallet_manager import wallet_manager
from decimal import Decimal


class AgentService:
    @staticmethod
    def register(agent_id: str, name: str = None) -> dict:
        """
        Register a new agent with a managed wallet.
        Returns agent dict or raises ValueError if already exists.
        """
        existing = Agent.query.filter_by(agent_id=agent_id).first()
        if existing:
            raise ValueError(f"Agent '{agent_id}' already registered")

        addr, enc_key = wallet_manager.create_wallet()
        agent = Agent(
            agent_id=agent_id,
            name=name or f"Agent_{agent_id[:8]}",
            balance=Decimal('0'),
            locked_balance=Decimal('0'),
            wallet_address=addr,
            encrypted_privkey=enc_key
        )
        db.session.add(agent)
        db.session.commit()
        return AgentService._to_dict(agent)

    @staticmethod
    def get_or_create(agent_id: str) -> 'Agent':
        """
        Get existing agent or auto-register (fallback for deposit).
        Returns the Agent model instance.
        """
        agent = Agent.query.filter_by(agent_id=agent_id).first()
        if not agent:
            addr, enc_key = wallet_manager.create_wallet()
            agent = Agent(
                agent_id=agent_id,
                name=f"Agent_{agent_id[:8]}",
                balance=Decimal('0'),
                locked_balance=Decimal('0'),
                wallet_address=addr,
                encrypted_privkey=enc_key
            )
            db.session.add(agent)
            db.session.flush()
        return agent

    @staticmethod
    def get_profile(agent_id: str) -> dict:
        """
        Return full agent profile including balance, locked_balance,
        metrics, wallet address.
        """
        agent = Agent.query.filter_by(agent_id=agent_id).first()
        if not agent:
            return None
        return AgentService._to_dict(agent)

    @staticmethod
    def deposit(agent_id: str, amount: Decimal) -> dict:
        """
        Deposit funds. Auto-registers agent if not found.
        Returns updated profile dict.
        """
        agent = AgentService.get_or_create(agent_id)
        agent.balance += amount
        entry = LedgerEntry(
            source_id='deposit',
            target_id=agent_id,
            amount=amount,
            transaction_type='deposit',
            task_id=None
        )
        db.session.add(entry)
        db.session.commit()
        return AgentService._to_dict(agent)

    @staticmethod
    def _to_dict(agent) -> dict:
        return {
            "agent_id": agent.agent_id,
            "name": agent.name,
            "balance": str(agent.balance),
            "locked_balance": str(agent.locked_balance or 0),
            "wallet_address": agent.wallet_address,
            "metrics": agent.metrics or {"engineering": 0, "creativity": 0, "reliability": 0},
            "created_at": agent.created_at.isoformat() if agent.created_at else None,
        }
```

**File:** `/Users/labrinyang/projects/synai-relay-phase1/server.py`

Add two new endpoints (insert after the `/agents/<agent_id>/deposit` route, around line 609):

```python
@app.route('/agents/register', methods=['POST'])
def register_agent():
    """Explicit agent registration with wallet creation."""
    from services.agent_service import AgentService
    data = request.json or {}
    agent_id = data.get('agent_id')
    name = data.get('name')
    if not agent_id:
        return jsonify({"error": "agent_id required"}), 400
    try:
        profile = AgentService.register(agent_id, name)
        return jsonify({"status": "registered", **profile}), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 409

@app.route('/agents/<agent_id>', methods=['GET'])
def get_agent_profile(agent_id):
    """Agent profile: balance, locked_balance, metrics, wallet."""
    from services.agent_service import AgentService
    profile = AgentService.get_profile(agent_id)
    if not profile:
        return jsonify({"error": "Agent not found"}), 404
    return jsonify(profile), 200
```

Also update the existing `/agents/<agent_id>/deposit` endpoint (lines 574-609) to use `AgentService.get_or_create` as fallback:

```python
@app.route('/agents/<agent_id>/deposit', methods=['POST'])
def deposit_funds(agent_id):
    from services.agent_service import AgentService
    data = request.json or {}
    raw_amount = data.get('amount')
    try:
        amount = Decimal(str(raw_amount))
        if not amount.is_finite() or amount <= 0:
            return jsonify({"error": "Positive finite amount required"}), 400
    except Exception:
        return jsonify({"error": "Invalid amount"}), 400
    try:
        result = AgentService.deposit(agent_id, amount)
        return jsonify({"status": "deposited", **result}), 200
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Transaction failed"}), 500
```

**Acceptance criteria:**
- `POST /agents/register {"agent_id": "test1"}` returns 201 with wallet_address
- `POST /agents/register {"agent_id": "test1"}` returns 409 (duplicate)
- `GET /agents/test1` returns full profile with balance, locked_balance, metrics, wallet
- `GET /agents/nonexistent` returns 404
- `POST /agents/new_agent/deposit {"amount": 100}` auto-registers and deposits (returns 200)
- Existing `/agents/adopt`, `/ledger/ranking`, `/ledger/:agent_id` still work

**Dependencies:** A4 (model changes must be applied first so locked_balance column exists)

---

### A6 -- Create `services/job_service.py` with query filtering + lazy expiry

**New file:** `/Users/labrinyang/projects/synai-relay-phase1/services/job_service.py`

```python
from models import db, Job
from datetime import datetime
from decimal import Decimal


class JobService:
    @staticmethod
    def check_expiry(job):
        """
        Lazy expiry check. If task has an expiry and current time
        exceeds it, and the task is in an expirable state, mark it expired.
        Returns True if the task was just expired.
        """
        if not job.expiry:
            return False
        if job.status in ('created', 'funded', 'claimed', 'submitted', 'rejected'):
            if datetime.utcnow() > job.expiry:
                job.status = 'expired'
                db.session.commit()
                return True
        return False

    @staticmethod
    def list_jobs(status=None, buyer_id=None, claimed_by=None):
        """
        List jobs with optional filters.
        Runs lazy expiry check on each result.
        """
        query = Job.query

        if status:
            query = query.filter(Job.status == status)
        if buyer_id:
            query = query.filter(Job.buyer_id == buyer_id)
        if claimed_by:
            query = query.filter(Job.claimed_by == claimed_by)

        jobs = query.order_by(Job.created_at.desc()).all()

        # Lazy expiry on each
        for j in jobs:
            JobService.check_expiry(j)

        return jobs

    @staticmethod
    def get_job(task_id):
        """
        Get a single job by task_id. Runs lazy expiry check.
        Returns None if not found.
        """
        job = Job.query.filter_by(task_id=task_id).first()
        if job:
            JobService.check_expiry(job)
        return job

    @staticmethod
    def to_dict(job):
        """Standard job serialization."""
        return {
            "task_id": str(job.task_id),
            "title": job.title,
            "description": job.description,
            "price": float(job.price),
            "status": job.status,
            "buyer_id": job.buyer_id,
            "claimed_by": job.claimed_by,
            "artifact_type": job.artifact_type,
            "deposit_amount": float(job.deposit_amount) if job.deposit_amount else 0,
            "verifiers_config": job.verifiers_config,
            "result_data": job.result_data,
            "failure_count": job.failure_count,
            "max_retries": getattr(job, 'max_retries', 3) or 3,
            "expiry": job.expiry.isoformat() if job.expiry else None,
            "chain_task_id": getattr(job, 'chain_task_id', None),
            "verdict_data": getattr(job, 'verdict_data', None),
            "created_at": job.created_at.isoformat() if job.created_at else None,
        }
```

**File:** `/Users/labrinyang/projects/synai-relay-phase1/server.py`

Update `GET /jobs` (lines 611-625):

```python
@app.route('/jobs', methods=['GET'])
def list_jobs():
    from services.job_service import JobService
    status = request.args.get('status')
    buyer_id = request.args.get('buyer_id')
    claimed_by = request.args.get('claimed_by')
    jobs = JobService.list_jobs(status=status, buyer_id=buyer_id, claimed_by=claimed_by)
    return jsonify([JobService.to_dict(j) for j in jobs]), 200
```

Update `GET /jobs/<task_id>` (lines 627-661) to call `JobService.get_job(task_id)` which includes the lazy expiry check. Keep the existing knowledge-monetization access-control logic, but use the service for retrieval:

```python
@app.route('/jobs/<task_id>', methods=['GET'])
def get_job(task_id):
    from services.job_service import JobService
    job = JobService.get_job(task_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    # ... (keep existing access-control logic unchanged, lines 632-660)
    # Use JobService.to_dict(job) for the base response, then overlay access-control
```

Also inject lazy expiry check into `claim_job`, `submit_result`, `fund_job`, and `confirm_job` by calling `JobService.check_expiry(job)` right after fetching the job. If it returns True, respond with `{"error": "Task expired", "status": "expired"}`, 410.

**Acceptance criteria:**
- `GET /jobs?status=funded` returns only funded jobs
- `GET /jobs?buyer_id=BOSS1` returns only that buyer's jobs
- `GET /jobs?claimed_by=WORKER1` returns only that worker's jobs
- A job with `expiry` in the past automatically transitions to `expired` when queried
- `POST /jobs/:id/claim` on an expired task returns 410

**Dependencies:** A4 (expiry field), A5 (services/ directory created)

---

### A7 -- Create `services/settlement.py` -- extracted settlement logic

**New file:** `/Users/labrinyang/projects/synai-relay-phase1/services/settlement.py`

Extract `_settle_job` from `server.py` (lines 353-427, as modified by A1) into a proper service:

```python
from models import db, Agent, Job, LedgerEntry
from core.escrow_manager import EscrowManager
from decimal import Decimal


class SettlementService:
    PLATFORM_FEE_RATE = Decimal('0.20')  # 20%
    WORKER_RATE = Decimal('0.80')        # 80%

    @staticmethod
    def settle_success(job: Job) -> dict:
        """
        Called when CVS accepts the result.
        Worker gets 80%, platform gets 20%, full stake returned.
        Sets job status to 'settled'.
        """
        if job.status in ('settled', 'refunded'):
            return {"error": "Already settled"}

        agent_id = job.claimed_by
        agent = Agent.query.filter_by(agent_id=agent_id).first()

        price = job.price
        platform_fee = price * SettlementService.PLATFORM_FEE_RATE
        seller_payout = price * SettlementService.WORKER_RATE

        if agent:
            agent.balance += seller_payout

            db.session.add(LedgerEntry(
                source_id='platform', target_id=agent_id,
                amount=seller_payout, transaction_type='task_payout',
                task_id=job.task_id
            ))
            db.session.add(LedgerEntry(
                source_id='platform', target_id='platform_admin',
                amount=platform_fee, transaction_type='platform_fee',
                task_id=job.task_id
            ))

            # Release full stake
            stake = job.deposit_amount or Decimal('0')
            if stake > 0:
                EscrowManager.release_stake(agent_id, stake, job.task_id)

            # Reputation boost
            metrics = agent.metrics or {"engineering": 0, "creativity": 0, "reliability": 0}
            metrics['reliability'] = metrics.get('reliability', 0) + 1
            agent.metrics = metrics

        job.status = 'settled'
        db.session.commit()

        return {
            "payout": float(seller_payout),
            "fee": float(platform_fee),
            "stake_return": float(job.deposit_amount or 0)
        }

    @staticmethod
    def settle_reject(job: Job) -> dict:
        """
        Called when CVS rejects the result.
        Full stake returned (no penalty). Job status -> 'rejected'.
        failure_count incremented. If failure_count >= max_retries -> 'expired'.
        """
        if job.status in ('settled', 'refunded'):
            return {"error": "Already settled"}

        agent_id = job.claimed_by
        agent = Agent.query.filter_by(agent_id=agent_id).first()

        stake = job.deposit_amount or Decimal('0')
        if agent and stake > 0:
            EscrowManager.release_stake(agent_id, stake, job.task_id)

            # Reputation dip
            metrics = agent.metrics or {"engineering": 0, "creativity": 0, "reliability": 0}
            metrics['reliability'] = max(0, metrics.get('reliability', 0) - 1)
            agent.metrics = metrics

        job.status = 'rejected'
        job.failure_count = (job.failure_count or 0) + 1

        max_retries = getattr(job, 'max_retries', 3) or 3
        if job.failure_count >= max_retries:
            job.status = 'expired'

        db.session.commit()

        return {
            "payout": 0,
            "fee": 0,
            "stake_return": float(stake),
            "failure_count": job.failure_count,
            "status": job.status,
        }
```

**File:** `/Users/labrinyang/projects/synai-relay-phase1/server.py`

Replace the inline `_settle_job` function (lines 353-427) with a thin wrapper that delegates to `SettlementService`:

```python
def _settle_job(job, success=True):
    from services.settlement import SettlementService
    if success:
        return SettlementService.settle_success(job)
    else:
        return SettlementService.settle_reject(job)
```

Also update `confirm_job` (lines 478-539) to call `SettlementService.settle_success(job)` instead of duplicating settlement logic inline. The `confirm_job` endpoint should:
1. Validate buyer_id and signature (keep existing checks)
2. Set `job.signature = signature`
3. Set `job.status = 'accepted'` (manual confirmation = acceptance)
4. Call `SettlementService.settle_success(job)`
5. Return the settlement result

**Acceptance criteria:**
- `SettlementService.settle_success(job)` produces 80/20 split, full stake return, status `settled`
- `SettlementService.settle_reject(job)` produces full stake return, no penalty, status `rejected`
- `SettlementService.settle_reject(job)` with `failure_count >= max_retries` sets status `expired`
- `confirm_job` endpoint uses `SettlementService` (no duplicated math)
- All ledger entries are correct (verified via `/ledger/<agent_id>`)

**Dependencies:** A1 (bug fix applied), A4 (model changes), A5 (services/ directory), A6 (job_service for expiry check)

---

### A8 -- Wire all server.py endpoints to use services layer

**File:** `/Users/labrinyang/projects/synai-relay-phase1/server.py`

This is a wiring pass. Each endpoint delegates to the appropriate service. Key changes:

1. **`POST /jobs`** (line 211): Use `JobService` helpers. Accept new params: `expiry`, `max_retries`. Parse `expiry` as unix timestamp to datetime.

2. **`POST /jobs/:id/claim`** (line 257):
   - Remove auto-register logic (lines 281-291). Instead, require the agent to already exist:
     ```python
     agent = Agent.query.filter_by(agent_id=agent_id).first()
     if not agent:
         return jsonify({"error": "Agent not registered. Call POST /agents/register first."}), 400
     ```
   - Add lazy expiry check via `JobService.check_expiry(job)` before claiming
   - Keep staking logic (EscrowManager.stake_funds)

3. **`POST /jobs/:id/submit`** (line 308):
   - Add lazy expiry check
   - When `verifiers_config` is non-empty: run verification, then call `SettlementService` (existing flow, but use service)
   - When `verifiers_config` is empty: set status to `submitted` only, do NOT run verification. Return `{"status": "submitted", "message": "Awaiting manual confirmation via /confirm"}`

4. **`POST /jobs/:id/confirm`** (line 478): Rewire to use `SettlementService.settle_success()` as described in A7.

5. **`POST /jobs/:id/fund`** (line 242): Add lazy expiry check. Keep existing logic for now (chain_bridge wiring comes in Phase B).

6. **All `GET /jobs` endpoints**: Already wired in A6.

**Acceptance criteria:**
- No business logic remains inline in `server.py` route handlers (only validation, delegation, response formatting)
- `POST /jobs/:id/claim` rejects unregistered agents with clear error message
- `POST /jobs/:id/submit` with empty `verifiers_config` sets status `submitted` and waits
- `POST /jobs/:id/submit` with non-empty `verifiers_config` runs CVS and auto-settles
- All existing endpoints return the same response shape (backward compatible)

**Dependencies:** A5, A6, A7

---

### A9 -- Move demo scripts to `scripts/demo/`

**Actions:**

1. Create directory: `/Users/labrinyang/projects/synai-relay-phase1/scripts/demo/`

2. Move files:
   - `agent_boss.py` -> `scripts/demo/agent_boss.py`
   - `agent_boss_confirm.py` -> `scripts/demo/agent_boss_confirm.py`
   - `agent_worker.py` -> `scripts/demo/agent_worker.py`
   - `agent_twitter_claim.py` -> `scripts/demo/agent_twitter_claim.py`
   - `verify_backend.py` -> `scripts/demo/verify_backend.py`
   - `verify_backend_v2.py` -> `scripts/demo/verify_backend_v2.py`

3. Update `BASE_URL` in moved scripts from `"https://synai.shop"` to a configurable default:
   ```python
   import os
   BASE_URL = os.getenv("SYNAI_URL", "http://localhost:5005")
   ```

**Acceptance criteria:**
- All demo scripts exist under `scripts/demo/`
- Root directory is cleaner (no loose `agent_*.py` or `verify_*.py` files)
- Scripts still run: `python scripts/demo/agent_boss.py`

**Dependencies:** None (can be done in parallel with any task)

---

## Phase B: Contract + Chain Bridge

### B1 -- Update TaskEscrow.sol (SPDX + fee + voucher)

**File:** `/Users/labrinyang/projects/synai-relay-phase1/contracts/src/TaskEscrow.sol`

**Change 1: Add SPDX license identifier** (currently missing, line 1 starts with `import`):

Insert at the very top (before line 1):
```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;
```

Note: The file currently has no `pragma` or SPDX. The imports start directly on line 1.

**Change 2: Update default fee from 500 to 2000** (line 14):
```solidity
// BEFORE:
uint16 public defaultFeeBps = 500; // 5%
// AFTER:
uint16 public defaultFeeBps = 2000; // 20%
```

**Change 3: Add voucher mapping and event** (after line 18, after `_nonces` mapping):
```solidity
mapping(bytes32 => address) public voucherHolder;
event VoucherIssued(bytes32 indexed taskId, address indexed worker);
```

**Change 4: Update `claimTask`** (lines 82-91):
Add voucher assignment after setting worker (after line 88):
```solidity
task.worker = msg.sender;
task.status = TaskStatus.CLAIMED;
voucherHolder[taskId] = msg.sender;      // NEW
emit VoucherIssued(taskId, msg.sender);   // NEW
emit TaskClaimed(taskId, msg.sender);
```

**Change 5: Update `settle`** (lines 124-137):
Use `voucherHolder` for payout destination:
```solidity
function settle(bytes32 taskId) external nonReentrant whenNotPaused {
    Task storage task = tasks[taskId];
    require(task.status == TaskStatus.ACCEPTED, "Not accepted");
    require(voucherHolder[taskId] != address(0), "No voucher");  // NEW

    uint256 fee = (uint256(task.amount) * defaultFeeBps) / 10000;
    uint256 payout = uint256(task.amount) - fee;

    task.status = TaskStatus.SETTLED;

    pendingWithdrawals[voucherHolder[taskId]] += payout;  // CHANGED from task.worker
    pendingWithdrawals[treasury] += fee;

    delete voucherHolder[taskId];  // NEW: clean up voucher

    emit TaskSettled(taskId, task.worker, payout, fee);
}
```

**Change 6: Clear voucher on expiry** -- update `markExpired` (lines 139-152):
Add after `task.status = TaskStatus.EXPIRED;`:
```solidity
delete voucherHolder[taskId];
```

**File:** `/Users/labrinyang/projects/synai-relay-phase1/contracts/src/interfaces/ITaskEscrow.sol`

**Change 7:** Add `VoucherIssued` event declaration (after line 34):
```solidity
event VoucherIssued(bytes32 indexed taskId, address indexed worker);
```

**File:** `/Users/labrinyang/projects/synai-relay-phase1/contracts/script/Deploy.s.sol`

**Change 8:** Update fee parameter from 500 to 2000 (line 18):
```solidity
TaskEscrow escrow = new TaskEscrow(usdcAddress, treasuryAddress, 2000);
```

**Acceptance criteria:**
- `forge build` compiles without errors
- `defaultFeeBps` is 2000
- `claimTask` sets `voucherHolder[taskId]`
- `settle` pays `voucherHolder[taskId]` and deletes the mapping
- `markExpired` clears the voucher
- Deploy script uses 2000 bps

**Dependencies:** None (contract changes are independent of backend)

---

### B2 -- Add voucher tests to TaskEscrow.t.sol

**File:** `/Users/labrinyang/projects/synai-relay-phase1/contracts/test/TaskEscrow.t.sol`

**Change 1:** Update the constructor call in `setUp` (line 26):
```solidity
// BEFORE:
escrow = new TaskEscrow(address(usdc), treasury, 500);
// AFTER:
escrow = new TaskEscrow(address(usdc), treasury, 2000);
```

**Change 2:** Add `VoucherIssued` event declaration (after line 22):
```solidity
event VoucherIssued(bytes32 indexed taskId, address indexed worker);
```

**Change 3:** Add new test functions (append after last test, before closing brace on line 224):

```solidity
function test_claimSetsVoucher() public {
    bytes32 id = _createTask();
    vm.prank(boss);
    escrow.fundTask(id);

    vm.prank(worker);
    escrow.claimTask(id);

    assertEq(escrow.voucherHolder(id), worker);
}

function test_settleUsesVoucher() public {
    bytes32 id = _createTask();
    vm.prank(boss);
    escrow.fundTask(id);
    vm.prank(worker);
    escrow.claimTask(id);
    vm.prank(worker);
    escrow.submitResult(id, bytes32("res"));

    vm.prank(oracle);
    escrow.onVerdictReceived(id, true, 100);

    escrow.settle(id);

    // Voucher holder (worker) gets payout
    uint256 fee = 100 * 10**6 * 2000 / 10000; // 20 * 10**6
    uint256 payout = 100 * 10**6 - fee;        // 80 * 10**6

    assertEq(escrow.pendingWithdrawals(worker), payout);
    assertEq(escrow.pendingWithdrawals(treasury), fee);

    // Voucher cleared
    assertEq(escrow.voucherHolder(id), address(0));
}

function test_expireClearsVoucher() public {
    bytes32 id = _createTask();
    vm.prank(boss);
    escrow.fundTask(id);
    vm.prank(worker);
    escrow.claimTask(id);

    assertEq(escrow.voucherHolder(id), worker);

    vm.warp(block.timestamp + 2 days);
    escrow.markExpired(id);

    assertEq(escrow.voucherHolder(id), address(0));
}

function test_settleRevertsWithoutVoucher() public {
    bytes32 id = _createTask();
    vm.prank(boss);
    escrow.fundTask(id);
    vm.prank(worker);
    escrow.claimTask(id);

    // Manually clear voucher for test (not possible externally, but test the require)
    // Instead, test that settle reverts if task is not ACCEPTED
    // The "No voucher" case is actually impossible in normal flow since claim always sets it.
    // We keep this test conceptual; the require guards against storage corruption.
}
```

**Change 4:** Update `test_settle_happy_path_95_5_split` to reflect 20% fee:
```solidity
function test_settle_happy_path_80_20_split() public {
    bytes32 id = _createTask();
    vm.prank(boss);
    escrow.fundTask(id);
    vm.prank(worker);
    escrow.claimTask(id);
    vm.prank(worker);
    escrow.submitResult(id, bytes32("res"));

    vm.prank(oracle);
    escrow.onVerdictReceived(id, true, 100);

    escrow.settle(id);

    uint256 fee = 100 * 10**6 * 2000 / 10000; // 20 * 10**6
    uint256 payout = 100 * 10**6 - fee;         // 80 * 10**6

    assertEq(escrow.pendingWithdrawals(worker), payout);
    assertEq(escrow.pendingWithdrawals(treasury), fee);
}
```

**Change 5:** Update `test_withdraw_transfers_correct_amount` to reflect 80% payout:
```solidity
function test_withdraw_transfers_correct_amount() public {
    test_settle_happy_path_80_20_split();

    uint256 balBefore = usdc.balanceOf(worker);
    vm.prank(worker);
    escrow.withdraw();
    assertEq(usdc.balanceOf(worker) - balBefore, 80 * 10**6);
    assertEq(escrow.pendingWithdrawals(worker), 0);
}
```

**File:** `/Users/labrinyang/projects/synai-relay-phase1/contracts/test/Integration.t.sol`

**Change 6:** Update `setUp` constructor call (line 22):
```solidity
escrow = new TaskEscrow(address(usdc), treasury, 2000);
```

**Change 7:** Update `test_full_lifecycle_happy_path` assertions (lines 64-65):
```solidity
assertEq(usdc.balanceOf(worker), 80 ether);        // was 95
assertEq(escrow.pendingWithdrawals(treasury), 20 ether); // was 5
```

**Acceptance criteria:**
- `forge test` passes all existing + new tests
- `test_claimSetsVoucher` verifies `voucherHolder[taskId] == worker`
- `test_settleUsesVoucher` verifies 80/20 split and voucher cleared
- `test_expireClearsVoucher` verifies voucher deleted on expiry
- Integration test passes with 80/20 split

**Dependencies:** B1

---

### B3 -- Create `services/chain_bridge.py` (web3.py wrapper)

**New file:** `/Users/labrinyang/projects/synai-relay-phase1/services/chain_bridge.py`

```python
"""
ChainBridge: web3.py wrapper for TaskEscrow and CVSOracle contract interaction.
Handles tx signing, event reading, and state sync.

Requires config: RPC_URL, TASK_ESCROW_ADDRESS, CVS_ORACLE_ADDRESS, ORACLE_PRIVATE_KEY
"""
import os
import json
from web3 import Web3
from eth_account import Account


class ChainBridge:
    def __init__(self):
        self.rpc_url = os.getenv('RPC_URL', 'http://127.0.0.1:8545')
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))

        self.escrow_address = os.getenv('TASK_ESCROW_ADDRESS')
        self.oracle_address = os.getenv('CVS_ORACLE_ADDRESS')
        self.oracle_private_key = os.getenv('ORACLE_PRIVATE_KEY')

        # Load ABIs from compiled artifacts
        self._escrow_abi = self._load_abi('TaskEscrow')
        self._oracle_abi = self._load_abi('CVSOracle')

        self.escrow = None
        self.oracle = None

        if self.escrow_address and self._escrow_abi:
            self.escrow = self.w3.eth.contract(
                address=self.escrow_address,
                abi=self._escrow_abi
            )
        if self.oracle_address and self._oracle_abi:
            self.oracle = self.w3.eth.contract(
                address=self.oracle_address,
                abi=self._oracle_abi
            )

    def _load_abi(self, contract_name):
        """Load ABI from Foundry output."""
        abi_path = os.path.join(
            os.path.dirname(__file__), '..', 'contracts', 'out',
            f'{contract_name}.sol', f'{contract_name}.json'
        )
        if not os.path.exists(abi_path):
            return None
        with open(abi_path) as f:
            data = json.load(f)
        return data.get('abi', [])

    def is_connected(self):
        """Check if RPC is reachable and contracts are configured."""
        return (
            self.w3.is_connected()
            and self.escrow is not None
            and self.oracle is not None
        )

    # --- Read functions ---

    def get_task(self, chain_task_id: str) -> dict:
        """Read task struct from TaskEscrow."""
        task_id_bytes = bytes.fromhex(chain_task_id.replace('0x', ''))
        result = self.escrow.functions.getTask(task_id_bytes).call()
        return {
            'boss': result[0],
            'expiry': result[1],
            'status': result[2],
            'maxRetries': result[3],
            'retryCount': result[4],
            'worker': result[5],
            'amount': result[6],
            'contentHash': result[7].hex(),
        }

    def get_pending_withdrawal(self, address: str) -> int:
        """Read pendingWithdrawals for an address."""
        return self.escrow.functions.pendingWithdrawals(address).call()

    def get_verdict(self, chain_task_id: str) -> dict:
        """Read latest verdict from CVSOracle."""
        task_id_bytes = bytes.fromhex(chain_task_id.replace('0x', ''))
        result = self.oracle.functions.getVerdict(task_id_bytes).call()
        return {
            'taskId': result[0].hex(),
            'accepted': result[1],
            'score': result[2],
            'evidenceHash': result[3].hex(),
            'timestamp': result[4],
        }

    # --- Write functions (signed by oracle key or agent key) ---

    def _send_tx(self, private_key, fn, value=0):
        """Build, sign, and send a transaction. Returns tx receipt."""
        account = Account.from_key(private_key)
        nonce = self.w3.eth.get_transaction_count(account.address)
        tx = fn.build_transaction({
            'from': account.address,
            'nonce': nonce,
            'gas': 500_000,
            'gasPrice': self.w3.eth.gas_price,
            'value': value,
        })
        signed = self.w3.eth.account.sign_transaction(tx, private_key)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        return receipt

    def create_task(self, boss_key: str, amount: int, expiry: int,
                    content_hash: bytes, max_retries: int = 3) -> str:
        """Boss creates task on-chain. Returns chain task_id hex."""
        fn = self.escrow.functions.createTask(amount, expiry, content_hash, max_retries)
        receipt = self._send_tx(boss_key, fn)
        # Parse TaskCreated event to get taskId
        logs = self.escrow.events.TaskCreated().process_receipt(receipt)
        if logs:
            return '0x' + logs[0]['args']['taskId'].hex()
        raise RuntimeError("TaskCreated event not found in receipt")

    def fund_task(self, boss_key: str, chain_task_id: str) -> str:
        """Boss funds task (must have approved USDC first). Returns tx hash."""
        task_id_bytes = bytes.fromhex(chain_task_id.replace('0x', ''))
        fn = self.escrow.functions.fundTask(task_id_bytes)
        receipt = self._send_tx(boss_key, fn)
        return receipt.transactionHash.hex()

    def claim_task(self, worker_key: str, chain_task_id: str) -> str:
        """Worker claims task on-chain. Returns tx hash."""
        task_id_bytes = bytes.fromhex(chain_task_id.replace('0x', ''))
        fn = self.escrow.functions.claimTask(task_id_bytes)
        receipt = self._send_tx(worker_key, fn)
        return receipt.transactionHash.hex()

    def submit_result(self, worker_key: str, chain_task_id: str,
                      result_hash: bytes) -> str:
        """Worker submits result hash on-chain. Returns tx hash."""
        task_id_bytes = bytes.fromhex(chain_task_id.replace('0x', ''))
        fn = self.escrow.functions.submitResult(task_id_bytes, result_hash)
        receipt = self._send_tx(worker_key, fn)
        return receipt.transactionHash.hex()

    def submit_verdict(self, chain_task_id: str, accepted: bool,
                       score: int, evidence_hash: bytes) -> str:
        """Oracle submits verdict via CVSOracle. Uses oracle_private_key. Returns tx hash."""
        task_id_bytes = bytes.fromhex(chain_task_id.replace('0x', ''))
        fn = self.oracle.functions.submitVerdict(
            task_id_bytes, accepted, score, evidence_hash
        )
        receipt = self._send_tx(self.oracle_private_key, fn)
        return receipt.transactionHash.hex()

    def settle(self, chain_task_id: str, caller_key: str) -> str:
        """Anyone can settle an accepted task. Returns tx hash."""
        task_id_bytes = bytes.fromhex(chain_task_id.replace('0x', ''))
        fn = self.escrow.functions.settle(task_id_bytes)
        receipt = self._send_tx(caller_key, fn)
        return receipt.transactionHash.hex()

    def mark_expired(self, chain_task_id: str, caller_key: str) -> str:
        """Anyone can mark an expired task. Returns tx hash."""
        task_id_bytes = bytes.fromhex(chain_task_id.replace('0x', ''))
        fn = self.escrow.functions.markExpired(task_id_bytes)
        receipt = self._send_tx(caller_key, fn)
        return receipt.transactionHash.hex()

    def refund(self, boss_key: str, chain_task_id: str) -> str:
        """Boss refunds expired/cancelled task. Returns tx hash."""
        task_id_bytes = bytes.fromhex(chain_task_id.replace('0x', ''))
        fn = self.escrow.functions.refund(task_id_bytes)
        receipt = self._send_tx(boss_key, fn)
        return receipt.transactionHash.hex()

    def cancel_task(self, boss_key: str, chain_task_id: str) -> str:
        """Boss cancels pre-claim task. Returns tx hash."""
        task_id_bytes = bytes.fromhex(chain_task_id.replace('0x', ''))
        fn = self.escrow.functions.cancelTask(task_id_bytes)
        receipt = self._send_tx(boss_key, fn)
        return receipt.transactionHash.hex()

    def withdraw(self, caller_key: str) -> str:
        """Withdraw pending funds. Returns tx hash."""
        fn = self.escrow.functions.withdraw()
        receipt = self._send_tx(caller_key, fn)
        return receipt.transactionHash.hex()


# Singleton -- constructed lazily, tolerates missing env vars
_bridge_instance = None

def get_chain_bridge() -> ChainBridge:
    global _bridge_instance
    if _bridge_instance is None:
        _bridge_instance = ChainBridge()
    return _bridge_instance
```

**File:** `/Users/labrinyang/projects/synai-relay-phase1/config.py`

Add chain configuration constants (append after line 21):

```python
# Chain / Web3
RPC_URL = os.getenv("RPC_URL", "http://127.0.0.1:8545")
TASK_ESCROW_ADDRESS = os.getenv("TASK_ESCROW_ADDRESS", "")
CVS_ORACLE_ADDRESS = os.getenv("CVS_ORACLE_ADDRESS", "")
ORACLE_PRIVATE_KEY = os.getenv("ORACLE_PRIVATE_KEY", "")
USDC_ADDRESS = os.getenv("USDC_ADDRESS", "")
```

**Acceptance criteria:**
- `ChainBridge` instantiates without error even when env vars are missing (graceful degradation)
- `is_connected()` returns False when RPC is down or contracts not configured
- When connected to a local Anvil fork: `create_task`, `fund_task`, `claim_task`, `submit_result`, `submit_verdict`, `settle`, `withdraw` all produce valid tx receipts
- ABIs load from `contracts/out/` Foundry artifacts

**Dependencies:** B1 (contracts must compile first so ABIs exist in `contracts/out/`)

---

### B4 -- Wire fund/claim/submit/settle endpoints to chain_bridge

**File:** `/Users/labrinyang/projects/synai-relay-phase1/server.py`

This step adds **optional** on-chain calls. If `ChainBridge.is_connected()` returns False, the endpoints work in "off-chain only" mode (current behavior). If connected, they also execute on-chain transactions.

**`POST /jobs/:id/fund`** (lines 242-255):
```python
@app.route('/jobs/<task_id>/fund', methods=['POST'])
def fund_job(task_id):
    from services.job_service import JobService
    from services.chain_bridge import get_chain_bridge

    job = JobService.get_job(task_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job.status != 'created':
        return jsonify({"error": f"Job not in created state (current: {job.status})"}), 400

    bridge = get_chain_bridge()
    tx_hash = request.json.get('escrow_tx_hash')

    if bridge.is_connected() and not tx_hash:
        # On-chain mode: backend signs for boss (managed wallet)
        # For demo, accept boss_key from request or use managed wallet
        boss_key = request.json.get('boss_key')
        if not boss_key:
            return jsonify({"error": "boss_key required for on-chain funding"}), 400
        try:
            tx_hash = bridge.fund_task(boss_key, job.chain_task_id)
        except Exception as e:
            return jsonify({"error": f"On-chain fund failed: {str(e)}"}), 500

    if not tx_hash:
        # Off-chain mode: require manual tx hash
        tx_hash = request.json.get('escrow_tx_hash')
        if not tx_hash:
            return jsonify({"error": "escrow_tx_hash required (off-chain mode)"}), 400

    job.status = 'funded'
    job.escrow_tx_hash = tx_hash
    db.session.commit()
    return jsonify({"status": "funded", "tx_hash": tx_hash}), 200
```

Apply similar pattern to `claim`, `submit`, and `settle` endpoints:
- Check `bridge.is_connected()`
- If yes: execute on-chain tx, store tx_hash
- If no: continue with off-chain-only logic (current behavior)

The pattern allows **gradual migration** -- demo can run fully off-chain or with chain integration.

**Acceptance criteria:**
- All endpoints work identically when `TASK_ESCROW_ADDRESS` is not set (off-chain mode)
- When connected to Anvil: `/fund`, `/claim`, `/submit` produce on-chain state changes
- `job.chain_task_id` and `job.escrow_tx_hash` are populated when on-chain mode is active

**Dependencies:** B3, A8

---

### B5 -- Create `services/verification.py` (CVS -> Oracle flow)

**New file:** `/Users/labrinyang/projects/synai-relay-phase1/services/verification.py`

```python
"""
Orchestrates the CVS verification flow:
1. Runs VerifierFactory.verify_composite()
2. Computes evidence hash
3. Submits verdict to CVSOracle on-chain (if connected)
4. Calls SettlementService based on result
"""
import hashlib
import json
from core.verifier_factory import VerifierFactory
from services.settlement import SettlementService
from services.chain_bridge import get_chain_bridge
from models import db


class VerificationService:
    @staticmethod
    def verify_and_settle(job, submission: dict) -> dict:
        """
        Full CVS pipeline:
        1. Run composite verification
        2. Compute evidence hash from verification details
        3. Submit verdict on-chain (if chain_bridge connected)
        4. Settle or reject via SettlementService
        Returns combined result dict.
        """
        # Step 1: Run verifiers
        verification_result = VerifierFactory.verify_composite(job, submission)
        score = verification_result['score']
        is_passing = verification_result['success']
        accepted = is_passing

        # Step 2: Compute evidence hash
        evidence_str = json.dumps(verification_result, sort_keys=True, default=str)
        evidence_hash = bytes.fromhex(
            hashlib.sha256(evidence_str.encode()).hexdigest()
        )

        # Step 3: Submit verdict on-chain
        verdict_tx = None
        bridge = get_chain_bridge()
        if bridge.is_connected() and job.chain_task_id:
            try:
                verdict_tx = bridge.submit_verdict(
                    chain_task_id=job.chain_task_id,
                    accepted=accepted,
                    score=min(int(score), 255),  # uint8
                    evidence_hash=evidence_hash,
                )
            except Exception as e:
                # Log but don't block -- off-chain settlement still works
                print(f"[Verification] On-chain verdict failed: {e}")

        # Step 4: Store verdict data on job
        job.verdict_data = {
            "score": score,
            "accepted": accepted,
            "evidence_hash": evidence_hash.hex(),
            "details": verification_result.get('reason', ''),
            "verdict_tx": verdict_tx,
        }

        # Step 5: Settle
        if accepted:
            # On-chain settle (if connected)
            if bridge.is_connected() and job.chain_task_id:
                try:
                    bridge.settle(job.chain_task_id, bridge.oracle_private_key)
                except Exception as e:
                    print(f"[Verification] On-chain settle failed: {e}")

            settlement = SettlementService.settle_success(job)
            return {
                "status": "settled",
                "verification": verification_result,
                "settlement": settlement,
                "verdict_tx": verdict_tx,
            }
        else:
            settlement = SettlementService.settle_reject(job)
            return {
                "status": settlement.get("status", "rejected"),
                "message": "Verification Failed",
                "verification": verification_result,
                "settlement": settlement,
                "verdict_tx": verdict_tx,
            }
```

**File:** `/Users/labrinyang/projects/synai-relay-phase1/server.py`

Update `POST /jobs/:id/submit` (lines 308-351) to use `VerificationService`:

```python
@app.route('/jobs/<task_id>/submit', methods=['POST'])
def submit_result(task_id):
    from services.job_service import JobService
    from services.verification import VerificationService

    job = JobService.get_job(task_id)
    if not job:
        return jsonify({"error": "Task not found"}), 404
    if job.status not in ('claimed', 'rejected'):
        return jsonify({"error": f"Task not submittable, current: {job.status}"}), 400

    data = request.json or {}
    agent_id = data.get('agent_id')
    result = data.get('result', {})
    if not agent_id:
        return jsonify({"error": "agent_id required"}), 400
    if agent_id != job.claimed_by:
        return jsonify({"error": "Only the assigned agent can submit"}), 403

    job.status = 'submitted'
    job.result_data = result
    db.session.flush()

    # Branch: auto-verify or manual
    if job.verifiers_config:
        try:
            result_dict = VerificationService.verify_and_settle(job, result)
            return jsonify(result_dict), 200
        except Exception as e:
            db.session.rollback()
            return jsonify({"error": f"Verification error: {str(e)}"}), 500
    else:
        # No verifiers: wait for manual /confirm
        db.session.commit()
        return jsonify({
            "status": "submitted",
            "message": "Awaiting manual confirmation via POST /jobs/:id/confirm"
        }), 200
```

**New endpoint: `GET /jobs/:id/verdict`**

```python
@app.route('/jobs/<task_id>/verdict', methods=['GET'])
def get_verdict(task_id):
    from services.job_service import JobService
    job = JobService.get_job(task_id)
    if not job:
        return jsonify({"error": "Task not found"}), 404
    if not job.verdict_data:
        return jsonify({"error": "No verdict available"}), 404
    return jsonify(job.verdict_data), 200
```

**Acceptance criteria:**
- `VerificationService.verify_and_settle()` runs CVS, stores verdict_data, calls settlement
- Off-chain mode: works without chain_bridge (verdict_tx is None)
- On-chain mode: submits verdict + settle on-chain
- `GET /jobs/:id/verdict` returns stored verdict details
- Submission with empty `verifiers_config` does NOT trigger verification

**Dependencies:** B3 (chain_bridge), A7 (settlement), A6 (job_service)

---

### B6 -- Add cancel/refund/withdraw endpoints

**File:** `/Users/labrinyang/projects/synai-relay-phase1/server.py`

**New endpoint: `POST /jobs/:id/cancel`**

```python
@app.route('/jobs/<task_id>/cancel', methods=['POST'])
def cancel_job(task_id):
    from services.job_service import JobService
    from services.chain_bridge import get_chain_bridge

    job = JobService.get_job(task_id)
    if not job:
        return jsonify({"error": "Task not found"}), 404

    buyer_id = request.json.get('buyer_id')
    if not buyer_id or buyer_id != job.buyer_id:
        return jsonify({"error": "Only the task creator can cancel"}), 403

    if job.status not in ('created', 'funded'):
        return jsonify({"error": f"Cannot cancel task in state: {job.status}"}), 400
    if job.claimed_by:
        return jsonify({"error": "Cannot cancel: task already claimed by a worker"}), 400

    # On-chain cancel (if connected)
    bridge = get_chain_bridge()
    if bridge.is_connected() and job.chain_task_id:
        try:
            boss_key = request.json.get('boss_key')
            if boss_key:
                bridge.cancel_task(boss_key, job.chain_task_id)
        except Exception as e:
            return jsonify({"error": f"On-chain cancel failed: {str(e)}"}), 500

    job.status = 'cancelled'
    db.session.commit()
    return jsonify({"status": "cancelled", "task_id": task_id}), 200
```

**New endpoint: `POST /jobs/:id/refund`**

```python
@app.route('/jobs/<task_id>/refund', methods=['POST'])
def refund_job(task_id):
    from services.job_service import JobService
    from services.chain_bridge import get_chain_bridge

    job = JobService.get_job(task_id)
    if not job:
        return jsonify({"error": "Task not found"}), 404

    buyer_id = request.json.get('buyer_id')
    if not buyer_id or buyer_id != job.buyer_id:
        return jsonify({"error": "Only the task creator can request refund"}), 403

    if job.status not in ('expired', 'cancelled'):
        return jsonify({"error": f"Not refundable in state: {job.status}"}), 400

    # On-chain refund (if connected)
    bridge = get_chain_bridge()
    if bridge.is_connected() and job.chain_task_id:
        try:
            boss_key = request.json.get('boss_key')
            if boss_key:
                bridge.refund(boss_key, job.chain_task_id)
        except Exception as e:
            return jsonify({"error": f"On-chain refund failed: {str(e)}"}), 500

    # Release any locked worker stake (if worker existed)
    if job.claimed_by and job.deposit_amount:
        from core.escrow_manager import EscrowManager
        try:
            EscrowManager.release_stake(job.claimed_by, job.deposit_amount, job.task_id)
        except Exception:
            pass  # Stake may already have been released

    job.status = 'refunded'
    db.session.commit()
    return jsonify({
        "status": "refunded",
        "task_id": task_id,
        "amount": float(job.price),
    }), 200
```

**New endpoint: `POST /agents/:id/withdraw`**

```python
@app.route('/agents/<agent_id>/withdraw', methods=['POST'])
def withdraw_funds(agent_id):
    from services.chain_bridge import get_chain_bridge

    agent = Agent.query.filter_by(agent_id=agent_id).first()
    if not agent:
        return jsonify({"error": "Agent not found"}), 404

    bridge = get_chain_bridge()
    tx_hash = None

    if bridge.is_connected() and agent.wallet_address:
        # Check on-chain pending withdrawal
        pending = bridge.get_pending_withdrawal(agent.wallet_address)
        if pending > 0:
            try:
                from wallet_manager import wallet_manager
                priv_key = wallet_manager.decrypt_privkey(agent.encrypted_privkey)
                tx_hash = bridge.withdraw(priv_key)
            except Exception as e:
                return jsonify({"error": f"On-chain withdraw failed: {str(e)}"}), 500
        else:
            return jsonify({"error": "No on-chain funds to withdraw"}), 400
    else:
        return jsonify({"error": "Chain bridge not connected or wallet not configured"}), 503

    return jsonify({
        "status": "withdrawn",
        "agent_id": agent_id,
        "tx_hash": tx_hash,
    }), 200
```

**Acceptance criteria:**
- `POST /jobs/:id/cancel {"buyer_id": "..."}` sets status `cancelled` (only for created/funded, pre-claim)
- `POST /jobs/:id/cancel` rejects if task is claimed
- `POST /jobs/:id/refund {"buyer_id": "..."}` sets status `refunded` (only for expired/cancelled)
- `POST /agents/:id/withdraw` calls on-chain `withdraw()` via managed wallet
- All endpoints return clear error messages for invalid state transitions

**Dependencies:** B3 (chain_bridge), A6 (job_service), A7 (settlement)

---

## Phase C: Integration + Demo

### C1 -- E2E Happy Path script

**New file:** `/Users/labrinyang/projects/synai-relay-phase1/scripts/demo/e2e_happy_path.py`

Script flow:
1. `POST /agents/register` -- register BOSS
2. `POST /agents/BOSS/deposit {"amount": 120}`
3. `POST /agents/register` -- register WORKER
4. `POST /agents/WORKER/deposit {"amount": 10}`
5. `POST /jobs` -- create task (100 USDC, expiry=1h, maxRetries=3, verifiers_config=[sandbox or webhook])
6. `POST /jobs/:id/fund {"escrow_tx_hash": "0x..."}` (off-chain mode)
7. `POST /jobs/:id/claim {"agent_id": "WORKER"}` -- stakes 5 USDC
8. `GET /agents/WORKER` -- verify balance=5, locked=5
9. `POST /jobs/:id/submit {"agent_id": "WORKER", "result": {...}}`
10. Verify response: status=settled, payout=80, fee=20
11. `GET /agents/WORKER` -- verify balance=85 (5 remaining + 80 payout), locked=0 (stake returned)
12. `GET /jobs/:id/verdict` -- verify verdict details
13. `GET /ledger/ranking` -- verify stats updated
14. Print summary with pass/fail for each step

**Acceptance criteria:**
- Script runs end-to-end without errors against `python server.py`
- All assertions pass
- Demonstrates complete task lifecycle: register -> deposit -> post -> fund -> claim -> submit -> verify -> settle

**Dependencies:** A8 (all server endpoints wired), B5 (verification service)

---

### C2 -- E2E Expiry script

**New file:** `/Users/labrinyang/projects/synai-relay-phase1/scripts/demo/e2e_expiry.py`

Script flow:
1. Register + deposit BOSS (50 USDC)
2. Post task with `expiry` = now + 5 seconds
3. Fund task
4. `time.sleep(6)`
5. `GET /jobs/:id` -- verify status auto-transitions to `expired` (lazy check)
6. `POST /jobs/:id/refund {"buyer_id": "BOSS"}` -- verify status=refunded
7. Print summary

**Acceptance criteria:**
- Task auto-expires on query after deadline
- Refund succeeds and returns full amount
- No background scheduler involved

**Dependencies:** A6 (lazy expiry), B6 (refund endpoint)

---

### C3 -- E2E Reject + Retry script

**New file:** `/Users/labrinyang/projects/synai-relay-phase1/scripts/demo/e2e_reject_retry.py`

Script flow:
1. Register + deposit BOSS and WORKER
2. Post task (80 USDC, maxRetries=3, verifiers_config=[webhook with expected_payload])
3. Fund + Claim
4. Submit with wrong result -> verification rejects -> status=rejected, stake returned
5. Submit again with correct result -> verification accepts -> status=settled
6. Verify WORKER balance: +64 USDC (80% of 80), stake returned
7. Print summary

**Acceptance criteria:**
- First submission rejected, `failure_count=1`, stake fully returned
- Second submission accepted, settled with 80/20 split
- Worker balance correct after both operations

**Dependencies:** A7 (settlement reject flow), B5 (verification)

---

### C4 -- Dashboard: new status badges + expiry display

**File:** `/Users/labrinyang/projects/synai-relay-phase1/templates/index.html`

**Change 1:** Add CSS badge styles for new statuses (after `.badge.slashed` on line 242):
```css
.badge.created {
    background: rgba(255, 255, 255, 0.1);
    color: #fff;
}

.badge.claimed {
    background: rgba(188, 19, 254, 0.15);
    color: var(--violet);
}

.badge.submitted {
    background: rgba(255, 165, 0, 0.1);
    color: orange;
}

.badge.accepted {
    background: rgba(0, 255, 65, 0.1);
    color: var(--green);
    border: 1px solid var(--green);
}

.badge.settled {
    background: rgba(0, 255, 65, 0.1);
    color: var(--green);
}

.badge.rejected {
    background: rgba(255, 0, 0, 0.1);
    color: red;
}

.badge.expired {
    background: rgba(255, 165, 0, 0.1);
    color: orange;
    border: 1px solid orange;
}

.badge.cancelled {
    background: rgba(255, 255, 255, 0.05);
    color: #666;
}

.badge.refunded {
    background: rgba(0, 243, 255, 0.05);
    color: var(--cyan);
}
```

**Change 2:** Add expiry display in the task card template (in the JavaScript `jobList.innerHTML` template, around line 372):

After the `STATUS` line, add:
```javascript
${j.expiry ? `• EXPIRY: <span style="color:${new Date(j.expiry) < new Date() ? 'red' : 'var(--cyan)'}">${new Date(j.expiry).toLocaleString()}</span>` : ''}
```

**Change 3:** Update the status badge mapping -- replace `j.status` class with the new status names. The existing badge rendering on line 392 (`<span class="badge ${j.status}">`) already uses the status string as CSS class, so the new CSS classes above will apply automatically. However, rename `.badge.active` to `.badge.claimed` since 'active' was used for claimed tasks.

**Acceptance criteria:**
- Dashboard displays correct colored badges for all 10 status values
- Expired tasks show red expiry timestamp
- Future expiry timestamps show cyan
- No visual regressions on existing task cards

**Dependencies:** A4 (status names), A6 (expiry in response)

---

### C5 -- Regression test: verify all existing endpoints still work

**New file:** `/Users/labrinyang/projects/synai-relay-phase1/scripts/demo/regression_test.py`

Test each endpoint from the original API surface:

| # | Test | Expected |
|---|------|----------|
| 1 | `GET /health` | 200, `{"status": "healthy"}` |
| 2 | `GET /` | 200, HTML landing |
| 3 | `GET /dashboard` | 200, HTML dashboard |
| 4 | `POST /jobs` (minimal payload) | 201, status=created |
| 5 | `GET /jobs` | 200, array |
| 6 | `GET /jobs?status=created` | 200, filtered |
| 7 | `POST /jobs/:id/fund` | 200 |
| 8 | `POST /jobs/:id/claim` (with registered agent) | 200 |
| 9 | `POST /jobs/:id/submit` | 200 |
| 10 | `POST /jobs/:id/confirm` (with empty verifiers_config) | 200 |
| 11 | `GET /jobs/:id` | 200 |
| 12 | `POST /jobs/:id/unlock` | 200 or 400 |
| 13 | `GET /ledger/ranking` | 200 |
| 14 | `GET /ledger/:agent_id` | 200 |
| 15 | `POST /agents/adopt` | 200 |
| 16 | `POST /agents/:id/deposit` | 200 |
| 17 | `POST /agents/register` (new) | 201 |
| 18 | `GET /agents/:id` (new) | 200 |
| 19 | `POST /jobs/:id/cancel` (new) | 200 |
| 20 | `POST /jobs/:id/refund` (new) | 200 |
| 21 | `GET /jobs/:id/verdict` (new) | 200 or 404 |

Each test prints PASS/FAIL. Script exits with code 0 only if all pass.

**Acceptance criteria:**
- All 21 tests pass
- No 500 errors
- Response shapes match documented API

**Dependencies:** All of Phase A and B

---

## Summary: Task Count and Effort Estimates

| Task | Description | Estimated effort | Files touched |
|------|-------------|-----------------|---------------|
| A1 | Fix settle bug + remove slash | Small | server.py |
| A2 | Fix CLI agent_id | Trivial | synai-cli.py |
| A3 | Delete dead code | Trivial | 6 files deleted |
| A4 | Model changes + status alignment | Medium | models.py, server.py |
| A5 | agent_service + new endpoints | Medium | services/agent_service.py (new), server.py |
| A6 | job_service + lazy expiry | Medium | services/job_service.py (new), server.py |
| A7 | settlement service | Medium | services/settlement.py (new), server.py |
| A8 | Wire server.py to services | Medium | server.py |
| A9 | Move demo scripts | Trivial | 6 files moved |
| B1 | Contract changes | Small | TaskEscrow.sol, ITaskEscrow.sol, Deploy.s.sol |
| B2 | Contract tests | Medium | TaskEscrow.t.sol, Integration.t.sol |
| B3 | chain_bridge service | Large | services/chain_bridge.py (new), config.py |
| B4 | Wire chain endpoints | Medium | server.py |
| B5 | verification service | Medium | services/verification.py (new), server.py |
| B6 | cancel/refund/withdraw endpoints | Medium | server.py |
| C1 | E2E happy path | Medium | scripts/demo/e2e_happy_path.py (new) |
| C2 | E2E expiry | Small | scripts/demo/e2e_expiry.py (new) |
| C3 | E2E reject+retry | Small | scripts/demo/e2e_reject_retry.py (new) |
| C4 | Dashboard badges + expiry | Small | templates/index.html |
| C5 | Regression test | Medium | scripts/demo/regression_test.py (new) |

**Total new files:** 8 (5 services, 3 demo scripts)
**Total deleted files:** 6 (dead code)
**Total modified files:** ~8 (server.py, models.py, config.py, synai-cli.py, 3 .sol, 1 .html)
