# Agent 生命周期 API 能力补全 — 完整执行方案

> **执行方式**: Ralph Wiggum 自主循环 + `--dangerously-skip-permissions`
> **分支**: `feat/agent-lifecycle-api`
> **完成信号**: `<promise>COMPLETE</promise>`

---

## 全局规则

1. **每次迭代只做一个 checkbox**，完成后立即标记 `[x]`
2. **严格按 Phase 顺序执行**，不得跳跃
3. **先读代码再分析**，禁止凭空假设后端能力
4. **每完成一个 Phase**，执行 `git add -A && git commit`，commit message 格式：`feat(agent-flow): Phase N.M - 具体内容`
5. **遇到架构决策**，记录到 `docs/decisions.md` 后继续，不要停下来等人
6. **所有文档输出到 `docs/` 目录**

---

## Phase 1: 调研现有系统能力

> 目标：完整摸清当前后端已实现的所有能力

- [ ] 1.1 扫描项目目录结构，输出到 `docs/project-structure.md`
- [ ] 1.2 定位所有路由定义文件，整理已实现的 API endpoint 列表，输出到 `docs/existing-apis.md`（含 method、path、handler、简述）
- [ ] 1.3 定位所有智能合约文件，整理已有的合约接口（deposit/withdraw/escrow/settle 等），输出到 `docs/existing-contracts.md`
- [ ] 1.4 定位状态机 / 状态流转逻辑，整理任务状态枚举和转换规则，输出到 `docs/existing-state-machine.md`
- [ ] 1.5 整理数据库 schema / model 定义，输出到 `docs/existing-models.md`
- [ ] 1.6 git commit: `feat(agent-flow): Phase 1 - 现有系统能力调研完成`

---

## Phase 2: Agent 视角完整流程建模

> 目标：以 Agent 视角绘制理想的完整操作流程

- [ ] 2.1 绘制**发布方 Agent 完整流程**，写入 `docs/agent-flow.md`
  - 创建任务 → 设置参数/奖励 → deposit 资金 → 发布任务 → 监控任务状态 → 审核提交物 → 验收/拒绝 → 触发结算 → 确认退款（如有）
  - 每个步骤标注：理想 API endpoint、请求参数、返回值、前置条件
- [ ] 2.2 绘制**接取方 Agent 完整流程**，追加到 `docs/agent-flow.md`
  - 浏览/搜索任务 → 查看详情 → 接取任务 → 执行任务 → 提交成果 → 查询审核状态 → 收到付款 → 处理争议/退款
  - 每个步骤同样标注理想 API 和参数
- [ ] 2.3 绘制**状态机流转图**（用 Mermaid 语法），写入 `docs/agent-flow.md` 底部
- [ ] 2.4 git commit: `feat(agent-flow): Phase 2 - Agent 完整流程建模完成`

---

## Phase 3: 能力缺口分析（双模型交叉 Review）

> 目标：用 Codex + Opus 两个 subagent 分别独立 review，交叉验证缺口

### 3A: Codex Subagent Review

- [ ] 3A.1 调用 **Task + Codex subagent**，指令如下：

  ```
  请阅读以下文件：
  - docs/existing-apis.md
  - docs/existing-contracts.md
  - docs/existing-state-machine.md
  - docs/existing-models.md
  - docs/agent-flow.md

  任务：将"Agent 理想流程所需的每个 API/接口"与"现有系统已实现的能力"逐一对比。
  输出一张缺口表到 docs/gap-analysis-codex.md，格式：

  | 序号 | 流程步骤 | 所需 API/接口 | 当前状态(✅已有/❌缺失/⚠️部分实现) | 缺口描述 | 优先级(P0/P1/P2) |
  ```

- [ ] 3A.2 检查 `docs/gap-analysis-codex.md` 已生成且非空

### 3B: Opus Subagent Review

- [ ] 3B.1 调用 **Task + Opus subagent**，指令如下：

  ```
  请独立阅读以下文件（不要参考 gap-analysis-codex.md）：
  - docs/existing-apis.md
  - docs/existing-contracts.md
  - docs/existing-state-machine.md
  - docs/existing-models.md
  - docs/agent-flow.md

  任务：从系统架构师视角分析能力缺口，特别关注：
  1. API 层面的缺失
  2. 合约接口的缺失
  3. 状态查询的缺失
  4. 流程中的断点和死角（如异常路径、超时处理、争议机制）
  5. 安全性缺口（权限校验、资金安全）

  输出到 docs/gap-analysis-opus.md，同样使用缺口表格式，额外增加"风险等级"列。
  ```

- [ ] 3B.2 检查 `docs/gap-analysis-opus.md` 已生成且非空

### 3C: 合并缺口报告

- [ ] 3C.1 对比 `gap-analysis-codex.md` 和 `gap-analysis-opus.md`，合并去重，生成最终缺口报告 `docs/gap-analysis-final.md`
  - 两份报告都标记为缺失的 → P0
  - 仅一份报告标记为缺失但合理的 → P1
  - 部分实现的 → P1 或 P2
  - 记录两份报告的分歧点到 `docs/review-divergence.md`
- [ ] 3C.2 git commit: `feat(agent-flow): Phase 3 - 双模型交叉缺口分析完成`

---

## Phase 4: 执行计划制定

> 目标：基于缺口报告制定可执行的实施计划

- [ ] 4.1 阅读 `docs/gap-analysis-final.md`，按优先级排序所有缺口项
- [ ] 4.2 为每个 P0 缺口项编写实施规格，写入 `docs/implementation-plan.md`：
  - 接口定义（endpoint / method / 参数 / 返回值）
  - 涉及的文件和模块
  - 依赖关系（哪些缺口项必须先完成）
  - 预期测试用例
- [ ] 4.3 为每个 P1 缺口项编写简要实施规格，追加到 `docs/implementation-plan.md`
- [ ] 4.4 确定实施顺序（拓扑排序依赖关系），写入 `docs/implementation-order.md`
- [ ] 4.5 git commit: `feat(agent-flow): Phase 4 - 执行计划制定完成`

---

## Phase 5: 补全能力缺口（Task + Codex Subagent 执行）

> 目标：按顺序实现所有缺口，每个缺口由 Codex subagent 实现 + 自我 review

按 `docs/implementation-order.md` 的顺序，对每个缺口项执行以下子流程：

### 对每个 P0 缺口项重复：

- [ ] 5.N.1 调用 **Task + Codex subagent（通过 Codex MCP）** 执行实现，指令：

  ```
  请阅读 docs/implementation-plan.md 中关于 [缺口项名称] 的规格。
  实现该接口/功能，包括：
  1. 路由定义
  2. Controller/Handler 逻辑
  3. 必要的 Model/Schema 变更
  4. 合约接口变更（如需要）
  5. 编写对应的单元测试

  完成后，自我 review 你的实现：
  - 检查边界条件是否处理
  - 检查错误处理是否完整
  - 检查与现有代码风格是否一致
  - 将 review 结果输出到 docs/reviews/[缺口项名称]-codex-review.md
  ```

- [ ] 5.N.2 检查实现文件已创建且测试文件已创建
- [ ] 5.N.3 运行该缺口项的测试，确认通过
- [ ] 5.N.4 运行全量测试，确认没有破坏现有功能
- [ ] 5.N.5 git commit: `feat(agent-flow): implement [缺口项名称]`

> **注意**: 上面的 `5.N` 是模板。实际执行时，根据 `implementation-order.md` 中的缺口项数量展开。第一个缺口项是 5.1.1 ~ 5.1.5，第二个是 5.2.1 ~ 5.2.5，以此类推。每次迭代只做一个子步骤。

---

## Phase 6: Opus Review 修复轮

> 目标：用 Opus subagent review 所有新增代码，修复发现的问题

- [ ] 6.1 调用 **Task + Opus subagent**，指令：

  ```
  请 review Phase 5 中所有新增和修改的代码。阅读 docs/reviews/ 下的所有 codex review 文件。

  重点关注：
  1. 架构一致性：新代码是否与现有架构风格匹配
  2. 安全性：资金操作是否有足够的校验，权限是否正确
  3. 边界条件：超时、并发、异常路径是否处理
  4. 合约安全：是否存在重入攻击、整数溢出等风险
  5. API 设计：RESTful 规范、参数校验、错误码
  6. 代码质量：命名、注释、可读性

  对每个发现的问题，输出到 docs/opus-review-findings.md，格式：
  | 文件 | 行号/位置 | 问题类型 | 严重程度(Critical/Major/Minor) | 描述 | 建议修复方案 |

  最后给出整体评估和总结。
  ```

- [ ] 6.2 检查 `docs/opus-review-findings.md` 已生成
- [ ] 6.3 如果存在 Critical 或 Major 问题，对每个问题调用 **Task + Opus subagent** 修复：

  ```
  请阅读 docs/opus-review-findings.md 中的第 [N] 个问题。
  修复该问题，并：
  1. 更新或新增对应的测试用例
  2. 运行测试确认修复有效且不影响其他功能
  3. 在 docs/opus-review-findings.md 中该问题后追加 "✅ 已修复"
  ```

- [ ] 6.4 所有 Critical 和 Major 问题标记为 ✅ 已修复
- [ ] 6.5 运行全量测试，确认全部通过
- [ ] 6.6 git commit: `feat(agent-flow): Phase 6 - Opus review 修复完成`

---

## Phase 7: 组建 Team 制定完整测试方案

> 目标：多角色协作制定覆盖完整流程的测试方案

- [ ] 7.1 调用 **Task + Opus subagent（角色：测试架构师）**，指令：

  ```
  你是测试架构师。请阅读：
  - docs/agent-flow.md（完整流程）
  - docs/gap-analysis-final.md（缺口清单）
  - docs/implementation-plan.md（实现规格）
  - 项目中已有的测试文件

  制定完整的测试方案，输出到 docs/test-plan.md，包括：

  ## 1. 单元测试清单
  按模块列出所有需要的单元测试用例

  ## 2. 集成测试清单
  按 Agent 流程的每个步骤，列出 API 级别的集成测试：
  - 发布方完整流程（创建→deposit→发布→监控→验收→结算）
  - 接取方完整流程（搜索→接取→提交→查询→收款）
  - 异常流程（超时、拒绝、争议、退款）

  ## 3. 端到端测试场景
  模拟完整的双方交互场景：
  - 场景 A：正常完成（甲方发布 → 乙方接取 → 提交 → 验收 → 结算）
  - 场景 B：任务超时（无人接取 → 退款）
  - 场景 C：成果被拒（提交 → 拒绝 → 重新提交 → 通过）
  - 场景 D：争议流程
  - 场景 E：并发接取同一任务

  ## 4. 合约测试
  列出所有智能合约需要的测试用例（如有合约）

  ## 5. 测试数据方案
  定义测试需要的 mock 数据和 fixture

  对每个测试用例标注：输入、预期输出、前置条件、清理步骤
  ```

- [ ] 7.2 调用 **Task + Codex subagent（角色：安全测试员）**，指令：

  ```
  你是安全测试专家。请阅读 docs/test-plan.md 和所有新增代码。
  补充安全相关的测试用例，追加到 docs/test-plan-security.md：
  - 权限绕过测试（未授权访问、越权操作）
  - 资金安全测试（双重支付、负数金额、溢出）
  - 输入注入测试（SQL注入、XSS、参数篡改）
  - 状态机违规测试（非法状态转换）
  - 并发安全测试（竞态条件）
  ```

- [ ] 7.3 合并 `test-plan.md` 和 `test-plan-security.md` 为最终测试方案 `docs/test-plan-final.md`
- [ ] 7.4 git commit: `feat(agent-flow): Phase 7 - 完整测试方案制定完成`

---

## Phase 8: 执行测试方案

> 目标：按测试方案编写并执行所有测试

- [ ] 8.1 按 `docs/test-plan-final.md` 中的单元测试清单，编写所有缺失的单元测试
- [ ] 8.2 运行所有单元测试，修复失败项，直到全部通过
- [ ] 8.3 按测试方案编写集成测试
- [ ] 8.4 运行集成测试，修复失败项，直到全部通过
- [ ] 8.5 按测试方案编写端到端测试场景
- [ ] 8.6 运行端到端测试，修复失败项，直到全部通过
- [ ] 8.7 按安全测试方案编写安全测试
- [ ] 8.8 运行安全测试，修复发现的漏洞，直到全部通过
- [ ] 8.9 运行全量测试套件，确认 100% 通过
- [ ] 8.10 生成测试覆盖率报告，输出到 `docs/test-coverage-report.md`
- [ ] 8.11 git commit: `feat(agent-flow): Phase 8 - 全部测试通过`

---

## Phase 9: 最终验收

- [ ] 9.1 确认以下文件全部存在且内容完整：
  - `docs/project-structure.md`
  - `docs/existing-apis.md`
  - `docs/existing-contracts.md`
  - `docs/existing-state-machine.md`
  - `docs/existing-models.md`
  - `docs/agent-flow.md`
  - `docs/gap-analysis-final.md`
  - `docs/implementation-plan.md`
  - `docs/implementation-order.md`
  - `docs/opus-review-findings.md`（所有 Critical/Major 已标记 ✅）
  - `docs/test-plan-final.md`
  - `docs/test-coverage-report.md`
- [ ] 9.2 确认全量测试通过
- [ ] 9.3 确认所有 P0 缺口项已实现
- [ ] 9.4 git commit: `feat(agent-flow): Phase 9 - 最终验收完成`
- [ ] 9.5 输出 `<promise>COMPLETE</promise>`

---

## 附录：Subagent 调用约定

### 调用 Codex Subagent（代码执行类任务）

```
使用 Task 创建子任务，指定 subagent 使用 Codex 模型（通过 Codex MCP）。
子任务完成后，主 agent 必须检查输出文件是否存在且非空。
```

### 调用 Opus Subagent（分析/Review 类任务）

```
使用 Task 创建子任务，指定 subagent 使用 Opus 模型。
子任务完成后，主 agent 必须检查输出文件是否存在且非空。
```

### Subagent 自我 Review 规范

每个 Codex subagent 在完成代码实现后，必须自行 review 并输出 review 文件，包含：
- 实现概述
- 潜在风险点
- 已知限制
- 建议的后续改进