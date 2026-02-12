# Dashboard Integration Verification — Design Plan

## Goal

Use real on-chain test (`test_e2e_onchain.py`) to verify Dashboard frontend-backend connectivity.
Two processes share `atp_dev.db`: the test writes real chain data, the server serves the Dashboard.
The test programmatically verifies Dashboard APIs via HTTP (`requests`), while the user watches the Dashboard live in browser.

## Architecture

```
Terminal 1:  python server.py                    → localhost:5005/dashboard (user watches)
Terminal 2:  pytest tests/test_e2e_onchain.py -v -m onchain -s  (test runs)

Both share:  atp_dev.db (absolute path, WAL mode)

Test flow:
  register agents → create job → fund (real USDC) → claim → submit → oracle (real GPT-4o) → payout
  After each step: requests.get('http://localhost:5005/...') → assert Dashboard API returns correct data
```

## Pre-requisites (Critical Fixes)

### Fix 1: SQLite WAL Mode

**Problem:** No WAL mode configured. Two processes (test + server) writing to same SQLite file will cause `database is locked`.

**Fix:** Add SQLAlchemy engine event in `server.py` (after `db.init_app(app)`):

```python
from sqlalchemy import event

@event.listens_for(db.engine, "connect")
def _set_sqlite_pragma(dbapi_conn, connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()
```

**Files:** `server.py`

### Fix 2: Absolute DB Path

**Problem:** `sqlite:///atp_dev.db` is relative to CWD. Test and server may resolve to different files if run from different directories.

**Fix:** In `config.py`:

```python
import os

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

class Config:
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        'DATABASE_URL',
        f'sqlite:///{os.path.join(_BASE_DIR, "atp_dev.db")}'
    )
```

**Files:** `config.py`

### Fix 3: Expiry Format Mismatch

**Problem:** `test_e2e_onchain.py:399` sends ISO 8601 string (`datetime.isoformat()`), but `server.py:877` parses with `int(raw_expiry)` (expects Unix timestamp). Job creation will 400.

**Fix:** In `test_e2e_onchain.py`, change expiry to Unix timestamp:

```python
"expiry": int((datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)).timestamp()),
```

**Files:** `tests/test_e2e_onchain.py`

## High Priority Fixes

### Fix 4: Agent Registration Idempotency

**Problem:** Test uses fixed agent IDs (`e2e-buyer-001`, `e2e-worker-001`). Second run fails with 409 because `atp_dev.db` is not cleaned between runs.

**Fix:** In test fixtures, handle 409 by fetching existing agent or deleting DB before run:

```python
@pytest.fixture(scope="module")
def buyer(app_client):
    rv = app_client.post('/agents', json={...})
    if rv.status_code == 409:
        # Agent exists from previous run — re-register with login
        # Or: delete atp_dev.db before test run
    ...
```

Preferred approach: delete `atp_dev.db` at module start (clean slate for each test session).

**Files:** `tests/test_e2e_onchain.py`

### Fix 5: Missing Fields in `to_dict()`

**Problem:** `JobService.to_dict()` is missing `failure_count` and `deposit_amount`. Dashboard JS references `failure_count` (never renders). `deposit_amount` needed to distinguish price vs actual deposit.

**Fix:** Add to `services/job_service.py` `to_dict()`:

```python
"failure_count": job.failure_count or 0,
"deposit_amount": float(job.deposit_amount) if job.deposit_amount else None,
```

**Files:** `services/job_service.py`

## Core: DashboardVerifier

### Fix 6: Integration Verification Class

Add `DashboardVerifier` to `test_e2e_onchain.py` with 7 verification checkpoints.

**Design:**
- Uses `requests` library to hit REAL running server at `http://localhost:5005`
- Gracefully skips all verification if server is not running (prints warning, does NOT fail test)
- Polls cached endpoints (stats: 45s timeout, leaderboard: 75s timeout) to handle DashboardService cache TTL

**Verification Checkpoints:**

| # | After Step | API Endpoint | Assertions |
|---|-----------|-------------|------------|
| C1 | Agent registration | `GET /dashboard/stats` | `total_agents >= 2` |
| C2 | Job created | `GET /jobs?limit=100` | Job in list, `status == "open"` |
| C3 | Job funded | `GET /jobs/{task_id}` + `GET /dashboard/stats` | `status == "funded"`, `deposit_tx_hash` set, `total_volume >= 0.10` |
| C4 | Worker claims | `GET /jobs/{task_id}` | `participants` contains worker `agent_id` |
| C5 | Worker submits | `GET /jobs/{task_id}` | `submission_count >= 1` |
| C6 | Oracle resolves + payout | `GET /jobs/{task_id}` + `GET /dashboard/leaderboard` | `status == "resolved"`, `winner_id` set, `payout_tx_hash` set, worker in leaderboard with `total_earned >= 0.08` |
| C7 | Final state | `GET /dashboard/leaderboard` | Worker `tasks_won >= 1` |

**Server startup check:**

```python
def ensure_server(self):
    try:
        r = requests.get(f"{self.BASE}/health", timeout=5)
        self._available = r.status_code == 200
    except requests.ConnectionError:
        self._available = False
        print("═" * 60)
        print("[DASHBOARD] Server not detected at localhost:5005.")
        print("  Dashboard verification will be SKIPPED.")
        print("  To enable: run `python server.py` in another terminal.")
        print("═" * 60)
    return self._available
```

**Files:** `tests/test_e2e_onchain.py`

## UI Fixes

### Fix 7: Dashboard UI Improvements

**7a. Detail panel auto-refresh:**

When `selectedTaskId` is set, re-fetch detail data on each poll cycle (every 10s).

In `dashboard.html`, after `fetchJobsAndStats()` completes:
```javascript
if (selectedTaskId) fetchDetail(selectedTaskId);
```

**7b. Safe `refund_tx_hash` rendering:**

Only render as BaseScan link if value starts with `0x`. Otherwise render as plain text.

```javascript
if (job.refund_tx_hash) {
    if (job.refund_tx_hash.startsWith('0x')) {
        // render as BaseScan link
    } else {
        // render as plain text (e.g., "off-chain", "pending")
    }
}
```

**Files:** `templates/dashboard.html`

## Out of Scope (Separate Tasks)

| Item | Reason |
|------|--------|
| N+1 query fix in `to_dict()` | Only 1-2 jobs in integration test; optimize when scaling |
| Remove mock wallet from `test_server_api.py` / `test_e2e_scenarios.py` | Separate concern from dashboard integration |
| Expose `GET /dashboard/hot-tasks` route | Dead code cleanup, not needed for verification |
| Pagination for >100 jobs | Not relevant for integration test |
| Submission detail endpoint | Nice-to-have for future dashboard enhancement |
| `depositor_address` display in UI | Low priority, data already in API response |

## Implementation Order

```
Fix 1 (WAL) → Fix 2 (abs path) → Fix 3 (expiry) → Fix 4 (idempotent)
  → Fix 5 (to_dict fields) → Fix 6 (DashboardVerifier) → Fix 7 (UI fixes)
```

## Verification

1. Delete `atp_dev.db`
2. Start server: `python server.py` (Terminal 1)
3. Open browser: `http://localhost:5005/dashboard`
4. Run test: `pytest tests/test_e2e_onchain.py -v -m onchain -s` (Terminal 2)
5. Watch Dashboard update in real-time as test progresses
6. Test output shows `[DASHBOARD] C1: PASS` through `[DASHBOARD] C7: PASS`
7. All 7 verification checkpoints pass

## Team Discussion Sources

This plan was produced from a 5-agent parallel investigation:
- **Researcher**: API inventory, data flow mapping, gap analysis
- **Architect**: DashboardVerifier design, checkpoint strategy, timing/cache handling
- **UI Reviewer**: Dashboard completeness, auto-refresh, XSS, edge cases
- **Devil's Advocate**: SQLite concurrency, path resolution, expiry bug, session isolation
- **Backend Reviewer**: API contracts, N+1 queries, missing fields, data types
