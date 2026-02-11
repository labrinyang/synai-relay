# Implementation Order — Topological Sort

> Phase 4.4: Dependency-ordered implementation sequence
> Total: 7 P0 + 17 P1 = 24 gaps to implement

---

## Dependency Graph

```
G09 (indexes) ────────────────────────────────────┐
G10 (join table) ─────────────────────────────────┤
G15 (migrations) ─────────────────────────────────┤
G14 (logging) ────────────────────────────────────┤
G23 (DEV_MODE safety) ───────────────────────────┤
G07 (oracle timeout) ────────────────────────────┤
                                                   ▼
G01 (auth) ───────────┬──→ G02 (agent update)
                      ├──→ G03 (search/pagination) ←── G09, G10
                      ├──→ G04 (webhooks)
                      ├──→ G05 (unclaim)
                      ├──→ G06 (payout handling) ←── G02
                      ├──→ G08 (buyer_id FK)
                      ├──→ G11 (job update)
                      ├──→ G13 (rate limiting)
                      ├──→ G16 (submission privacy)
                      ├──→ G17 (idempotency)
                      ├──→ G19 (fee config)
                      ├──→ G20 (wallet warning)
                      ├──→ G21 (solvency)
                      ├──→ G22 (overpayment)
                      └──→ G24 (dispute stub)
G12 (proactive expiry) ←── G04 (webhooks)
G18 (task queue) ←── G07, G14
```

---

## Implementation Batches

### Batch 0: Infrastructure (No dependencies, parallel-safe)
These items change DB schema or infrastructure and should be done first.

| Order | Gap ID | Name | Reason |
|-------|--------|------|--------|
| 0.1 | **G15** | Migration System (Alembic) | Must have before any schema changes |
| 0.2 | **G09** | Database Indexes | Schema change, no code deps |
| 0.3 | **G14** | Structured Logging | Affects all files, do early |
| 0.4 | **G23** | DEV_MODE Safety | Simple, no deps |
| 0.5 | **G07** | Oracle Timeout | Independent, P0 |

### Batch 1: Core Auth + Schema
Auth is the foundation for everything else.

| Order | Gap ID | Name | Reason |
|-------|--------|------|--------|
| 1.1 | **G10** | Participants Join Table | Schema refactor before adding more features |
| 1.2 | **G01** | Authentication (API Key) | P0, blocks all auth-dependent gaps |
| 1.3 | **G08** | buyer_id FK | Schema fix, pairs with auth |

### Batch 2: P0 Agent Lifecycle
Direct agent lifecycle blockers, depend on auth.

| Order | Gap ID | Name | Reason |
|-------|--------|------|--------|
| 2.1 | **G02** | Agent Profile Update | P0, blocks G06 |
| 2.2 | **G05** | Unclaim / Withdraw | P0, needs join table from G10 |
| 2.3 | **G06** | Payout Failure Handling | P0, needs G02 for wallet update |
| 2.4 | **G03** | Job Search / Pagination | P0, needs G09 indexes + G10 join table |

### Batch 3: P0 Events + P1 Features
Event push and remaining P1 items.

| Order | Gap ID | Name | Reason |
|-------|--------|------|--------|
| 3.1 | **G04** | Webhooks | P0, needs auth |
| 3.2 | **G12** | Proactive Expiry | P1, triggers webhooks from G04 |
| 3.3 | **G11** | Job Update Endpoint | P1, needs auth |
| 3.4 | **G13** | Rate Limiting | P1, needs auth for per-agent limits |
| 3.5 | **G16** | Submission Privacy | P1, needs auth for content filtering |
| 3.6 | **G17** | Idempotency | P1, standalone |
| 3.7 | **G19** | Fee Configurability | P1, schema + logic change |
| 3.8 | **G20** | Wallet Warning | P1, simple response enhancement |
| 3.9 | **G22** | Overpayment Handling | P1, wallet_service change |

### Batch 4: Operational
Background and admin features.

| Order | Gap ID | Name | Reason |
|-------|--------|------|--------|
| 4.1 | **G18** | Background Task Queue (Partial) | P1, builds on G07 + G14 |
| 4.2 | **G21** | Solvency Monitoring | P1, wallet_service + new endpoint |
| 4.3 | **G24** | Dispute Stub | P1, new model + endpoints |

---

## Flat Execution Order (for Phase 5)

For the Phase 5 loop, execute in this exact sequence:

```
5.1  → G15  Migration System
5.2  → G09  Database Indexes
5.3  → G14  Structured Logging
5.4  → G23  DEV_MODE Safety
5.5  → G07  Oracle Timeout
5.6  → G10  Participants Join Table
5.7  → G01  Authentication
5.8  → G08  buyer_id FK
5.9  → G02  Agent Profile Update
5.10 → G05  Unclaim / Withdraw
5.11 → G06  Payout Failure Handling
5.12 → G03  Job Search / Pagination
5.13 → G04  Webhooks
5.14 → G12  Proactive Expiry
5.15 → G11  Job Update Endpoint
5.16 → G13  Rate Limiting
5.17 → G16  Submission Privacy
5.18 → G17  Idempotency
5.19 → G19  Fee Configurability
5.20 → G20  Wallet Warning
5.21 → G22  Overpayment Handling
5.22 → G18  Background Task Queue
5.23 → G21  Solvency Monitoring
5.24 → G24  Dispute Stub
```

Total: **24 implementation items** (7 P0 + 17 P1)
