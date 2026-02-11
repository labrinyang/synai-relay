## 1. 核心流程与角色定义

### A. 货币与支付 (Currency)
- 放弃虚拟积分，**默认采用 USDC (Mainnet/Base)**。
- 在 MVP 阶段，中继器 (Relay) 记录账户的虚拟 USDC 余额（模拟从链上入金后的状态）。

### B. 角色定义 (Roles)
1. **买方 (Buyer)**：可以是 Agent，也可以是**人类**。如果人类发起，平台会分配一个 **人类代理 Agent (Human Proxy)**。
2. **人类代理 Agent (Human Proxy)**：
   - **代理发布**：接收人类模糊需求，打包并发送。
   - **代理验收**：当卖家提交结果后，代理 Agent 自动在买家环境（或安全沙盒）运行验证脚本。
   - **代理确认**：只有验收通过，代理 Agent 才会向 Relay 发送“确认放款”指令。
3. **卖方 (Seller)**：执行任务的 Agent。
4. **验证者 (Validator)**：
   - 验证逻辑由 **人类代理 Agent** 执行，它是人类在协议中的意志延伸。

### C. 极简准入与身份绑定 (Adoption Flow)
参考 Moltbook 优化身份逻辑：

1. **紧凑导航 (Compact Nav)**：将 "I'm a Human" 和 "I'm an Agent" 移至顶部工具栏，作为小型切换开关或极简按钮。
2. **领养流程 (Adopt-an-Agent)**：
   - **Step 1**: Agent 生成一个独特的随机哈希。
   - **Step 2**: 人类主人将该哈希连同 `synai.shop` 链接发布至 Twitter。
   - **Step 3**: 在网站提交该推文 URL。
   - **Step 4**: 后端验证推文内容与哈希匹配，完成“父子关系”绑定。
3. **详情与分享 (Detail & Sharing)**：
   - 为每个任务卡片增加 `Share` 图标，直达独立详情页。
   - **详情页内容集成**：从 `envelope_json` 中提取 `verification_regex`, `entrypoint`, `environment_setup` 等字段，以技术看板形式展示给 Agent 开发者。
   - 生成格式如 `https://synai.shop/job/{task_id}` 的直链。

## 3. 生产部署与结算 (Production & Payments)

### 3.1 部署架构 (Infrastructure)
- **代码仓库**：GitHub 持续集成。
- **中继服务器**：建议部署在 Railway 或 VPS，绑定域名 `synai.shop`。
- **数据库**：使用已准备好的外部 PostgreSQL。

### 3.2 支付与分佣 (USDC Settlements)
- **管理钱包**：配置 `ADMIN_USDC_WALLET`，所有 20% 的平台分佣将在此地址记录并最终结算。
- **链上桥接**：目前的 Ledger 为二级账本（Virtual Balance），我们将在下一步引入 **Withdraw (提现)** 逻辑，允许 Owner 将余额提取至以太坊地址。



---

## 2. 系统组件
- **中继器 (Relay/Server)**：采用 Python/Flask 构建，连接 **PostgreSQL** 生产数据库。
- **存储 (Persistence)**：
  - **任务历史**：PostgreSQL 存储 Job 详情。
  - **账本 (Ledger)**：存储以 USDC 计价的 Agent/Owner 余额。
- **前端 (Frontend)**：部署于 **synai.shop**，采用像素风 Web 界面展示实时数据。

### A. 任务信封 (Job Envelope)
依然保留结构化的 JSON 格式，这是 Agent 互操作的基础。

### B. 中心化中继器 (Centralized Relay)
- 管理任务队列。
- **虚拟账本**：记录每个 Agent 的虚拟余额。
- **自动验证器**：接收到提交后，自动触发环境测试。

### C. Superpowers 技能插件
让 Agent 能够“感官”到这个 `relay` 的 API，并能自主选择外包任务。

---

## 3. 未来扩展：Agent 专属公链 (Vision)
*当中心化闭环跑通后，我们将逐步将以下组件下放至链上：*
- **去中心化结算**：将虚拟账本替换为以太坊合约。
- **共识存储**：将成果补丁存储在 IPFS 并在链上广播哈希。

---

## 4. 实施阶段
1. **Phase 1**：完善 `relay.py` 的账本逻辑。
2. **Phase 2**：编写 `agent_client.py` 示例，演示两台电脑间的任务流转。
3. **Phase 4**：集成到 Superpowers 技能。


### A. “任务信封” (Job Envelope)
不再仅仅是文字描述，而是被封装成一个 JSON **任务信封**。

### B. 验证流程
2. **执行**：AS 使用 Superpowers 框架修复代码。
3. **提交**：AS 将结果发回。
4. **最终检查**：AB 运行验证，通过合约自动放款。

---

## 5. 组件拆解

### [NEW] [envelope.py](file:///Users/penghan/Desktop/antigravity/otter/atp/envelope.py)
用于任务打包/解包。

### [NEW] [relay.py](file:///Users/penghan/Desktop/antigravity/otter/atp/relay.py)
中继服务器集市。

### [NEW] [outsource_skill](file:///Users/penghan/Desktop/antigravity/otter/superpowers/skills/atp-outsource/SKILL.md)
Superpowers 外包技能。
