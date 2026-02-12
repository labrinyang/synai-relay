# SYNAI Dashboard 需求分析 — 综合汇总报告

> 综合 4 份分析报告，标注各 Agent 的共识与分歧
> 日期: 2026-02-12

---

## 一、四份报告来源

| 编号 | 报告 | Agent | 文件 |
|------|------|-------|------|
| R1 | 任务列表视角切换 | view-researcher | `dashboard-task-list-analysis.md` |
| R2 | 排行榜设计 | leaderboard-researcher | `dashboard-leaderboard-analysis.md` |
| R3 | 数据持久化与 API | data-architect | `dashboard-data-persistence-analysis.md` |
| R4 | 首页架构与 UX | ux-architect | `dashboard-ux-architecture-analysis.md` |

---

## 二、全员共识（无分歧）

以下结论所有报告一致同意：

### 2.1 当前阻塞项

- **`GET /` 和 `GET /dashboard` 路由不存在**，两个 HTML 模板无法被访问（R3, R4）
- **`GET /ledger/ranking` 是 V1 遗留接口，V2 中不存在**，Dashboard 的排行榜和 Header 统计完全失效（R1, R2, R3, R4 均发现）
- **XSS 漏洞**：`index.html` 中 `${j.title}` 等用户输入未转义直接注入 DOM（R4 首先发现，R1 间接提及）

### 2.2 新任务（NEW）定义

- 按 `created_at DESC` 排序，**不做 24h/48h 时间窗口过滤**（R1, R4 一致）
- 原因：早期任务发布量低，时间窗口会导致空列表
- 展示 `open` + `funded` 状态的任务（R1）

### 2.3 切换 UX

- **分段控制器（Segmented Control）**，非下拉菜单（R1, R4 一致）
- JetBrains Mono 字体，大写，CRT 风格 cyan 高亮

### 2.4 排行榜主指标

- **`total_earned`（USDC 累计收益）** 作为默认排名（R2, R3, R4 一致）
- 匹配现有 "PROFIT RANKING" 标题
- 过滤条件：`total_earned > 0` 且 `is_ghost = false`（R2）

### 2.5 排行榜显示数量

- **Top 20，无分页**（R2, R4 一致）
- 350px/320px 侧边栏放不下更多行

### 2.6 排行榜点击行为

- **模态框（Modal Overlay）** 展示 Agent 只读详情，不跳转新页面（R2, R4 一致）

### 2.7 数据库拓扑

- **Phase 1 使用同一个数据库**，不设只读副本（R3）
- PostgreSQL MVCC 天然支持读写并发，Dashboard 只做 SELECT，不会阻塞写入
- 只读副本推迟到并发 >500 用户时考虑

### 2.8 事务隔离级别

- 保持 **READ COMMITTED**（PostgreSQL 默认），无需更高级别（R3）

### 2.9 数据归档

- **Phase 1 不归档**（R3）
- 当前规模（~36K rows/年）PostgreSQL 轻松应对
- 需要清理过期的 `IdempotencyKey`（24h TTL，目前从不清理）

### 2.10 skill.md

- **静态文件**，`Content-Type: text/markdown`（R4）
- 内容：协议身份、认证流程、任务生命周期、Deposit 流程、关键参数
- 不从数据库动态生成，保持稳定的 Agent 参考文档

### 2.11 导航结构

- **最小化**：仅 Landing + Dashboard 两个页面（R4）
- Phase 1 不需要独立的任务详情页或 Agent 个人主页
- 路由：`GET /` → landing.html, `GET /dashboard` → dashboard.html, `GET /skill.md`

### 2.12 `metrics.creativity` 和 `metrics.engineering`

- **当前始终为 0**，代码中从未计算（R2 发现）
- 只有 `reliability` 是活的
- `completion_rate` 是更用户友好的展示指标

---

## 三、分歧点（需决策）

### ⚠️ 分歧 1：「热门任务」的定义

| Agent | "热门" = 什么？ |
|-------|----------------|
| **view-researcher (R1)** | 活跃参与者数量 `participants.length`（claim 了且未 unclaim 的 worker 数） |
| **data-architect (R3)** | 同 R1：`COUNT(job_participants) WHERE unclaimed_at IS NULL` |
| **ux-architect (R4)** | ⚠️ **最高赏金金额** `sort_by=price&sort_order=desc` |

**分析**：R1/R3 基于需求文档原文（"系统会统计目前正在处理或承接该任务的 Agent 数量，数量最多的即为热门任务"），R4 偏离了需求。

**建议采用 R1/R3 方案**：热门 = 活跃 worker 数量，与需求文档一致。价格排序可以作为 Filter Bar 中独立的排序选项。

---

### ⚠️ 分歧 2：API 端点命名

| Agent | 排行榜端点 | 统计端点 | 热门任务端点 |
|-------|-----------|---------|------------|
| **leaderboard-researcher (R2)** | `GET /leaderboard` | （包含在 leaderboard 响应的 `stats` 字段中） | — |
| **data-architect (R3)** | `GET /dashboard/leaderboard` | `GET /dashboard/stats` | `GET /dashboard/hot-tasks` |
| **ux-architect (R4)** | `GET /ledger/ranking`（沿用 V1 命名） | （包含在 ranking 响应中） | — |

**分析**：
- R2 主张独立顶级路径 `/leaderboard`，理由是"清洁、目的明确、避免重载 /agents 命名空间"
- R3 主张统一 `/dashboard/*` 前缀，理由是"每个端点有不同的缓存特征，分离更利于 ETag 和 TTL 管理"
- R4 建议沿用 V1 命名 `/ledger/ranking`，理由是兼容现有前端代码

**建议采用 R3 方案 (`/dashboard/*` 前缀)**：
- 统一前缀便于路由管理和中间件（如缓存、CORS）
- V1 `/ledger/ranking` 的 response shape 已经不兼容，沿用旧名反而误导
- 三个端点各有不同 TTL，分离更合理

---

### ⚠️ 分歧 3：轮询间隔

| Agent | 任务列表轮询 | 排行榜轮询 |
|-------|------------|-----------|
| **view-researcher (R1)** | **保持 5 秒** | — |
| **leaderboard-researcher (R2)** | — | **30-60 秒**（独立轮询） |
| **data-architect (R3)** | **提升到 15 秒** + ETag | 同一轮询周期 |
| **ux-architect (R4)** | **15-30 秒** | — |

**分析**：
- R1 认为 5 秒对监控型 Dashboard 是合理的
- R3/R4 认为 5 秒过于激进，增加服务端负载，建议 15 秒
- R2 指出排行榜变化极低频（仅在任务 resolve 时更新），应该独立轮询且间隔更长

**建议折中方案**：
- 任务列表：**10 秒**（兼顾实时感与负载）
- 排行榜/统计：**30 秒**（独立轮询，采纳 R2 建议）
- 加 ETag 支持减少无效传输（采纳 R3 建议）

---

### ⚠️ 分歧 4：排行榜 API 响应结构

| Agent | 顶层 key | Agent 列表 key | Owner 信息 |
|-------|---------|--------------|----------|
| **R2** | `{ agents, stats, total, limit, offset }` | `agents[]` | 嵌套 `owner: { username, twitter_handle, avatar_url }` |
| **R3** | `{ agents, total }` | `agents[]` | 扁平字段，无 owner 嵌套 |
| **R4** | `{ stats, agent_ranking }` | `agent_ranking[]` | 扁平 `owner_twitter` 字段 |

**分析**：
- R2 最完整：含分页参数、嵌套 Owner、`tasks_won` 子查询
- R3 最简洁：无 Owner join，缺分页
- R4 沿用 V1 结构

**建议采用 R2 结构**（最完整）：
```json
{
  "agents": [
    {
      "rank": 1,
      "agent_id": "...",
      "name": "...",
      "owner": { "username": "...", "twitter_handle": "...", "avatar_url": "..." },
      "total_earned": 1250.00,
      "completion_rate": 0.952,
      "tasks_won": 12
    }
  ],
  "stats": { "total_agents": 42, "total_active_agents": 15, "total_volume": 25000.50 },
  "total": 15,
  "limit": 20,
  "offset": 0
}
```

---

### ⚠️ 分歧 5：热门任务 — 前端排序 vs 后端端点

| Agent | Phase 1 方案 |
|-------|-------------|
| **view-researcher (R1)** | **前端排序**：`GET /jobs` 已返回 `participants[]`，JS 按 `.length` 排序即可，无需后端改动 |
| **data-architect (R3)** | **新建后端端点** `GET /dashboard/hot-tasks`：SQL 子查询 JOIN job_participants，返回轻量字段 |

**分析**：
- R1 方案零后端改动，但受限于 `GET /jobs` 的 50 条分页上限（热门任务可能不在前 50 条中）
- R3 方案需要后端开发，但数据更准确（SQL 级 COUNT+ORDER），且响应更轻量（不含 description/rubric）

**建议**：
- **Phase 1 先用 R1 前端排序**（快速上线）
- **Phase 1.5 加 R3 后端端点**（分页 >50 任务时前端排序不可靠）

---

### ⚠️ 分歧 6：响应式断点

| Agent | 双列→单列 | 移动端 |
|-------|----------|-------|
| **view-researcher (R1)** | **900px** | **600px**（缩小内距、堆叠元素） |
| **ux-architect (R4)** | **900px** | 无独立小屏断点，仅在 900px 处理 |

**分析**：R1 多了一个 600px 小屏幕断点用于优化移动端体验（缩小 padding、card 标题缩至 15px、touch target 44px）。

**建议采用 R1 三级断点**：`>900px` 双列 → `600-900px` 单列 → `<600px` 紧凑移动端。

---

### ⚠️ 分歧 7：侧边栏宽度

| Agent | 侧边栏宽度 |
|-------|-----------|
| **view-researcher (R1)** | **320px** |
| **leaderboard-researcher (R2)** | **350px**（引用当前 CSS `grid-template-columns: 1fr 350px`） |
| **ux-architect (R4)** | 先说 350px，后说 320px |

**分析**：当前 `index.html` 使用 350px。主内容区 max-width 720px，加上 gap 30px，总宽度：
- 350px 方案：720 + 30 + 350 = **1100px**
- 320px 方案：720 + 30 + 320 = **1070px**

**建议 320px**：留更多呼吸空间给居中布局。排行榜仅展示名称 + 金额，320px 足够。

---

### ⚠️ 分歧 8：排行榜 Tab 设计

| Agent | Tab 数量 | Tab 内容 |
|-------|---------|---------|
| **leaderboard-researcher (R2)** | **2 个**：Earnings（默认）+ Reputation（completion_rate，最少 5 次 claim） |
| **ux-architect (R4)** | **1 个**（无 Tab，仅按 total_earned 排序） |
| **data-architect (R3)** | 支持 `sort_by` 参数（total_earned / completion_rate），但未明确 UI Tab |

**建议采用 R2 方案**：两个 Tab，但 Phase 1 可以只做 Earnings tab，Reputation tab 标记为 "Coming Soon" 或 Phase 2 实现。

---

## 四、各报告独有贡献（仅一方提出的亮点）

### 仅 R1（view-researcher）提出

- **空状态设计**：居中 "NO ACTIVE TASKS / The network is quiet." + 脉冲动画
- **新任务滑入动画**：CSS `task-slide-in` + `previousTaskIds` Set 做 diff 渲染
- **任务数量 badge**：`[ NEW TASKS (12) ] [ HOT TASKS (5) ]` 含数量

### 仅 R2（leaderboard-researcher）提出

- **Top 3 视觉处理**：金(#ffd700)/ 银(#c0c0c0)/ 铜(#cd7f32) 文字色调 + text-shadow 发光
- **`completion_rate` 最少 5 次 claim 阈值**：避免 1/1=100% 的虚高数据
- **`tasks_won` 字段**：通过 `Job.winner_id` 批量子查询获取每个 Agent 胜出任务数
- **V1→V2 迁移对照表**：`balance` → `total_earned`, `/ledger/ranking` → `/leaderboard`

### 仅 R3（data-architect）提出

- **TTL 缓存实现**：完整的 `DashboardService` + `TTLCache` 工具类代码
- **ETag 支持**：`etag_response()` 辅助函数，304 Not Modified 减少带宽
- **6 个新数据库索引**：含 Alembic 迁移代码
- **SSE 实现方案**：Phase 2 用 Flask 原生 streaming response，无需 flask-socketio
- **IdempotencyKey 清理**：定期删除 24h 过期的幂等键

### 仅 R4（ux-architect）提出

- **XSS 修复方案**：`function esc(s)` HTML 转义函数
- **`<html lang="zh-CN">` → `lang="en"`** 修复
- **SEO/Meta 标签缺失**：Open Graph、Twitter Card
- **`prompt()` 替换**：浏览器原生 prompt 用于支付确认是糟糕的 UX，应改为 styled modal
- **Filter Bar 完整设计**：Status 下拉、Price 排序切换、Type 筛选
- **分享页 `/share/job/<task_id>`**：当前 Share 按钮指向不存在的路由

---

## 五、综合推荐方案（决策总结）

| # | 决策项 | 推荐方案 | 采纳来源 | 备注 |
|---|--------|---------|---------|------|
| 1 | 热门任务定义 | 活跃 worker 数量（非赏金） | R1/R3 | R4 偏离需求 |
| 2 | API 命名空间 | `/dashboard/*` 统一前缀 | R3 | 便于缓存/路由管理 |
| 3 | 任务列表轮询 | 10 秒 + ETag | R1/R3 折中 | R1 主张 5s，R3 主张 15s |
| 4 | 排行榜轮询 | 30 秒独立轮询 | R2 | 变化低频 |
| 5 | API 响应结构 | R2 完整版（嵌套 owner, tasks_won, 分页） | R2 | R3/R4 过于简化 |
| 6 | 热门排序实现 | Phase 1 前端排序，Phase 1.5 后端端点 | R1→R3 渐进 | 平衡速度和准确性 |
| 7 | 响应式断点 | 三级：>900, 600-900, <600 | R1 | R4 仅两级 |
| 8 | 侧边栏宽度 | 320px | R1 | 更多呼吸空间 |
| 9 | 排行榜 Tab | 两个（Earnings + Reputation），Phase 1 只做 Earnings | R2 | R4 无 Tab |
| 10 | 排行榜指标 | `total_earned` 为主，`completion_rate` 为辅 | R2 (全员共识) | — |
| 11 | 数据库 | 同库 + 6 个新索引，无副本 | R3 (全员共识) | — |
| 12 | 缓存策略 | 内存 TTL（30/60/15s）+ ETag | R3 | 无需 Redis |
| 13 | 实时推送 | Phase 1 轮询，Phase 2 SSE | R3 (全员共识) | 非 WebSocket |
| 14 | skill.md | 静态文件 + `text/markdown` | R4 (全员共识) | — |
| 15 | 导航结构 | Landing + Dashboard 两页 | R4 (全员共识) | — |

---

## 六、实施优先级（综合版）

### Phase 1 — MVP（必须上线）

1. `server.py` 新增 `GET /`, `GET /dashboard`, `GET /skill.md` 路由
2. 实现 `GET /dashboard/stats`（TTL 30s 缓存）
3. 实现 `GET /dashboard/leaderboard`（TTL 60s，R2 完整响应格式）
4. 新建 `dashboard.html` 模板（双列布局，720px+320px）
5. 实现 New/Hot 分段控制器（前端排序 `participants.length`）
6. 修复 XSS（HTML 转义所有用户输入）
7. 修复 `<html lang="en">`
8. 创建 `static/skill.md` 文件
9. Alembic 迁移：添加 6 个数据库索引

### Phase 2 — 增强

10. 实现 `GET /dashboard/hot-tasks` 后端端点（SQL 子查询）
11. Filter Bar（Status 下拉、Price 排序）
12. 排行榜 Reputation Tab（completion_rate，min 5 claims）
13. ETag 304 支持
14. 分页 "Load More"
15. 提取共享 CSS 到 `base.css`

### Phase 3 — 精打磨

16. SSE 实时事件推送
17. 新任务滑入动画（diff 渲染）
18. Agent 详情模态框
19. Meta 标签（OG, Twitter Card）
20. 分享页 `/share/job/<task_id>`

---

## 七、需要你做的决策

以上分歧点中，大部分我已给出推荐方案。但以下几项需要你确认：

1. **热门任务定义**：活跃 worker 数量（推荐）还是赏金金额？
2. **API 端点前缀**：`/dashboard/*`（推荐）还是顶级 `/leaderboard`？
3. **任务列表轮询间隔**：5s / 10s / 15s？
4. **排行榜是否需要 Reputation Tab**（Phase 1 还是 Phase 2）？
