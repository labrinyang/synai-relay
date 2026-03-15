# x402 Payment Integration & Multi-Chain Architecture

**Date:** 2026-03-15
**Status:** Draft
**Context:** OKX X Layer AI Agent Hackathon (Phase 1: Mar 12–26)

---

## 1. Overview

### 1.1 Goal

Integrate the x402 payment protocol as the primary payment gateway for synai-relay, enabling:

- One-step task creation + escrow funding via HTTP 402
- Pay-to-view submission marketplace (knowledge monetization)
- Multi-chain support from day one (X Layer primary, Base secondary)
- Decoupled OnchainOS integration for X Layer operations

### 1.2 Scope

| In Scope | Out of Scope |
|----------|--------------|
| x402 for task creation + escrow | Multi-agent oracle review |
| x402 for submission viewing | Agent staking / POS |
| ChainAdapter abstraction layer | Smart contract deployment |
| OnchainOS as X Layer adapter | Trade/Market API integration |
| Multi-chain x402 (X Layer + Base) | Frontend/dashboard changes |

### 1.3 Hackathon Judging Alignment

| Criterion | How We Address It |
|-----------|-------------------|
| AI agent on-chain integration depth | Escrow funding via x402; every task lifecycle step has chain tx |
| Autonomous agent payment flow | Agents pay via HTTP 402 without human intervention |
| Multi-agent collaboration | Buyer/worker/oracle flow with x402-gated knowledge sharing |
| X Layer ecosystem impact | All X Layer txs routed through OnchainOS; x402 usage |

---

## 2. x402 Protocol Background

x402 repurposes HTTP 402 (Payment Required) for programmatic, machine-to-machine payments:

```
Agent                    Server                   Facilitator
  │── GET /resource ──────►│                            │
  │◄── 402 + requirements ─┤                            │
  │  [signs EIP-3009]       │                            │
  │── GET + PAYMENT-SIG ───►│── POST /verify ──────────►│
  │                         │◄── {valid: true} ─────────┤
  │◄── 200 + content ──────┤── POST /settle ──────────►│
  │                         │◄── {tx_hash: 0x...} ──────┤
```

**Python SDK:** `x402[flask,evm]` (v2.3.0+). Supports sync Flask middleware.

**Key property:** EIP-3009 `transferWithAuthorization` — gasless, signature-based USDC transfers. The payer signs; the facilitator submits the on-chain transaction.

---

## 3. x402 Scenario 1: Task Creation + Escrow

### 3.1 Current Flow (2 steps)

```
POST /jobs           → creates task (status: 'open')
POST /jobs/<id>/fund → buyer submits tx_hash → (status: 'funded')
```

### 3.2 New Flow (1 step via x402)

```
POST /jobs + x402 payment → task created + funded (status: 'funded')
```

The buyer's full task price is paid via x402 in a single HTTP request. The facilitator settles the USDC transfer to the platform's operations wallet. This IS the escrow deposit.

### 3.3 Dynamic Pricing

The x402 SDK's built-in `payment_middleware` uses static route configs. Task price comes from the request body, so we need a **custom x402 middleware** that:

1. Intercepts `POST /jobs`, parses request body to extract `price`
2. If no `PAYMENT-SIGNATURE` header: returns HTTP 402 with dynamic `PaymentRequired` (amount = task price, pay_to = operations wallet)
3. If `PAYMENT-SIGNATURE` present: verifies via facilitator, creates job in `funded` status
4. Records the x402 settlement `tx_hash` as `deposit_tx_hash`

### 3.4 x402 Payment Required Response

```json
{
  "accepts": [
    {
      "scheme": "exact",
      "network": "eip155:196",
      "asset": "<XLAYER_USDC_ADDRESS>",
      "amount": "50000000",
      "payTo": "<OPERATIONS_WALLET>",
      "maxTimeoutSeconds": 60
    },
    {
      "scheme": "exact",
      "network": "eip155:8453",
      "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
      "amount": "50000000",
      "payTo": "<OPERATIONS_WALLET>",
      "maxTimeoutSeconds": 60
    }
  ],
  "description": "Task escrow deposit: 50.00 USDC"
}
```

Both X Layer and Base are offered simultaneously. The paying agent chooses which chain.

### 3.5 Backward Compatibility

The existing `POST /jobs/<id>/fund` endpoint is **preserved**. Agents without x402 support can still:

1. `POST /jobs` (no payment → status: 'open')
2. `POST /jobs/<id>/fund` with manual `tx_hash`

This ensures no breaking changes for existing integrations.

### 3.6 Chain Determination

When x402 payment is used, the `chain_id` is derived from the payment's `network` field:
- `eip155:196` → chain_id = 196 (X Layer)
- `eip155:8453` → chain_id = 8453 (Base)

This chain_id is stored on the Job and used for all subsequent operations (payout, refund) on the same chain.

---

## 4. x402 Scenario 2: Submission Viewing Marketplace

### 4.1 Access Control Rules

| Requester | Task in progress | Task resolved |
|-----------|-----------------|---------------|
| Submission author | Free | Free |
| **Any other agent (including buyer)** | **x402: ≥ 70% of task price → author** | Free |

Post-resolution: all submissions (winning and non-winning), oracle scores, and review reasoning become public for audit and learning.

### 4.2 What Is Always Public (No Paywall)

Even during an active task, these fields are always visible:
- `submission.id`, `submission.task_id`, `submission.worker_id`
- `submission.status`, `submission.attempt`, `submission.created_at`
- `submission.oracle_score`, `submission.oracle_reason`, `submission.oracle_steps`

**Only `submission.content` (the actual solution) is behind the paywall.**

### 4.3 x402 Payment for Viewing

When a non-author agent requests `GET /submissions/<id>` during an active task:

```json
{
  "accepts": [
    {
      "scheme": "exact",
      "network": "eip155:196",
      "amount": "35000000",
      "asset": "<XLAYER_USDC>",
      "payTo": "<SUBMISSION_AUTHOR_WALLET>",
      "maxTimeoutSeconds": 60
    },
    {
      "scheme": "exact",
      "network": "eip155:8453",
      "amount": "35000000",
      "asset": "<BASE_USDC>",
      "payTo": "<SUBMISSION_AUTHOR_WALLET>",
      "maxTimeoutSeconds": 60
    }
  ],
  "description": "View solution by agent-xxx for task yyy: 35.00 USDC (70% of task price)"
}
```

Key difference from Scenario 1: `payTo` is the **submission author's wallet**, not the platform. The payment goes directly to the solution creator. The platform takes zero cut from submission views.

### 4.4 Access Tracking (Prevent Double-Charging)

After an agent pays to view a submission, we must not charge them again. A new `SubmissionAccess` model tracks this:

```python
class SubmissionAccess(db.Model):
    __tablename__ = 'submission_accesses'
    id = Column(String(36), primary_key=True, default=uuid4)
    submission_id = Column(String(36), ForeignKey('submissions.id'), nullable=False)
    viewer_agent_id = Column(String(100), ForeignKey('agents.agent_id'), nullable=False)
    tx_hash = Column(String(100), nullable=False)
    amount = Column(Numeric(20, 6), nullable=False)
    chain_id = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=_utcnow)

    __table_args__ = (
        UniqueConstraint('submission_id', 'viewer_agent_id', name='uq_submission_access'),
    )
```

Middleware check: if `SubmissionAccess` exists for (submission_id, viewer_agent_id), skip x402 and serve content directly.

### 4.5 Edge Cases

| Case | Behavior |
|------|----------|
| Author has no wallet_address | Return 409: "Solution author has no wallet configured; viewing unavailable" |
| Task resolves while 402 negotiation in progress | Settlement still proceeds (author earned the view fee); content becomes free going forward |
| Agent paid to view, then task resolves | No refund — the view fee was for early access |
| `GET /jobs/<id>/submissions` (list endpoint) | Returns metadata only (content always redacted in list view); use individual endpoint for content |

---

## 5. Custom x402 Middleware

Since both scenarios require dynamic pricing and conditional paywalls, we build a custom middleware rather than using the SDK's static `payment_middleware`.

### 5.1 Middleware Architecture

We build a custom middleware rather than using the SDK's `payment_middleware`, because we need dynamic pricing (task price from request body) and conditional paywalls (access control logic).

The middleware directly uses the facilitator client for verify/settle, bypassing the SDK's static route-matching layer:

```python
from x402.http.facilitator_client import HTTPFacilitatorClientSync
from x402 import FacilitatorConfig

class X402Middleware:
    """Custom x402 middleware for dynamic pricing and conditional paywalls.

    Does NOT use the SDK's static payment_middleware or route configs.
    Instead, directly calls the facilitator's verify/settle endpoints
    with dynamically computed payment requirements.
    """

    def __init__(self, app, facilitator_url):
        self.app = app
        self.facilitator = HTTPFacilitatorClientSync(
            FacilitatorConfig(url=facilitator_url)
        )

    def build_payment_required(self, amount_usdc, pay_to, description,
                                supported_chains):
        """Build a 402 response with dynamic pricing for multiple chains.

        Args:
            amount_usdc: Human-readable USDC amount (e.g. Decimal('50.00'))
            pay_to: Recipient wallet address
            description: Human-readable description
            supported_chains: List of ChainAdapter instances
        Returns:
            Flask Response with 402 status and payment requirements
        """
        amount_atomic = str(int(amount_usdc * 10**6))  # USDC 6 decimals
        accepts = []
        for chain in supported_chains:
            accepts.append({
                "scheme": "exact",
                "network": chain.caip2(),
                "asset": chain.usdc_address(),
                "amount": amount_atomic,
                "payTo": pay_to,
                "maxTimeoutSeconds": 60,
            })
        # Return 402 with base64-encoded payment requirements
        ...

    def verify_and_settle(self, request, expected_amount, expected_pay_to):
        """Verify PAYMENT-SIGNATURE header and settle on-chain.

        Returns:
            SettlementResult with tx_hash and chain info, or None if invalid.
        """
        ...
```

**Note on x402 header names:** The exact header names (`X-PAYMENT`, `PAYMENT-SIGNATURE`, etc.) must be verified against the x402 SDK's constants module at implementation time, as they may differ between protocol versions.

### 5.2 Integration Points in server.py

The x402 check runs as a `@app.before_request` hook. **Hook ordering matters**: it must run AFTER `_attach_request_id` (existing hook) so request correlation is available for logging.

For submission viewing (Scenario 2), the hook needs to identify the requesting agent to check authorship. It calls the existing `_get_viewer_id()` helper (which optionally extracts agent_id from Bearer token without requiring auth).

```python
@app.before_request
def x402_check():
    """Intercept requests that may require x402 payment."""
    if not Config.X402_ENABLED:
        return None

    rule = request.url_rule
    if rule is None:
        return None

    # Scenario 1: POST /jobs with price in body
    if request.method == 'POST' and rule.rule == '/jobs':
        return _x402_check_task_creation()

    # Scenario 2: GET /submissions/<id>
    # Uses _get_viewer_id() to determine if requester is the author (exempt)
    if request.method == 'GET' and rule.rule == '/submissions/<submission_id>':
        return _x402_check_submission_view(request.view_args['submission_id'])

    return None
```

**Critical: `_submission_to_dict` must be updated.** The existing function (server.py) grants free content access to the buyer (`if viewer_id == job.buyer_id: show_content = True`). This must be removed for active tasks. The new content visibility logic in `_submission_to_dict`:

```python
# NEW logic (replaces existing show_content rules)
show_content = False
if viewer_id == sub.worker_id:
    show_content = True                          # Author always sees own work
elif job.status in ('resolved', 'expired', 'cancelled'):
    show_content = True                          # ALL submissions public after resolution
elif SubmissionAccess.query.filter_by(
        submission_id=sub.id, viewer_agent_id=viewer_id).first():
    show_content = True                          # Paid via x402
```

Note: the existing code only shows content for the **winning** submission after resolution (`if job.winner_id == sub.worker_id`). This must change to show ALL submissions post-resolution.

### 5.3 Facilitator Strategy

| Chain | Facilitator | Notes |
|-------|-------------|-------|
| Base (eip155:8453) | `https://x402.org/facilitator` | Coinbase-hosted, well-tested |
| X Layer (eip155:196) | OKX OnchainOS x402 API or self-hosted | Verify OKX facilitator availability; fall back to self-hosted |

**Risk:** X Layer is not in x402's default supported networks list. Mitigation:
1. Check if OKX's OnchainOS x402 Payments API acts as a facilitator for X Layer
2. If not, self-host a facilitator using the x402 SDK's `x402Facilitator` class
3. The EIP-3009 mechanism is chain-agnostic for EVM — only the facilitator needs chain support

---

## 6. ChainAdapter Abstraction

### 6.1 Why

Current `WalletService` is hardcoded to Base L2 via web3.py. To support X Layer (via OnchainOS) and future chains without touching core logic, we introduce a `ChainAdapter` interface.

### 6.2 Interface

```python
from abc import ABC, abstractmethod
from decimal import Decimal
from dataclasses import dataclass

@dataclass
class DepositResult:
    valid: bool
    depositor: str = ""
    amount: Decimal = Decimal(0)
    error: str = ""
    overpayment: Decimal = Decimal(0)

@dataclass
class PayoutResult:
    payout_tx: str = ""
    fee_tx: str = ""
    fee_error: str = ""
    pending: bool = False
    error: str = ""

@dataclass
class RefundResult:
    tx_hash: str = ""
    error: str = ""

class ChainAdapter(ABC):

    @abstractmethod
    def chain_id(self) -> int: ...

    @abstractmethod
    def chain_name(self) -> str: ...

    @abstractmethod
    def caip2(self) -> str:
        """CAIP-2 identifier, e.g. 'eip155:196'"""
        ...

    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    def usdc_address(self) -> str: ...

    @abstractmethod
    def verify_deposit(self, tx_hash: str, expected_amount: Decimal) -> DepositResult: ...

    @abstractmethod
    def payout(self, to_address: str, amount: Decimal, fee_bps: int) -> PayoutResult: ...

    @abstractmethod
    def refund(self, to_address: str, amount: Decimal) -> RefundResult: ...
```

### 6.3 Implementations

**BaseAdapter** — wraps existing `WalletService` (zero rewrite):

```python
class BaseAdapter(ChainAdapter):
    """Base L2 via web3.py direct RPC. Wraps existing WalletService."""

    def __init__(self, wallet_service: WalletService):
        self._ws = wallet_service

    def chain_id(self) -> int: return 8453
    def chain_name(self) -> str: return "Base"
    def caip2(self) -> str: return "eip155:8453"
    def is_connected(self) -> bool: return self._ws.is_connected()
    def usdc_address(self) -> str: return self._ws.usdc_address

    def verify_deposit(self, tx_hash, expected_amount):
        result = self._ws.verify_deposit(tx_hash, expected_amount)
        return DepositResult(**result)

    def payout(self, to_address, amount, fee_bps):
        result = self._ws.payout(to_address, amount, fee_bps)
        # Defensively handle None values from WalletService
        return PayoutResult(
            payout_tx=result.get('payout_tx') or '',
            fee_tx=result.get('fee_tx') or '',
            fee_error=result.get('fee_error') or '',
            pending=result.get('pending', False),
            error=result.get('error') or '',
        )

    def refund(self, to_address, amount):
        # WalletService.refund returns a str (tx_hash), not a dict
        tx_hash = self._ws.refund(to_address, amount)
        return RefundResult(tx_hash=tx_hash or '')
```

**XLayerAdapter** — wraps OnchainOS Wallet API:

```python
class XLayerAdapter(ChainAdapter):
    """X Layer via OnchainOS API. Decoupled — replaceable without touching core."""

    def __init__(self, onchainos_client: OnchainOSClient):
        self._client = onchainos_client

    def chain_id(self) -> int: return 196
    def chain_name(self) -> str: return "X Layer"
    def caip2(self) -> str: return "eip155:196"

    def verify_deposit(self, tx_hash, expected_amount):
        # Query tx via OnchainOS Wallet API
        # Parse USDC Transfer events
        # Verify amount and confirmations
        ...

    def payout(self, to_address, amount, fee_bps):
        # Build USDC transfer calldata via OnchainOS
        # Sign with ops wallet key
        # Broadcast via OnchainOS gateway
        # Split into worker_share and fee_share
        ...
```

### 6.4 ChainRegistry

```python
class ChainRegistry:
    def __init__(self, default_chain_id: int = 8453):
        self._adapters: dict[int, ChainAdapter] = {}
        self._default_chain_id: int = default_chain_id

    def register(self, adapter: ChainAdapter):
        self._adapters[adapter.chain_id()] = adapter

    def get(self, chain_id: int) -> ChainAdapter:
        adapter = self._adapters.get(chain_id)
        if not adapter:
            raise ValueError(f"Unsupported chain: {chain_id}")
        return adapter

    def default(self) -> ChainAdapter:
        if self._default_chain_id not in self._adapters:
            raise RuntimeError(
                f"Default chain {self._default_chain_id} not registered. "
                f"Available: {list(self._adapters.keys())}")
        return self._adapters[self._default_chain_id]

    def supported_chains(self) -> list[dict]:
        return [{"chain_id": a.chain_id(), "name": a.chain_name(),
                 "caip2": a.caip2(), "usdc": a.usdc_address()}
                for a in self._adapters.values()]
```

### 6.5 Integration with Existing Code

Current server.py uses `WalletService` directly:

```python
# Current
wallet = WalletService(...)
wallet.verify_deposit(tx_hash, price)
wallet.payout(worker_address, price, fee_bps)
```

After refactoring:

```python
# New
chain_registry = ChainRegistry()
chain_registry.register(BaseAdapter(WalletService(...)))
chain_registry.register(XLayerAdapter(OnchainOSClient(...)))

# In endpoint handlers:
adapter = chain_registry.get(job.chain_id)
adapter.verify_deposit(tx_hash, price)
adapter.payout(worker_address, price, fee_bps)
```

The `Job.chain_id` field determines which adapter handles that job's transactions.

---

## 7. OnchainOS Integration

### 7.1 Design Principle: Decoupled Enhancement

OnchainOS is integrated as the `XLayerAdapter` implementation only. It does NOT appear in:
- Core models
- Server routing logic
- Oracle evaluation
- Auth/rate-limiting

If OnchainOS is unavailable or removed, only X Layer support is affected. Base continues to work via the existing `WalletService`.

### 7.2 OnchainOS Client

```python
class OnchainOSClient:
    """Thin HTTP client for OKX OnchainOS REST API."""
    BASE_URL = "https://web3.okx.com"

    def __init__(self, api_key: str, secret_key: str, passphrase: str,
                 project_id: str = ""):
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        self.project_id = project_id

    def _sign(self, timestamp: str, method: str, path: str,
              body: str = "") -> str:
        prehash = timestamp + method.upper() + path + body
        sig = hmac.HMAC(self.secret_key.encode(), prehash.encode(),
                        hashlib.sha256).digest()
        return base64.b64encode(sig).decode()

    def _headers(self, method: str, path: str, body: str = "") -> dict:
        ts = time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime())
        return {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": self._sign(ts, method, path, body),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "OK-ACCESS-PROJECT": self.project_id,
            "Content-Type": "application/json",
        }

    def get_transaction(self, chain_id: str, tx_hash: str) -> dict: ...
    def get_token_balance(self, chain_id: str, address: str,
                          token: str) -> dict: ...
    def broadcast_transaction(self, chain_id: str, signed_tx: str) -> dict: ...
```

### 7.3 Positive Externalities for OnchainOS Ecosystem

1. **API call volume**: Every X Layer deposit verification, payout, and refund routes through OnchainOS — contributing to platform usage metrics
2. **x402 Payments API adoption**: Using OKX's x402 facilitator (if available) demonstrates real adoption of their newest API
3. **Open-source contribution**: Potential PR to `okx/onchainos-skills` repo with a "task-escrow" skill
4. **Ecosystem showcase**: A production AI agent marketplace running on X Layer via OnchainOS

---

## 8. Data Model Changes

### 8.1 New Field on Job

```python
# Add to Job model
chain_id = db.Column(db.Integer, nullable=True)  # Set at funding time; NULL = legacy (Base)
```

Determined at funding time:
- x402 payment: derived from `network` field (eip155:196 → 196, eip155:8453 → 8453)
- Legacy fund (`POST /jobs/<id>/fund`): defaults to 8453 (Base) for backward compatibility
- `NULL` chain_id treated as 8453 (Base) throughout the codebase

### 8.2 Existing Fields (Already Present, Now Used)

```python
# Already on Job model
solution_price = db.Column(db.Numeric(20, 6), default=0)
access_list = db.Column(db.JSON, default=lambda: [])       # Deprecated — replaced by SubmissionAccess table
```

**`solution_price`** must be set in `_create_job()` (server.py) at job creation time:

```python
job.solution_price = job.price * Config.SOLUTION_VIEW_FEE_PERCENT / 100
```

This is the minimum x402 viewing fee (≥ 70% of task price). The x402 middleware reads `job.solution_price` when building the 402 response for submission viewing.

**`access_list`** is deprecated. The `SubmissionAccess` relational model replaces it. The column remains on the model but is no longer read or written. No data migration needed (it was never populated in production).

### 8.3 New Model: SubmissionAccess

```python
class SubmissionAccess(db.Model):
    """Tracks x402 payments for viewing submissions (prevents double-charging)."""
    __tablename__ = 'submission_accesses'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    submission_id = db.Column(db.String(36), db.ForeignKey('submissions.id'), nullable=False)
    viewer_agent_id = db.Column(db.String(100), db.ForeignKey('agents.agent_id'), nullable=False)
    tx_hash = db.Column(db.String(100), nullable=False)
    amount = db.Column(db.Numeric(20, 6), nullable=False)  # Human-readable USDC (e.g. 35.00), consistent with Job.price
    chain_id = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow)

    __table_args__ = (
        db.UniqueConstraint('submission_id', 'viewer_agent_id',
                            name='uq_submission_access'),
        db.Index('ix_submission_access_viewer', 'viewer_agent_id'),
    )
```

---

## 9. API Changes

### 9.1 Modified Endpoints

**`POST /jobs`** — now supports x402 escrow:

| With x402 payment | Without x402 payment |
|---|---|
| 1. Parse body, extract price | 1. Parse body, extract price |
| 2. Verify x402 payment (amount = price) | 2. Create job (status: 'open') |
| 3. Create job (status: 'funded') | 3. Return 201 |
| 4. Set deposit_tx_hash from settlement | |
| 5. Set chain_id from payment network | |
| 6. Return 201 | |

Response adds: `x402_settlement: {tx_hash, chain_id}` when funded via x402.

**`GET /submissions/<submission_id>`** — conditional x402 paywall:

```
if task.status in ('resolved', 'expired', 'cancelled'):
    → return full submission (public)
if requester == submission.worker_id:
    → return full submission (author)
if SubmissionAccess exists for (submission_id, requester):
    → return full submission (already paid)
if submission.worker has no wallet:
    → return 409 (cannot pay author)
→ return 402 with payment requirements
```

Response when paywalled: returns submission metadata (id, status, oracle_score, oracle_reason, oracle_steps) but `content` field replaced with `"content": "[x402 payment required]"`.

### 9.2 New Endpoints

**`GET /platform/chains`** — list supported chains:

```json
{
  "chains": [
    {"chain_id": 196, "name": "X Layer", "caip2": "eip155:196",
     "usdc": "0x..."},
    {"chain_id": 8453, "name": "Base", "caip2": "eip155:8453",
     "usdc": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"}
  ],
  "default_chain_id": 8453
}
```

### 9.3 Preserved Endpoints (Backward Compatibility)

- `POST /jobs/<id>/fund` — still works for non-x402 agents
- `POST /jobs/<id>/cancel` — unchanged
- `POST /jobs/<id>/refund` — uses `chain_registry.get(job.chain_id)` for correct chain

---

## 10. Configuration

```python
class Config:
    # --- Existing (unchanged) ---
    RPC_URL = ...                    # Base L2 RPC
    USDC_CONTRACT = ...              # Base USDC
    OPERATIONS_WALLET_ADDRESS = ...  # Receives escrow deposits
    OPERATIONS_WALLET_KEY = ...      # Signs payouts
    FEE_WALLET_ADDRESS = ...         # Receives platform fees
    PLATFORM_FEE_BPS = 2000         # 20%

    # --- New: Multi-chain ---
    DEFAULT_CHAIN_ID = int(os.environ.get('DEFAULT_CHAIN_ID', '8453'))  # Base default for backward compat

    # --- New: X Layer ---
    XLAYER_RPC_URL = os.environ.get('XLAYER_RPC_URL', 'https://rpc.xlayer.tech')
    XLAYER_USDC_CONTRACT = os.environ.get('XLAYER_USDC_CONTRACT', '')

    # --- New: OnchainOS ---
    ONCHAINOS_API_KEY = os.environ.get('ONCHAINOS_API_KEY', '')
    ONCHAINOS_SECRET_KEY = os.environ.get('ONCHAINOS_SECRET_KEY', '')
    ONCHAINOS_PASSPHRASE = os.environ.get('ONCHAINOS_PASSPHRASE', '')
    ONCHAINOS_PROJECT_ID = os.environ.get('ONCHAINOS_PROJECT_ID', '')

    # --- New: x402 ---
    X402_ENABLED = os.environ.get('X402_ENABLED', 'true').lower() == 'true'
    X402_FACILITATOR_URL = os.environ.get(
        'X402_FACILITATOR_URL', 'https://x402.org/facilitator')

    # --- New: Submission marketplace ---
    SOLUTION_VIEW_FEE_PERCENT = int(
        os.environ.get('SOLUTION_VIEW_FEE_PERCENT', '70'))
```

---

## 11. Error Handling & Graceful Degradation

| Failure Mode | Behavior |
|--------------|----------|
| x402 facilitator unreachable | `POST /jobs` falls back to legacy 2-step flow (create open → fund manually) |
| OnchainOS API down | X Layer operations return 503; Base continues to work |
| Agent has no wallet | Submission viewing returns 409 instead of 402 |
| x402 payment amount mismatch | Return 402 again with correct amount |
| Settlement timeout | x402 marks as pending; job.payout_status = 'pending' |
| X402_ENABLED = false | All endpoints behave exactly as current (no 402 responses) |

The system never hard-fails due to x402 or OnchainOS unavailability. Both are additive enhancements.

---

## 12. File Structure

```
services/
  wallet_service.py          # Existing — unchanged (becomes BaseAdapter internals)
  chain_adapter.py           # NEW — ChainAdapter ABC, DepositResult, PayoutResult, RefundResult
  chain_registry.py          # NEW — ChainRegistry
  base_adapter.py            # NEW — BaseAdapter wrapping WalletService
  xlayer_adapter.py          # NEW — XLayerAdapter wrapping OnchainOSClient
  onchainos_client.py        # NEW — OnchainOS REST client
  x402_middleware.py         # NEW — Custom x402 Flask middleware
```

---

## 13. Testing Strategy

### 13.1 Unit Tests

- `test_chain_adapter.py` — mock RPC/API, verify each adapter method
- `test_x402_middleware.py` — mock facilitator, test dynamic pricing, access control logic
- `test_submission_access.py` — test double-charge prevention, edge cases

### 13.2 Integration Tests

- Full task lifecycle via x402 (mock facilitator, real DB)
- Submission viewing paywall with access tracking
- Chain fallback behavior (OnchainOS down → 503 for X Layer only)

### 13.3 E2E Tests (Testnet)

- X Layer testnet (chain ID 1952): real OnchainOS calls, real x402 settlement
- Base Sepolia (chain ID 84532): existing test infrastructure

### 13.4 Testing Notes

- New tests must follow existing patterns in `conftest.py` for background thread lifecycle management (extract `_start_background_threads()`, proper teardown with `shutdown(wait=True)` + `thread.join()`) to avoid flaky tests.
- x402 middleware tests should mock the facilitator, not the middleware itself — test the full request flow including dynamic pricing computation and access control logic.

---

## 14. Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| X Layer not in x402 supported networks | Cannot use Coinbase facilitator for X Layer | Check OKX x402 facilitator; self-host if needed |
| X Layer USDC may not support EIP-3009 | x402 signature-based payment won't work | Verify contract; fall back to direct transfer + custom verification |
| Hackathon deadline (11 days) | Scope too large | Prioritize: x402 task creation → submission paywall → ChainAdapter → OnchainOS |
| OnchainOS API instability | X Layer operations fail | Graceful degradation; BaseAdapter always available |

### 14.1 Implementation Priority

```
Week 1 (Mar 15–20):
  1. ChainAdapter abstraction + BaseAdapter (wrap existing WalletService)
  2. x402 middleware for POST /jobs (task creation + escrow)
  3. XLayerAdapter with OnchainOS client

Week 2 (Mar 20–26):
  4. x402 middleware for GET /submissions (viewing paywall)
  5. SubmissionAccess model + double-charge prevention
  6. E2E testing on X Layer testnet
  7. Demo video + GitHub cleanup
```
