# Ralph Fix Plan

## High Priority
- [x] 1.1 扫描项目目录结构，输出到 `docs/project-structure.md`
- [x] 1.2 定位所有路由定义文件，整理已实现的 API endpoint 列表，输出到 `docs/existing-apis.md`（含 method、path、handler、简述）
- [x] 1.3 定位所有智能合约文件，整理已有的合约接口（deposit/withdraw/escrow/settle 等），输出到 `docs/existing-contracts.md`
- [x] 1.4 定位状态机 / 状态流转逻辑，整理任务状态枚举和转换规则，输出到 `docs/existing-state-machine.md`
- [x] 1.5 整理数据库 schema / model 定义，输出到 `docs/existing-models.md`
- [x] 1.6 git commit: `feat(agent-flow): Phase 1 - 现有系统能力调研完成`
- [ ] 2.1 绘制**发布方 Agent 完整流程**，写入 `docs/agent-flow.md`
- [ ] 2.2 绘制**接取方 Agent 完整流程**，追加到 `docs/agent-flow.md`
- [ ] 2.3 绘制**状态机流转图**（用 Mermaid 语法），写入 `docs/agent-flow.md` 底部
- [ ] 2.4 git commit: `feat(agent-flow): Phase 2 - Agent 完整流程建模完成`
- [ ] 3A.1 调用 **Task + Codex subagent**，指令如下：
- [ ] 3A.2 检查 `docs/gap-analysis-codex.md` 已生成且非空
- [ ] 3B.1 调用 **Task + Opus subagent**，指令如下：
- [ ] 3B.2 检查 `docs/gap-analysis-opus.md` 已生成且非空
- [ ] 3C.1 对比 `gap-analysis-codex.md` 和 `gap-analysis-opus.md`，合并去重，生成最终缺口报告 `docs/gap-analysis-final.md`
- [ ] 3C.2 git commit: `feat(agent-flow): Phase 3 - 双模型交叉缺口分析完成`
- [ ] 4.1 阅读 `docs/gap-analysis-final.md`，按优先级排序所有缺口项
- [ ] 4.2 为每个 P0 缺口项编写实施规格，写入 `docs/implementation-plan.md`：
- [ ] 4.3 为每个 P1 缺口项编写简要实施规格，追加到 `docs/implementation-plan.md`
- [ ] 4.4 确定实施顺序（拓扑排序依赖关系），写入 `docs/implementation-order.md`
- [ ] 4.5 git commit: `feat(agent-flow): Phase 4 - 执行计划制定完成`
- [ ] 5.N.1 调用 **Task + Codex subagent（通过 Codex MCP）** 执行实现，指令：
- [ ] 5.N.2 检查实现文件已创建且测试文件已创建
- [ ] 5.N.3 运行该缺口项的测试，确认通过
- [ ] 5.N.4 运行全量测试，确认没有破坏现有功能
- [ ] 5.N.5 git commit: `feat(agent-flow): implement [缺口项名称]`
- [ ] 6.1 调用 **Task + Opus subagent**，指令：
- [ ] 6.2 检查 `docs/opus-review-findings.md` 已生成
- [ ] 6.3 如果存在 Critical 或 Major 问题，对每个问题调用 **Task + Opus subagent** 修复：
- [ ] 6.4 所有 Critical 和 Major 问题标记为 ✅ 已修复
- [ ] 6.5 运行全量测试，确认全部通过
- [ ] 6.6 git commit: `feat(agent-flow): Phase 6 - Opus review 修复完成`
- [ ] 7.1 调用 **Task + Opus subagent（角色：测试架构师）**，制定完整测试方案 → `docs/test-plan.md`
- [ ] 7.2 调用 **Task + Codex subagent（角色：安全测试员）**，补充安全测试用例 → `docs/test-plan-security.md`
- [ ] 7.3 合并 `test-plan.md` 和 `test-plan-security.md` 为最终测试方案 `docs/test-plan-final.md`
- [ ] 7.4 git commit: `feat(agent-flow): Phase 7 - 完整测试方案制定完成`
- [ ] 8.1 按 `docs/test-plan-final.md` 编写所有缺失的单元测试
- [ ] 8.2 运行所有单元测试，修复失败项，直到全部通过
- [ ] 8.3 按测试方案编写集成测试
- [ ] 8.4 运行集成测试，修复失败项，直到全部通过
- [ ] 8.5 按测试方案编写端到端测试场景
- [ ] 8.6 运行端到端测试，修复失败项，直到全部通过
- [ ] 8.7 按安全测试方案编写安全测试
- [ ] 8.8 运行安全测试，修复发现的漏洞，直到全部通过
- [ ] 8.9 运行全量测试套件，确认 100% 通过
- [ ] 8.10 生成测试覆盖率报告 → `docs/test-coverage-report.md`
- [ ] 8.11 git commit: `feat(agent-flow): Phase 8 - 全部测试通过`
- [ ] 9.1 确认所有必需文档文件存在且内容完整
- [ ] 9.2 确认全量测试通过
- [ ] 9.3 确认所有 P0 缺口项已实现
- [ ] 9.4 git commit: `feat(agent-flow): Phase 9 - 最终验收完成`
- [ ] 9.5 输出 `<promise>COMPLETE</promise>`


## Medium Priority


## Low Priority


## Completed
- [x] Project enabled for Ralph

## Notes
- Focus on MVP functionality first
- Ensure each feature is properly tested
- Update this file after each major milestone
