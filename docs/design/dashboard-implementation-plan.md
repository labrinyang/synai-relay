# SYNAI Dashboard 实施计划

> 日期: 2026-02-12
> 分支: feat/agent-lifecycle-api
> 前置文档: dashboard-synthesis-report.md（需求分析汇总）

## Context

SYNAI Relay 是一个 Agent-to-Agent 任务交易协议（Flask + SQLAlchemy）。当前 `server.py` 是纯 JSON REST API，没有任何 HTML 渲染路由。两个已有的 HTML 模板（`landing.html`, `index.html`）无法被访问。Dashboard 的排行榜依赖不存在的 V1 端点 `/ledger/ranking`。

本计划实现一个只读 Dashboard，分为 8 个步骤，顺序执行。

---

## 涉及文件

**新建文件：**
- `services/dashboard_service.py` — Dashboard 数据服务 + TTL 缓存
- `templates/dashboard.html` — 新 Dashboard 模板
- `static/skill.md` — Agent 指令文档
- `migrations/versions/xxxx_dashboard_indexes.py` — 新索引迁移

**修改文件：**
- `server.py` — 添加 5 个路由（`/`, `/dashboard`, `/skill.md`, `/dashboard/stats`, `/dashboard/leaderboard`）
- `templates/landing.html` — 修复 `lang="zh-CN"` → `lang="en"`

**不修改的文件：**
- `models.py` — 无 schema 变更
- `config.py` — 无配置变更
- 现有 API 端点 — 不动

---

## Step 1: 创建 `services/dashboard_service.py`

Dashboard 数据服务层，包含缓存和聚合查询。

### 1.1 TTL 缓存工具

```python
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

def etag_response(data, cache_max_age=15):
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

### 1.2 DashboardService 类

三个核心方法：

**`get_stats()`** — 聚合统计（TTL 30s）
- `COUNT(agents)` → total_agents
- `SUM(jobs.price) WHERE status IN ('funded','resolved')` → total_volume
- `GROUP BY status` on jobs → tasks_by_status
- `COUNT(agents) WHERE total_earned > 0` → total_active_agents

**`get_leaderboard(sort_by, limit, offset)`** — Agent 排名（TTL 60s）
- 查询 Agent LEFT JOIN Owner，按 `total_earned DESC` 或 `completion_rate DESC`
- 过滤：`is_ghost = False`, `total_earned > 0`（earnings tab）或 `completion_rate IS NOT NULL`（reputation tab）
- 批量子查询 `tasks_won`：`COUNT(jobs) WHERE winner_id = agent_id AND status = 'resolved'`
- 返回嵌套 owner 对象（username, twitter_handle, avatar_url）

**`get_hot_tasks(limit)`** — 热门任务（按 worker 数降序）
- 子查询：`COUNT(job_participants) WHERE unclaimed_at IS NULL GROUP BY task_id`
- 主查询：`Job LEFT JOIN subquery WHERE status = 'funded' ORDER BY participant_count DESC`
- 返回轻量字段（task_id, title, price, status, participant_count, submission_count, failure_count, expiry, created_at）
- 不含 description/rubric（减少传输量）

---

## Step 2: server.py 新增路由

在 `server.py` 中添加以下路由。需要 `from flask import render_template, send_from_directory`。

### 2.1 页面路由

```python
# GET / — 首页 Landing
@app.route('/')
def landing():
    return render_template('landing.html')

# GET /dashboard — Dashboard 页面
@app.route('/dashboard')
def dashboard_page():
    return render_template('dashboard.html')

# GET /skill.md — Agent 指令文档
@app.route('/skill.md')
def skill_md():
    return send_from_directory('static', 'skill.md', mimetype='text/markdown')
```

### 2.2 Dashboard API 路由

```python
# GET /dashboard/stats
@app.route('/dashboard/stats', methods=['GET'])
def dashboard_stats():
    from services.dashboard_service import DashboardService, etag_response
    stats = DashboardService.get_stats()
    return etag_response(stats, cache_max_age=30)

# GET /dashboard/leaderboard
@app.route('/dashboard/leaderboard', methods=['GET'])
def dashboard_leaderboard():
    from services.dashboard_service import DashboardService, etag_response
    sort_by = request.args.get('sort_by', 'total_earned')
    limit = min(max(1, int(request.args.get('limit', 20))), 100)
    offset = max(0, int(request.args.get('offset', 0)))
    data = DashboardService.get_leaderboard(sort_by=sort_by, limit=limit, offset=offset)
    return etag_response(data, cache_max_age=30)
```

**注意**：不删除任何现有路由。现有 `GET /jobs` 端点继续工作。

---

## Step 3: Alembic 迁移 — 6 个新索引

创建新迁移文件 `migrations/versions/xxxx_dashboard_indexes.py`：

```python
def upgrade():
    # 排行榜排序
    op.create_index('ix_agents_total_earned', 'agents',
                    [sa.text('total_earned DESC NULLS LAST')])
    op.create_index('ix_agents_completion_rate', 'agents',
                    [sa.text('completion_rate DESC NULLS LAST')])
    # 胜出任务计数
    op.create_index('ix_jobs_winner_id', 'jobs', ['winner_id'],
                    postgresql_where=sa.text("winner_id IS NOT NULL"))
    # 结算状态统计
    op.create_index('ix_jobs_payout_status', 'jobs', ['payout_status'],
                    postgresql_where=sa.text("payout_status IS NOT NULL"))
    # 热门任务（活跃参与者）
    op.create_index('ix_job_participants_active', 'job_participants', ['task_id'],
                    postgresql_where=sa.text("unclaimed_at IS NULL"))
    # 幂等键清理
    op.create_index('ix_idempotency_created', 'idempotency_keys', ['created_at'])
```

注意：`DESC NULLS LAST` 和 `postgresql_where` 是 PostgreSQL 特性。SQLite dev 环境需要简化索引。

---

## Step 4: 创建 `static/skill.md`

基于现有 `templates/agent_manual.md`（V1 内容已过时）重写为 V2 协议文档。内容包括：

- 协议身份（Base L2, USDC 合约地址, 20% 费率）
- Quick Start（6 步 Worker 流程）
- 认证方式（Bearer API Key）
- Buyer Flow（创建任务 + Deposit）
- 关键参数（最低金额 0.1 USDC, 评分阈值 80, 重试 3 次, 超时 120s）
- 完整 API 参考列表

---

## Step 5: 创建 `templates/dashboard.html`

新建 Dashboard 模板（不修改现有 `index.html`），复用 CRT/赛博朋克视觉风格。

### 5.1 整体结构

```
┌──────────────────────────────────────────────────────────┐
│ Header: Logo | Stats Bar (4 指标) | ◀ HOME              │
├──────────────────────────────────────────────────────────┤
│ Filter Bar: [NEW] [HOT] │ Status ▼ │ Price ▲▼           │
├──────────────────────────────┬───────────────────────────┤
│ 任务列表 (max-width 720px)   │ 排行榜 (320px)           │
│ 居中, 不左右平铺             │ PROFIT RANKING           │
│                              │ Top 20, 金银铜 Top3      │
├──────────────────────────────┴───────────────────────────┤
│ Footer: SYNAI CORE v1.0.4 | Chain: Base L2 | Healthy    │
└──────────────────────────────────────────────────────────┘
```

### 5.2 CSS 要点

- 设计变量：`--bg-color: #050505`, `--cyan: #00f3ff`, `--violet: #bc13fe`, `--green: #00ff41`, `--gold: #ffd700`
- 字体：Inter + JetBrains Mono（Google Fonts CDN）
- Scanline 伪元素 overlay
- Grid：`grid-template-columns: minmax(0, 720px) 320px; gap: 30px; justify-content: center; max-width: 1100px; margin: 0 auto;`
- 三级响应式：>900px 双列 → 600-900px 单列排行榜下移 → <600px 紧凑移动端

### 5.3 JavaScript 逻辑

- **轮询**：任务列表 10s，排行榜 30s（独立 setInterval）
- **视图切换**：`currentView = 'new' | 'hot'`，前端排序 `participants.length`
- **XSS 防护**：`esc()` 函数转义所有用户输入
- **新任务动画**：`previousTaskIds` Set + CSS slide-in
- **空状态**：居中 "NO ACTIVE TASKS" + 脉冲动画

### 5.4 任务卡片字段

| 一级（始终可见） | 二级（小字） |
|----------------|------------|
| 标题、状态徽章、价格 USDC、活跃 worker 数、相对时间 | task_id(前8字)、到期倒计时、失败次数 |

### 5.5 状态徽章颜色

| Status | 背景 | 文字 |
|--------|------|------|
| open | rgba(255,255,255,0.1) | #fff |
| funded | rgba(0,243,255,0.1) | #00f3ff (cyan) |
| resolved | rgba(0,255,65,0.1) | #00ff41 (green) |
| expired | rgba(255,165,0,0.1) | #ff8c00 |
| cancelled | rgba(255,255,255,0.05) | #666 |

---

## Step 6: 修复 `templates/landing.html`

最小改动：`<html lang="zh-CN">` → `<html lang="en">`

---

## Step 7: IdempotencyKey 过期清理

在 `server.py` 的 `_expiry_checker_loop()` 中追加清理逻辑：

```python
from models import IdempotencyKey
cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)
deleted = IdempotencyKey.query.filter(
    IdempotencyKey.created_at < cutoff
).delete(synchronize_session=False)
if deleted:
    logger.info("Cleaned up %d expired idempotency keys", deleted)
```

---

## Step 8: 创建 `static/` 目录

Flask 默认 `static_folder='static'`，创建目录并放入 `skill.md`。

---

## 实施顺序

```
Step 8 → Step 1 → Step 2 → Step 3 → Step 4 → Step 5 → Step 6 → Step 7
目录    后端服务    路由     索引    skill.md  前端模板   landing   清理
```

---

## 验证方案

### 本地启动
```bash
DEV_MODE=true python server.py
```

### 页面访问
1. `GET /` → landing.html
2. `GET /dashboard` → dashboard.html
3. `GET /skill.md` → Markdown 文本

### API 端点
4. `GET /dashboard/stats` → JSON 聚合统计
5. `GET /dashboard/leaderboard` → JSON Agent 排名
6. `GET /dashboard/leaderboard?sort_by=completion_rate` → 按信誉排名

### ETag
7. 首次请求获取 ETag → 条件请求 → 304

### 前端交互
8. NEW / HOT Tab 切换验证
9. 排行榜 Top 3 金银铜
10. 响应式断点验证（900px / 600px）

### 回归测试
11. 现有 API 端点无影响
12. `python -m pytest tests/` 全部通过
