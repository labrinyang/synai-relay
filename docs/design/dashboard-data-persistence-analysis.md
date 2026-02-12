# Dashboard Data Persistence & API Strategy Analysis

> **Author**: data-architect
> **Date**: 2026-02-12
> **Scope**: Read-only SYNAI Dashboard — data layer, API design, caching, refresh strategy

---

## 1. Same DB vs Read Replica

### Current State
- **Dev**: SQLite (`atp_dev.db`), single-process via Flask dev server
- **Prod**: PostgreSQL (enforced by `Config.validate_production()` — SQLite is rejected when `DEV_MODE=false`)
- Database URI: `Config.SQLALCHEMY_DATABASE_URI` from `DATABASE_URL` env var

### Recommendation: **Same DB with proper indexing (Phase 1); read replica deferred to Phase 2**

**Justification**:
- The dashboard is read-only. All writes come from API endpoints (`POST /jobs`, `/fund`, `/submit`, etc.) and one background thread (expiry checker, every 60s).
- Write throughput is low: task creation, funding, submission, and oracle evaluation are human/agent-paced — realistically tens to low hundreds of writes per minute at peak.
- PostgreSQL handles concurrent reads and writes well with MVCC; readers never block writers and vice versa.
- A read replica adds operational complexity (replication lag, connection routing, failover) that is unjustified at this scale.
- **When to revisit**: If dashboard concurrent users exceed ~500 or aggregate queries take >200ms under load, introduce a read replica using SQLAlchemy `binds`:

```python
# Future: config.py
SQLALCHEMY_BINDS = {
    'dashboard': os.environ.get('DASHBOARD_DATABASE_URL', SQLALCHEMY_DATABASE_URI)
}
```

---

## 2. Read-Write Concurrency

### Current State
- Flask runs on Gunicorn with sync workers (per `gunicorn==21.2.0` in requirements).
- Write operations use `with_for_update()` row-level locks for: fund, claim, unclaim, cancel, refund, retry-payout, oracle resolve.
- Background expiry checker thread acquires no explicit locks (just filters `status='funded'` + `expiry < now`).
- SQLAlchemy default isolation: uses the DB default (PostgreSQL = READ COMMITTED).

### Analysis

| Concern | Risk | Mitigation |
|---------|------|------------|
| Dashboard SELECT blocking writes | **None** — PostgreSQL MVCC means SELECTs never acquire row locks | N/A |
| Long aggregation queries blocking writes | **Negligible** — aggregate stats touch small row counts (agents: hundreds, jobs: thousands) | Use indexes, keep queries simple |
| Dirty reads on dashboard | **None** — READ COMMITTED is the default; dashboard sees committed data only | N/A |
| `with_for_update()` contention from dashboard | **None** — dashboard is read-only, never uses `FOR UPDATE` | Ensure dashboard endpoints never lock rows |

### Recommendation
- **Keep PostgreSQL default isolation (READ COMMITTED)**. No need for REPEATABLE READ or SERIALIZABLE.
- Dashboard queries must never use `with_for_update()`. This is naturally enforced since all dashboard endpoints are GET-only.
- For aggregate queries that touch multiple tables (e.g., stats), consider wrapping in a single transaction to get a consistent snapshot:

```python
@app.route('/dashboard/stats', methods=['GET'])
def dashboard_stats():
    with db.session.begin():  # Single transaction = consistent snapshot
        total_agents = db.session.query(func.count(Agent.agent_id)).scalar()
        # ... other aggregates
```

---

## 3. Data Aggregation Strategy

### Stats the Dashboard Needs

| Stat | Source | Query Complexity |
|------|--------|------------------|
| Total agents | `COUNT(agents)` | Trivial |
| Total volume (all time) | `SUM(jobs.price) WHERE status IN ('funded','resolved')` | Light |
| Tasks by status | `GROUP BY status` on jobs | Light |
| Active workers per task | `COUNT(job_participants) WHERE unclaimed_at IS NULL GROUP BY task_id` | Medium |
| Leaderboard (top agents by earnings) | `ORDER BY total_earned DESC LIMIT N` from agents | Light (indexed) |
| Hot tasks (by participant count) | Subquery on `job_participants` | Medium |

### Approach Comparison

| Approach | Freshness | Complexity | Performance |
|----------|-----------|------------|-------------|
| **On-the-fly queries** | Real-time | Low | Good for <10K rows |
| **Background aggregation thread** | 30-60s stale | Medium | Best for heavy aggregates |
| **PostgreSQL materialized view** | Manual refresh | Low app-side | Excellent read perf |
| **In-memory cache (TTL)** | TTL-based (e.g. 30s) | Low | Best overall |

### Recommendation: **In-memory TTL cache for aggregate stats; on-the-fly for lists**

**Rationale**:
- Aggregate stats (total agents, volume, task counts) change slowly and are queried by every dashboard user. Computing them on every request is wasteful.
- List queries (jobs, leaderboard) already have pagination and indexing — compute on-the-fly.
- A materialized view is PostgreSQL-specific and adds migration complexity. An in-memory cache achieves the same benefit with zero DB overhead.

**Implementation** — simple dict-based cache (no Redis dependency needed yet):

```python
# services/dashboard_service.py
import time
import threading
from sqlalchemy import func
from models import db, Agent, Job, Submission, JobParticipant

_stats_cache = {"data": None, "expires_at": 0}
_stats_lock = threading.Lock()
STATS_TTL = 30  # seconds

class DashboardService:
    @staticmethod
    def get_stats():
        now = time.time()
        with _stats_lock:
            if _stats_cache["data"] and now < _stats_cache["expires_at"]:
                return _stats_cache["data"]

        # Cache miss — compute
        total_agents = db.session.query(func.count(Agent.agent_id)).scalar()

        status_counts = dict(
            db.session.query(Job.status, func.count(Job.task_id))
            .group_by(Job.status).all()
        )

        total_volume = db.session.query(
            func.coalesce(func.sum(Job.price), 0)
        ).filter(Job.status.in_(['funded', 'resolved'])).scalar()

        total_submissions = db.session.query(func.count(Submission.id)).scalar()

        stats = {
            "total_agents": total_agents,
            "total_volume": float(total_volume),
            "total_submissions": total_submissions,
            "tasks_by_status": {
                "open": status_counts.get("open", 0),
                "funded": status_counts.get("funded", 0),
                "resolved": status_counts.get("resolved", 0),
                "expired": status_counts.get("expired", 0),
                "cancelled": status_counts.get("cancelled", 0),
            },
            "computed_at": time.time(),
        }

        with _stats_lock:
            _stats_cache["data"] = stats
            _stats_cache["expires_at"] = now + STATS_TTL

        return stats
```

---

## 4. Historical Data / Archival

### Current State
- All jobs remain in the `jobs` table regardless of status (open/funded/resolved/expired/cancelled).
- No archival, no soft-delete, no partitioning.
- `IdempotencyKey` has a 24h TTL checked at read-time (`is_expired` property), but expired keys are not proactively cleaned.

### Growth Projections
- At 100 tasks/day: ~36K rows/year. At 1000 tasks/day: ~365K rows/year.
- The `submissions` table grows faster (multiple submissions per task) — could be 3-10x job count.
- JSON columns (`content`, `oracle_steps`, `result_data`) are the heaviest payload.

### Recommendation: **No archival for Phase 1; add periodic cleanup for idempotency keys**

**Justification**:
- PostgreSQL handles millions of rows efficiently with proper indexing.
- The dashboard queries filter by status (indexed) and use pagination (LIMIT/OFFSET), so completed tasks don't degrade performance.
- Archival adds complexity (archive tables, data integrity, joins across tables) with no benefit until data exceeds ~1M rows.
- The `ix_jobs_status_created` composite index already makes status-filtered time-ordered queries efficient.

**Immediate action — clean up idempotency keys** (these have no dashboard value and grow unbounded):

```python
# Add to expiry_checker_loop or a separate periodic task
def _cleanup_idempotency_keys():
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)
    deleted = IdempotencyKey.query.filter(
        IdempotencyKey.created_at < cutoff
    ).delete(synchronize_session=False)
    db.session.commit()
    if deleted:
        logger.info("Cleaned up %d expired idempotency keys", deleted)
```

**Phase 2 archival** (when data exceeds 500K jobs):
- Use PostgreSQL table partitioning by `created_at` month.
- Partition the `submissions` table first (highest growth rate, largest rows due to JSON `content`).
- Avoid separate archive tables — partitioning is transparent to queries.

---

## 5. Refresh Strategy: Polling vs WebSocket vs SSE

### Current State
- Dashboard polls `/ledger/ranking` + `/jobs` every 5 seconds (see `templates/index.html:432`).
- `/ledger/ranking` endpoint does not exist in current `server.py` — it was a legacy V1 endpoint. This is a **broken reference** that needs to be replaced.
- No WebSocket or SSE infrastructure.

### Approach Comparison

| Approach | Latency | Server Load | Implementation Cost | Flask Compatibility |
|----------|---------|-------------|--------------------|--------------------|
| **Polling (5s)** | 0-5s | High (N users = N*2 req/5s) | Already done | Native |
| **Polling (15s) + ETag** | 0-15s | Low (304 for unchanged) | Low | Native |
| **SSE (Server-Sent Events)** | <1s | Low (1 conn/user) | Medium | Needs streaming response |
| **WebSocket (flask-socketio)** | <1s | Medium | High (new dep, event loop) | Requires eventlet/gevent |

### Recommendation: **Polling with ETag caching (Phase 1); SSE for event notifications (Phase 2)**

**Phase 1 — Smart Polling**:
- Increase poll interval from 5s to 15s (dashboard data doesn't change that fast).
- Add ETag support on `/dashboard/stats` and `/jobs` — return `304 Not Modified` when data hasn't changed:

```python
import hashlib

@app.route('/dashboard/stats', methods=['GET'])
def dashboard_stats():
    stats = DashboardService.get_stats()
    body = jsonify(stats)
    etag = hashlib.md5(body.get_data()).hexdigest()

    if request.if_none_match and etag in request.if_none_match:
        return '', 304

    response = make_response(body, 200)
    response.headers['ETag'] = etag
    response.headers['Cache-Control'] = 'private, max-age=15'
    return response
```

Client-side:
```javascript
// Use fetch with ETag for conditional requests
let statsEtag = null;
async function fetchStats() {
    const headers = {};
    if (statsEtag) headers['If-None-Match'] = statsEtag;
    const resp = await fetch('/dashboard/stats', { headers });
    if (resp.status === 304) return; // No change
    statsEtag = resp.headers.get('ETag');
    const data = await resp.json();
    updateStatsUI(data);
}
```

**Phase 2 — SSE for real-time events**:
- Flask supports SSE natively via streaming responses (no extra dependency):

```python
from flask import Response, stream_with_context
import queue

# Per-client event queues
_sse_clients = []
_sse_lock = threading.Lock()

def push_event(event_type, data):
    """Called from webhook_service.fire_event to push to SSE clients."""
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try:
                q.put_nowait({"event": event_type, "data": data})
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_clients.remove(q)

@app.route('/dashboard/events')
def dashboard_events():
    q = queue.Queue(maxsize=50)
    with _sse_lock:
        _sse_clients.append(q)
    def generate():
        try:
            while True:
                msg = q.get(timeout=30)
                yield f"event: {msg['event']}\ndata: {json.dumps(msg['data'])}\n\n"
        except queue.Empty:
            yield ": keepalive\n\n"  # Prevent timeout
        finally:
            with _sse_lock:
                if q in _sse_clients:
                    _sse_clients.remove(q)
    return Response(stream_with_context(generate()), mimetype='text/event-stream')
```

**Why not WebSocket**: flask-socketio requires eventlet or gevent, which conflicts with the current sync Gunicorn setup and the `ThreadPoolExecutor`-based oracle system. SSE is simpler, unidirectional (server-to-client, which is exactly what a read-only dashboard needs), and works with standard HTTP.

---

## 6. New API Endpoints

### Current Endpoints Used by Dashboard
- `GET /jobs` — job listing with filtering/sorting/pagination (works, but returns too much data per job)
- `GET /ledger/ranking` — **BROKEN** (endpoint does not exist in current `server.py`)

### Proposed Dashboard Endpoints

#### 6.1 `GET /dashboard/stats` — Aggregate Statistics

```
Response:
{
    "total_agents": 142,
    "total_volume": 25430.50,
    "total_submissions": 891,
    "tasks_by_status": {
        "open": 12, "funded": 8, "resolved": 95,
        "expired": 5, "cancelled": 3
    },
    "computed_at": 1739347200.0
}
```

- **Auth**: None (public)
- **Cache**: 30s TTL in-memory + ETag
- **Implementation**: `DashboardService.get_stats()` (see Section 3)

#### 6.2 `GET /dashboard/leaderboard` — Agent Ranking

```
GET /dashboard/leaderboard?sort_by=total_earned&limit=20

Response:
{
    "agents": [
        {
            "agent_id": "agent-alpha",
            "name": "Alpha",
            "total_earned": 1250.00,
            "completion_rate": 0.85,
            "metrics": {"engineering": 5, "creativity": 3, "reliability": 7},
            "tasks_won": 12
        }
    ],
    "total": 142
}
```

- **Auth**: None (public)
- **Sort options**: `total_earned` (default), `completion_rate`, `reliability`
- **Implementation**: Single indexed query on `agents` table + a subquery for `tasks_won` count

```python
@staticmethod
def get_leaderboard(sort_by='total_earned', limit=20, offset=0):
    sort_map = {
        'total_earned': Agent.total_earned.desc().nulls_last(),
        'completion_rate': Agent.completion_rate.desc().nulls_last(),
    }
    order = sort_map.get(sort_by, Agent.total_earned.desc().nulls_last())

    agents = Agent.query.order_by(order).offset(offset).limit(limit).all()
    total = Agent.query.count()

    # Batch-fetch win counts
    agent_ids = [a.agent_id for a in agents]
    wins = dict(
        db.session.query(Job.winner_id, func.count(Job.task_id))
        .filter(Job.winner_id.in_(agent_ids), Job.status == 'resolved')
        .group_by(Job.winner_id).all()
    )

    return [{
        "agent_id": a.agent_id,
        "name": a.name,
        "total_earned": float(a.total_earned or 0),
        "completion_rate": float(a.completion_rate) if a.completion_rate else None,
        "metrics": a.metrics or {},
        "tasks_won": wins.get(a.agent_id, 0),
    } for a in agents], total
```

#### 6.3 `GET /dashboard/hot-tasks` — Tasks Sorted by Participant Count

```
GET /dashboard/hot-tasks?limit=10

Response:
{
    "tasks": [
        {
            "task_id": "abc-123",
            "title": "Build RAG pipeline",
            "price": 50.0,
            "status": "funded",
            "participant_count": 7,
            "submission_count": 3,
            "created_at": "2026-02-12T10:00:00"
        }
    ]
}
```

- **Auth**: None (public)
- **Filter**: Only `funded` status (active tasks)
- **Implementation**:

```python
@staticmethod
def get_hot_tasks(limit=10):
    subq = db.session.query(
        JobParticipant.task_id,
        func.count(JobParticipant.id).label('participant_count')
    ).filter(
        JobParticipant.unclaimed_at.is_(None)
    ).group_by(JobParticipant.task_id).subquery()

    tasks = db.session.query(Job, subq.c.participant_count).outerjoin(
        subq, Job.task_id == subq.c.task_id
    ).filter(
        Job.status == 'funded'
    ).order_by(
        subq.c.participant_count.desc().nulls_last()
    ).limit(limit).all()

    return [{"task_id": j.task_id, "title": j.title, "price": float(j.price),
             "status": j.status, "participant_count": pc or 0,
             "created_at": j.created_at.isoformat()} for j, pc in tasks]
```

### Endpoint Design Decision: Separate vs Query Params

**Recommendation: Separate endpoints** (`/dashboard/stats`, `/dashboard/leaderboard`, `/dashboard/hot-tasks`).

**Reasons**:
- Each endpoint has different caching characteristics (stats: 30s TTL; leaderboard: 60s; hot-tasks: 15s).
- The existing `/jobs` endpoint returns full job details including description, rubric, and oracle config — too heavy for a dashboard list view. A separate `/dashboard/hot-tasks` can return a lightweight projection.
- Separate endpoints allow independent ETag caching per resource.
- The dashboard frontend can parallelize fetches to different endpoints.

---

## 7. Caching Layer

### Recommendation: **In-memory TTL cache (Phase 1); add Redis only when scaling horizontally**

**Why not Flask-Caching**: It adds a dependency for what is essentially a dict with a TTL. The dashboard has 3 cacheable endpoints — a 30-line custom cache is simpler and more debuggable.

**Why not Redis (yet)**: With a single Gunicorn process (or multiple workers on one machine), in-memory caching works. Redis becomes necessary only when running multiple server instances behind a load balancer, where cache coherence matters.

### Cache TTLs

| Endpoint | TTL | Reason |
|----------|-----|--------|
| `/dashboard/stats` | 30s | Aggregate counts change slowly |
| `/dashboard/leaderboard` | 60s | Earnings update only on job resolution |
| `/dashboard/hot-tasks` | 15s | Participant claims happen more frequently |
| `/jobs` (existing) | 0 (no cache) | Already paginated + indexed; cache invalidation is complex for filtered lists |

### ETag Strategy

For browser-level caching, every dashboard endpoint should:
1. Compute a response body
2. Generate an ETag (MD5 of response JSON)
3. Check `If-None-Match` header; return 304 if match
4. Set `Cache-Control: private, max-age=<TTL>`

This reduces bandwidth (304 has no body) and gives the browser permission to skip requests within the `max-age` window.

### Implementation Pattern

```python
# services/cache_utils.py
import time, threading, hashlib
from flask import request, make_response, jsonify

class TTLCache:
    def __init__(self, ttl_seconds):
        self._data = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds

    def get(self, key):
        with self._lock:
            entry = self._data.get(key)
            if entry and time.time() < entry["expires"]:
                return entry["value"]
            return None

    def set(self, key, value):
        with self._lock:
            self._data[key] = {"value": value, "expires": time.time() + self._ttl}

    def invalidate(self, key=None):
        with self._lock:
            if key:
                self._data.pop(key, None)
            else:
                self._data.clear()

def etag_response(data, cache_max_age=15):
    """Wrap a dict in a JSON response with ETag support."""
    body = jsonify(data)
    raw = body.get_data()
    etag = hashlib.md5(raw).hexdigest()

    if request.if_none_match and etag in request.if_none_match:
        return '', 304

    response = make_response(body, 200)
    response.headers['ETag'] = etag
    response.headers['Cache-Control'] = f'private, max-age={cache_max_age}'
    return response
```

---

## 8. Database Indexes Needed

### Existing Indexes (from migration)

| Index | Table | Columns |
|-------|-------|---------|
| `ix_jobs_status` | jobs | status |
| `ix_jobs_status_created` | jobs | status, created_at |
| `ix_jobs_buyer_id` | jobs | buyer_id |
| `ix_submissions_task_id` | submissions | task_id |
| `ix_submissions_worker_id` | submissions | worker_id |
| `ix_submissions_task_worker` | submissions | task_id, worker_id |
| `ix_job_participants_task_id` | job_participants | task_id |
| `ix_job_participants_worker_id` | job_participants | worker_id |
| `ix_agents_api_key_hash` | agents | api_key_hash (unique) |

### Additional Indexes for Dashboard

```sql
-- 1. Leaderboard: sort agents by total_earned (most common leaderboard query)
CREATE INDEX ix_agents_total_earned ON agents (total_earned DESC NULLS LAST);

-- 2. Leaderboard: sort by completion_rate (alternative ranking)
CREATE INDEX ix_agents_completion_rate ON agents (completion_rate DESC NULLS LAST);

-- 3. Hot tasks: winner_id for counting wins per agent in leaderboard
CREATE INDEX ix_jobs_winner_id ON jobs (winner_id) WHERE winner_id IS NOT NULL;

-- 4. Stats: count resolved jobs with successful payouts
CREATE INDEX ix_jobs_payout_status ON jobs (payout_status) WHERE payout_status IS NOT NULL;

-- 5. Active participants: filter unclaimed_at IS NULL (used by hot-tasks + job listing)
CREATE INDEX ix_job_participants_active ON job_participants (task_id)
    WHERE unclaimed_at IS NULL;

-- 6. Idempotency cleanup: find expired keys
CREATE INDEX ix_idempotency_created ON idempotency_keys (created_at);
```

### Alembic Migration

```python
# migrations/versions/xxxx_dashboard_indexes.py
def upgrade():
    op.create_index('ix_agents_total_earned', 'agents',
                    [sa.text('total_earned DESC NULLS LAST')])
    op.create_index('ix_agents_completion_rate', 'agents',
                    [sa.text('completion_rate DESC NULLS LAST')])
    op.create_index('ix_jobs_winner_id', 'jobs', ['winner_id'],
                    postgresql_where=sa.text("winner_id IS NOT NULL"))
    op.create_index('ix_jobs_payout_status', 'jobs', ['payout_status'],
                    postgresql_where=sa.text("payout_status IS NOT NULL"))
    op.create_index('ix_job_participants_active', 'job_participants', ['task_id'],
                    postgresql_where=sa.text("unclaimed_at IS NULL"))
    op.create_index('ix_idempotency_created', 'idempotency_keys', ['created_at'])
```

---

## 9. Known Issue: Missing `/ledger/ranking` Endpoint

The current dashboard template (`templates/index.html:325`) calls `fetch('/ledger/ranking')`, but this endpoint does not exist in `server.py`. This was a V1 endpoint that was removed during the V2 rewrite.

**Impact**: The existing dashboard header stats and leaderboard sidebar are **completely broken** — they fail silently (the `catch` block at line 404 logs to console but shows no error to the user).

**Fix**: The new `/dashboard/leaderboard` and `/dashboard/stats` endpoints replace this. The frontend JS must be updated to call the new endpoints.

---

## 10. Summary of Recommendations

| # | Decision | Recommendation | Phase |
|---|----------|---------------|-------|
| 1 | DB topology | Same DB, no read replica | 1 |
| 2 | Isolation level | Keep READ COMMITTED (PostgreSQL default) | 1 |
| 3 | Stats aggregation | In-memory TTL cache (30s) | 1 |
| 4 | Data archival | No archival; add idempotency key cleanup | 1 |
| 5 | Refresh strategy | Polling (15s) + ETag | 1 |
| 6 | New endpoints | `/dashboard/stats`, `/dashboard/leaderboard`, `/dashboard/hot-tasks` | 1 |
| 7 | Caching | In-memory TTL + ETag; Redis deferred | 1 |
| 8 | New indexes | 6 new indexes (see Section 8) | 1 |
| 9 | SSE real-time | Native Flask streaming for event push | 2 |
| 10 | Read replica | Add when >500 concurrent dashboard users | 2 |
| 11 | Table partitioning | Partition `submissions` by month when >500K rows | 2 |

### Critical Path for Dashboard MVP
1. Add 6 database indexes (Alembic migration)
2. Implement `DashboardService` with cached stats, leaderboard, hot-tasks
3. Add 3 new `/dashboard/*` endpoints to `server.py`
4. Add ETag support via `etag_response()` helper
5. Update frontend JS to call new endpoints instead of `/ledger/ranking`
6. Increase poll interval from 5s to 15s

### What NOT to Do
- Do not add Redis, flask-socketio, or celery as dependencies for Phase 1
- Do not create materialized views (PostgreSQL-specific, adds migration complexity)
- Do not archive completed tasks (premature optimization at current scale)
- Do not use `with_for_update()` in any dashboard endpoint
