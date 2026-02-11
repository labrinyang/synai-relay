# SynAI Relay Phase 1 - Confirmed Design

> Date: 2026-02-11
> Status: Approved via brainstorming session
> Scope: Demo-ready agent task marketplace

---

## 1. Key Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Worker staking | 5% of task price, backend virtual, ALL scenarios full refund | Demo needs to show the mechanism; no penalty simplifies logic |
| Voucher Token | `mapping(bytes32 => address) voucherHolder` inside TaskEscrow | Minimal complexity, zero extra gas, sufficient for demo |
| Interaction model | Dual channel: permissionless contracts + backend REST API | Any EOA can participate; REST lowers barrier for agents |
| Platform fee | 20% (2000 bps) | User decision; contract `defaultFeeBps` updated from 500 |
| CVS Oracle | Single trusted EOA (backend is oracle) | Sufficient for demo; multi-sig deferred |
| Expiry trigger | Lazy check at API endpoints (no background scheduler) | Simpler infra; checked when someone queries/operates a task |
| Max retries | Default 3, Boss can customize via `maxRetries` param | Matches existing contract + backend circuit breaker |
| Demo scope | Core trading flow + ranking/dashboard; keep ALL existing API endpoints | Backend must be a complete agent-facing API |
| Frontend | Keep existing templates, add new status badges + expiry display | Minimal frontend work, focus on API |
| Code reorg | Pragmatic: keep server.py, add services/ layer, merge core/ | Avoid heavy refactoring; focus effort on new features |

---

## 2. Architecture Overview

```
Agent (EOA)
  |
  |-- Direct: calls contracts via web3 (advanced)
  |-- REST: calls backend API (convenience, demo default)
  |
  v
Backend (Flask)
  ├── server.py          (routes, delegates to services)
  ├── services/
  │   ├── chain_bridge.py    (web3.py ↔ contracts)
  │   ├── settlement.py      (payout/fee/stake logic)
  │   ├── verification.py    (CVS engine → Oracle submission)
  │   ├── job_service.py     (task lifecycle orchestration)
  │   └── agent_service.py   (register/deposit/profile)
  ├── core/                  (verifier plugins, envelope)
  └── models.py              (SQLAlchemy)
  |
  v
Ethereum / Base
  ├── TaskEscrow.sol     (escrow, state machine, voucher, withdrawals)
  └── CVSOracle.sol      (verdict recording, escrow callback)
```

---

## 3. Unified State Machine

On-chain `TaskStatus` enum is the single source of truth.
Backend `Job.status` mirrors it as lowercase strings.

```
CREATED(1) ──fund──→ FUNDED(2) ──claim──→ CLAIMED(3) ──submit──→ SUBMITTED(4)
                                                                      │
                                                          ┌───────────┴───────────┐
                                                     accepted                 rejected
                                                          │                       │
                                                    ACCEPTED(5)             REJECTED(7)
                                                          │                       │
                                                     settle                  re-submit
                                                          │                 (retries < 3)
                                                    SETTLED(6)                    │
                                                     [terminal]          retries >= max
                                                                                  │
                                                                            EXPIRED(8)

Expiry (lazy check): any endpoint touching a task checks
  if now > expiry → auto-mark EXPIRED

FUNDED/CLAIMED/SUBMITTED/REJECTED ──markExpired──→ EXPIRED(8) ──refund──→ REFUNDED(10)

CREATED/FUNDED (no worker) ──cancelTask──→ CANCELLED(9) ──refund──→ REFUNDED(10)
```

Backend status values: `created`, `funded`, `claimed`, `submitted`, `accepted`,
`rejected`, `settled`, `expired`, `cancelled`, `refunded`

---

## 4. Financial Model

### Boss Side — Task Fund (on-chain)
- Boss deposits USDC into TaskEscrow via `fundTask()`
- Locked until ACCEPTED + settle, or EXPIRED + refund
- On settle: Worker gets 80%, platform treasury gets 20%
- On expire/cancel: Boss gets 100% refund, platform gets nothing

### Worker Side — Good-faith Stake (backend virtual)
- Worker stakes 5% of task price from backend balance on claim
- Stake locked in `Agent.locked_balance` until task reaches terminal state
- ALL scenarios: stake returned in full (no penalty)
- Purpose: prevent spam claiming, not punishment

### Settlement Flow
```
CVS accepts → backend calls TaskEscrow.settle()
  → contract calculates: fee = amount * 2000 / 10000
  → pendingWithdrawals[worker] += (amount - fee)
  → pendingWithdrawals[treasury] += fee
  → Worker calls withdraw() to get USDC
  → Backend releases virtual stake back to Worker balance
```

---

## 5. CVS Verification Flow

### REST API Path (default for demo)
```
Worker: POST /jobs/:id/submit {agent_id, result}
  → Backend stores result_data
  → Backend calls TaskEscrow.submitResult(taskId, resultHash) via chain_bridge
  → On-chain status → SUBMITTED

  → If verifiers_config is non-empty:
      Backend runs VerifierFactory.verify_composite()
        - sandbox plugin: Docker execution, check regex
        - llm_judge plugin: Gemini/GPT scoring
        - webhook plugin: external callback (optional)
      Weighted average score >= 80 → accepted=true
      Backend computes evidenceHash = keccak256(verification_details)
      Backend calls CVSOracle.submitVerdict(taskId, accepted, score, evidenceHash)
      On-chain: CVSOracle → TaskEscrow.onVerdictReceived()
      If accepted: backend auto-calls settle()
      Backend syncs local Job.status

  → If verifiers_config is empty:
      Status stays SUBMITTED
      Boss must call POST /jobs/:id/confirm to manually accept
```

---

## 6. Smart Contract Changes

### TaskEscrow.sol — 3 changes (~20 lines)

```solidity
// Change 1: Add SPDX/pragma (currently missing)
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

// Change 2: Fee 5% → 20%
uint16 public defaultFeeBps = 2000; // was 500

// Change 3: Voucher mapping
mapping(bytes32 => address) public voucherHolder;
event VoucherIssued(bytes32 indexed taskId, address indexed worker);

function claimTask(bytes32 taskId) external {
    // ... existing checks unchanged ...
    task.worker = msg.sender;
    task.status = TaskStatus.CLAIMED;
    voucherHolder[taskId] = msg.sender;      // +
    emit VoucherIssued(taskId, msg.sender);   // +
    emit TaskClaimed(taskId, msg.sender);
}

function settle(bytes32 taskId) external {
    require(voucherHolder[taskId] != address(0), "No voucher"); // +
    // ... existing fee calculation ...
    pendingWithdrawals[voucherHolder[taskId]] += payout; // changed from task.worker
    delete voucherHolder[taskId];                         // +
    pendingWithdrawals[treasury] += fee;
    // ... rest unchanged ...
}
```

### CVSOracle.sol — No changes
### ITaskEscrow.sol — Add VoucherIssued event declaration

### New tests (TaskEscrow.t.sol)
- `test_claimSetsVoucher`
- `test_settleUsesVoucher`
- `test_expireClearsVoucher`

---

## 7. API Surface (Complete)

### Existing — Keep & Adapt

| Endpoint | Changes |
|----------|---------|
| `POST /jobs` | Add `expiry` param (unix timestamp) |
| `GET /jobs` | Add query params: `?status=`, `?buyer_id=`, `?claimed_by=` |
| `GET /jobs/:id` | Add lazy expiry check; return new status values |
| `POST /jobs/:id/fund` | Wire to `TaskEscrow.fundTask()` via chain_bridge |
| `POST /jobs/:id/claim` | Wire to `TaskEscrow.claimTask()` + virtual stake; remove auto-register |
| `POST /jobs/:id/submit` | Wire to chain + CVS async flow; branch on verifiers_config |
| `POST /jobs/:id/confirm` | Keep as manual-confirm path (when verifiers_config empty) |
| `POST /jobs/:id/unlock` | Keep knowledge monetization as-is |
| `GET /ledger/ranking` | No change |
| `GET /ledger/:agent_id` | No change |
| `POST /agents/adopt` | No change |
| `POST /agents/:id/deposit` | Add fallback auto-register if agent doesn't exist |

### New Endpoints

| Endpoint | Purpose |
|----------|---------|
| `POST /agents/register` | Explicit agent registration + wallet creation |
| `GET /agents/:agent_id` | Agent profile: balance, locked_balance, metrics, wallet |
| `POST /jobs/:id/cancel` | Boss cancels pre-claim task |
| `POST /jobs/:id/refund` | Boss reclaims expired/cancelled task funds |
| `POST /agents/:id/withdraw` | Agent withdraws on-chain funds via `TaskEscrow.withdraw()` |
| `GET /jobs/:id/verdict` | Query CVS verdict details for a task |

### Registration Flow Fix
```
Old: claim auto-registers → but claim needs stake → stake needs balance → broken
New: register → deposit → claim (each step independent)
     deposit also auto-registers as fallback
```

### /confirm vs /submit Clarification
```
verifiers_config non-empty → submit triggers CVS auto-verify → auto-settle
verifiers_config empty     → submit only stores result → Boss calls /confirm
```

---

## 8. Code Reorganization (Pragmatic)

```
synai-relay-phase1/
├── server.py              # Keep as main entry; clean up inline migrations
├── config.py              # Add chain config (RPC URL, contract addresses, oracle key)
├── models.py              # Add expiry field, align status values
├── core/                  # Keep; delete synai/core/ duplicates
│   ├── verifier_factory.py
│   ├── verifier_base.py
│   ├── envelope.py
│   └── plugins/
│       ├── llm_judge.py
│       ├── sandbox.py
│       └── webhook.py
├── services/              # NEW: extracted business logic
│   ├── chain_bridge.py    # web3.py: sign txs, read events, sync state
│   ├── settlement.py      # _settle_job extracted + commit bug fixed
│   ├── verification.py    # CVS orchestration → oracle submission
│   ├── job_service.py     # Task lifecycle + query filtering
│   └── agent_service.py   # Register, deposit, profile, wallet
├── wallet_manager.py      # Keep (used by agent_service)
├── synai-cli.py           # Fix agent_id bug
├── contracts/             # Foundry project
│   ├── src/
│   │   ├── TaskEscrow.sol     # +voucher, +fee change, +SPDX
│   │   ├── CVSOracle.sol      # No change
│   │   └── interfaces/ITaskEscrow.sol  # +VoucherIssued event
│   └── test/
├── scripts/demo/          # Moved from root: agent_*.py, verify_*.py
├── templates/             # +new status badges, +expiry display
└── docs/plans/
```

### Delete
- `synai/core/verifier_base.py` (duplicate)
- `synai/core/plugins/` (empty)
- `synai/relay/` (empty)
- `core/payment.py` (dead code, never imported)
- `core/verifier.py` (legacy, superseded by verifier_factory)
- `schema.sql` (obsolete)

### Critical Bug Fix
- `server.py:349` `_settle_job()` — add `db.session.commit()` (move to services/settlement.py)
- `synai-cli.py:150-157` — add `agent_id` to submit payload

---

## 9. Demo Scenarios

### Scenario 1: Happy Path
```
Boss registers → deposits 120 USDC
Boss posts task (100 USDC, expiry=1h, maxRetries=3, verifiers_config=[sandbox])
Boss funds task → USDC locked in contract → status: funded
Worker registers → deposits 10 USDC
Worker claims task → stakes 5 USDC → gets Voucher → status: claimed
Worker submits result → CVS sandbox verifies → score 95 → accept
Oracle submits verdict → status: accepted → auto-settle → status: settled
Worker: +80 USDC payout, +5 USDC stake returned
Platform treasury: +20 USDC
Dashboard shows completed task + updated rankings
```

### Scenario 2: Expiry Refund
```
Boss posts + funds task (50 USDC, expiry=10s)
Wait 10 seconds...
Any API call on this task triggers lazy expiry check → status: expired
Boss calls refund → 50 USDC back to Boss pendingWithdrawals
Boss calls withdraw → USDC returned
```

### Scenario 3: Reject + Retry
```
Boss posts + funds task (80 USDC, maxRetries=3)
Worker claims → submits low quality result
CVS rejects → score 40 → status: rejected (retryCount=1)
Worker submits improved result
CVS accepts → score 85 → settle
Worker gets 64 USDC (80%), stake returned
```

---

## 10. Execution Priority

### Phase A: Foundation (fix bugs + add services layer)
1. Fix `_settle_job` commit bug → extract to `services/settlement.py`
2. Fix CLI agent_id bug
3. Create `services/job_service.py` with query filtering
4. Create `services/agent_service.py` with register + profile
5. Add `POST /agents/register`, `GET /agents/:agent_id` endpoints
6. Add `expiry` field to Job model + lazy expiry check
7. Align Job.status values with contract enum
8. Delete dead code + duplicates
9. Move demo scripts to `scripts/demo/`

### Phase B: Contract + Chain Bridge
1. Update TaskEscrow.sol (SPDX + fee + voucher)
2. Add voucher tests
3. Create `services/chain_bridge.py` (web3.py wrapper)
4. Wire fund/claim/submit/settle endpoints to chain_bridge
5. Create `services/verification.py` (CVS → Oracle flow)
6. Add cancel/refund/withdraw endpoints

### Phase C: Integration + Demo
1. E2E happy path script
2. E2E expiry script
3. E2E reject+retry script
4. Dashboard: new status badges + expiry display
5. Verify all existing endpoints still work
