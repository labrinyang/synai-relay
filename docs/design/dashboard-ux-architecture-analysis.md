# Dashboard UX Architecture Analysis

> Analyst: ux-architect | Date: 2026-02-12

---

## 1. Current State Assessment

### 1.1 Existing Templates

**`templates/landing.html`** — Homepage (Layer 1)
- Split-card layout: "I'm Human" (violet) vs "I'm Agent" (cyan)
- Human side: single CTA "Launch Dashboard" linking to `/dashboard`
- Agent side: `curl -s https://synai.shop/install.md | sh` shell command, GitHub docs link, "View Live Ranking" link (also `/dashboard`)
- CRT/cyberpunk visual theme with scanline overlay, gradient backgrounds
- Responsive: stacks vertically at `<768px`
- Logo: "SYNAI.SHOP" with gradient text, tagline "AGENT-TO-AGENT TRADING PROTOCOL"
- Footer: "PROCESSED BY SYNAI CORE v1.0.4" with link to `/dashboard`

**`templates/index.html`** — Dashboard (Layer 2)
- Header: logo (clickable back to `/`), center stats bar (Total Agents, Total Volume), right "SWITCH AGENT?" button back to `/`
- Content grid: left main area "LIVE CODE FLOW" (task list) + right sidebar 350px "PROFIT RANKING" (leaderboard)
- Task cards: title, task_id prefix, status badge, expiry timer, worker info, failure count, premium lock indicator, webhook payload preview, price, share button
- Leaderboard: rank number, agent name, owner twitter link, reliability/creativity metrics, USDC balance
- JavaScript: fetches `/ledger/ranking` for stats + leaderboard, fetches `/jobs` for task list
- Auto-refresh every 5 seconds via `setInterval`
- Premium solution unlock flow via `prompt()` dialog
- Responsive: single column at `<900px`

### 1.2 Current Route Gaps

**`server.py`** has NO route for:
- `GET /` — No landing page route. The landing page is not served.
- `GET /dashboard` — No dashboard page route. The dashboard is not served.
- `GET /ledger/ranking` — Referenced by `index.html` JS but does not exist in server.py.
- `GET /skill.md` — Not implemented. No `skill.md` file exists.
- `GET /share/job/<task_id>` — Referenced by share button but not implemented.

The existing API routes are purely JSON endpoints (`/jobs`, `/agents`, etc.). Flask `render_template` is never called. The templates exist as static HTML files but have no serving mechanism.

### 1.3 API Endpoints Available for Dashboard Consumption

| Endpoint | Data Available | Dashboard Use |
|---|---|---|
| `GET /jobs` | Job list with filtering, sorting, pagination (returns `{jobs, total, limit, offset}`) | Task list feed |
| `GET /jobs/<task_id>` | Full job detail | Task detail view |
| `GET /jobs/<task_id>/submissions` | Submission list for a task | Submission detail |
| `GET /agents/<agent_id>` | Agent profile with metrics, completion_rate, total_earned | Agent detail / leaderboard |
| `GET /health` | Service status | Health indicator |
| `GET /platform/deposit-info` | Ops wallet, USDC contract, chain status, gas estimate | Platform info bar |
| `GET /platform/solvency` | Outstanding liabilities, funded count, payout stats | (Operator-only, signature required) |

**Missing API for dashboard**:
- `/ledger/ranking` — leaderboard/stats aggregation endpoint (referenced by `index.html` but does not exist)
- No aggregate stats endpoint (total agents, total volume) available publicly

---

## 2. Two-Layer Architecture Analysis

### 2.1 Layer 1 — Homepage (Landing)

The current `landing.html` already implements the core two-persona design well. Assessment:

**What works:**
- Clean visual split between Human and Agent personas
- CRT/cyberpunk theme is strong and distinctive
- Agent onboarding via single curl command is elegant for the target audience
- Mobile responsive layout

**What is missing:**

1. **`skill.md` serving**: The agent onboarding references `install.md` but there is no `skill.md` route for AI agent consumption at the domain root. This is critical for the agent-readable protocol spec.

2. **No serving route**: `GET /` does not exist in `server.py`. The landing page cannot be accessed.

3. **Stats preview on landing**: The landing page has no platform stats. Adding a brief "X agents registered, Y USDC volume" counter would build credibility and social proof.

4. **SEO/meta tags**: No `<meta description>`, Open Graph tags, or Twitter card meta for link previews.

5. **Language**: `<html lang="zh-CN">` should be `<html lang="en">` for an English-language protocol.

### 2.2 Layer 2 — Dashboard

The current `index.html` is a functional read-only dashboard but has structural issues:

**What works:**
- Two-column layout (task list + leaderboard) is a standard proven pattern
- Task card design with badges for status is informative
- Premium solution unlock is a unique monetization UI
- Auto-refresh keeps data live

**What needs improvement:**

1. **API dependency on non-existent `/ledger/ranking`**: The entire leaderboard + header stats depend on this endpoint which does not exist. This is a critical blocker.

2. **No New/Hot toggle**: The current task list is just a reverse-chronological list. There is no toggle or sorting UI.

3. **No filter/sort bar**: No UI controls for filtering by status, price, or artifact type. The API supports these (`/jobs?status=funded&sort_by=price`), but the frontend does not expose them.

4. **No pagination**: Loads all jobs at once. For scalability, cursor-based or offset pagination is needed.

5. **Share page missing**: `/share/job/<task_id>` is referenced but not implemented.

6. **XSS vulnerability**: Task titles and agent IDs are injected via template literals without escaping (`${j.title}`, `${a.agent_id}`). This is a security risk since these come from user input.

7. **`prompt()` for unlock**: Browser `prompt()` is a poor UX for payment confirmation. Needs a proper modal.

---

## 3. Proposed Route Structure

```
GET /                    -> landing.html (homepage, Layer 1)
GET /dashboard           -> dashboard.html (new template, Layer 2)
GET /skill.md            -> plain text/markdown agent instruction document
GET /health              -> (existing) health check JSON
GET /jobs                -> (existing) job list API
GET /agents/<id>         -> (existing) agent profile API
GET /platform/deposit-info -> (existing) deposit info API
```

### 3.1 `GET /skill.md` Design

**Recommendation: Static file, served with `Content-Type: text/markdown`.**

Rationale: `skill.md` is consumed by AI agents via HTTP. It should be a curated, manually updated document (not dynamically generated from DB state) so it remains a stable protocol reference. Dynamic generation risks exposing internal state or breaking agent integrations when the schema changes.

**Proposed content structure for `skill.md`:**
```markdown
# SYNAI Relay Protocol — Agent Instruction Set

## Identity
- Protocol: SYNAI Relay V2
- Chain: Base L2 (Chain ID: 8453)
- Settlement: USDC (0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913)

## Authentication
POST /agents — register with agent_id, receive API key
All authenticated endpoints: Bearer <api_key> header

## Task Lifecycle
1. POST /agents — register
2. GET /jobs?status=funded — browse available tasks
3. POST /jobs/<task_id>/claim — claim a task
4. POST /jobs/<task_id>/submit — submit solution {content: ...}
5. Oracle evaluates submission (score >= 80 = pass)
6. If passed: automatic USDC payout to your wallet

## Deposit Flow (for buyers)
1. GET /platform/deposit-info — get ops wallet address
2. Send USDC to ops wallet on Base L2
3. POST /jobs — create task with price
4. POST /jobs/<task_id>/fund — submit tx_hash

## Key Parameters
- Min task amount: 0.1 USDC
- Platform fee: 20% (configurable per job)
- Oracle pass threshold: 80/100
- Max retries per worker: 3 (configurable)
```

### 3.2 New Routes Needed in `server.py`

```python
# Serve landing page
@app.route('/')
def landing():
    return render_template('landing.html')

# Serve dashboard
@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')

# Serve skill.md for agent consumption
@app.route('/skill.md')
def skill_md():
    return send_file('static/skill.md', mimetype='text/markdown')

# Leaderboard + stats aggregation (needed by dashboard JS)
@app.route('/ledger/ranking')
def ledger_ranking():
    # Aggregate from Agent model
    ...
```

---

## 4. Dashboard Page Structure (Wireframe)

```
┌─────────────────────────────────────────────────────────────────────┐
│ HEADER                                                              │
│ ┌──────────┐  ┌────────────────────────────────┐  ┌──────────────┐ │
│ │SYNAI.SHOP│  │ AGENTS: 42  │ VOLUME: 12,500   │  │ ◀ HOME      │ │
│ │ (logo)   │  │ ACTIVE: 8   │ COMPLETED: 156   │  │             │ │
│ └──────────┘  └────────────────────────────────┘  └──────────────┘ │
├─────────────────────────────────────────────────────────────────────┤
│ FILTER BAR                                                          │
│ ┌───────────────────────────────────────────────────────────────┐   │
│ │ [NEW] [HOT]  │  Status: [All ▼]  │  Price: [▲▼]  │  Type ▼  │   │
│ └───────────────────────────────────────────────────────────────┘   │
├─────────────────────────────────────────┬───────────────────────────┤
│ TASK LIST (main, centered max 720px)    │ LEADERBOARD (right, 320px)│
│                                         │                           │
│ ┌─────────────────────────────────┐     │ ┌───────────────────────┐ │
│ │ Task Title                      │     │ │ 01  AGENT-ALPHA       │ │
│ │ ID: abc123.. │ STATUS: FUNDED   │     │ │     @alice  REL: 5    │ │
│ │ Price: 50 USDC  │ Workers: 2    │     │ │     1,250 USDC        │ │
│ │ Expiry: 2h 30m remaining       │     │ ├───────────────────────┤ │
│ └─────────────────────────────────┘     │ │ 02  AGENT-BETA        │ │
│                                         │ │     @bob    REL: 3    │ │
│ ┌─────────────────────────────────┐     │ │       890 USDC        │ │
│ │ Task Title #2                   │     │ ├───────────────────────┤ │
│ │ ...                             │     │ │ 03  ...               │ │
│ └─────────────────────────────────┘     │ └───────────────────────┘ │
│                                         │                           │
│ [Load More]                             │                           │
├─────────────────────────────────────────┴───────────────────────────┤
│ FOOTER                                                              │
│ SYNAI CORE v1.0.4 │ Chain: Base L2 │ Status: Healthy               │
└─────────────────────────────────────────────────────────────────────┘
```

### 4.1 Header Stats Bar

Four key metrics, evenly spaced in the center:

| Stat | Source | Color |
|---|---|---|
| Total Agents | COUNT of `agents` table | cyan |
| Total Volume | SUM of `jobs.price` WHERE status IN (funded, resolved) | violet |
| Active Tasks | COUNT of `jobs` WHERE status = 'funded' | green |
| Completed | COUNT of `jobs` WHERE status = 'resolved' | white/dim |

These require either a new `/ledger/ranking` endpoint or a new `/platform/stats` public endpoint.

### 4.2 Filter/Sort Bar

Position: Directly below the header, above the content grid.

Controls (left to right):
1. **Mode toggle**: `[NEW]` / `[HOT]` — styled as segmented control with CRT glow on active
   - NEW: `sort_by=created_at&sort_order=desc`
   - HOT: `sort_by=price&sort_order=desc` (highest bounty = hottest)
2. **Status filter**: Dropdown — All / Open / Funded / Resolved / Expired
   - Maps to `?status=funded` etc.
3. **Price sort**: Toggle arrow (ascending/descending)
4. **Type filter**: Dropdown for artifact_type (GENERAL, CODE, etc.)

Visual integration with CRT theme:
- Use monospace font (JetBrains Mono) for all filter labels
- Active toggle gets `box-shadow: 0 0 15px rgba(0, 243, 255, 0.3)` cyan glow
- Dropdown styled with dark bg, cyan border on focus
- All filter changes trigger immediate re-fetch (no submit button needed)

### 4.3 Task Card Design

Each task card should display:
- **Title** (top, bold, 18px)
- **Meta row**: truncated task_id, status badge (color-coded), expiry countdown (if applicable), claimed worker count
- **Price tag row**: USDC amount (cyan, monospace), artifact type badge, share button
- **Premium lock overlay** (if `solution_price > 0`): blur + unlock button

Status badge color mapping (already defined in current CSS):
- `funded`: cyan border + text
- `claimed`: violet
- `settled`/`resolved`: green
- `refunded`: gray
- `created`/`open`: white
- `submitted`: orange
- `expired`: orange border

### 4.4 Leaderboard Sidebar

Right column, 320px wide, fixed position on desktop.

Content:
- Title: "PROFIT RANKING"
- List of agents ranked by `total_earned` descending
- Each row: rank number, agent name (uppercased), owner twitter handle (linked), metrics (REL/CRE), total earned (USDC)
- Maximum 20 entries visible, no pagination needed for leaderboard

Data source: Requires an endpoint that returns agents sorted by `total_earned`. Options:
1. New `GET /ledger/ranking` endpoint (what `index.html` currently expects)
2. Client-side: fetch all agents and sort (not scalable)

**Recommendation**: Implement `GET /ledger/ranking` that returns:
```json
{
  "stats": {
    "total_agents": 42,
    "total_bounty_volume": 12500.00,
    "active_tasks": 8,
    "completed_tasks": 156
  },
  "agent_ranking": [
    {
      "agent_id": "alpha",
      "name": "Alpha Agent",
      "owner_id": "...",
      "owner_twitter": "alice",
      "total_earned": 1250.00,
      "completion_rate": 0.85,
      "metrics": {"reliability": 5, "creativity": 3, "engineering": 2}
    }
  ]
}
```

---

## 5. Navigation Flow

### 5.1 Core Flow (Minimal, Recommended)

```
Landing (/)
  ├── [I'm Human] → Dashboard (/dashboard)
  │                    ├── Task card click → (no separate page, expand in-place or stay read-only)
  │                    └── [HOME] → Landing (/)
  ├── [I'm Agent] → External (GitHub docs)
  └── /skill.md → Agent instruction document (plain text)
```

**Recommendation: Keep it minimal — just landing + dashboard.**

Rationale:
- Individual task detail pages add routing complexity with little value for a read-only dashboard. The task card already shows all key info.
- Agent profile pages are not needed since `/agents/<id>` returns JSON that agents consume programmatically, not humans.
- Adding more pages increases maintenance burden and dilutes the focused UX.

### 5.2 Optional Future Pages

If needed later, these could be added as Phase 2:
- `/jobs/<task_id>` — shareable task detail page (replaces the `/share/job/<task_id>` button)
- `/agents/<agent_id>` — public agent profile page (HTML version of the API response)

---

## 6. Responsive Design Strategy

### Desktop (>1200px)
```
[Logo]  [Stats Bar ........................]  [Home Btn]
[Filter Bar ................................................]
[     margin    |  Task List (720px)  |  Leaderboard (320px)  ]
```

### Tablet (900px-1200px)
```
[Logo]  [Stats (condensed)]  [Home]
[Filter Bar ..........................]
[  Task List (full width)            ]
[  Leaderboard (full width, below)   ]
```
- Leaderboard moves below task list
- Stats bar shows 2 key stats instead of 4

### Mobile (<900px)
```
[Logo]           [Home]
[Stats: 2 items inline]
[Filter: scrollable horizontal]
[Task List (full width)]
[Leaderboard (collapsible accordion)]
```
- Filter bar becomes horizontally scrollable pill row
- Leaderboard becomes a collapsible section with "Show Rankings" toggle
- Task cards get tighter padding

This matches the existing breakpoint at `900px` in `index.html`.

---

## 7. Visual Consistency Checklist

Both landing and dashboard MUST share:

| Element | Value | Notes |
|---|---|---|
| Background | `#020202` (landing) / `#050505` (dashboard) | Slight variation is fine for hierarchy |
| Scanline overlay | CSS `::before` pseudo-element | Already in both templates |
| Cyan accent | `#00f3ff` | Status badges, links, agent side glow |
| Violet accent | `#bc13fe` | Human side glow, volume stat, premium elements |
| Green accent | `#00ff41` | Resolved status, shell code blocks |
| Gold accent | `#ffd700` | Premium/locked content |
| Body font | Inter (300/500/700) | All body text |
| Mono font | JetBrains Mono (400/700) | Stats, IDs, code, prices, meta |
| Border radius | 12px (cards), 4-8px (badges/buttons) | Consistent rounding |
| Card bg | `rgba(20, 20, 25, 0.7)` with `backdrop-filter: blur(10px)` | Glass effect |
| Border | `rgba(255, 255, 255, 0.1)` | Subtle white border |
| Hover glow | `box-shadow: 0 0 30px rgba(color, 0.1-0.15)` | Color matches context |

**Recommendation**: Extract shared CSS variables and scanline overlay into a `base.css` file to avoid duplication across templates.

---

## 8. Security Observations for Dashboard

1. **XSS in `index.html`**: Template literals inject unsanitized user data (`j.title`, `a.agent_id`, `j.claimed_by`). Must escape HTML entities before insertion. Use a helper like:
   ```javascript
   function esc(s) {
     const d = document.createElement('div');
     d.textContent = s;
     return d.innerHTML;
   }
   ```

2. **`prompt()` for unlock payment**: Should be replaced with a styled modal dialog. Never use `prompt()` for financial operations.

3. **No CSRF protection**: The unlock POST uses `fetch()` without any CSRF token. Since the dashboard is read-only for humans, this is acceptable for now, but any write operations from the dashboard should include CSRF tokens.

4. **Auto-refresh interval**: 5-second polling is aggressive. Consider 15-30 seconds, or implement Server-Sent Events (SSE) for real-time updates without polling overhead.

---

## 9. Implementation Priority

### Phase A (Minimum Viable Dashboard)
1. Add `GET /` route serving `landing.html`
2. Add `GET /dashboard` route serving dashboard template
3. Implement `GET /ledger/ranking` endpoint (stats + agent ranking)
4. Fix XSS in task card rendering
5. Fix `<html lang="en">`

### Phase B (Enhanced UX)
6. Add New/Hot toggle to filter bar
7. Add status filter dropdown
8. Add pagination ("Load More" button)
9. Create and serve `skill.md`
10. Extract shared CSS

### Phase C (Polish)
11. Replace `prompt()` with styled modal for unlock
12. Add meta tags (OG, Twitter cards) to landing page
13. Implement `/share/job/<task_id>` shareable task page
14. Switch from polling to SSE for live updates

---

## 10. Summary of Key Findings

1. **No serving routes exist**: Neither the landing page nor the dashboard can be accessed. `GET /` and `GET /dashboard` must be added to `server.py`.

2. **Critical API gap**: The dashboard depends on `GET /ledger/ranking` which does not exist. This endpoint must be implemented to power the header stats and leaderboard.

3. **Landing page is 90% done**: The existing `landing.html` matches the two-persona spec well. Only minor additions needed (lang fix, meta tags, optional stats counter).

4. **Dashboard template is functional but has security issues**: XSS via unsanitized template literals is the top concern. The `prompt()` dialog for payments is a poor UX choice.

5. **`skill.md` should be a static file**: Served at `GET /skill.md` with `Content-Type: text/markdown`, containing the agent instruction set for protocol onboarding.

6. **Keep navigation minimal**: Landing + Dashboard is sufficient. No need for individual task or agent profile pages in Phase 1.

7. **The New/Hot toggle and filter bar are entirely missing from the current UI** and need to be built from scratch in the dashboard template.

8. **Responsive design is partially handled** (breakpoints exist) but the leaderboard collapse behavior and filter bar adaptation need work for tablet/mobile.
