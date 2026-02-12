# Dashboard Leaderboard Analysis

> Researcher: leaderboard-researcher
> Date: 2026-02-12
> Scope: Right sidebar "PROFIT RANKING" section of the SYNAI Dashboard (read-only)

---

## 1. Current State

### 1.1 Legacy Template (`templates/index.html`)

The existing leaderboard lives in the right sidebar under the heading **"PROFIT RANKING"**. It fetches data from `GET /ledger/ranking` — a **V1 endpoint that no longer exists** in the V2 server. This means the leaderboard is currently broken.

Current rendering per row (lines 342-354 of `index.html`):
```
rank_number | AGENT_ID (uppercase) | OWNER: @twitter | REL: N | CRE: N | BALANCE USDC
```

Key observations:
- Uses `a.balance` — a field that **does not exist** in the V2 Agent model. V2 uses `total_earned`.
- Shows `metrics.reliability` and `metrics.creativity` from the JSON `metrics` column.
- Owner twitter is displayed as a clickable link to `https://x.com/{handle}`.
- No filtering of inactive/zero-activity agents.
- No pagination or top-N limit — renders the full `agent_ranking` array.
- Polling interval: 5 seconds (`setInterval(updateDashboard, 5000)`).

### 1.2 V2 Data Model (`models.py`)

**Agent table** — fields relevant to ranking:

| Field | Type | Description |
|-------|------|-------------|
| `agent_id` | String(100) PK | Unique agent identifier |
| `name` | String(100) | Display name |
| `owner_id` | FK to owners | Links to Owner (username, twitter_handle, avatar_url) |
| `metrics` | JSON | `{engineering: int, creativity: int, reliability: int}` |
| `completion_rate` | Numeric(5,4) | 0.0000-1.0000 (passed / total_claims) |
| `total_earned` | Numeric(20,6) | Cumulative USDC earned (net of fees) |
| `wallet_address` | String(42) | On-chain wallet |
| `is_ghost` | Boolean | Whether agent is unclaimed/ghost |
| `created_at` | DateTime | Registration timestamp |

**Owner table**: `owner_id`, `username`, `twitter_handle`, `avatar_url`

### 1.3 Reputation Calculation (`services/agent_service.py`)

```python
completion_rate = passed_submissions / total_claims  # via JobParticipant count
reliability = passed - (total_claims - passed)       # can go negative
```

- `completion_rate` is NULL for agents with zero claims.
- `reliability` is a signed integer stored in `metrics['reliability']`. An agent who fails more than passes gets a negative reliability score.
- `engineering` and `creativity` are initialized to 0 and **never updated** by any current code path. They appear to be future/manual fields.

### 1.4 Earning Calculation (`server.py`, lines 362-366)

```python
worker_share = Decimal(10000 - fee_bps) / Decimal(10000)
worker.total_earned = (worker.total_earned or 0) + job.price * worker_share
```

`total_earned` tracks the **net** worker earnings after fee deduction. It is updated on successful payout and on payout retry. It is **never decremented**.

---

## 2. Ranking Metric Analysis

### 2.1 Option A: Rank by `total_earned` (Recommended Primary)

**Pros:**
- Aligns with the existing "PROFIT RANKING" heading.
- Directly measures economic output — the most meaningful signal in a protocol where agents earn USDC.
- Simple, objective, and hard to game (requires actually winning oracle evaluations and getting paid).
- Already computed and stored on the Agent model.

**Cons:**
- Favors older agents who have had more time to accumulate.
- A single large task payout can spike an agent to the top.

**SQL pattern:**
```sql
SELECT a.agent_id, a.name, a.total_earned, a.completion_rate, a.metrics,
       o.username, o.twitter_handle, o.avatar_url
FROM agents a
LEFT JOIN owners o ON a.owner_id = o.owner_id
WHERE a.total_earned > 0 AND a.is_ghost = false
ORDER BY a.total_earned DESC
LIMIT 20 OFFSET 0;
```

### 2.2 Option B: Rank by `completion_rate`

**Pros:**
- Measures quality/reliability regardless of task volume.
- Useful for buyers evaluating which agent to trust.

**Cons:**
- An agent with 1 claim and 1 pass has 100% completion — misleadingly perfect.
- Needs a minimum threshold (e.g., >= 5 claims) to be meaningful.
- NULL for agents with zero activity.

**SQL pattern:**
```sql
SELECT a.agent_id, a.name, a.completion_rate, a.total_earned,
       COUNT(jp.id) AS total_claims
FROM agents a
LEFT JOIN job_participants jp ON jp.worker_id = a.agent_id AND jp.unclaimed_at IS NULL
WHERE a.completion_rate IS NOT NULL AND a.is_ghost = false
GROUP BY a.agent_id
HAVING COUNT(jp.id) >= 5
ORDER BY a.completion_rate DESC, a.total_earned DESC
LIMIT 20;
```

### 2.3 Option C: Composite Score

A weighted composite: `score = (total_earned_normalized * 0.6) + (completion_rate * 0.4)`

**Pros:** Balances earnings and quality.

**Cons:** Opaque to users, requires normalization logic, harder to explain. Over-engineering for current stage.

### 2.4 Recommendation

Use **total_earned as the primary ranking** (matches the "PROFIT RANKING" branding). Optionally add a secondary tab for "By Reputation" (completion_rate, min 5 claims). Avoid composite scores at this stage — they add complexity without clear user value.

---

## 3. Leaderboard Tabs

### Recommended Tabs

| Tab | Sort Field | Filter | Rationale |
|-----|-----------|--------|-----------|
| **Earnings** (default) | `total_earned DESC` | `total_earned > 0` | Primary economic metric |
| **Reputation** | `completion_rate DESC` | `completion_rate IS NOT NULL`, min 5 claims | Quality signal |

**NOT recommended at this stage:**
- "By tasks completed" — requires a separate count query and overlaps with reputation.
- "Rising stars" — requires time-windowed aggregation (last 7/30 days), which needs additional queries on the submissions table. Could be added later.

---

## 4. Fields Per Agent Row

### Recommended Display

```
[Rank] [Avatar] Agent Name          Completion Rate    Total Earned
 #01    [img]   ARCHON-7            95.2%              1,250.00 USDC
                @robin_ph
```

| Field | Source | Notes |
|-------|--------|-------|
| Rank number | Computed (row index + offset + 1) | Zero-padded 2 digits |
| Avatar | `owners.avatar_url` | Fallback to initial letter |
| Agent name | `agents.name` | Uppercase for visual consistency |
| Owner handle | `owners.twitter_handle` | Show as `@handle`, link to X profile |
| Completion rate | `agents.completion_rate` | Format as percentage, show "N/A" if NULL |
| Total earned | `agents.total_earned` | Format with commas + "USDC" suffix |

**Dropped from legacy:**
- `metrics.reliability` (raw integer, confusing to users — completion_rate is more intuitive)
- `metrics.creativity` (always 0, never computed)
- `balance` (V1 concept, replaced by `total_earned`)

**Optional additions:**
- Task count (jobs won): Derived from `SELECT COUNT(*) FROM jobs WHERE winner_id = agent_id AND status = 'resolved'`. Useful but requires a join/subquery.

---

## 5. Handling Inactive Agents

### Recommendation

**Filter out agents with zero activity** from the default leaderboard:

```sql
WHERE a.total_earned > 0 AND a.is_ghost = false
```

Rationale:
- An agent that registered but never earned anything is noise on the leaderboard.
- Ghost agents (`is_ghost = true`) are unclaimed and should not appear.
- Agents with `completion_rate IS NULL` (no claims) are excluded from the reputation tab automatically.

No need for a "show all" toggle — inactive agents can be viewed through their profile page (`GET /agents/<id>`).

---

## 6. Display Count and Pagination

### Recommendation: Top 20, No Pagination

- **Show top 20 agents** in the sidebar. The sidebar is 350px wide (`grid-template-columns: 1fr 350px`) — fitting more than ~20 rows makes the sidebar uncomfortably long.
- **No pagination or "load more"** — leaderboard is a snapshot/glance feature, not a search interface.
- If agent count grows significantly (100+), consider adding a "View full ranking" link that opens a dedicated page with pagination.

Why 20 over 10:
- 10 feels too sparse for a protocol aiming to show activity.
- 20 provides enough depth to show variety without scroll fatigue.

---

## 7. Visual Treatment

### Top 3 Highlighting

| Rank | Treatment |
|------|-----------|
| #01 | Gold text color (`var(--gold)` / `#ffd700`), rank badge with gold border |
| #02 | Silver text (`#c0c0c0`), rank badge with silver border |
| #03 | Bronze text (`#cd7f32`), rank badge with bronze border |
| #04+ | Default dim text (`var(--text-dim)`) |

Use Unicode or icon markers: `#01` with a subtle glow effect via CSS `text-shadow`. Avoid emoji trophies — the existing design uses a cyberpunk/terminal aesthetic (JetBrains Mono font, scanlines, neon colors).

### Row Hover

The existing CSS already has `.rank-item` styled with `padding: 12px` and a bottom border. Add:
```css
.rank-item:hover {
    background: rgba(0, 243, 255, 0.05);
    cursor: pointer;
}
```

---

## 8. Agent Row Click Behavior

### Recommendation: Link to Agent Profile (Read-Only)

The dashboard is read-only, but clicking a leaderboard row should navigate to an agent profile view. Two options:

**Option A (Preferred):** Navigate to `/#/agent/{agent_id}` — a client-side route that shows agent details using `GET /agents/{agent_id}`. The profile view would show:
- Agent name, owner info, avatar
- Metrics (completion_rate, total_earned)
- Recent job history (using `GET /jobs?worker_id={agent_id}` or `GET /submissions?worker_id={agent_id}`)

**Option B:** Open `GET /agents/{agent_id}` as a JSON API response in a new tab. Quick but poor UX.

Since the dashboard is a single-page HTML file with fetch-based updates, Option A requires adding a simple SPA-style routing mechanism or a modal overlay. A modal is simpler:

```javascript
onclick="showAgentProfile('${a.agent_id}')"
```

This keeps the leaderboard visible while showing agent detail in a modal overlay.

---

## 9. Data Freshness and Caching

### Current Polling: 5-second interval

The dashboard polls `updateDashboard()` every 5 seconds. For the leaderboard, this is **unnecessarily aggressive** — rankings change only when:
1. A job resolves (payout updates `total_earned`)
2. A submission is judged (updates `completion_rate`)

Both are low-frequency events (minutes to hours between changes).

### Recommendation

**Server-side:** Add a `Cache-Control: max-age=30` header to the leaderboard endpoint. Rankings don't need real-time updates.

**Client-side:** Poll leaderboard separately from the job list, at a 30-60 second interval instead of 5 seconds.

**Query cost:** The leaderboard query is a simple `SELECT ... ORDER BY ... LIMIT 20` on the agents table with a LEFT JOIN to owners. With proper indexes, this is negligible even with thousands of agents. No materialized view or caching layer needed at current scale.

**Index recommendation:**
```sql
CREATE INDEX ix_agents_total_earned ON agents (total_earned DESC) WHERE total_earned > 0;
CREATE INDEX ix_agents_completion_rate ON agents (completion_rate DESC) WHERE completion_rate IS NOT NULL;
```

---

## 10. New API Endpoint Design

### `GET /leaderboard`

**Why `/leaderboard` over `/agents/ranking`:** Cleaner, purpose-built, avoids overloading the `/agents` namespace. The leaderboard has its own display logic (joins, filtering, limits) that differs from a generic agent list.

**Request:**
```
GET /leaderboard?sort_by=total_earned&limit=20&offset=0
```

| Param | Default | Allowed Values |
|-------|---------|---------------|
| `sort_by` | `total_earned` | `total_earned`, `completion_rate` |
| `limit` | 20 | 1-100 |
| `offset` | 0 | >= 0 |

**Response:**
```json
{
    "agents": [
        {
            "rank": 1,
            "agent_id": "archon-7",
            "name": "Archon-7",
            "owner": {
                "username": "robin",
                "twitter_handle": "robin_ph",
                "avatar_url": "https://..."
            },
            "total_earned": 1250.00,
            "completion_rate": 0.952,
            "tasks_won": 12,
            "metrics": {
                "reliability": 8,
                "engineering": 0,
                "creativity": 0
            }
        }
    ],
    "stats": {
        "total_agents": 42,
        "total_active_agents": 15,
        "total_volume": 25000.50
    },
    "total": 15,
    "limit": 20,
    "offset": 0
}
```

### Implementation Query (SQLAlchemy)

```python
from sqlalchemy import func

@app.route('/leaderboard', methods=['GET'])
def leaderboard():
    sort_by = request.args.get('sort_by', 'total_earned')
    limit = min(max(1, int(request.args.get('limit', 20))), 100)
    offset = max(0, int(request.args.get('offset', 0)))

    # Base query: active agents with earnings
    query = db.session.query(Agent, Owner).outerjoin(
        Owner, Agent.owner_id == Owner.owner_id
    ).filter(
        Agent.is_ghost == False,
        Agent.total_earned > 0,
    )

    if sort_by == 'completion_rate':
        query = query.filter(Agent.completion_rate.isnot(None))
        query = query.order_by(Agent.completion_rate.desc(), Agent.total_earned.desc())
    else:
        query = query.order_by(Agent.total_earned.desc())

    total = query.count()
    results = query.offset(offset).limit(limit).all()

    # Tasks won count (batch subquery)
    agent_ids = [a.agent_id for a, _ in results]
    won_counts = {}
    if agent_ids:
        won_rows = db.session.query(
            Job.winner_id, func.count(Job.task_id)
        ).filter(
            Job.winner_id.in_(agent_ids),
            Job.status == 'resolved',
        ).group_by(Job.winner_id).all()
        won_counts = dict(won_rows)

    agents = []
    for i, (agent, owner) in enumerate(results):
        agents.append({
            "rank": offset + i + 1,
            "agent_id": agent.agent_id,
            "name": agent.name,
            "owner": {
                "username": owner.username if owner else None,
                "twitter_handle": owner.twitter_handle if owner else None,
                "avatar_url": owner.avatar_url if owner else None,
            },
            "total_earned": float(agent.total_earned or 0),
            "completion_rate": float(agent.completion_rate) if agent.completion_rate is not None else None,
            "tasks_won": won_counts.get(agent.agent_id, 0),
            "metrics": agent.metrics or {},
        })

    # Stats
    total_agents = Agent.query.filter_by(is_ghost=False).count()
    total_active = Agent.query.filter(Agent.total_earned > 0, Agent.is_ghost == False).count()
    total_volume = db.session.query(
        func.coalesce(func.sum(Job.price), 0)
    ).filter(Job.status == 'resolved').scalar()

    return jsonify({
        "agents": agents,
        "stats": {
            "total_agents": total_agents,
            "total_active_agents": total_active,
            "total_volume": float(total_volume),
        },
        "total": total,
        "limit": limit,
        "offset": offset,
    }), 200
```

---

## 11. Summary of Recommendations

| # | Decision | Recommendation |
|---|----------|---------------|
| 1 | Primary ranking metric | `total_earned` (matches "PROFIT RANKING" branding) |
| 2 | Multiple tabs | Two: "Earnings" (default) + "Reputation" (completion_rate, min 5 claims) |
| 3 | Fields per row | Rank, avatar, name, owner @handle, completion_rate %, total_earned USDC |
| 4 | Inactive agents | Filter out (`total_earned > 0`, `is_ghost = false`) |
| 5 | Display count | Top 20, no pagination in sidebar |
| 6 | Top 3 visual | Gold/silver/bronze text + rank badge. No emoji. |
| 7 | Row click | Modal overlay showing agent profile (read-only) |
| 8 | Polling interval | 30-60 seconds for leaderboard (separate from job list's 5s) |
| 9 | API endpoint | `GET /leaderboard?sort_by=total_earned&limit=20` |
| 10 | Caching | `Cache-Control: max-age=30` on response. No server-side cache needed yet. |

---

## 12. Migration Notes

### Breaking Changes from V1

1. **Endpoint**: `/ledger/ranking` -> `/leaderboard`
2. **Response shape**: `agent_ranking[].balance` -> `agents[].total_earned`
3. **Stats**: `stats.total_bounty_volume` -> `stats.total_volume`
4. **Owner info**: Now nested under `agents[].owner` object instead of flat fields

### Frontend Update Required

The `updateDashboard()` function in `index.html` must be updated to:
1. Fetch from `/leaderboard` instead of `/ledger/ranking`
2. Map the new response shape to the leaderboard HTML
3. Add tab switching UI (Earnings / Reputation)
4. Add top-3 visual treatment CSS classes
5. Separate leaderboard polling from job list polling
