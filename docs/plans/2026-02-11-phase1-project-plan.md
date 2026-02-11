# SynAI Relay Phase 1 - Project Plan

> Generated: 2026-02-11
> Team: researcher + architect + codex analysis
> Target: Demo-ready agent task marketplace

---

## Part 1: Requirements Gap Analysis

### 1.1 State Machine Gap

| Step | Backend (current) | Contract (current) | New Requirement |
|------|-------------------|-------------------|-----------------|
| Post | `posted` | `CREATED` (1) | CREATED |
| Fund | `funded` | `FUNDED` (2) | FUNDED (on-chain USDC lock) |
| Claim | `claimed` | `CLAIMED` (3) | CLAIMED + **Voucher Token minted** |
| Submit | `submitted` | `SUBMITTED` (4) | SUBMITTED (multiple allowed) |
| Accept | _none_ | `ACCEPTED` (5) | ACCEPTED (CVS verdict) |
| Reject | `failed` | `REJECTED` (7) | REJECTED (stays open for retry) |
| Settle | `completed` | `SETTLED` (6) | SETTLED (voucher redeemed) |
| Expire | _none_ | `EXPIRED` (8) | EXPIRED (auto-refund to Boss) |
| Cancel | _none_ | `CANCELLED` (9) | CANCELLED (pre-claim only) |
| Refund | _none_ | `REFUNDED` (10) | REFUNDED (Boss withdraws) |

**Key gaps:**
- Backend has NO expiry mechanism (no `expiry` field, no expiry check)
- Backend jumps `submit → completed` in one step (auto-verify + settle)
- Backend `failed` state ≠ contract `REJECTED` (different retry semantics)
- No `ACCEPTED` → `SETTLED` separation in backend
- Backend has `paused` and `slashed` states that don't exist in contract

### 1.2 Financial Model Gap

| Aspect | Current | Required |
|--------|---------|----------|
| Currency | Virtual balance in SQLite | USDC on-chain |
| Escrow | `agent.balance -= amount; agent.locked_balance += amount` | Contract holds USDC via `safeTransferFrom` |
| Fee | Hardcoded 20% (`server.py:362`) | Configurable BPS (contract: 500 = 5%) |
| Settlement | Instant in `_settle_job` | Pull pattern: `settle()` → `pendingWithdrawals` → `withdraw()` |
| Worker guarantee | None | **Voucher Token** (ERC-721) = proof of settlement right |
| Boss refund | Not implemented | `markExpired()` → `refund()` |
| Staking | Worker stakes from balance | **Not required** in new spec (Boss locks, not Worker) |

**Critical insight:** Current system makes the **Worker** stake funds. New spec makes the **Boss** lock funds. Fundamentally different economic model.

### 1.3 Verification Gap

| Aspect | Current | Required |
|--------|---------|----------|
| Engine | `VerifierFactory.verify_composite()` (off-chain) | Same engine, but verdict goes **on-chain** |
| Plugins | llm_judge, webhook, sandbox | Same plugins (reusable) |
| Oracle | CVSOracle.sol exists but disconnected | CVSOracle receives verdict from backend → calls `onVerdictReceived()` |
| Trust | Backend is trusted verifier | Backend = trusted oracle operator (single signer for demo) |
| Retry | `failure_count` + circuit breaker at 3 | `maxRetries` + `retryCount` in contract |

**Verdict flow (new):** Backend runs composite verification → signs verdict → submits to CVSOracle → CVSOracle calls TaskEscrow.onVerdictReceived()

### 1.4 Identity / Wallet Gap

| Aspect | Current | Required |
|--------|---------|----------|
| Agent identity | String `agent_id`, auto-registered | EOA address or **Proxy Wallet** |
| Wallet | Auto-generated via `wallet_manager.py` | Proxy Wallet with controlled permissions |
| Boss identity | `buyer_id` string | EOA → Proxy Wallet → Escrow |
| Auth | None (trust HTTP caller) | Signature-based (for demo: API key sufficient) |

### 1.5 Reusable Components

| Component | Location | Reuse Strategy |
|-----------|----------|----------------|
| `VerifierFactory` + plugins | `core/verifier_factory.py`, `core/plugins/*` | Keep as CVS engine, add oracle bridge |
| `BaseVerifier` interface | `core/verifier_base.py` | Keep as-is |
| `TaskEscrow.sol` | `contracts/src/TaskEscrow.sol` | Evolve (good bones, needs Voucher) |
| `CVSOracle.sol` | `contracts/src/CVSOracle.sol` | Evolve (add composite key, done) |
| `ITaskEscrow` state enum | `contracts/src/interfaces/ITaskEscrow.sol` | Canonical state machine |
| `Job` / `LedgerEntry` models | `models.py` | Adapt (add expiry, remove Worker staking) |
| `JobEnvelope` | `core/envelope.py` | Keep for task packaging |
| Foundry test suite | `contracts/test/*` | Extend |

### 1.6 Must Be Rebuilt

| Component | Reason |
|-----------|--------|
| `server.py` | Monolith → split into api/services |
| `_settle_job()` | Wrong model (Worker staking), missing DB commits |
| `EscrowManager` | Virtual balance model → on-chain bridge |
| `agent_client.py` | Protocol mismatch, missing fields |
| `synai-cli.py` | Doesn't send `agent_id`, wrong status filtering |
| Database migrations | Inline `ALTER TABLE` → proper migration |
| `config.py` | Needs chain config, oracle keys |

### 1.7 Entirely New Features

| Feature | Description |
|---------|-------------|
| **Voucher Token** | ERC-721 minted on claim, burned on settle/expire |
| **Proxy Wallet** | Smart contract wallet per Boss/Worker with permissions |
| **Expiry mechanism** | Backend cron/check + contract `markExpired()` |
| **Contract Bridge** | Backend ↔ chain interaction layer (web3.py / viem) |
| **Oracle Signer** | Backend signs verdicts, submits to CVSOracle on-chain |
| **On-chain fund locking** | Boss deposits USDC into TaskEscrow |
| **Pull-pattern withdrawal** | `pendingWithdrawals` mapping + `withdraw()` |

---

## Part 2: System Architecture

### 2.1 Architecture Overview

```
                    ┌─────────────────────────────────────┐
                    │           Frontend / CLI / SDK       │
                    │  (synai-cli, agent_client, web UI)   │
                    └──────────────┬──────────────────────┘
                                   │ HTTP REST
                    ┌──────────────▼──────────────────────┐
                    │         Backend (Flask API)          │
                    │                                      │
                    │  api/          → Route handlers      │
                    │  services/     → Business logic      │
                    │  core/         → Verifiers, Envelope │
                    │  contract_bridge → Chain interaction  │
                    └──────┬───────────────┬──────────────┘
                           │               │
              Off-chain    │               │  On-chain
              (DB, CVS)    │               │  (web3.py)
                           │               │
                    ┌──────▼───┐    ┌──────▼──────────────┐
                    │ SQLite/  │    │   Ethereum / Base    │
                    │ Postgres │    │                      │
                    └──────────┘    │  TaskEscrow.sol      │
                                   │  CVSOracle.sol       │
                                   │  VoucherToken.sol    │
                                   │  ProxyWalletFactory  │
                                   └──────────────────────┘
```

### 2.2 Smart Contract Layer

#### TaskEscrow.sol (evolve existing)
- Already has: createTask, fundTask, claimTask, submitResult, onVerdictReceived, settle, markExpired, refund, cancelTask, withdraw
- **Add:** Mint VoucherToken on `claimTask`, burn on `settle` or `markExpired`
- **Add:** ProxyWallet integration for `fundTask`
- **Fix:** Missing SPDX/pragma header

#### VoucherToken.sol (NEW - simple approach for demo)
```
Option A (Recommended): Simple mapping inside TaskEscrow
  - mapping(bytes32 => address) public voucherHolder;
  - Set on claim, check on settle, clear on expire
  - Pros: Minimal complexity, no extra contract
  - Cons: Not transferable

Option B: ERC-721 NFT
  - Each claim mints tokenId = uint256(taskId)
  - Transferable settlement rights
  - Pros: Composable, visible in wallets
  - Cons: More code, gas cost

For demo: Option A. Upgrade to Option B later.
```

#### ProxyWallet (NEW - for demo)
```
For demo: Skip full ProxyWallet contract.
Use EOA wallets with backend managing keys (existing wallet_manager.py pattern).
Document as "Phase 2: Replace with smart contract wallets (Safe/ERC-4337)".
```

#### CVSOracle.sol (keep existing)
- Already has composite key for verdicts (taskId + retryCount)
- Backend acts as trusted oracle signer
- For demo: single oracle address sufficient

### 2.3 Backend Layer (code reorganization)

```
synai-relay-phase1/
├── server.py                  # Thin entry: from synai.app import create_app; app = create_app()
├── synai-cli.py               # Thin entry: from synai.client.cli import cli; cli()
├── Procfile                   # web: gunicorn synai.app:create_app()
├── requirements.txt
│
├── synai/
│   ├── __init__.py
│   ├── app.py                 # Flask factory + blueprint registration
│   ├── config.py              # DB + chain + oracle config
│   ├── models.py              # Owner, Agent, Job, LedgerEntry
│   ├── db_migrations.py       # Lightweight schema upgrades
│   │
│   ├── api/                   # Route handlers (thin: validate → delegate → respond)
│   │   ├── health.py          # GET /health
│   │   ├── web.py             # GET / /dashboard /docs /share
│   │   ├── jobs.py            # POST/GET /jobs, /jobs/<id>/fund|claim|submit|confirm
│   │   ├── agents.py          # POST /agents/adopt, /agents/<id>/deposit
│   │   ├── ledger.py          # GET /ledger/ranking, /ledger/<id>
│   │   └── webhook.py         # POST /v1/verify/webhook/<task_id>
│   │
│   ├── services/              # Business logic (testable, no Flask dependency)
│   │   ├── job_service.py     # Post, fund, claim, submit orchestration
│   │   ├── settlement.py      # Settle, slash, refund logic
│   │   ├── verification.py    # Dispatch to VerifierFactory, threshold check
│   │   ├── agent_service.py   # Register, adopt, deposit
│   │   └── chain_bridge.py    # On-chain interaction (web3.py)
│   │
│   ├── core/                  # Unified core (merge of old core/ + synai/core/)
│   │   ├── envelope.py
│   │   ├── escrow_manager.py  # Refactored: virtual + on-chain modes
│   │   ├── verifier_base.py
│   │   ├── verifier_factory.py
│   │   └── plugins/
│   │       ├── llm_judge.py
│   │       ├── sandbox.py
│   │       └── webhook.py
│   │
│   └── client/                # Unified SDK + CLI
│       ├── sdk.py             # RelayClient (HTTP wrapper)
│       ├── cli.py             # Click commands
│       └── config_store.py    # ~/.synai/config.json
│
├── contracts/                 # Foundry project (unchanged structure)
│   ├── src/
│   │   ├── TaskEscrow.sol
│   │   ├── CVSOracle.sol
│   │   └── interfaces/ITaskEscrow.sol
│   ├── test/
│   ├── script/
│   └── abi/                   # Exported ABIs for backend
│
├── scripts/                   # Demo & verification scripts
│   ├── demo/
│   │   ├── boss_post_and_fund.py
│   │   ├── worker_claim_and_submit.py
│   │   └── full_e2e_flow.py
│   └── smoke/
│       ├── api_flow.py
│       └── contract_flow.py
│
├── templates/                 # HTML templates
└── docs/
    └── plans/                 # This file
```

### 2.4 Unified State Machine

```
    ┌──────────┐
    │  CREATED  │ ← Boss calls createTask (off-chain record + on-chain)
    └────┬─────┘
         │ Boss funds (USDC → escrow)
    ┌────▼─────┐
    │  FUNDED   │ ← Funds locked in contract, task visible to workers
    └────┬─────┘
         │ Worker claims (+ Voucher Token minted)
    ┌────▼─────┐
    │  CLAIMED  │ ← Worker assigned, deadline counting
    └────┬─────┘
         │ Worker submits result
    ┌────▼──────┐
    │ SUBMITTED  │ ← CVS verification triggered
    └──┬─────┬──┘
       │     │
  accepted  rejected
       │     │
  ┌────▼─┐ ┌─▼───────┐
  │ACCEPT│ │REJECTED  │──→ Worker can re-submit (if retries left)
  └──┬───┘ └─────────┘      └──→ if maxRetries hit → EXPIRED
     │
     │ Anyone calls settle()
  ┌──▼─────┐
  │SETTLED  │ ← Worker payout + fee calculated, in pendingWithdrawals
  └────────┘

  At any time after expiry:
  ┌────────┐
  │EXPIRED │ ← markExpired() if past deadline with no ACCEPTED
  └───┬────┘
      │ Boss calls refund()
  ┌───▼─────┐
  │REFUNDED │ ← Full amount → Boss pendingWithdrawals
  └─────────┘

  Pre-claim cancellation:
  ┌─────────┐
  │CANCELLED│ ← Boss cancels before worker claims
  └────┬────┘
       │ Boss calls refund()
  ┌────▼────┐
  │REFUNDED │
  └─────────┘
```

### 2.5 Data Flows

#### Boss Flow
```
1. POST /jobs                → Backend creates DB record + calls TaskEscrow.createTask()
2. POST /jobs/<id>/fund      → Boss approves USDC + calls TaskEscrow.fundTask()
3. (wait for worker...)
4. GET /jobs/<id>             → Check status
5. (if settled) withdraw()   → Boss gets nothing (Worker got paid)
6. (if expired) refund()     → Boss gets full refund via withdraw()
```

#### Worker Flow
```
1. GET /jobs                  → Browse available tasks (status=FUNDED)
2. POST /jobs/<id>/claim      → Backend calls TaskEscrow.claimTask(), Voucher minted
3. POST /jobs/<id>/submit     → Worker submits result, CVS runs
4. (if accepted) settle()     → Payout credited to pendingWithdrawals
5. withdraw()                 → Worker gets USDC
```

#### CVS Verification Flow
```
1. Worker submits → Backend receives result
2. Backend runs VerifierFactory.verify_composite(job, result)
   - llm_judge: Gemini/GPT evaluates quality
   - sandbox: runs test script, checks regex
   - webhook: awaits external callback
3. Score >= 80 → accepted=true
4. Backend signs verdict → submits to CVSOracle.submitVerdict()
5. CVSOracle calls TaskEscrow.onVerdictReceived()
6. TaskEscrow updates status (ACCEPTED or REJECTED)
```

---

## Part 3: Milestones & Execution Plan

### M0: Code Reorganization (Foundation)
> Goal: Clean up codebase without changing behavior

| # | Issue | Priority | Estimate |
|---|-------|----------|----------|
| M0-1 | Create `synai/` package with `app.py` factory + blueprints | P0 | S |
| M0-2 | Move routes from `server.py` into `synai/api/*.py` | P0 | M |
| M0-3 | Extract `_settle_job` + business logic into `synai/services/` | P0 | M |
| M0-4 | Merge `core/` into `synai/core/`, delete `synai/core/` duplicates | P1 | S |
| M0-5 | Move `models.py` + `config.py` into `synai/` | P1 | S |
| M0-6 | Unify SDK: `synai/client/sdk.py` + `synai/client/cli.py` | P1 | M |
| M0-7 | Move demo scripts to `scripts/demo/`, verify to `scripts/smoke/` | P2 | S |
| M0-8 | Fix `_settle_job` missing `db.session.commit()` | P0 | XS |
| M0-9 | Align CLI/SDK `agent_id` in submit + status filters | P1 | S |
| M0-10 | Add `contracts/abi/` with exported ABIs | P2 | XS |

### M1: State Machine & Expiry (Contract Evolution)
> Goal: Align backend with contract state machine, add expiry

| # | Issue | Priority | Estimate |
|---|-------|----------|----------|
| M1-1 | Add `expiry` field to `Job` model + POST /jobs payload | P0 | S |
| M1-2 | Backend state strings → use shared enum from `ITaskEscrow` | P0 | M |
| M1-3 | Implement expiry check on claim/submit (reject if expired) | P0 | S |
| M1-4 | Add background task / endpoint to mark expired jobs | P1 | M |
| M1-5 | Remove Worker staking from claim flow (Boss locks, not Worker) | P0 | S |
| M1-6 | Separate ACCEPTED and SETTLED states in backend | P1 | S |
| M1-7 | Implement reject → re-submit flow with retry counting | P1 | M |
| M1-8 | Fix TaskEscrow.sol: add SPDX/pragma header | P0 | XS |
| M1-9 | Align fee: make configurable, default 500 bps (5%) | P1 | S |

### M2: On-Chain Integration (Bridge)
> Goal: Backend ↔ Chain connected for core flow

| # | Issue | Priority | Estimate |
|---|-------|----------|----------|
| M2-1 | Implement `synai/services/chain_bridge.py` (web3.py wrapper) | P0 | L |
| M2-2 | Backend `POST /jobs` → calls `TaskEscrow.createTask()` | P0 | M |
| M2-3 | Backend `POST /jobs/<id>/fund` → calls `TaskEscrow.fundTask()` | P0 | M |
| M2-4 | Backend `POST /jobs/<id>/claim` → calls `TaskEscrow.claimTask()` | P0 | M |
| M2-5 | CVS verdict → sign + submit to `CVSOracle.submitVerdict()` | P0 | L |
| M2-6 | Backend `settle` → calls `TaskEscrow.settle()` | P1 | M |
| M2-7 | Implement Voucher mapping in TaskEscrow (`voucherHolder`) | P1 | S |
| M2-8 | Deploy contracts to local Anvil + Base testnet | P1 | M |
| M2-9 | Add `ESCROW_MODE=offchain|onchain` toggle in config | P2 | S |

### M3: Demo Polish & E2E
> Goal: Working demo flow end-to-end

| # | Issue | Priority | Estimate |
|---|-------|----------|----------|
| M3-1 | E2E script: Boss posts → Worker claims → submits → CVS accepts → settle | P0 | L |
| M3-2 | E2E script: Boss posts → expiry → refund | P0 | M |
| M3-3 | E2E script: Boss posts → Worker submits → rejected → retry → accept | P1 | M |
| M3-4 | Update CLI to support new flows (fund, check expiry) | P1 | M |
| M3-5 | Update landing page / dashboard with new states | P2 | M |
| M3-6 | Smoke test suite covering all API endpoints | P1 | M |
| M3-7 | Write demo walkthrough documentation | P2 | S |

---

## Part 4: Technical Decisions

### 4.1 Voucher Token: Simple Mapping (Recommended for Demo)

```solidity
// Inside TaskEscrow.sol
mapping(bytes32 => address) public voucherHolder;

function claimTask(bytes32 taskId) external {
    // ... existing checks ...
    task.worker = msg.sender;
    task.status = TaskStatus.CLAIMED;
    voucherHolder[taskId] = msg.sender;  // <-- voucher
    emit TaskClaimed(taskId, msg.sender);
}

function settle(bytes32 taskId) external {
    // ... existing logic ...
    require(voucherHolder[taskId] != address(0), "No voucher");
    // payout goes to voucherHolder[taskId]
    pendingWithdrawals[voucherHolder[taskId]] += payout;
    delete voucherHolder[taskId];  // <-- burn voucher
}
```

Rationale: For demo, a mapping is sufficient. ERC-721 can be added in Phase 2 for transferability.

### 4.2 Proxy Wallet: Defer to Phase 2

For demo: Use managed EOA wallets (existing `wallet_manager.py` pattern) with backend holding keys.

Phase 2: Integrate ERC-4337 Account Abstraction or Safe smart wallets.

### 4.3 CVS Trust Model: Single Trusted Oracle

For demo: Backend is the sole oracle signer. One EOA signs verdicts.

Phase 2: Multi-sig oracle committee, or ZK-proof of verification.

### 4.4 On-Chain vs Off-Chain Split

| Operation | Demo (M2) | Production (future) |
|-----------|-----------|---------------------|
| Task creation | On-chain | On-chain |
| Fund locking | On-chain | On-chain |
| Worker claim | On-chain | On-chain |
| Result submission | Off-chain (backend stores) | IPFS hash on-chain |
| CVS verification | Off-chain (backend runs) | Off-chain + ZK proof |
| Verdict submission | On-chain (oracle) | On-chain (multi-sig) |
| Settlement | On-chain | On-chain |
| Task browsing | Off-chain (backend DB) | Backend + The Graph |

---

## Part 5: Risk Identification

| Risk | Impact | Mitigation |
|------|--------|------------|
| web3.py integration complexity | M2 delay | Start with Anvil local chain; use `ESCROW_MODE=offchain` toggle |
| Oracle key management | Security | For demo: env var. Production: HSM/KMS |
| Gas costs on Base | Demo cost | Use testnet; batch transactions where possible |
| State sync (backend ↔ chain) | Data inconsistency | Backend is source of truth for reads; chain is source of truth for funds |
| Scope creep (full Proxy Wallet) | Timeline | Explicitly defer to Phase 2; document boundary |
| Contract upgrade needed mid-demo | Deployment pain | Use CREATE2 or proxy pattern from start |

---

## Part 6: Task Ordering & Dependencies

```
M0 (Code Reorg) ──────────────┐
                               ├──→ M1 (State Machine) ──→ M2 (On-Chain) ──→ M3 (Demo)
M0 can start immediately       │
M1 can start after M0-1..M0-3 ─┘
M2 requires M1 complete
M3 requires M2 complete

Parallel tracks:
- Contract work (M1-8, M2-7, M2-8) can run alongside backend M0/M1
- CLI updates (M0-6, M0-9, M3-4) can run alongside services work
```

**Suggested execution order:**
1. **Week 1:** M0-1 through M0-5 (package restructure) + M0-8 (critical bug fix)
2. **Week 1-2:** M0-6, M0-7, M0-9 (client + scripts cleanup) | M1-8 (contract fix)
3. **Week 2:** M1-1 through M1-5 (state machine alignment)
4. **Week 2-3:** M1-6, M1-7, M1-9 (backend states) | M2-7 (voucher mapping)
5. **Week 3-4:** M2-1 through M2-6 (chain bridge) | M2-8 (deployment)
6. **Week 4:** M3-1 through M3-7 (E2E demo + polish)

---

## Part 7: Dead Code & Cleanup

Identified by researcher — should be removed in M0:

| File | Reason |
|------|--------|
| `core/payment.py` | Fully mock (in-memory dict + print stubs), never imported by server.py |
| `core/verifier.py` | Legacy single-verifier, superseded by `verifier_factory.py` composite |
| `synai/core/verifier_base.py` | Exact duplicate of `core/verifier_base.py` |
| `synai/core/plugins/` | Empty directory |
| `synai/relay/` | Empty directory |
| `schema.sql` | Obsolete — doesn't match current models |

---

## Part 8: Incremental Migration Path (from Architect)

Each step is independently testable and deployable:

1. **Add `voucherHolder` mapping to TaskEscrow** — non-breaking, just adds a field
2. **Add `createAndFund` convenience function** — non-breaking, optional shortcut for Boss
3. **Create `synai/services/chain_bridge.py`** — new file, connects backend to contracts via web3.py
4. **Create `synai/services/cvs_orchestrator.py`** — extracts CVS logic from server.py submit route
5. **Create `synai/services/job_service.py`** — extracts business logic from route handlers
6. **Split server.py routes into `synai/api/`** — mechanical extraction
7. **Wire chain_bridge into job_service** — backend now sends real on-chain txs
8. **Deprecate backend-only settlement** — settlement happens on-chain, backend just relays
9. **Package SDK** — extract synai/agent_client.py into synai/client/

---

## Part 9: Fee Model Resolution

**Problem:** Backend hardcodes 20% fee (`server.py:362`), contract defaults to 5% (`TaskEscrow.sol:14`).

**Resolution:** On-chain fee is the real fee. The backend should NOT calculate its own fee. When `settle()` is called on-chain, it splits `amount * feeBps / 10000` to treasury and the rest to worker's `pendingWithdrawals`. The backend reads the `TaskSettled` event to get actual payout and fee amounts. The 20% in server.py is legacy from before the contract existed — remove it.

---

## Appendix: Current vs Target File Mapping

| Current File | Target Location | Action |
|-------------|-----------------|--------|
| `server.py` | `server.py` (thin) + `synai/app.py` + `synai/api/*.py` | Split |
| `models.py` | `synai/models.py` | Move |
| `config.py` | `synai/config.py` | Move + extend |
| `core/escrow_manager.py` | `synai/core/escrow_manager.py` | Move + refactor |
| `core/verifier_factory.py` | `synai/core/verifier_factory.py` | Move |
| `core/verifier_base.py` | `synai/core/verifier_base.py` | Move |
| `core/plugins/*` | `synai/core/plugins/*` | Move |
| `core/envelope.py` | `synai/core/envelope.py` | Move |
| `synai/core/verifier_base.py` | (delete) | Duplicate |
| `synai/core/plugins/` | (delete) | Empty |
| `synai/relay/` | (delete) | Empty |
| `synai/agent_client.py` | `synai/client/sdk.py` | Rewrite |
| `synai-cli.py` | `synai-cli.py` (thin) + `synai/client/cli.py` | Split |
| `wallet_manager.py` | `synai/services/agent_service.py` | Merge |
| `agent_boss.py` | `scripts/demo/boss_post_and_fund.py` | Move |
| `agent_worker.py` | `scripts/demo/worker_claim_and_submit.py` | Move |
| `agent_boss_confirm.py` | `scripts/demo/boss_confirm.py` | Move |
| `agent_twitter_claim.py` | `scripts/demo/twitter_claim.py` | Move |
| `synai/demo_antigravity.py` | `scripts/demo/antigravity.py` | Move |
| `verify_backend.py` | `scripts/smoke/api_flow_v1.py` | Move |
| `verify_backend_v2.py` | `scripts/smoke/api_flow_v2.py` | Move |
| `schema.sql` | (delete or archive) | Obsolete |
