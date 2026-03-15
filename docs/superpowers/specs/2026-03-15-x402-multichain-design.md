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
  │◄── 402 + PAYMENT-REQUIRED header ─┤                │
  │  [signs EIP-3009]       │                            │
  │── GET + X-PAYMENT hdr ─►│── POST /verify ──────────►│
  │                         │◄── {is_valid: true} ──────┤
  │◄── 200 + X-PAYMENT-RESPONSE hdr ─┤                 │
  │                         ├── POST /settle ──────────►│
  │                         │◄── {transaction: 0x...} ──┤
```

### 2.1 HTTP Headers (verified from x402 SDK v2.3.0)

| Constant | Header Name | Direction | Content |
|----------|-------------|-----------|---------|
| `PAYMENT_REQUIRED_HEADER` | `PAYMENT-REQUIRED` | Server → Client | Base64 `PaymentRequired` JSON |
| `PAYMENT_SIGNATURE_HEADER` | `PAYMENT-SIGNATURE` | Client → Server | Base64 `PaymentPayload` JSON |
| `PAYMENT_RESPONSE_HEADER` | `PAYMENT-RESPONSE` | Server → Client | Base64 `SettleResponse` JSON |
| `X_PAYMENT_HEADER` | `X-PAYMENT` | Client → Server | Alternative to PAYMENT-SIGNATURE |
| `X_PAYMENT_RESPONSE_HEADER` | `X-PAYMENT-RESPONSE` | Server → Client | Alternative to PAYMENT-RESPONSE |

The SDK checks both header variants: `payment-signature` OR `x-payment`.

### 2.2 Core Data Schemas (verified from SDK)

**PaymentRequired** (402 response):
```python
class PaymentRequired:
    x402_version: int         # Protocol version (default: 2)
    accepts: list[PaymentRequirements]  # What payments are accepted
    error: str | None         # Error message
    resource: ResourceInfo | None
    extensions: dict | None
```

**PaymentRequirements** (each item in `accepts`):
```python
class PaymentRequirements:
    scheme: str               # "exact"
    network: str              # CAIP-2: "eip155:196", "eip155:8453"
    asset: str                # Token contract address
    amount: str               # Atomic units string: "50000000" = 50 USDC
    pay_to: str               # Recipient wallet address
    max_timeout_seconds: int
    extra: dict | None
```

**SettleResponse** (from facilitator):
```python
class SettleResponse:
    success: bool
    transaction: str          # On-chain tx hash ← this becomes deposit_tx_hash
    network: str              # CAIP-2 network ID
    payer: str | None         # Payer wallet address ← this becomes depositor_address
    error_reason: str | None
    error_message: str | None
```

**VerifyResponse** (from facilitator):
```python
class VerifyResponse:
    is_valid: bool
    payer: str | None
    invalid_reason: str | None
    invalid_message: str | None
```

### 2.3 SDK Components

**Python SDK:** `x402[flask,evm]` (v2.3.0). Key classes:

| Class | Import | Purpose |
|-------|--------|---------|
| `x402ResourceServerSync` | `from x402 import ...` | Core verify/settle logic |
| `ExactEvmServerScheme` | `from x402.mechanisms.evm.exact import ...` | EVM payment scheme |
| `HTTPFacilitatorClientSync` | `from x402.http.facilitator_client import ...` | HTTP facilitator client |
| `FacilitatorConfig` | `from x402 import ...` | Facilitator URL config |
| `PaymentRequired` | `from x402 import ...` | 402 response model |
| `PaymentRequirements` | `from x402 import ...` | Payment option model |

**Lifecycle hooks** on `x402ResourceServerSync`:
- `on_before_verify()` / `on_after_verify()` / `on_verify_failure()`
- `on_before_settle()` / `on_after_settle()` / `on_settle_failure()`

**Built-in dynamic pricing:** `PaymentOption` accepts `DynamicPrice` (callable) and `DynamicPayTo` (callable) — the SDK natively supports dynamic pricing per-request. However, we handle x402 in route handlers directly for reasons explained in Section 5.

### 2.4 Facilitators

**Two facilitators needed** (different chains, different APIs):

| Chain | Facilitator | URL | Auth |
|-------|-------------|-----|------|
| Base (8453) | Coinbase | `https://x402.org/facilitator` | None required |
| X Layer (196) | **OKX OnchainOS** | `https://web3.okx.com/api/v6/x402/` | HMAC (OK-ACCESS-*) |

**OKX x402 endpoints** (verified):
- `GET /api/v6/x402/supported` — list supported schemes/networks
- `POST /api/v6/x402/verify` — verify payment (returns `isValid`, `payer`)
- `POST /api/v6/x402/settle` — settle payment (returns `txHash`, `chainIndex`)

**OKX API format differs** from Coinbase's. The OKX API uses `chainIndex` (string), `maxAmountRequired` (vs `amount`), and wraps responses in `{code, data, msg}`. We need a custom `FacilitatorClientSync` adapter (see Section 5).

**Key property:** EIP-3009 `transferWithAuthorization` — gasless, signature-based USDC transfers. The payer signs; the facilitator submits the on-chain transaction. X Layer USDC must support EIP-3009 for x402 to work (to be verified during implementation).

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
      "pay_to": "<OPERATIONS_WALLET>",
      "max_timeout_seconds": 60
    },
    {
      "scheme": "exact",
      "network": "eip155:8453",
      "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
      "amount": "50000000",
      "pay_to": "<OPERATIONS_WALLET>",
      "max_timeout_seconds": 60
    }
  ],
  "resource": {"description": "Task escrow deposit: 50.00 USDC"}
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
      "pay_to": "<SUBMISSION_AUTHOR_WALLET>",
      "max_timeout_seconds": 60
    },
    {
      "scheme": "exact",
      "network": "eip155:8453",
      "amount": "35000000",
      "asset": "<BASE_USDC>",
      "pay_to": "<SUBMISSION_AUTHOR_WALLET>",
      "max_timeout_seconds": 60
    }
  ],
  "resource": {"description": "View solution by agent-xxx: 35.00 USDC (70% of task price)"}
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

## 5. x402 Integration Architecture

### 5.1 Why Not Use Built-in `payment_middleware`

The SDK's `PaymentMiddleware` (WSGI wrapper) supports `DynamicPrice` and `DynamicPayTo` callbacks, which could handle dynamic pricing. However, two problems make it unsuitable:

1. **Settlement happens AFTER the route handler returns.** The middleware runs `settle_payment()` post-response, storing the tx_hash only in the `PAYMENT-RESPONSE` header. We need `deposit_tx_hash` in the database at job creation time — not after the response is sent.

2. **Conditional paywalling is complex.** Submission viewing requires DB queries (task status, authorship, SubmissionAccess records) to decide whether to paywall. The middleware's `requires_payment()` only matches route patterns.

### 5.2 Chosen Approach: Route-Level x402 Handling

We use `x402ResourceServerSync` directly in route handlers for verify/settle, and build 402 responses manually using the SDK's data models. This gives us full control over the flow:

```python
from x402 import (x402ResourceServerSync, FacilitatorConfig,
                   PaymentRequired, PaymentRequirements)
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.http.facilitator_client import HTTPFacilitatorClientSync
from x402.http import (encode_payment_required_header,
                       decode_payment_signature_header,
                       encode_payment_response_header,
                       PAYMENT_SIGNATURE_HEADER, X_PAYMENT_HEADER)

# Initialize at startup
coinbase_facilitator = HTTPFacilitatorClientSync(
    FacilitatorConfig(url="https://x402.org/facilitator"))
coinbase_server = x402ResourceServerSync(coinbase_facilitator)
coinbase_server.register("eip155:8453", ExactEvmServerScheme())

okx_facilitator = OKXFacilitatorClient(  # Custom adapter (see 5.3)
    api_key=Config.ONCHAINOS_API_KEY, ...)
okx_server = x402ResourceServerSync(okx_facilitator)
okx_server.register("eip155:196", ExactEvmServerScheme())
```

**Scenario 1 — POST /jobs (task creation + escrow):**

```python
@app.route('/jobs', methods=['POST'])
@require_auth
def create_job():
    data = request.get_json()
    price = Decimal(str(data.get('price', 0)))

    # Check for x402 payment header
    payment_header = (request.headers.get(PAYMENT_SIGNATURE_HEADER)
                      or request.headers.get(X_PAYMENT_HEADER))

    if payment_header and Config.X402_ENABLED:
        # Verify → settle → THEN create job (avoid orphaned 'funded' jobs on crash)
        payload = decode_payment_signature_header(payment_header)
        network = payload.accepted.network
        server = _get_x402_server(network)  # Route to correct facilitator

        verify_result = server.verify_payment(payload, payload.accepted)
        if not verify_result.is_valid:
            return jsonify({"error": verify_result.invalid_reason}), 402

        settle_result = server.settle_payment(payload, payload.accepted)
        if not settle_result.success:
            return jsonify({"error": "x402 settlement failed",
                           "reason": settle_result.error_reason}), 402

        # Settlement succeeded — now create job as funded
        job = _create_job(data, status='funded')
        job.deposit_tx_hash = settle_result.transaction
        job.depositor_address = settle_result.payer
        job.chain_id = _parse_chain_id(settle_result.network)
        job.deposit_amount = price
        job.solution_price = price * Config.SOLUTION_VIEW_FEE_PERCENT / 100
        db.session.commit()

        return jsonify({..., "x402_settlement": {
            "tx_hash": settle_result.transaction,
            "chain_id": job.chain_id,
            "payer": settle_result.payer,
        }}), 201

    elif not payment_header and Config.X402_ENABLED:
        # No payment header — return 402 with requirements
        requirements = _build_requirements(price, Config.OPERATIONS_WALLET_ADDRESS)
        payment_required = PaymentRequired(accepts=requirements)
        resp = jsonify({"error": "Payment required",
                       "description": f"Task escrow: {price} USDC"})
        resp.status_code = 402
        resp.headers['PAYMENT-REQUIRED'] = encode_payment_required_header(
            payment_required)
        return resp

    else:
        # Legacy flow (X402_ENABLED=false or no header)
        job = _create_job(data, status='open')
        return jsonify({...}), 201
```

**Scenario 2 — GET /submissions/<id> (viewing paywall):** handled similarly in route handler with access control checks (see Section 5.4).

### 5.3 OKX Facilitator Adapter

OKX's x402 API differs from the Coinbase standard. We implement a custom `FacilitatorClientSync`:

```python
class OKXFacilitatorClient:
    """Adapts OKX's x402 API to the x402 SDK's FacilitatorClientSync protocol."""

    BASE = "https://web3.okx.com"

    def __init__(self, api_key, secret_key, passphrase):
        self._client = OnchainOSClient(api_key, secret_key, passphrase)

    # NOTE: OKX API uses x402Version "1" (string). The SDK's PaymentRequired
    # defaults to version 2. This is a known discrepancy — OKX may update
    # to v2 later. Verify during implementation and adjust if needed.

    def verify(self, payload, requirements) -> VerifyResponse:
        resp = self._client.post("/api/v6/x402/verify", {
            "x402Version": "1",
            "chainIndex": _network_to_chain_index(requirements.network),
            "paymentPayload": payload.model_dump(),
            "paymentRequirements": {
                "scheme": requirements.scheme,
                "maxAmountRequired": requirements.amount,
                "payTo": requirements.pay_to,
                "asset": requirements.asset,
                "description": "",
            },
        })
        data = resp["data"][0]
        return VerifyResponse(
            is_valid=data["isValid"],
            payer=data.get("payer"),
            invalid_reason=data.get("invalidReason"),
            invalid_message=data.get("invalidMessage"),
        )

    def settle(self, payload, requirements) -> SettleResponse:
        resp = self._client.post("/api/v6/x402/settle", {
            "x402Version": "1",
            "chainIndex": _network_to_chain_index(requirements.network),
            "paymentPayload": payload.model_dump(),
            "paymentRequirements": {
                "scheme": requirements.scheme,
                "maxAmountRequired": requirements.amount,
                "payTo": requirements.pay_to,
                "asset": requirements.asset,
            },
        })
        data = resp["data"][0]
        return SettleResponse(
            success=data["success"],
            transaction=data.get("txHash", ""),
            network=f"eip155:{data.get('chainIndex', '196')}",
            payer=data.get("payer"),
            error_reason=data.get("errorReason"),
            error_message=data.get("errorMessage"),
        )
```

### 5.4 Submission Viewing Flow (Route-Level)

The `GET /submissions/<id>` route handler integrates x402 directly. Uses `_get_viewer_id()` to identify the requester without requiring auth:

```python
@app.route('/submissions/<submission_id>')
def get_submission(submission_id):
    sub = Submission.query.get_or_404(submission_id)
    job = Job.query.get(sub.task_id)
    viewer_id = _get_viewer_id()  # Optional auth — extracts from Bearer token

    # Determine if content should be shown
    show_content = _check_submission_access(sub, job, viewer_id)

    if show_content is None and Config.X402_ENABLED:
        # show_content=None means "payment required"
        payment_header = (request.headers.get(PAYMENT_SIGNATURE_HEADER)
                          or request.headers.get(X_PAYMENT_HEADER))

        if payment_header:
            # Verify and settle payment to author
            payload = decode_payment_signature_header(payment_header)

            # Validate payment amount matches expected price
            expected_atomic = str(int(job.solution_price * 10**6))
            if payload.accepted.amount != expected_atomic:
                return jsonify({"error": "Payment amount mismatch",
                               "expected": expected_atomic}), 402

            server = _get_x402_server(payload.accepted.network)
            verify = server.verify_payment(payload, payload.accepted)
            if verify.is_valid:
                settle = server.settle_payment(payload, payload.accepted)
                if settle.success:
                    _record_submission_access(
                        sub, viewer_id, settle, job.solution_price)
                    show_content = True
            if not show_content:
                return jsonify({"error": "Payment verification failed"}), 402
        else:
            # Return 402 with payment requirements (pay_to = author wallet)
            author = Agent.query.get(sub.worker_id)
            if not author or not author.wallet_address:
                return jsonify({"error": "Author has no wallet"}), 409
            requirements = _build_requirements(
                job.solution_price, author.wallet_address)
            payment_required = PaymentRequired(accepts=requirements)
            resp = _submission_to_dict(sub, viewer_id, show_content=False)
            resp_obj = jsonify(resp)
            resp_obj.status_code = 402
            resp_obj.headers['PAYMENT-REQUIRED'] = (
                encode_payment_required_header(payment_required))
            return resp_obj

    return jsonify(_submission_to_dict(sub, viewer_id,
                                       show_content=bool(show_content)))
```

### 5.5 Access Control Changes to `_submission_to_dict`

**Critical: `_submission_to_dict` must be updated.** The existing function grants free content access to the buyer (`if viewer_id == job.buyer_id: show_content = True`). This must be removed for active tasks.

New `_check_submission_access` function:

```python
def _check_submission_access(sub, job, viewer_id):
    """Returns True (show), False (hide), or None (payment required)."""
    if viewer_id == sub.worker_id:
        return True                              # Author always sees own work
    if job.status in ('resolved', 'expired', 'cancelled'):
        return True                              # ALL submissions public after resolution
    if viewer_id and SubmissionAccess.query.filter_by(
            submission_id=sub.id, viewer_agent_id=viewer_id).first():
        return True                              # Already paid via x402
    if job.status == 'funded':
        return None                              # Payment required
    return False                                 # Task not funded, content unavailable
```

**Breaking change from existing behavior:**
1. Buyer no longer gets free access during active tasks (was: `if viewer_id == job.buyer_id: show_content = True`)
2. ALL submissions become public after resolution (was: only winning submission visible)

### 5.6 Helper Functions

```python
def _build_requirements(amount_usdc, pay_to):
    """Build PaymentRequirements for all supported chains."""
    # USDC is 6 decimals on all supported chains (Base, X Layer)
    amount_atomic = str(int(amount_usdc * 10**6))
    requirements = []
    for adapter in chain_registry.adapters():
        requirements.append(PaymentRequirements(
            scheme="exact",
            network=adapter.caip2(),
            asset=adapter.usdc_address(),
            amount=amount_atomic,
            pay_to=pay_to,
            max_timeout_seconds=60,
        ))
    return requirements

# x402 server registry — maps chain_id to facilitator-backed server
_x402_servers: dict[int, x402ResourceServerSync] = {}
# Populated at startup: _x402_servers[8453] = coinbase_server, _x402_servers[196] = okx_server

def _get_x402_server(network: str) -> x402ResourceServerSync:
    """Route to correct facilitator based on CAIP-2 network string."""
    chain_id = _parse_chain_id(network)
    server = _x402_servers.get(chain_id)
    if not server:
        raise ValueError(f"No x402 facilitator registered for chain {chain_id}")
    return server

def _parse_chain_id(network: str) -> int:
    """Extract chain ID from CAIP-2 network string. e.g. 'eip155:196' → 196."""
    try:
        return int(network.split(":")[-1])
    except (ValueError, IndexError):
        raise ValueError(f"Invalid CAIP-2 network: {network}")

def _record_submission_access(sub, viewer_id, settle_result, solution_price):
    """Record that viewer paid to access this submission."""
    access = SubmissionAccess(
        submission_id=sub.id,
        viewer_agent_id=viewer_id,
        tx_hash=settle_result.transaction,
        amount=Decimal(solution_price),
        chain_id=_parse_chain_id(settle_result.network),
    )
    db.session.add(access)
    db.session.commit()
```

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

    def adapters(self) -> list[ChainAdapter]:
        """All registered adapters (for building multi-chain x402 requirements)."""
        return list(self._adapters.values())

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
| 2. Verify x402 payment | 2. Create job (status: 'open') |
| 3. Settle x402 payment (get tx_hash) | 3. Return 201 |
| 4. Create job (status: 'funded') + set deposit_tx_hash, chain_id, solution_price | |
| 5. Return 201 | |

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
    X402_COINBASE_FACILITATOR_URL = os.environ.get(
        'X402_COINBASE_FACILITATOR_URL', 'https://x402.org/facilitator')
    X402_OKX_FACILITATOR_URL = os.environ.get(
        'X402_OKX_FACILITATOR_URL', 'https://web3.okx.com/api/v6/x402')

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
  onchainos_client.py        # NEW — OnchainOS REST client (HMAC auth)
  okx_facilitator.py         # NEW — OKX x402 facilitator adapter
  x402_service.py            # NEW — x402 route-level helpers (build_requirements, verify, settle)
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
| X Layer USDC may not support EIP-3009 | x402 signature-based payment won't work | Verify contract; fall back to direct USDC transfer + custom verification |
| OKX facilitator API format mismatch | Custom adapter needed | `OKXFacilitatorClient` adapter (Section 5.3) — already designed |
| Hackathon deadline (11 days) | Scope too large | Prioritize: x402 task creation → submission paywall → ChainAdapter → OnchainOS |
| OnchainOS API instability | X Layer operations fail | Graceful degradation; BaseAdapter always available |

**Resolved risk:** X Layer facilitator — OKX confirmed to have their own x402 facilitator at `web3.okx.com/api/v6/x402/` supporting X Layer (chain 196). No need to self-host.

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
