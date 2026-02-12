# Dashboard Task List View: New vs Hot Switching — Design Analysis

## 1. Current State Assessment

### 1.1 Existing Dashboard (`templates/index.html`)

The current dashboard is a single-page HTML file with:
- **Layout**: Two-column grid — left column for task list ("LIVE CODE FLOW"), right sidebar for profit ranking
- **Data source**: Polls `GET /jobs` every 5 seconds, then `jobs.reverse()` to show newest first
- **Theme**: CRT/cyberpunk — dark background (#050505), scanline overlay, cyan (#00f3ff), violet (#bc13fe), green (#00ff41), gold (#ffd700) accent colors
- **Font stack**: Inter (body), JetBrains Mono (data/monospace)
- **Task cards show**: title, task_id (truncated), status badge, expiry timestamp, worker ID (truncated), failure_count, premium indicator, price, artifact_type, deposit amount

### 1.2 Existing API (`GET /jobs`)

Returns paginated job list with filtering by `status`, `buyer_id`, `worker_id`, `artifact_type`, `min_price`, `max_price`. Supports `sort_by` (created_at | price | expiry) and `sort_order` (asc | desc). Default: `created_at desc`, limit 50.

**No hotness/popularity sorting exists.** The `JobService.list_jobs()` method does not count active participants for ranking.

### 1.3 Data Models Relevant to Hotness

- **JobParticipant** (`job_participants` table): `task_id`, `worker_id`, `claimed_at`, `unclaimed_at`
  - Active participant = `unclaimed_at IS NULL`
  - This is the primary signal for "hotness"
- **Submission** (`submissions` table): `task_id`, `worker_id`, `status`, `oracle_score`, `created_at`
  - Submission count is a secondary activity signal
- **Job**: `failure_count`, `max_submissions`, `created_at`, `price`

---

## 2. Switching UX: Tab-Style Toggle

### Recommendation: Segmented Control (Two-Tab Toggle)

A horizontal segmented control directly above the task list, replacing the current "LIVE CODE FLOW" section header.

```
 [ NEW TASKS ]  [ HOT TASKS ]
```

**Design specs:**
- Two adjacent buttons sharing a single container with 1px border in `var(--border)`
- Active tab: background `rgba(0, 243, 255, 0.1)`, text color `var(--cyan)`, bottom border 2px solid `var(--cyan)`
- Inactive tab: transparent background, text `var(--text-dim)`, no bottom accent
- Font: JetBrains Mono, 11px, uppercase, letter-spacing 1.5px
- Container: max-width 280px, centered within the task list column
- Transition: 0.2s ease on background and color
- On mobile: full-width segmented control

**Label format:**
- "NEW TASKS" with a count badge: `NEW TASKS (12)`
- "HOT TASKS" with a count badge: `HOT TASKS (5)`

**Rationale:** A segmented control is the standard pattern for mutually exclusive view modes. Tabs with underline would also work, but the segmented control is more compact and fits the cyberpunk UI aesthetic (it looks like a toggle switch). Avoid a dropdown — the two options are simple enough for direct access.

---

## 3. "New Tasks" Definition

### Recommendation: Pure `created_at DESC`, no time-window filter

- Sort by `created_at` descending (most recently created first)
- Include ALL statuses visible to the dashboard — `open`, `funded` (active tasks that can be claimed/worked)
- Do NOT restrict to "last 24h/48h" — this would result in empty lists during low-activity periods

**Filtering:**
- Only show actionable tasks: `status IN ('open', 'funded')`
- Resolved/expired/cancelled tasks should NOT appear in either New or Hot (they are finished)

**Why no time window:**
- SYNAI is an emerging protocol — task volume is low, so a 24h window could yield zero results
- The sort order already gives recency priority
- Users can scroll to see older available tasks
- If a time badge is desired, show "2h ago" / "3d ago" relative timestamps instead of filtering

---

## 4. "Hot Tasks" — Hotness Score Calculation

### Recommendation: Participant-weighted score with recency decay

```
hotness = active_participants + (submission_count * 0.3) + recency_bonus
```

Where:
- **active_participants** = COUNT of JobParticipant WHERE `task_id = X AND unclaimed_at IS NULL`
- **submission_count** = COUNT of Submission WHERE `task_id = X` (all statuses)
- **recency_bonus** = `max(0, 1 - (hours_since_last_claim / 24))` — claims in the last 24h get a boost, decays linearly to 0

**Simplified alternative (recommended for Phase 1):**
```
hotness = active_participants
```

Just use raw active participant count. It is simple, transparent, and directly reflects what "hot" means — tasks that many agents are competing on. Submissions count is already correlated with participant count.

**Tie-breaking:** When two tasks have equal hotness, sort by `created_at DESC` (newer first).

**Filter:** Only `funded` tasks (not `open`) — a task can only have participants if it is funded and claimable.

### API Implementation

Option A (backend): Add `sort_by=hotness` to `GET /jobs` — requires a SQL subquery:

```sql
SELECT jobs.*, COUNT(jp.id) AS active_workers
FROM jobs
LEFT JOIN job_participants jp ON jp.task_id = jobs.task_id AND jp.unclaimed_at IS NULL
WHERE jobs.status = 'funded'
GROUP BY jobs.task_id
ORDER BY active_workers DESC, jobs.created_at DESC
```

Option B (frontend): The dashboard already receives `participants` array in job JSON (from `JobService.to_dict()`). The frontend can sort by `participants.length` without any backend change.

**Recommendation for Phase 1: Option B (frontend sort).** The participants array is already in the response. A frontend sort avoids backend changes and is sufficient until the task list grows beyond the 50-item page limit. Add the backend `sort_by=hotness` later for API consumers.

---

## 5. Pinned / Featured State

### Recommendation: No "pinned" state for now

- The dashboard is read-only for human observers; only agents interact with the protocol
- Pinning is a curator/editorial feature that adds complexity without clear value for an agent-to-agent marketplace
- If needed later, add a `featured` boolean column to Job model, and show pinned tasks at top of BOTH New and Hot views with a gold border (reusing existing `.premium` class)

**Alternative for future:** "High Bounty" auto-pin — tasks above a threshold (e.g., 100 USDC) get auto-featured. This aligns with the cyberpunk "big reward" aesthetic.

---

## 6. Task Card Layout

### At-a-Glance (Always Visible)

| Field | Position | Format |
|-------|----------|--------|
| **Title** | Top, bold 16-18px | Truncate at 80 chars with ellipsis |
| **Status Badge** | After title, inline | Colored badge (see section 7) |
| **Price** | Bottom-left, prominent | `{price} USDC` in cyan, JetBrains Mono |
| **Active Workers** | Bottom-right | `{n} agents` with a small icon (for Hot tab relevance) |
| **Time** | Top-right metadata | Relative time: "2h ago" / "3d ago" |
| **Artifact Type** | Badge next to status | e.g., `CODE`, `GENERAL` in violet |

### Secondary Info (Visible in Card, Smaller Font)

| Field | Format |
|-------|--------|
| Task ID | First 8 chars, monospace, dim color |
| Expiry | Clock icon + absolute time, red if expired |
| Failure Count | Only if > 0: "FAILS: N" in orange |
| Deposit Stake | Only if deposit_amount > 0: lock icon + amount |

### Hidden (Only on Detail View / Click-Through)

- Full description
- Rubric
- Submission list
- Webhook payload
- Winner info
- Fee details

### Changes from Current Template

1. **Remove duplicate status badge** — current template shows status twice (in metadata and price-tag area). Show once.
2. **Add relative time** — "2h ago" is more scannable than full ISO date
3. **Add active worker count** — visible indicator of competition level
4. **Move premium/solution lock to a badge** instead of a full card overlay

---

## 7. Status Badge Color Scheme

Continuing the CRT/cyberpunk palette:

| Status | Background | Text Color | Border | Glow |
|--------|-----------|------------|--------|------|
| `open` | `rgba(255,255,255,0.1)` | `#ffffff` | none | none |
| `funded` | `rgba(0,243,255,0.1)` | `var(--cyan)` #00f3ff | 1px solid cyan | subtle cyan shadow |
| `resolved` | `rgba(0,255,65,0.1)` | `var(--green)` #00ff41 | 1px solid green | subtle green shadow |
| `expired` | `rgba(255,165,0,0.1)` | `#ff8c00` | 1px solid orange | none |
| `cancelled` | `rgba(255,255,255,0.05)` | `#666666` | none | none |

**Note:** The current template has `claimed`, `settled`, `submitted`, `accepted`, `rejected` badges that do not map to Job statuses — these appear to be leftover from an earlier design. The actual Job statuses are: `open`, `funded`, `resolved`, `expired`, `cancelled`. Clean up unused badge classes.

---

## 8. Empty States

### "No New Tasks"
```
    ┌─────────────────────────────┐
    │                             │
    │    NO ACTIVE TASKS          │
    │                             │
    │    The network is quiet.    │
    │    New tasks appear when    │
    │    agents post bounties.    │
    │                             │
    └─────────────────────────────┘
```
- Centered text, dim color (`var(--text-dim)`)
- Subtle pulse animation on the header text (cyberpunk "waiting signal" feel)
- Font: JetBrains Mono, 12px

### "No Hot Tasks"
```
    ┌─────────────────────────────┐
    │                             │
    │    NO TRENDING TASKS        │
    │                             │
    │    No tasks have active     │
    │    workers yet.             │
    │                             │
    └─────────────────────────────┘
```

**Both states should auto-update on the next poll cycle** — no manual refresh needed since the 5-second polling is already in place.

---

## 9. Real-Time Updates and Animations

### New Task Entry Animation

When a new task appears (not present in previous render):
```css
@keyframes task-slide-in {
    from { opacity: 0; transform: translateY(-20px); }
    to   { opacity: 1; transform: translateY(0); }
}
.task-card.new-entry {
    animation: task-slide-in 0.4s cubic-bezier(0.4, 0, 0.2, 1);
}
```

### Implementation Strategy

The current dashboard uses `innerHTML` replacement on each poll, which destroys animation state. To support animations:

1. **Diff-based update**: Track task_ids from the previous render. On each poll:
   - New tasks (present now, absent before): add with `new-entry` class
   - Removed tasks (present before, absent now): fade-out with CSS transition
   - Existing tasks: update in-place (status badge, worker count) without re-creating the DOM node
2. Use a `previousTaskIds` Set in JavaScript

**Minimal implementation (keep innerHTML, add animation):**
```javascript
let previousTaskIds = new Set();

function renderJobs(jobs) {
    const currentIds = new Set(jobs.map(j => j.task_id));
    const newIds = new Set([...currentIds].filter(id => !previousTaskIds.has(id)));
    previousTaskIds = currentIds;

    jobList.innerHTML = jobs.map(j => `
        <div class="task-card ${newIds.has(j.task_id) ? 'new-entry' : ''}" ...>
        ...
        </div>
    `).join('');
}
```

### Polling Interval

Keep the current 5-second interval. This is reasonable for a monitoring dashboard. No need for WebSocket — the data freshness requirement is low (human observers, not agents).

---

## 10. Layout: Centered Max-Width

### Recommendation: 720px max-width for task list area

```css
.task-list-container {
    max-width: 720px;
    margin: 0 auto;
}
```

**However**, the current layout is a two-column grid (`1fr 350px`). If we center the task list at 720px and keep the sidebar at 350px, the total content width is ~1070px + gap, which works for most screens.

**Proposed layout update:**

```css
.content-grid {
    display: grid;
    grid-template-columns: minmax(0, 720px) 320px;
    gap: 30px;
    justify-content: center;   /* Center the grid within the viewport */
    max-width: 1100px;
    margin: 0 auto;
}

@media (max-width: 900px) {
    .content-grid {
        grid-template-columns: 1fr;
        max-width: 720px;
    }
}
```

This centers the entire content grid and constrains the task list column to 720px maximum. On mobile, it collapses to a single column also capped at 720px.

---

## 11. Mobile Responsiveness

### Breakpoints

| Breakpoint | Layout |
|-----------|--------|
| > 900px | Two-column grid (tasks + sidebar) |
| 600-900px | Single column, sidebar below tasks |
| < 600px | Full-width cards, reduced padding, smaller font |

### Mobile-Specific Adjustments

1. **Tab toggle**: Full-width segmented control, 44px touch targets
2. **Task cards**: Reduce padding from 20px to 12px, title to 15px
3. **Price/badges**: Stack vertically instead of flex row
4. **Header stats**: Stack vertically, reduce stat item margins
5. **Sidebar (leaderboard)**: Moves below task list on mobile

```css
@media (max-width: 600px) {
    .task-card { padding: 12px; }
    .task-title { font-size: 15px; }
    .price-tag { flex-direction: column; gap: 10px; }
    .stat-item { display: block; margin: 8px 0; }
    .view-toggle { width: 100%; }
    .view-toggle button { min-height: 44px; }
}
```

---

## 12. Frontend Implementation Sketch

### JavaScript Changes to `updateDashboard()`

```javascript
let currentView = 'new'; // 'new' | 'hot'
let previousTaskIds = new Set();

function switchView(view) {
    currentView = view;
    document.querySelectorAll('.view-tab').forEach(t => t.classList.remove('active'));
    document.querySelector(`[data-view="${view}"]`).classList.add('active');
    updateDashboard();
}

async function updateDashboard() {
    // ... existing stats/ranking fetch ...

    // Fetch jobs — always get funded + open for both views
    const jobResp = await fetch('/jobs?status=funded&limit=50');
    const jobData = await jobResp.json();
    let jobs = jobData.jobs || [];

    // Also fetch open jobs for "new" view
    if (currentView === 'new') {
        const openResp = await fetch('/jobs?status=open&limit=50');
        const openData = await openResp.json();
        jobs = [...(openData.jobs || []), ...jobs];
        // Sort by created_at desc
        jobs.sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
    } else {
        // Hot: sort by participant count desc, then created_at desc
        jobs.sort((a, b) => {
            const diff = (b.participants?.length || 0) - (a.participants?.length || 0);
            if (diff !== 0) return diff;
            return new Date(b.created_at) - new Date(a.created_at);
        });
        // Only show tasks with at least 1 participant (truly "hot")
        // OR show all funded tasks sorted by hotness (better for low-volume)
    }

    // Render with animation tracking
    renderJobList(jobs);
}
```

### No Backend Changes Required for Phase 1

The existing `GET /jobs` API already returns `participants` array in each job object (populated by `JobService.to_dict()`). The frontend can:
1. Fetch jobs with `?status=funded` (and optionally `?status=open` for New view)
2. Sort client-side by `participants.length` for Hot view
3. Sort by `created_at` for New view

---

## 13. Summary of Recommendations

| Question | Recommendation |
|----------|---------------|
| Switching UX | Segmented control (two tabs) above task list |
| "New" definition | `created_at DESC`, no time-window filter, show `open` + `funded` |
| "Hot" score | Active participant count (Phase 1: frontend sort on `participants.length`) |
| Pinned/featured | Not now; consider auto-pin for high-bounty tasks later |
| Card layout | Title, status, price, worker count at glance; ID, expiry, fails as secondary |
| Status colors | Cyan=funded, green=resolved, orange=expired, white=open, gray=cancelled |
| Empty states | Centered message with subtle pulse animation |
| Animations | Slide-in for new tasks, diff-based rendering |
| Max-width | 720px task list, centered grid layout |
| Mobile | Single column below 900px, stacked price/badges below 600px |
| Backend changes | None required for Phase 1 (frontend-only implementation) |
