# Full Codebase Security & Quality Review

**Date**: 2026-03-16
**Scope**: All production code — server.py, services/*, models.py, config.py
**Methodology**: 4 parallel review agents covering input validation, error handling, concurrency, config/secrets

---

## Executive Summary

| Category | Critical | High | Medium | Low |
|----------|----------|------|--------|-----|
| Input Validation | 3 | 5 | 6 | - |
| Error Handling | 2 | 4 | 4 | 4 |
| Concurrency | 2 | - | 3 | 2 |
| Config & Secrets | 2 | 5 | 4 | 3 |
| **Total** | **9** | **14** | **17** | **9** |

**What's already good:**
- IDOR protection: all endpoints properly check ownership (require_buyer, agent_id checks)
- Replay attack protection: deposit_tx_hash has unique constraint, IntegrityError caught
- USDC token validation: verify_deposit only processes USDC Transfer events
- Amount validation on fund: verify_deposit checks amount >= expected
- Double-resolve protection: conditional UPDATE WHERE status='funded'
- Double-refund protection: conditional UPDATE WHERE refund_tx_hash IS NULL
- WalletService nonce management: _tx_lock serializes transactions, self-healing on nonce desync
- Wallet address validation: regex check on registration and update
- Self-dealing prevention: buyer cannot claim own job

---

## CRITICAL — Must Fix

### SEC-1. No `tx_hash` format validation on `/fund` endpoint
- **Category**: Input Validation
- **File**: `server.py:1498-1501`
- **Issue**: `tx_hash` accepted as any non-empty string. No regex `^0x[0-9a-fA-F]{64}$` check. Stored in DB before on-chain verification.
- **Fix**: Add format validation before processing.

### SEC-2. No upper bound on `price` field
- **Category**: Input Validation
- **File**: `server.py:1318-1328`, `server.py:245-253`
- **Issue**: Lower bound (MIN_TASK_AMOUNT=0.1) exists, but no max. `Decimal("999999999999999999999999")` overflows `Numeric(20,6)` column, breaks atomic USDC conversion.
- **Fix**: Add `price > MAX_TASK_AMOUNT` cap (e.g., 1,000,000 USDC).

### SEC-3. PATCH `/jobs` — title/description updated without length limits
- **Category**: Input Validation
- **File**: `server.py:2183-2200`
- **Issue**: Create enforces title<=500, description<=50000, but PATCH has no limits. Attacker can set multi-MB description → oracle LLM context overflow.
- **Fix**: Apply same limits as _create_job.

### SEC-4. `validate_production()` is a no-op + weak default SECRET_KEY
- **Category**: Config & Secrets
- **File**: `config.py:12,60-63`
- **Issue**: SECRET_KEY defaults to `'dev-secret-key-change-me'` (public in source). `validate_production()` is `pass`. Server starts in "production" with SQLite, no wallet, default secret.
- **Fix**: Restore real validation — at minimum check SECRET_KEY, DATABASE_URL, OPERATIONS_WALLET_KEY.

### SEC-5. DB init failure doesn't halt startup
- **Category**: Error Handling
- **File**: `server.py:132-133`
- **Issue**: `except Exception as e: logger.critical(...)` logs but doesn't re-raise or exit. App runs with broken/incomplete schema → 500 on every request.
- **Fix**: Add `sys.exit(1)` or re-raise.

### SEC-6. Submit endpoint TOCTOU — job status not re-checked under row lock
- **Category**: Concurrency
- **File**: `server.py:1674-1724`
- **Issue**: Checks `job.status != 'funded'` at line 1678 (no lock), acquires row lock at line 1701, never re-checks status. Between check and lock, job could be cancelled/resolved → submission created against terminal-state job → wasted oracle LLM calls.
- **Fix**: Add `if job_for_submit.status != 'funded': return error` after line 1701.

### SEC-7. `get_wallet_service()` singleton not thread-safe
- **Category**: Concurrency
- **File**: `services/wallet_service.py:323-329`
- **Issue**: Classic check-then-act race. Two threads both see `None`, create two WalletService instances. WalletService holds `_tx_lock` and `_local_nonce` — orphaned instance means nonce management breaks.
- **Fix**: Add `threading.Lock()`, same pattern as `_x402_init_lock`.

### SEC-8. Global Flask error handler missing
- **Category**: Error Handling
- **File**: `server.py` (absent)
- **Issue**: No `@app.errorhandler(Exception)`. Unhandled exceptions (from bare `db.session.commit()` in 6+ locations) return Flask's default HTML error page instead of JSON. In debug mode, leaks full stack traces.
- **Fix**: Add global JSON error handler.

### SEC-9. Zero security response headers
- **Category**: Config & Secrets
- **File**: `server.py` (absent)
- **Issue**: No X-Frame-Options, CSP, X-Content-Type-Options, HSTS. Dashboard is vulnerable to clickjacking and MIME-sniffing.
- **Fix**: Add `@app.after_request` hook with standard security headers.

---

## HIGH — Should Fix

### H1. No `name` validation on agent registration
- **File**: `server.py:1074-1090`
- **Issue**: `agent_id` validated with strict regex, `wallet_address` validated, but `name` has no checks. Column is `String(100)` but no app-level length/type check. Potential XSS in dashboard.

### H2. Dispute `reason` has no length limit + no duplicate check
- **File**: `server.py:2375-2391`
- **Issue**: `reason` is `db.Text` (unbounded). Same agent can file unlimited disputes for same job. No `@rate_limit()` on endpoint.

### H3. `artifact_type` no whitelist + `max_submissions`/`max_retries` no upper bound
- **File**: `server.py:1335,1345-1351,2196-2200`
- **Issue**: Any string accepted for artifact_type. max_submissions can be set to 2^31-1.

### H4. `oracle_guard.llm_scan()` — no status check, no retry
- **File**: `services/oracle_guard.py:126-156`
- **Issue**: Goes straight to `resp.json()['choices'][0]` without checking `resp.ok`. Single transient 429 blocks legitimate submissions (fail-closed). `oracle_service._call_llm()` has retries but guard does not.

### H5. `oracle_service._call_llm()` — `data['choices'][0]` can KeyError
- **File**: `services/oracle_service.py:65`
- **Issue**: Empty `choices` array → IndexError. If first attempt, `last_error` is None → `raise None` → TypeError. Both guard and oracle have this pattern.

### H6. `onchainos_client` — no retry logic for HTTP calls
- **File**: `services/onchainos_client.py:45-70`
- **Issue**: `timeout=30` (good), `raise_for_status()` (good), but zero retries. Single OKX API failure kills entire x402 flow.

### H7. `encrypted_privkey` column exists but no encryption logic
- **File**: `models.py:53`
- **Issue**: Column named "encrypted" but no encrypt/decrypt code in codebase. If populated, private keys stored plaintext in misleadingly-named column.

### H8. No rate limiting on authentication failures
- **File**: `services/auth_service.py:28-46`
- **Issue**: Failed auth gets general IP rate limit (60/min) not a tighter auth-failure limit. Registration endpoint creates agents at 60/min/IP — Sybil attack vector.

### H9. `db.session.commit()` without try/except in 6+ service methods
- **Files**: `agent_service.py:21,69`, `webhook_service.py:88,104`, `server.py:1160,1392,2391`
- **Issue**: Any commit failure leaves session in dirty state. Covered by SEC-8 (global error handler) but should have targeted rollback.

---

## MEDIUM

| # | Category | File | Issue |
|---|----------|------|-------|
| M1 | Input | `server.py:2188` | PATCH job accepts non-string types for title/description |
| M2 | Input | `server.py:1338-1343` | Expiry accepts past timestamps (x402: money taken, job immediately expires) |
| M3 | Input | `server.py:2126` | Webhook event names not validated against known set |
| M4 | Input | `server.py:1417` | list_jobs query params not validated |
| M5 | Error | `server.py:780` | `fire_event` dispatched BEFORE db.session.commit() — webhook sent with uncommitted data |
| M6 | Error | `oracle_service.py:95-228` | Partial oracle failure loses all step outputs (API credits wasted) |
| M7 | Error | `server.py:403` | `_auto_refund` db.session.commit() has no try/except |
| M8 | Error | `wallet_service.py:103` | `w3.is_connected()` can raise ConnectionError — not caught |
| M9 | Concurrency | `server.py:387` | `_chain_registry` may be None during early expiry checks |
| M10 | Concurrency | `server.py:310-316` | `_ScheduledExecutor.ensure_pool` check-then-act race |
| M11 | Config | `server.py` | No CORS configuration |
| M12 | Config | `config.py:10` | SQLite default with no production guard |
| M13 | Config | `models.py:180` | Webhook secrets stored in plaintext |
| M14 | Config | `rate_limiter.py` | In-memory rate limiter resets on restart, doesn't scale across workers |
| M15 | Input | `server.py:1069-1112` | Registration IP-based rate limiting bypassable (rotating IPs) |
| M16 | Error | `server.py:1235` | Payment header parse error leaks internal details |
| M17 | Config | `.gitignore` | Missing `*.db-shm`, `*.db-wal`, `*.key`, `*.pem` patterns |

---

## Recommended Fix Priority

### Batch 1 — Quick wins (1-2 lines each, high impact)
| Issue | Fix | Effort |
|-------|-----|--------|
| SEC-1 | Add `re.match(r'^0x[0-9a-fA-F]{64}$', tx_hash)` | 2 lines |
| SEC-2 | Add `price > 1_000_000` cap | 2 lines |
| SEC-3 | Add length checks to PATCH job | 4 lines |
| SEC-6 | Re-check `job_for_submit.status` after lock | 2 lines |
| H1 | Add `name` length/type validation | 3 lines |
| H3 | Cap max_submissions<=100, max_retries<=10 | 4 lines |
| M2 | Reject past expiry timestamps | 2 lines |

### Batch 2 — Small functions (5-20 lines each)
| Issue | Fix | Effort |
|-------|-----|--------|
| SEC-4 | Restore validate_production() | 15 lines |
| SEC-5 | Add sys.exit(1) on DB init failure | 1 line |
| SEC-7 | Add lock to get_wallet_service() | 5 lines |
| SEC-8 | Add global @app.errorhandler(Exception) | 10 lines |
| SEC-9 | Add security headers @app.after_request | 10 lines |
| H2 | Add reason length limit + duplicate check | 5 lines |
| H5 | Wrap choices[0] in try/except KeyError/IndexError | 5 lines per file |

### Batch 3 — Moderate effort
| Issue | Fix | Effort |
|-------|-----|--------|
| H4 | Add retry logic to oracle_guard.llm_scan() | 20 lines |
| H6 | Add retry wrapper for onchainos_client | 15 lines |
| M5 | Move fire_event after commit | Refactor |

---

## Post-Fix Review (self-review of Batch 1-3 fixes)

**Date**: 2026-03-16 (same day, second pass)
**Method**: Abstracted 7 vulnerability patterns from the fixes above, then searched the entire codebase for unfixed instances of the same patterns.

### Patterns Abstracted

| # | Pattern | Description | Original Issues |
|---|---------|-------------|-----------------|
| P1 | Create validates, Update doesn't | Validation on POST missing on PATCH | SEC-3, H3 |
| P2 | Unvalidated user input stored | String/format accepted without checks | SEC-1, H1, H2 |
| P3 | No upper bound on numerics | Numeric fields with floor but no ceiling | SEC-2, H3 |
| P4 | TOCTOU: check then lock, no re-check | Status checked before lock, not re-verified after | SEC-6 |
| P5 | Singleton without lock | Module-level `None` → create race | SEC-7 |
| P6 | External API call without retry | Single failure kills entire flow | H4, H6 |
| P7 | Direct-index JSON response | `resp.json()['key'][0]` without try/except | H5 |

### New Findings

| # | Pattern | Severity | File | Line | Issue |
|---|---------|----------|------|------|-------|
| N1 | P1 | Medium | `server.py` | 2226 | PATCH open job expiry accepts past timestamps (create rejects them via M2, but update doesn't) |
| N2 | P3 | Low | `server.py` | 1456 | `list_jobs` limit has no upper bound cap (other list endpoints cap at 200) |
| N3 | P2 | Low | `server.py` | 1354 | `artifact_type` accepts any string; column is `String(20)`, longer values silently truncated |
| R1 | P1 | Low | `server.py` | 1102,1169 | Name length mismatch: register caps at 100, update allows 200, DB column is `String(100)` |
| R2 | — | Low | `server.py` | 314 | Global error handler `db.session.rollback()` can raise if DB connection is broken |

**Status**: All 5 fixed in commit following this review.
