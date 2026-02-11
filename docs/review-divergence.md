# Cross-Review Divergence Notes — Phase 3C

> Codex vs. Opus gap analysis comparison

## Methodology

Two independent subagents analyzed the same source code and documentation. This document records where they disagreed on priority, scope, or identification of gaps.

## Key Divergences

### 1. Authentication Priority
- **Codex**: Split into 4 P0 items (#35-37, #39) covering auth, authorization, and key management
- **Opus**: Single P0 item (#38) covering authentication holistically
- **Resolution**: Merged to single **G01 (P0)**. Both agree it's the most fundamental security gap.

### 2. Custodial Wallet Model
- **Codex**: Mentioned as a gap in existing-contracts.md analysis but not explicitly prioritized as P0
- **Opus**: Flagged as **P0 (#14)** — "single largest security risk in the system"
- **Resolution**: **Deferred to Phase 2**. Per-task escrow contracts are architected (see docs/design/smart-contracts.md) but are out of scope for the API lifecycle MVP. The acute risk is addressed by G21 (solvency monitoring, P1).

### 3. Event Push Priority
- **Codex**: P1 (#14, #27, #56) — grouped as notification gaps
- **Opus**: **P0 (#11)** — "forces agents into polling loop"
- **Resolution**: **P0 (G04)**. For autonomous agents, polling is a fundamental limitation. Even a minimal webhook or SSE implementation unblocks agent autonomy.

### 4. Unclaim Mechanism
- **Codex**: P1 (#29) — "workers permanently locked into claimed tasks"
- **Opus**: **P0 (#9)** — "irreversible action with no undo mechanism"
- **Resolution**: **P0 (G05)**. A worker's reputation is damaged by claims they can't withdraw from. This directly blocks reliable agent operation.

### 5. Items Found by Only One Reviewer

**Codex-only gaps** (Opus did not identify):
- C#41: No database indexes (→ G09, P1)
- C#43: Participants as JSON array (→ G10, P1)
- C#58: No migration system (→ G15, P1)
- C#55: No idempotency on POST /jobs (→ G17, P1)
- C#44: Submission compound uniqueness (→ G33, P2)
- C#48: Agent deactivation (→ G32, P2)

**Opus-only gaps** (Codex did not identify):
- O#17: Overpayment handling (→ G22, P1)
- O#22,43: Submission content privacy (→ G16, P1)
- O#45: DEV_MODE safety (→ G23, P1)
- O#35: Expiry/oracle race condition detail (incorporated into G12)
- O#37: Server crash during payout (incorporated into G18)
- O#34: Concurrent oracle resolution ambiguity (incorporated into G33)
- O#10: Batch operations (→ G31, P2)
- O#48: Metrics/monitoring (→ G34, P2)

### 6. Overall Priority Distribution

| | Codex | Opus | Final |
|---|---|---|---|
| P0 | 5 | 11 | 7 |
| P1 | 19 | 23 | 17 |
| P2 | 14 | 14 | 10 |
| **Total unique gaps** | 63 items (38 gaps) | 48 gaps | **34 gaps** |

Opus was more aggressive with P0 classification. The final merged report balances between the two by: (1) promoting items both flagged as critical, (2) keeping Opus's higher priority when justified by fund-loss or lifecycle-blocking risk, (3) deferring infrastructure-level items (escrow, task queue) that are valid concerns but out of MVP scope.
