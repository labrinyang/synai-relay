# x402 Payment Integration & Multi-Chain Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate x402 payment protocol for one-step task creation + escrow and pay-to-view submission marketplace, with multi-chain support (Base L2 + X Layer).

**Architecture:** Route-level x402 handling using `x402ResourceServerSync` directly in Flask handlers (not middleware). Two facilitators: Coinbase (`HTTPFacilitatorClientSync`) for Base, custom `OKXFacilitatorClient` for X Layer. `ChainAdapter` abstraction wraps existing `WalletService` for Base and `OnchainOSClient` for X Layer. `ChainRegistry` routes operations by `job.chain_id`.

**Tech Stack:** x402 SDK v2.3.0 (`x402[flask,evm]`), Flask 3.0, SQLAlchemy 2.0, web3.py 6.0+

**Spec:** `docs/superpowers/specs/2026-03-15-x402-multichain-design.md`

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| CREATE | `services/chain_adapter.py` | ChainAdapter ABC + DepositResult, PayoutResult, RefundResult dataclasses |
| CREATE | `services/base_adapter.py` | BaseAdapter wrapping existing WalletService |
| CREATE | `services/chain_registry.py` | ChainRegistry — adapter lookup by chain_id |
| CREATE | `services/onchainos_client.py` | OnchainOS REST client (HMAC auth, Wallet API) |
| CREATE | `services/xlayer_adapter.py` | XLayerAdapter wrapping OnchainOSClient |
| CREATE | `services/okx_facilitator.py` | OKX x402 facilitator adapter (translates OKX API → SDK types) |
| CREATE | `services/x402_service.py` | x402 helpers: build_requirements, get_server, parse_chain_id, record_access |
| CREATE | `tests/test_chain_adapter.py` | Unit tests for ChainAdapter, BaseAdapter, ChainRegistry |
| CREATE | `tests/test_x402_service.py` | Unit tests for x402 helpers, access control, POST /jobs x402, GET /submissions paywall |
| MODIFY | `models.py` | Add SubmissionAccess model + Job.chain_id column |
| MODIFY | `config.py` | Add X402, OnchainOS, X Layer, and marketplace config |
| MODIFY | `server.py` | x402 integration in POST /jobs, GET /submissions, payout/refund chain routing |
| MODIFY | `requirements.txt` | Add `x402[flask,evm]` dependency |

---

## Chunk 1: Foundation — ChainAdapter Abstraction

### Task 1: ChainAdapter ABC + Result Dataclasses

**Files:**
- Create: `services/chain_adapter.py`
- Test: `tests/test_chain_adapter.py`

- [ ] **Step 1: Write the test for result dataclasses**

```python
# tests/test_chain_adapter.py
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from decimal import Decimal
from services.chain_adapter import DepositResult, PayoutResult, RefundResult


class TestDepositResult:
    def test_defaults(self):
        r = DepositResult(valid=False)
        assert r.valid is False
        assert r.depositor == ""
        assert r.amount == Decimal(0)
        assert r.error == ""
        assert r.overpayment == Decimal(0)

    def test_valid_deposit(self):
        r = DepositResult(valid=True, depositor="0xABC", amount=Decimal("50.0"))
        assert r.valid is True
        assert r.depositor == "0xABC"
        assert r.amount == Decimal("50.0")


class TestPayoutResult:
    def test_defaults(self):
        r = PayoutResult()
        assert r.payout_tx == ""
        assert r.fee_tx == ""
        assert r.pending is False

    def test_success(self):
        r = PayoutResult(payout_tx="0x123", fee_tx="0x456")
        assert r.payout_tx == "0x123"
        assert r.fee_tx == "0x456"


class TestRefundResult:
    def test_defaults(self):
        r = RefundResult()
        assert r.tx_hash == ""
        assert r.error == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_chain_adapter.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.chain_adapter'`

- [ ] **Step 3: Write ChainAdapter ABC and result dataclasses**

```python
# services/chain_adapter.py
"""
ChainAdapter abstraction for multi-chain support.
Each chain (Base, X Layer, etc.) implements this interface.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class DepositResult:
    valid: bool
    depositor: str = ""
    amount: Decimal = field(default_factory=lambda: Decimal(0))
    error: str = ""
    overpayment: Decimal = field(default_factory=lambda: Decimal(0))


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
    def chain_id(self) -> int:
        ...

    @abstractmethod
    def chain_name(self) -> str:
        ...

    @abstractmethod
    def caip2(self) -> str:
        """CAIP-2 identifier, e.g. 'eip155:196'."""
        ...

    @abstractmethod
    def is_connected(self) -> bool:
        ...

    @abstractmethod
    def usdc_address(self) -> str:
        ...

    @abstractmethod
    def ops_address(self) -> str:
        ...

    @abstractmethod
    def verify_deposit(self, tx_hash: str, expected_amount: Decimal) -> DepositResult:
        ...

    @abstractmethod
    def payout(self, to_address: str, amount: Decimal, fee_bps: int) -> PayoutResult:
        ...

    @abstractmethod
    def refund(self, to_address: str, amount: Decimal) -> RefundResult:
        ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_chain_adapter.py -v`
Expected: PASS (all 5 tests)

- [ ] **Step 5: Commit**

```bash
git add services/chain_adapter.py tests/test_chain_adapter.py
git commit -m "feat: add ChainAdapter ABC and result dataclasses"
```

---

### Task 2: BaseAdapter Wrapping WalletService

**Files:**
- Create: `services/base_adapter.py`
- Test: `tests/test_chain_adapter.py` (append)

- [ ] **Step 1: Write tests for BaseAdapter**

Append to `tests/test_chain_adapter.py`:

```python
from unittest.mock import MagicMock
from services.base_adapter import BaseAdapter


class TestBaseAdapter:
    def _make_adapter(self, connected=True, ops_address="0xOPS"):
        ws = MagicMock()
        ws.is_connected.return_value = connected
        ws.usdc_address = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        ws.ops_address = ops_address
        return BaseAdapter(ws), ws

    def test_chain_metadata(self):
        adapter, _ = self._make_adapter()
        assert adapter.chain_id() == 8453
        assert adapter.chain_name() == "Base"
        assert adapter.caip2() == "eip155:8453"
        assert adapter.usdc_address() == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        assert adapter.ops_address() == "0xOPS"

    def test_is_connected(self):
        adapter, _ = self._make_adapter(connected=True)
        assert adapter.is_connected() is True
        adapter2, _ = self._make_adapter(connected=False)
        assert adapter2.is_connected() is False

    def test_verify_deposit(self):
        adapter, ws = self._make_adapter()
        ws.verify_deposit.return_value = {
            "valid": True, "depositor": "0xBUYER", "amount": Decimal("50.0"),
        }
        result = adapter.verify_deposit("0xtx", Decimal("50.0"))
        assert result.valid is True
        assert result.depositor == "0xBUYER"
        ws.verify_deposit.assert_called_once_with("0xtx", Decimal("50.0"))

    def test_verify_deposit_with_overpayment(self):
        adapter, ws = self._make_adapter()
        ws.verify_deposit.return_value = {
            "valid": True, "depositor": "0xBUYER",
            "amount": Decimal("55.0"), "overpayment": 5.0,
        }
        result = adapter.verify_deposit("0xtx", Decimal("50.0"))
        assert result.valid is True
        assert result.overpayment == Decimal("5.0")

    def test_payout(self):
        adapter, ws = self._make_adapter()
        ws.payout.return_value = {"payout_tx": "0xPAY", "fee_tx": "0xFEE"}
        result = adapter.payout("0xWORKER", Decimal("50.0"), 2000)
        assert result.payout_tx == "0xPAY"
        assert result.fee_tx == "0xFEE"
        assert result.pending is False

    def test_payout_pending(self):
        adapter, ws = self._make_adapter()
        ws.payout.return_value = {
            "payout_tx": "0xPAY", "fee_tx": None,
            "pending": True, "error": "timeout",
        }
        result = adapter.payout("0xWORKER", Decimal("50.0"), 2000)
        assert result.pending is True
        assert result.payout_tx == "0xPAY"

    def test_refund(self):
        """WalletService.refund returns a str, not a dict."""
        adapter, ws = self._make_adapter()
        ws.refund.return_value = "0xREFUND"
        result = adapter.refund("0xBUYER", Decimal("50.0"))
        assert result.tx_hash == "0xREFUND"
        assert result.error == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_chain_adapter.py::TestBaseAdapter -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.base_adapter'`

- [ ] **Step 3: Implement BaseAdapter**

```python
# services/base_adapter.py
"""Base L2 adapter — wraps existing WalletService (zero rewrite)."""
from decimal import Decimal
from services.chain_adapter import ChainAdapter, DepositResult, PayoutResult, RefundResult


class BaseAdapter(ChainAdapter):

    def __init__(self, wallet_service):
        self._ws = wallet_service

    def chain_id(self) -> int:
        return 8453

    def chain_name(self) -> str:
        return "Base"

    def caip2(self) -> str:
        return "eip155:8453"

    def is_connected(self) -> bool:
        return self._ws.is_connected()

    def usdc_address(self) -> str:
        return self._ws.usdc_address

    def ops_address(self) -> str:
        return self._ws.ops_address or ''

    def verify_deposit(self, tx_hash: str, expected_amount: Decimal) -> DepositResult:
        result = self._ws.verify_deposit(tx_hash, expected_amount)
        return DepositResult(
            valid=result.get('valid', False),
            depositor=result.get('depositor', ''),
            amount=Decimal(str(result.get('amount', 0))),
            error=result.get('error', ''),
            overpayment=Decimal(str(result.get('overpayment', 0))),
        )

    def payout(self, to_address: str, amount: Decimal, fee_bps: int) -> PayoutResult:
        result = self._ws.payout(to_address, amount, fee_bps=fee_bps)
        return PayoutResult(
            payout_tx=result.get('payout_tx') or '',
            fee_tx=result.get('fee_tx') or '',
            fee_error=result.get('fee_error') or '',
            pending=result.get('pending', False),
            error=result.get('error') or '',
        )

    def refund(self, to_address: str, amount: Decimal) -> RefundResult:
        tx_hash = self._ws.refund(to_address, amount)
        return RefundResult(tx_hash=tx_hash or '')
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_chain_adapter.py -v`
Expected: PASS (all 13 tests)

- [ ] **Step 5: Commit**

```bash
git add services/base_adapter.py tests/test_chain_adapter.py
git commit -m "feat: add BaseAdapter wrapping WalletService"
```

---

### Task 3: ChainRegistry

**Files:**
- Create: `services/chain_registry.py`
- Test: `tests/test_chain_adapter.py` (append)

- [ ] **Step 1: Write tests for ChainRegistry**

Append to `tests/test_chain_adapter.py`:

```python
import pytest
from services.chain_registry import ChainRegistry


class TestChainRegistry:
    def _make_adapter(self, cid=8453, name="Base"):
        a = MagicMock()
        a.chain_id.return_value = cid
        a.chain_name.return_value = name
        a.caip2.return_value = f"eip155:{cid}"
        a.usdc_address.return_value = "0xUSDC"
        return a

    def test_register_and_get(self):
        reg = ChainRegistry()
        adapter = self._make_adapter(8453, "Base")
        reg.register(adapter)
        assert reg.get(8453) is adapter

    def test_get_unknown_raises(self):
        reg = ChainRegistry()
        with pytest.raises(ValueError, match="Unsupported chain"):
            reg.get(999)

    def test_default(self):
        reg = ChainRegistry(default_chain_id=8453)
        adapter = self._make_adapter(8453)
        reg.register(adapter)
        assert reg.default() is adapter

    def test_default_not_registered_raises(self):
        reg = ChainRegistry(default_chain_id=8453)
        with pytest.raises(RuntimeError, match="Default chain"):
            reg.default()

    def test_adapters_list(self):
        reg = ChainRegistry()
        a1 = self._make_adapter(8453, "Base")
        a2 = self._make_adapter(196, "X Layer")
        reg.register(a1)
        reg.register(a2)
        adapters = reg.adapters()
        assert len(adapters) == 2

    def test_supported_chains(self):
        reg = ChainRegistry()
        a = self._make_adapter(8453, "Base")
        reg.register(a)
        chains = reg.supported_chains()
        assert len(chains) == 1
        assert chains[0]["chain_id"] == 8453
        assert chains[0]["name"] == "Base"

    def test_get_or_default_with_none(self):
        """NULL chain_id should return default adapter (Base)."""
        reg = ChainRegistry(default_chain_id=8453)
        adapter = self._make_adapter(8453)
        reg.register(adapter)
        assert reg.get_or_default(None) is adapter

    def test_get_or_default_with_valid_id(self):
        reg = ChainRegistry(default_chain_id=8453)
        a1 = self._make_adapter(8453)
        a2 = self._make_adapter(196, "X Layer")
        reg.register(a1)
        reg.register(a2)
        assert reg.get_or_default(196) is a2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_chain_adapter.py::TestChainRegistry -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.chain_registry'`

- [ ] **Step 3: Implement ChainRegistry**

```python
# services/chain_registry.py
"""Registry of chain adapters, keyed by chain_id."""
from services.chain_adapter import ChainAdapter


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

    def get_or_default(self, chain_id: int | None) -> ChainAdapter:
        """Get adapter by chain_id, falling back to default if None."""
        if chain_id is None:
            return self.default()
        return self.get(chain_id)

    def default(self) -> ChainAdapter:
        if self._default_chain_id not in self._adapters:
            raise RuntimeError(
                f"Default chain {self._default_chain_id} not registered. "
                f"Available: {list(self._adapters.keys())}")
        return self._adapters[self._default_chain_id]

    def adapters(self) -> list[ChainAdapter]:
        return list(self._adapters.values())

    def supported_chains(self) -> list[dict]:
        return [{"chain_id": a.chain_id(), "name": a.chain_name(),
                 "caip2": a.caip2(), "usdc": a.usdc_address()}
                for a in self._adapters.values()]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_chain_adapter.py -v`
Expected: PASS (all 21 tests)

- [ ] **Step 5: Commit**

```bash
git add services/chain_registry.py tests/test_chain_adapter.py
git commit -m "feat: add ChainRegistry for multi-chain adapter lookup"
```

---

## Chunk 2: Data Model + Configuration

### Task 4: Add Config Variables

**Files:**
- Modify: `config.py`

- [ ] **Step 1: Add new config variables**

Add after `OPERATOR_SIGNATURE_MAX_AGE` (line 35) in `config.py`:

```python
    # Multi-chain
    DEFAULT_CHAIN_ID = int(os.environ.get('DEFAULT_CHAIN_ID', '8453'))

    # X Layer
    XLAYER_RPC_URL = os.environ.get('XLAYER_RPC_URL', 'https://rpc.xlayer.tech')
    XLAYER_USDC_CONTRACT = os.environ.get('XLAYER_USDC_CONTRACT', '')

    # OnchainOS (OKX)
    ONCHAINOS_API_KEY = os.environ.get('ONCHAINOS_API_KEY', '')
    ONCHAINOS_SECRET_KEY = os.environ.get('ONCHAINOS_SECRET_KEY', '')
    ONCHAINOS_PASSPHRASE = os.environ.get('ONCHAINOS_PASSPHRASE', '')
    ONCHAINOS_PROJECT_ID = os.environ.get('ONCHAINOS_PROJECT_ID', '')

    # x402
    X402_ENABLED = os.environ.get('X402_ENABLED', 'true').lower() == 'true'
    X402_COINBASE_FACILITATOR_URL = os.environ.get(
        'X402_COINBASE_FACILITATOR_URL', 'https://x402.org/facilitator')
    X402_OKX_FACILITATOR_URL = os.environ.get(
        'X402_OKX_FACILITATOR_URL', 'https://web3.okx.com/api/v6/x402')

    # Submission marketplace
    SOLUTION_VIEW_FEE_PERCENT = int(os.environ.get('SOLUTION_VIEW_FEE_PERCENT', '70'))
```

- [ ] **Step 2: Run existing tests to verify no regressions**

Run: `pytest tests/test_server_api.py::TestHealth -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat: add x402, OnchainOS, X Layer, and marketplace config"
```

---

### Task 5: SubmissionAccess Model + Job.chain_id

**Files:**
- Modify: `models.py`
- Test: `tests/test_x402_service.py` (create)

- [ ] **Step 1: Write test for SubmissionAccess model**

```python
# tests/test_x402_service.py
"""Tests for x402 integration: models, access control, and route-level handling."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from decimal import Decimal
from server import app
from models import db, Agent, Job, Submission, SubmissionAccess


@pytest.fixture
def client():
    app.config['TESTING'] = True
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()


class TestSubmissionAccessModel:
    def test_create_access_record(self, client):
        with app.app_context():
            agent = Agent(agent_id='viewer-1', name='Viewer')
            worker = Agent(agent_id='worker-1', name='Worker')
            db.session.add_all([agent, worker])
            db.session.flush()

            job = Job(title='Test', description='Desc', price=Decimal('50'),
                      buyer_id='viewer-1', status='funded')
            db.session.add(job)
            db.session.flush()

            sub = Submission(task_id=job.task_id, worker_id='worker-1',
                             content={"answer": "test"}, status='pending')
            db.session.add(sub)
            db.session.flush()

            access = SubmissionAccess(
                submission_id=sub.id,
                viewer_agent_id='viewer-1',
                tx_hash='0x123abc',
                amount=Decimal('35.0'),
                chain_id=8453,
            )
            db.session.add(access)
            db.session.commit()

            found = SubmissionAccess.query.filter_by(
                submission_id=sub.id, viewer_agent_id='viewer-1').first()
            assert found is not None
            assert found.tx_hash == '0x123abc'
            assert found.chain_id == 8453

    def test_unique_constraint_prevents_double_access(self, client):
        """Same viewer + submission cannot have two access records."""
        from sqlalchemy.exc import IntegrityError
        with app.app_context():
            agent = Agent(agent_id='viewer-2', name='Viewer')
            worker = Agent(agent_id='worker-2', name='Worker')
            db.session.add_all([agent, worker])
            db.session.flush()

            job = Job(title='Test', description='Desc', price=Decimal('50'),
                      buyer_id='viewer-2', status='funded')
            db.session.add(job)
            db.session.flush()

            sub = Submission(task_id=job.task_id, worker_id='worker-2',
                             content={"answer": "x"}, status='pending')
            db.session.add(sub)
            db.session.flush()

            access1 = SubmissionAccess(
                submission_id=sub.id, viewer_agent_id='viewer-2',
                tx_hash='0xfirst', amount=Decimal('35'), chain_id=8453)
            db.session.add(access1)
            db.session.commit()

            access2 = SubmissionAccess(
                submission_id=sub.id, viewer_agent_id='viewer-2',
                tx_hash='0xsecond', amount=Decimal('35'), chain_id=8453)
            db.session.add(access2)
            with pytest.raises(IntegrityError):
                db.session.commit()
            db.session.rollback()


class TestJobChainId:
    def test_job_chain_id_default_null(self, client):
        with app.app_context():
            agent = Agent(agent_id='buyer-1', name='Buyer')
            db.session.add(agent)
            db.session.flush()

            job = Job(title='Test', description='Desc', price=Decimal('10'),
                      buyer_id='buyer-1')
            db.session.add(job)
            db.session.commit()

            assert job.chain_id is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_x402_service.py -v`
Expected: FAIL with `ImportError: cannot import name 'SubmissionAccess' from 'models'`

- [ ] **Step 3: Add SubmissionAccess model and Job.chain_id to models.py**

Add after line 100 in `models.py` (after `updated_at` in Job):

```python
    chain_id = db.Column(db.Integer, nullable=True)  # Set at funding; NULL = legacy (Base)
```

Add after the Dispute class (end of `models.py`):

```python
class SubmissionAccess(db.Model):
    """Tracks x402 payments for viewing submissions (prevents double-charging)."""
    __tablename__ = 'submission_accesses'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    submission_id = db.Column(db.String(36), db.ForeignKey('submissions.id'), nullable=False)
    viewer_agent_id = db.Column(db.String(100), db.ForeignKey('agents.agent_id'), nullable=False)
    tx_hash = db.Column(db.String(100), nullable=False)
    amount = db.Column(db.Numeric(20, 6), nullable=False)
    chain_id = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow)

    __table_args__ = (
        db.UniqueConstraint('submission_id', 'viewer_agent_id',
                            name='uq_submission_access'),
        db.Index('ix_submission_access_viewer', 'viewer_agent_id'),
    )
```

Also add `SubmissionAccess` to the existing models import in `server.py` (line 10) — append it to the import list:

```python
# Add SubmissionAccess to the end of the existing import:
from models import db, Owner, Agent, Job, Submission, Webhook, IdempotencyKey, Dispute, JobParticipant, utc_iso, SubmissionAccess
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_x402_service.py -v`
Expected: PASS (all 3 tests)

- [ ] **Step 5: Run full test suite to check for regressions**

Run: `pytest tests/test_server_api.py -x -q`
Expected: PASS (all existing tests still pass)

- [ ] **Step 6: Commit**

```bash
git add models.py server.py tests/test_x402_service.py
git commit -m "feat: add SubmissionAccess model and Job.chain_id column"
```

---

### Task 6: Add x402 to requirements.txt

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add x402 dependency**

Add to `requirements.txt`:

```
x402[flask,evm]>=2.3.0
```

- [ ] **Step 2: Install dependency**

Run: `pip install -r requirements.txt`
Expected: x402 installed successfully

- [ ] **Step 3: Verify import**

Run: `python3 -c "from x402 import x402ResourceServerSync; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "feat: add x402 SDK dependency"
```

---

## Chunk 3: x402 Service Layer

### Task 7: OnchainOS Client

**Files:**
- Create: `services/onchainos_client.py`
- Test: `tests/test_chain_adapter.py` (append)

- [ ] **Step 1: Write test for OnchainOS HMAC signing**

Append to `tests/test_chain_adapter.py`:

```python
from services.onchainos_client import OnchainOSClient


class TestOnchainOSClient:
    def test_sign(self):
        """HMAC signature must be deterministic for same input."""
        client = OnchainOSClient(
            api_key="test-key",
            secret_key="test-secret",
            passphrase="test-pass",
        )
        sig1 = client._sign("2026-03-15T00:00:00.000Z", "POST", "/api/v6/x402/verify", '{"foo":"bar"}')
        sig2 = client._sign("2026-03-15T00:00:00.000Z", "POST", "/api/v6/x402/verify", '{"foo":"bar"}')
        assert sig1 == sig2
        assert len(sig1) > 0

    def test_headers(self):
        client = OnchainOSClient(
            api_key="test-key",
            secret_key="test-secret",
            passphrase="test-pass",
            project_id="proj-1",
        )
        headers = client._headers("POST", "/api/test", "body")
        assert headers["OK-ACCESS-KEY"] == "test-key"
        assert headers["OK-ACCESS-PASSPHRASE"] == "test-pass"
        assert headers["OK-ACCESS-PROJECT"] == "proj-1"
        assert "OK-ACCESS-SIGN" in headers
        assert "OK-ACCESS-TIMESTAMP" in headers
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_chain_adapter.py::TestOnchainOSClient -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement OnchainOS client**

```python
# services/onchainos_client.py
"""Thin HTTP client for OKX OnchainOS REST API with HMAC authentication."""
import base64
import hashlib
import hmac
import json
import logging
import time

import requests

logger = logging.getLogger('relay.onchainos')


class OnchainOSClient:
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
        sig = hmac.new(
            self.secret_key.encode(), prehash.encode(), hashlib.sha256
        ).digest()
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

    def post(self, path: str, data: dict) -> dict:
        body = json.dumps(data)
        headers = self._headers("POST", path, body)
        url = self.BASE_URL + path
        resp = requests.post(url, headers=headers, data=body, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") != "0":
            raise RuntimeError(
                f"OnchainOS error: code={result.get('code')} msg={result.get('msg')}")
        return result

    def get(self, path: str, params: dict = None) -> dict:
        query = ""
        if params:
            query = "?" + "&".join(f"{k}={v}" for k, v in params.items())
        full_path = path + query
        headers = self._headers("GET", full_path)
        url = self.BASE_URL + full_path
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") != "0":
            raise RuntimeError(
                f"OnchainOS error: code={result.get('code')} msg={result.get('msg')}")
        return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_chain_adapter.py::TestOnchainOSClient -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/onchainos_client.py tests/test_chain_adapter.py
git commit -m "feat: add OnchainOS REST client with HMAC auth"
```

---

### Task 8: OKX Facilitator Adapter

**Files:**
- Create: `services/okx_facilitator.py`
- Test: `tests/test_chain_adapter.py` (append)

- [ ] **Step 1: Write tests for OKXFacilitatorClient**

Append to `tests/test_chain_adapter.py`:

```python
from services.okx_facilitator import OKXFacilitatorClient, _network_to_chain_index


class TestOKXFacilitator:
    def test_network_to_chain_index(self):
        assert _network_to_chain_index("eip155:196") == "196"
        assert _network_to_chain_index("eip155:8453") == "8453"

    def test_verify_translates_response(self):
        """Mock OnchainOS client and verify response translation."""
        mock_client = MagicMock()
        mock_client.post.return_value = {
            "code": "0",
            "data": [{"isValid": True, "payer": "0xPAYER"}],
        }
        fac = OKXFacilitatorClient.__new__(OKXFacilitatorClient)
        fac._client = mock_client

        # Create mock payload and requirements
        payload = MagicMock()
        payload.model_dump.return_value = {"test": "payload"}
        requirements = MagicMock()
        requirements.scheme = "exact"
        requirements.network = "eip155:196"
        requirements.amount = "50000000"
        requirements.pay_to = "0xOPS"
        requirements.asset = "0xUSDC"

        result = fac.verify(payload, requirements)
        assert result.is_valid is True
        assert result.payer == "0xPAYER"

    def test_settle_translates_response(self):
        mock_client = MagicMock()
        mock_client.post.return_value = {
            "code": "0",
            "data": [{
                "success": True,
                "txHash": "0xTX123",
                "chainIndex": "196",
                "payer": "0xPAYER",
            }],
        }
        fac = OKXFacilitatorClient.__new__(OKXFacilitatorClient)
        fac._client = mock_client

        payload = MagicMock()
        payload.model_dump.return_value = {}
        requirements = MagicMock()
        requirements.scheme = "exact"
        requirements.network = "eip155:196"
        requirements.amount = "50000000"
        requirements.pay_to = "0xOPS"
        requirements.asset = "0xUSDC"

        result = fac.settle(payload, requirements)
        assert result.success is True
        assert result.transaction == "0xTX123"
        assert result.network == "eip155:196"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_chain_adapter.py::TestOKXFacilitator -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement OKXFacilitatorClient**

```python
# services/okx_facilitator.py
"""OKX x402 facilitator adapter — translates OKX API format to x402 SDK types."""
import logging

# Verified import path: x402.facilitator re-exports from x402.schemas.responses
from x402.facilitator import VerifyResponse, SettleResponse

from services.onchainos_client import OnchainOSClient

logger = logging.getLogger('relay.okx_facilitator')


def _network_to_chain_index(network: str) -> str:
    """Extract chain index from CAIP-2 network. 'eip155:196' -> '196'."""
    return network.split(":")[-1]


class OKXFacilitatorClient:
    """Adapts OKX's x402 API to the x402 SDK's FacilitatorClientSync protocol."""

    def __init__(self, api_key: str, secret_key: str, passphrase: str,
                 project_id: str = ""):
        self._client = OnchainOSClient(api_key, secret_key, passphrase, project_id)

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
            is_valid=data.get("isValid", False),
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
        # PLAN FIX: OKX settle response uses "errorMsg" not "errorReason"
        # (verified via Context7 OKX docs: POST /api/v6/x402/settle)
        return SettleResponse(
            success=data.get("success", False),
            transaction=data.get("txHash", ""),
            network=f"eip155:{data.get('chainIndex', _network_to_chain_index(requirements.network))}",
            payer=data.get("payer"),
            error_reason=data.get("errorMsg"),
            error_message=data.get("errorMsg"),
        )

    def get_supported(self):
        return self._client.get("/api/v6/x402/supported")

    def close(self):
        pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_chain_adapter.py::TestOKXFacilitator -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/okx_facilitator.py tests/test_chain_adapter.py
git commit -m "feat: add OKX x402 facilitator adapter"
```

---

### Task 9: x402 Service Helpers

**Files:**
- Create: `services/x402_service.py`
- Test: `tests/test_x402_service.py` (append)

- [ ] **Step 1: Write tests for x402 helpers**

Append to `tests/test_x402_service.py`:

```python
from services.x402_service import parse_chain_id, build_requirements


class TestParseChainId:
    def test_base(self):
        assert parse_chain_id("eip155:8453") == 8453

    def test_xlayer(self):
        assert parse_chain_id("eip155:196") == 196

    def test_invalid(self):
        with pytest.raises(ValueError, match="Invalid CAIP-2"):
            parse_chain_id("not-a-network")

    def test_empty(self):
        with pytest.raises(ValueError):
            parse_chain_id("")


class TestBuildRequirements:
    def test_single_chain(self):
        from unittest.mock import MagicMock
        adapter = MagicMock()
        adapter.caip2.return_value = "eip155:8453"
        adapter.usdc_address.return_value = "0xUSDC"

        reqs = build_requirements(
            Decimal("50"), "0xPAYTO", [adapter])
        assert len(reqs) == 1
        assert reqs[0].scheme == "exact"
        assert reqs[0].network == "eip155:8453"
        assert reqs[0].amount == "50000000"
        assert reqs[0].pay_to == "0xPAYTO"
        assert reqs[0].asset == "0xUSDC"

    def test_multi_chain(self):
        from unittest.mock import MagicMock
        a1 = MagicMock()
        a1.caip2.return_value = "eip155:8453"
        a1.usdc_address.return_value = "0xBASE_USDC"
        a2 = MagicMock()
        a2.caip2.return_value = "eip155:196"
        a2.usdc_address.return_value = "0xXLAYER_USDC"

        reqs = build_requirements(Decimal("50"), "0xPAYTO", [a1, a2])
        assert len(reqs) == 2
        assert reqs[0].network == "eip155:8453"
        assert reqs[1].network == "eip155:196"

    def test_uses_decimal_precision(self):
        """Ensure amount conversion uses Decimal, not float."""
        from unittest.mock import MagicMock
        adapter = MagicMock()
        adapter.caip2.return_value = "eip155:8453"
        adapter.usdc_address.return_value = "0xUSDC"

        reqs = build_requirements(Decimal("0.1"), "0xPAYTO", [adapter])
        assert reqs[0].amount == "100000"  # 0.1 * 10^6
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_x402_service.py::TestParseChainId -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement x402 service helpers**

```python
# services/x402_service.py
"""x402 route-level helpers: build requirements, parse chain IDs, record access."""
import logging
from decimal import Decimal

from x402 import PaymentRequirements

logger = logging.getLogger('relay.x402')

# USDC has 6 decimals on all supported chains (Base, X Layer)
USDC_DECIMALS = 6


def parse_chain_id(network: str) -> int:
    """Extract chain ID from CAIP-2 network string. 'eip155:196' -> 196."""
    try:
        parts = network.split(":")
        if len(parts) != 2:
            raise ValueError()
        return int(parts[-1])
    except (ValueError, IndexError):
        raise ValueError(f"Invalid CAIP-2 network: {network!r}")


def build_requirements(amount_usdc: Decimal, pay_to: str,
                       adapters: list) -> list[PaymentRequirements]:
    """Build PaymentRequirements for all supported chains."""
    amount_atomic = str(int(amount_usdc * Decimal(10 ** USDC_DECIMALS)))
    requirements = []
    for adapter in adapters:
        requirements.append(PaymentRequirements(
            scheme="exact",
            network=adapter.caip2(),
            asset=adapter.usdc_address(),
            amount=amount_atomic,
            pay_to=pay_to,
            max_timeout_seconds=60,
        ))
    return requirements
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_x402_service.py::TestParseChainId tests/test_x402_service.py::TestBuildRequirements -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/x402_service.py tests/test_x402_service.py
git commit -m "feat: add x402 service helpers (parse_chain_id, build_requirements)"
```

---

## Chunk 4: POST /jobs x402 Integration

### Task 10: Refactor _create_job to Accept Parameters

**Files:**
- Modify: `server.py:1017-1105`
- Test: `tests/test_x402_service.py` (append)

This refactoring is needed because x402 flow must:
1. Verify + settle payment BEFORE creating the job
2. Pass `status='funded'` and deposit fields to `_create_job`

- [ ] **Step 1: Write test for funded job creation via refactored _create_job**

Append to `tests/test_x402_service.py`:

```python
from services.auth_service import generate_api_key


class TestCreateJobRefactor:
    """Verify _create_job supports status and extra fields."""

    def _make_agent(self, agent_id='buyer-test'):
        agent = Agent(agent_id=agent_id, name='Test Buyer')
        api_key = generate_api_key(agent)
        db.session.add(agent)
        db.session.commit()
        return agent, api_key

    def test_legacy_creates_open_job(self, client):
        """POST /jobs without x402 still creates status='open'."""
        with app.app_context():
            _, api_key = self._make_agent()
        resp = client.post('/jobs', json={
            'title': 'Test Task',
            'description': 'A test description',
            'price': 10.0,
        }, headers={'Authorization': f'Bearer {api_key}'})
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['status'] == 'open'

    def test_job_has_chain_id_field(self, client):
        """Job model should have chain_id accessible."""
        with app.app_context():
            _, api_key = self._make_agent('buyer-chain')
        resp = client.post('/jobs', json={
            'title': 'Chain Test',
            'description': 'Testing chain_id',
            'price': 5.0,
        }, headers={'Authorization': f'Bearer {api_key}'})
        assert resp.status_code == 201
        task_id = resp.get_json()['task_id']
        with app.app_context():
            job = Job.query.get(task_id)
            assert job.chain_id is None  # Legacy: no chain_id
```

- [ ] **Step 2: Run tests to verify they pass (baseline)**

Run: `pytest tests/test_x402_service.py::TestCreateJobRefactor -v`
Expected: PASS (these test existing behavior)

- [ ] **Step 3: Refactor _create_job to accept keyword arguments**

In `server.py`, replace `_create_job()` (lines 1024-1105) with:

```python
def _create_job(override_status=None, deposit_tx_hash=None,
                depositor_address=None, chain_id=None,
                deposit_amount=None, solution_price=None):
    data = request.get_json(silent=True) or {}

    # buyer_id is the authenticated agent
    buyer_id = g.current_agent_id

    # Required fields
    title = data.get('title')
    description = data.get('description')

    if not title:
        return jsonify({"error": "title is required"}), 400
    if len(title) > 500:
        return jsonify({"error": "title must be <= 500 characters"}), 400
    if not description:
        return jsonify({"error": "description is required"}), 400
    if len(description) > 50000:
        return jsonify({"error": "description must be <= 50000 characters"}), 400

    # Price validation
    raw_price = data.get('price')
    if raw_price is None:
        return jsonify({"error": "price is required"}), 400
    try:
        price = Decimal(str(raw_price))
        if not price.is_finite() or price < Decimal(str(Config.MIN_TASK_AMOUNT)):
            return jsonify({
                "error": f"price must be >= {Config.MIN_TASK_AMOUNT}"
            }), 400
    except (InvalidOperation, ValueError, TypeError):
        return jsonify({"error": "Invalid price value"}), 400

    # Optional fields
    rubric = data.get('rubric')
    # P2-5 fix (m-S07): Rubric length limit
    if rubric and len(rubric) > 10000:
        return jsonify({"error": "rubric must be <= 10000 characters"}), 400
    artifact_type = data.get('artifact_type', 'GENERAL')

    expiry = None
    raw_expiry = data.get('expiry')
    if raw_expiry is not None:
        try:
            expiry = datetime.datetime.fromtimestamp(int(raw_expiry), tz=datetime.timezone.utc)
        except (ValueError, TypeError, OSError):
            return jsonify({"error": "Invalid expiry timestamp"}), 400

    max_submissions = data.get('max_submissions', 20)
    if not isinstance(max_submissions, int) or max_submissions < 1:
        max_submissions = 20

    max_retries = data.get('max_retries', 3)
    if not isinstance(max_retries, int) or max_retries < 1:
        max_retries = 3

    status = override_status or 'open'

    job = Job(
        title=title,
        description=description,
        rubric=rubric,
        price=price,
        buyer_id=buyer_id,
        status=status,
        artifact_type=artifact_type,
        expiry=expiry,
        max_submissions=max_submissions,
        max_retries=max_retries,
        fee_bps=Config.PLATFORM_FEE_BPS,
        chain_id=chain_id,
        deposit_tx_hash=deposit_tx_hash,
        depositor_address=depositor_address,
        deposit_amount=deposit_amount or price,
    )
    if solution_price is not None:
        job.solution_price = solution_price

    db.session.add(job)
    db.session.flush()  # materialise task_id before commit expires attrs
    task_id = job.task_id
    price_f = float(job.price)
    db.session.commit()
    logger.info("Job created: task_id=%s buyer=%s price=%.2f status=%s",
                task_id, buyer_id, price_f, status)

    resp_data = {
        "status": status,
        "task_id": task_id,
        "price": price_f,
    }
    if deposit_tx_hash:
        resp_data["x402_settlement"] = {
            "tx_hash": deposit_tx_hash,
            "chain_id": chain_id,
            "payer": depositor_address,
        }

    return jsonify(resp_data), 201
```

- [ ] **Step 4: Run tests to verify no regressions**

Run: `pytest tests/test_x402_service.py tests/test_server_api.py::TestJobCRUD -v`
Expected: PASS (all tests — refactor is backward-compatible)

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_x402_service.py
git commit -m "refactor: _create_job accepts override params for x402 funded creation"
```

---

### Task 11: x402 POST /jobs Integration

**Files:**
- Modify: `server.py` — `create_job_endpoint()` and new x402 initialization block
- Test: `tests/test_x402_service.py` (append)

- [ ] **Step 1: Write tests for x402 POST /jobs**

Append to `tests/test_x402_service.py`:

```python
from unittest.mock import patch, MagicMock


class TestX402CreateJob:
    def _make_agent(self, agent_id='x402-buyer', wallet='0xBUYER'):
        agent = Agent(agent_id=agent_id, name='x402 Buyer',
                      wallet_address=wallet)
        api_key = generate_api_key(agent)
        db.session.add(agent)
        db.session.commit()
        return agent, api_key

    def test_no_payment_header_returns_402(self, client):
        """POST /jobs with x402 enabled and no payment header returns 402."""
        with app.app_context():
            _, api_key = self._make_agent('buyer-402')
            app.config['X402_ENABLED'] = True

        resp = client.post('/jobs', json={
            'title': 'x402 Task',
            'description': 'Needs payment',
            'price': 50.0,
        }, headers={'Authorization': f'Bearer {api_key}'})
        assert resp.status_code == 402
        assert 'PAYMENT-REQUIRED' in resp.headers

    def test_402_response_contains_requirements(self, client):
        """402 response must include payment requirements for all chains."""
        with app.app_context():
            _, api_key = self._make_agent('buyer-reqs')
            app.config['X402_ENABLED'] = True

        resp = client.post('/jobs', json={
            'title': 'x402 Task',
            'description': 'Needs payment',
            'price': 25.0,
        }, headers={'Authorization': f'Bearer {api_key}'})
        assert resp.status_code == 402
        data = resp.get_json()
        assert data.get('error') == 'Payment required'

    def test_x402_disabled_creates_open_job(self, client):
        """When X402_ENABLED=false, POST /jobs behaves as legacy."""
        with app.app_context():
            _, api_key = self._make_agent('buyer-legacy')
            app.config['X402_ENABLED'] = False

        resp = client.post('/jobs', json={
            'title': 'Legacy Task',
            'description': 'No x402',
            'price': 10.0,
        }, headers={'Authorization': f'Bearer {api_key}'})
        assert resp.status_code == 201
        assert resp.get_json()['status'] == 'open'

    @patch('server._get_x402_server')
    def test_valid_x402_payment_creates_funded_job(self, mock_get_server, client):
        """Valid x402 payment header creates job with status='funded'."""
        with app.app_context():
            _, api_key = self._make_agent('buyer-funded')
            app.config['X402_ENABLED'] = True

        # Mock x402 server
        mock_server = MagicMock()
        mock_server.verify_payment.return_value = MagicMock(is_valid=True)
        mock_server.settle_payment.return_value = MagicMock(
            success=True,
            transaction="0xSETTLE_TX",
            network="eip155:8453",
            payer="0xBUYER",
        )
        mock_get_server.return_value = mock_server

        # Mock decode
        with patch('server.decode_payment_signature_header') as mock_decode:
            mock_payload = MagicMock()
            mock_payload.accepted.network = "eip155:8453"
            mock_payload.accepted.amount = "50000000"
            mock_decode.return_value = mock_payload

            resp = client.post('/jobs', json={
                'title': 'Funded Task',
                'description': 'Paid via x402',
                'price': 50.0,
            }, headers={
                'Authorization': f'Bearer {api_key}',
                'X-PAYMENT': 'base64-encoded-payment',
            })

        assert resp.status_code == 201
        data = resp.get_json()
        assert data['status'] == 'funded'
        assert data['x402_settlement']['tx_hash'] == '0xSETTLE_TX'
        assert data['x402_settlement']['chain_id'] == 8453

    @patch('server._get_x402_server')
    def test_failed_x402_verify_returns_402(self, mock_get_server, client):
        """Invalid x402 payment returns 402."""
        with app.app_context():
            _, api_key = self._make_agent('buyer-fail')
            app.config['X402_ENABLED'] = True

        mock_server = MagicMock()
        mock_server.verify_payment.return_value = MagicMock(
            is_valid=False, invalid_reason="Insufficient funds")
        mock_get_server.return_value = mock_server

        with patch('server.decode_payment_signature_header') as mock_decode:
            mock_payload = MagicMock()
            mock_payload.accepted.network = "eip155:8453"
            mock_decode.return_value = mock_payload

            resp = client.post('/jobs', json={
                'title': 'Failed Task',
                'description': 'Bad payment',
                'price': 50.0,
            }, headers={
                'Authorization': f'Bearer {api_key}',
                'X-PAYMENT': 'bad-payment',
            })

        assert resp.status_code == 402
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_x402_service.py::TestX402CreateJob -v`
Expected: FAIL (x402 handling not yet in server.py)

- [ ] **Step 3: Add x402 imports and initialization to server.py**

At the top of `server.py`, after the existing imports (around line 15), add:

```python
# x402 imports (conditional — graceful when SDK not installed)
try:
    from x402 import x402ResourceServerSync, PaymentRequired, PaymentRequirements
    from x402.mechanisms.evm.exact import ExactEvmServerScheme
    from x402.http.facilitator_client import HTTPFacilitatorClientSync
    from x402.http import (encode_payment_required_header,
                           decode_payment_signature_header,
                           encode_payment_response_header,
                           PAYMENT_SIGNATURE_HEADER, X_PAYMENT_HEADER)
    from x402.facilitator import SettleResponse, VerifyResponse
    _X402_SDK_AVAILABLE = True
except ImportError:
    _X402_SDK_AVAILABLE = False

from services.x402_service import parse_chain_id, build_requirements
```

After app initialization (after `db.init_app(app)` and before route definitions), add the x402 server initialization:

```python
# ---------------------------------------------------------------------------
# x402 facilitator setup
# ---------------------------------------------------------------------------

_x402_servers: dict[int, object] = {}  # chain_id -> x402ResourceServerSync
_chain_registry = None  # Set in _init_x402()


def _init_x402():
    """Initialize x402 facilitators and chain registry at startup."""
    global _x402_servers, _chain_registry
    from services.chain_registry import ChainRegistry
    from services.base_adapter import BaseAdapter

    _chain_registry = ChainRegistry(default_chain_id=Config.DEFAULT_CHAIN_ID)

    # Base L2 adapter (always available)
    try:
        from services.wallet_service import get_wallet_service
        ws = get_wallet_service()
        base_adapter = BaseAdapter(ws)
        _chain_registry.register(base_adapter)
    except Exception as e:
        logger.warning("Failed to init Base adapter: %s", e)

    if not (_X402_SDK_AVAILABLE and Config.X402_ENABLED):
        logger.info("x402 disabled or SDK not available")
        return

    # Coinbase facilitator (Base)
    try:
        coinbase_fac = HTTPFacilitatorClientSync(
            {"url": Config.X402_COINBASE_FACILITATOR_URL})
        coinbase_server = x402ResourceServerSync(coinbase_fac)
        coinbase_server.register("eip155:8453", ExactEvmServerScheme())
        _x402_servers[8453] = coinbase_server
        logger.info("x402: Coinbase facilitator registered for Base (8453)")
    except Exception as e:
        logger.warning("x402: Failed to init Coinbase facilitator: %s", e)

    # OKX facilitator (X Layer) — only if OnchainOS credentials configured
    if Config.ONCHAINOS_API_KEY:
        try:
            from services.okx_facilitator import OKXFacilitatorClient
            okx_fac = OKXFacilitatorClient(
                api_key=Config.ONCHAINOS_API_KEY,
                secret_key=Config.ONCHAINOS_SECRET_KEY,
                passphrase=Config.ONCHAINOS_PASSPHRASE,
                project_id=Config.ONCHAINOS_PROJECT_ID,
            )
            okx_server = x402ResourceServerSync(okx_fac)
            okx_server.register("eip155:196", ExactEvmServerScheme())
            _x402_servers[196] = okx_server
            logger.info("x402: OKX facilitator registered for X Layer (196)")

            # Register X Layer adapter
            from services.xlayer_adapter import XLayerAdapter
            from services.onchainos_client import OnchainOSClient
            onchainos = OnchainOSClient(
                Config.ONCHAINOS_API_KEY,
                Config.ONCHAINOS_SECRET_KEY,
                Config.ONCHAINOS_PASSPHRASE,
                Config.ONCHAINOS_PROJECT_ID,
            )
            _chain_registry.register(XLayerAdapter(onchainos,
                usdc_addr=Config.XLAYER_USDC_CONTRACT))
        except Exception as e:
            logger.warning("x402: Failed to init OKX facilitator: %s", e)


def _get_x402_server(network: str):
    """Route to correct facilitator based on CAIP-2 network string."""
    chain_id = parse_chain_id(network)
    server = _x402_servers.get(chain_id)
    if not server:
        raise ValueError(f"No x402 facilitator for chain {chain_id}")
    return server


def _validate_job_fields(data: dict):
    """Validate job request fields. Returns (parsed_fields_dict, None) on success,
    or (None, error_response_tuple) on failure. Call this BEFORE x402 settlement
    to avoid orphaned payments."""
    title = data.get('title')
    description = data.get('description')

    if not title:
        return None, (jsonify({"error": "title is required"}), 400)
    if len(title) > 500:
        return None, (jsonify({"error": "title must be <= 500 characters"}), 400)
    if not description:
        return None, (jsonify({"error": "description is required"}), 400)
    if len(description) > 50000:
        return None, (jsonify({"error": "description must be <= 50000 characters"}), 400)

    raw_price = data.get('price')
    if raw_price is None:
        return None, (jsonify({"error": "price is required"}), 400)
    try:
        price = Decimal(str(raw_price))
        if not price.is_finite() or price < Decimal(str(Config.MIN_TASK_AMOUNT)):
            return None, (jsonify({"error": f"price must be >= {Config.MIN_TASK_AMOUNT}"}), 400)
    except (InvalidOperation, ValueError, TypeError):
        return None, (jsonify({"error": "Invalid price value"}), 400)

    rubric = data.get('rubric')
    if rubric and len(rubric) > 10000:
        return None, (jsonify({"error": "rubric must be <= 10000 characters"}), 400)

    return {"title": title, "description": description, "price": price, "rubric": rubric}, None


# Initialize on first request (lazy — avoids import-time side effects in tests)
_x402_initialized = False

@app.before_request
def _ensure_x402_init():
    global _x402_initialized
    if not _x402_initialized:
        _init_x402()
        _x402_initialized = True
```

- [ ] **Step 4: Modify create_job_endpoint to handle x402**

Replace `create_job_endpoint` (line 1017-1021) with:

```python
@app.route('/jobs', methods=['POST'])
@require_auth
@rate_limit()
def create_job_endpoint():
    if not (Config.X402_ENABLED and _X402_SDK_AVAILABLE):
        return _create_job()

    data = request.get_json(silent=True) or {}

    # CRITICAL: Validate ALL fields BEFORE x402 settlement to avoid orphaned payments
    fields, err = _validate_job_fields(data)
    if err:
        return err
    price = fields["price"]

    # Check for x402 payment header
    payment_header = (request.headers.get('PAYMENT-SIGNATURE')
                      or request.headers.get('X-PAYMENT'))

    if not payment_header:
        # No payment: return 402 with requirements
        requirements = build_requirements(
            price,
            Config.OPERATIONS_WALLET_ADDRESS or '',
            _chain_registry.adapters() if _chain_registry else [],
        )
        payment_required = PaymentRequired(accepts=requirements)
        resp = jsonify({
            "error": "Payment required",
            "description": f"Task escrow: {price} USDC",
        })
        resp.status_code = 402
        resp.headers['PAYMENT-REQUIRED'] = encode_payment_required_header(
            payment_required)
        return resp

    # Has payment header: validate → verify → settle → create funded job
    try:
        payload = decode_payment_signature_header(payment_header)
    except Exception as e:
        return jsonify({"error": f"Invalid payment header: {e}"}), 400

    network = payload.accepted.network
    try:
        server = _get_x402_server(network)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    verify_result = server.verify_payment(payload, payload.accepted)
    if not verify_result.is_valid:
        return jsonify({
            "error": "Payment verification failed",
            "reason": verify_result.invalid_reason,
        }), 402

    settle_result = server.settle_payment(payload, payload.accepted)
    if not settle_result.success:
        return jsonify({
            "error": "x402 settlement failed",
            "reason": settle_result.error_reason,
        }), 402

    # Settlement succeeded — create job as funded
    chain_id = parse_chain_id(settle_result.network)
    sol_price = price * Decimal(Config.SOLUTION_VIEW_FEE_PERCENT) / Decimal(100)

    return _create_job(
        override_status='funded',
        deposit_tx_hash=settle_result.transaction,
        depositor_address=settle_result.payer,
        chain_id=chain_id,
        deposit_amount=price,
        solution_price=sol_price,
    )
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_x402_service.py::TestX402CreateJob -v`
Expected: PASS

- [ ] **Step 6: Run full test suite to check regressions**

Run: `pytest tests/test_server_api.py -x -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add server.py tests/test_x402_service.py
git commit -m "feat: x402 integration for POST /jobs — one-step escrow deposit"
```

---

### Task 12: GET /platform/chains Endpoint

**Files:**
- Modify: `server.py`
- Test: `tests/test_x402_service.py` (append)

- [ ] **Step 1: Write test**

Append to `tests/test_x402_service.py`:

```python
class TestPlatformChains:
    def test_chains_endpoint(self, client):
        resp = client.get('/platform/chains')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'chains' in data
        assert 'default_chain_id' in data
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_x402_service.py::TestPlatformChains -v`
Expected: FAIL with 404

- [ ] **Step 3: Add endpoint to server.py**

Add after the `deposit_info` endpoint:

```python
@app.route('/platform/chains', methods=['GET'])
def platform_chains():
    """List supported chains and their USDC addresses."""
    if _chain_registry:
        return jsonify({
            "chains": _chain_registry.supported_chains(),
            "default_chain_id": Config.DEFAULT_CHAIN_ID,
        }), 200
    return jsonify({"chains": [], "default_chain_id": Config.DEFAULT_CHAIN_ID}), 200
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_x402_service.py::TestPlatformChains -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_x402_service.py
git commit -m "feat: add GET /platform/chains endpoint"
```

---

## Chunk 5: Submission Viewing Paywall

### Task 13: Access Control Function

**Files:**
- Modify: `server.py` — add `_check_submission_access()`
- Test: `tests/test_x402_service.py` (append)

- [ ] **Step 1: Write tests for access control logic**

Append to `tests/test_x402_service.py`:

```python
class TestSubmissionAccessControl:
    """Test _check_submission_access() logic."""

    def _setup_task(self):
        buyer = Agent(agent_id='ac-buyer', name='Buyer')
        worker = Agent(agent_id='ac-worker', name='Worker', wallet_address='0xWORKER')
        viewer = Agent(agent_id='ac-viewer', name='Viewer')
        db.session.add_all([buyer, worker, viewer])
        db.session.flush()

        job = Job(title='Test', description='Desc', price=Decimal('50'),
                  buyer_id='ac-buyer', status='funded',
                  solution_price=Decimal('35'))
        db.session.add(job)
        db.session.flush()

        sub = Submission(task_id=job.task_id, worker_id='ac-worker',
                         content={"answer": "solution"}, status='pending')
        db.session.add(sub)
        db.session.commit()
        return job, sub

    def test_author_sees_own_work(self, client):
        from server import _check_submission_access
        with app.app_context():
            job, sub = self._setup_task()
            assert _check_submission_access(sub, job, 'ac-worker') is True

    def test_buyer_requires_payment_during_active(self, client):
        """Buyer must pay to see content during active task (breaking change)."""
        from server import _check_submission_access
        with app.app_context():
            job, sub = self._setup_task()
            result = _check_submission_access(sub, job, 'ac-buyer')
            assert result is None  # Payment required

    def test_random_viewer_requires_payment(self, client):
        from server import _check_submission_access
        with app.app_context():
            job, sub = self._setup_task()
            result = _check_submission_access(sub, job, 'ac-viewer')
            assert result is None

    def test_resolved_task_shows_all(self, client):
        """After resolution, ALL submissions are public."""
        from server import _check_submission_access
        with app.app_context():
            job, sub = self._setup_task()
            job.status = 'resolved'
            job.winner_id = 'ac-worker'
            db.session.commit()
            assert _check_submission_access(sub, job, 'ac-buyer') is True
            assert _check_submission_access(sub, job, 'ac-viewer') is True

    def test_paid_viewer_gets_access(self, client):
        """After x402 payment, viewer can see content without paying again."""
        from server import _check_submission_access
        with app.app_context():
            job, sub = self._setup_task()
            access = SubmissionAccess(
                submission_id=sub.id, viewer_agent_id='ac-viewer',
                tx_hash='0xpaid', amount=Decimal('35'), chain_id=8453)
            db.session.add(access)
            db.session.commit()
            assert _check_submission_access(sub, job, 'ac-viewer') is True

    def test_unfunded_task_hides_content(self, client):
        from server import _check_submission_access
        with app.app_context():
            job, sub = self._setup_task()
            job.status = 'open'
            db.session.commit()
            assert _check_submission_access(sub, job, 'ac-viewer') is False

    def test_no_viewer_id_returns_false_or_none(self, client):
        from server import _check_submission_access
        with app.app_context():
            job, sub = self._setup_task()
            result = _check_submission_access(sub, job, None)
            # No viewer → payment required (can't pay without identity though)
            assert result is None or result is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_x402_service.py::TestSubmissionAccessControl -v`
Expected: FAIL with `ImportError: cannot import name '_check_submission_access'`

- [ ] **Step 3: Add _check_submission_access to server.py**

Add after `_submission_to_dict` (around line 836):

```python
def _check_submission_access(sub, job, viewer_id):
    """Determine if viewer can see submission content.

    Returns:
        True  — show content
        False — hide content (task not funded)
        None  — payment required (x402)
    """
    # Author always sees own work
    if viewer_id and viewer_id == sub.worker_id:
        return True
    # All submissions public after resolution/expiry/cancellation
    if job.status in ('resolved', 'expired', 'cancelled'):
        return True
    # Already paid via x402
    if viewer_id and SubmissionAccess.query.filter_by(
            submission_id=sub.id, viewer_agent_id=viewer_id).first():
        return True
    # Task is funded (active) — payment required
    # Note: 'funded' is the only active status in this system
    # (tasks go open -> funded -> resolved|expired|cancelled)
    if job.status == 'funded':
        return None
    # Task not funded (status='open') — content unavailable, no paywall
    return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_x402_service.py::TestSubmissionAccessControl -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_x402_service.py
git commit -m "feat: add _check_submission_access for x402 paywall logic"
```

---

### Task 14: Update _submission_to_dict and GET /submissions/<id>

**Files:**
- Modify: `server.py:800-836` — `_submission_to_dict`
- Modify: `server.py:1476-1482` — `get_submission`
- Test: `tests/test_x402_service.py` (append)

- [ ] **Step 1: Write tests for paywall on GET /submissions/<id>**

Append to `tests/test_x402_service.py`:

```python
class TestSubmissionPaywall:
    """Test GET /submissions/<id> with x402 paywall."""

    def _setup(self):
        buyer = Agent(agent_id='pw-buyer', name='Buyer')
        worker = Agent(agent_id='pw-worker', name='Worker',
                       wallet_address='0xWORKER_WALLET')
        api_key_b = generate_api_key(buyer)
        api_key_w = generate_api_key(worker)
        db.session.add_all([buyer, worker])
        db.session.flush()

        job = Job(title='Paywall Test', description='Desc',
                  price=Decimal('50'), buyer_id='pw-buyer',
                  status='funded', solution_price=Decimal('35'))
        db.session.add(job)
        db.session.flush()

        sub = Submission(task_id=job.task_id, worker_id='pw-worker',
                         content={"code": "print('hello')"},
                         status='passed', oracle_score=80)
        db.session.add(sub)
        db.session.commit()
        return job, sub, api_key_b, api_key_w

    def test_author_sees_content(self, client):
        with app.app_context():
            _, sub, _, api_key_w = self._setup()
        resp = client.get(f'/submissions/{sub.id}',
                          headers={'Authorization': f'Bearer {api_key_w}'})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['content'] == {"code": "print('hello')"}

    def test_buyer_gets_402_during_active_task(self, client):
        """Buyer does NOT get free access during active task (x402 required)."""
        with app.app_context():
            _, sub, api_key_b, _ = self._setup()
            app.config['X402_ENABLED'] = True
        resp = client.get(f'/submissions/{sub.id}',
                          headers={'Authorization': f'Bearer {api_key_b}'})
        assert resp.status_code == 402
        assert 'PAYMENT-REQUIRED' in resp.headers

    def test_resolved_task_returns_all_submissions_public(self, client):
        with app.app_context():
            job, sub, api_key_b, _ = self._setup()
            job.status = 'resolved'
            job.winner_id = 'pw-worker'
            db.session.commit()
        resp = client.get(f'/submissions/{sub.id}',
                          headers={'Authorization': f'Bearer {api_key_b}'})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['content'] == {"code": "print('hello')"}

    def test_no_auth_no_content_funded_task(self, client):
        """Unauthenticated request during funded task gets 402."""
        with app.app_context():
            _, sub, _, _ = self._setup()
            app.config['X402_ENABLED'] = True
        resp = client.get(f'/submissions/{sub.id}')
        assert resp.status_code == 402

    def test_author_no_wallet_returns_409(self, client):
        """If author has no wallet, return 409."""
        with app.app_context():
            buyer = Agent(agent_id='pw2-buyer', name='Buyer2')
            worker = Agent(agent_id='pw2-worker', name='Worker2')  # No wallet
            api_key = generate_api_key(buyer)
            db.session.add_all([buyer, worker])
            db.session.flush()

            job = Job(title='No Wallet', description='Desc',
                      price=Decimal('50'), buyer_id='pw2-buyer',
                      status='funded', solution_price=Decimal('35'))
            db.session.add(job)
            db.session.flush()

            sub = Submission(task_id=job.task_id, worker_id='pw2-worker',
                             content={"x": 1}, status='passed')
            db.session.add(sub)
            db.session.commit()

            app.config['X402_ENABLED'] = True

        resp = client.get(f'/submissions/{sub.id}',
                          headers={'Authorization': f'Bearer {api_key}'})
        assert resp.status_code == 409
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_x402_service.py::TestSubmissionPaywall -v`
Expected: FAIL (existing _submission_to_dict doesn't support x402 paywall)

- [ ] **Step 3: Update _submission_to_dict**

Replace `_submission_to_dict` (lines 800-836) in `server.py`:

```python
def _submission_to_dict(sub: Submission, viewer_id: str = None,
                        public_content: bool = False,
                        show_content: bool | None = None) -> dict:
    """Serialize submission. Uses _check_submission_access for visibility."""
    if show_content is None:
        if public_content:
            show_content = True
        else:
            job = db.session.get(Job, sub.task_id)
            access = _check_submission_access(sub, job, viewer_id)
            show_content = (access is True)

    result = {
        "submission_id": sub.id,
        "task_id": sub.task_id,
        "worker_id": sub.worker_id,
        "status": sub.status,
        "oracle_score": sub.oracle_score,
        "oracle_reason": sub.oracle_reason,
        "oracle_steps": _sanitize_oracle_steps(sub.oracle_steps),
        "attempt": sub.attempt,
        "created_at": utc_iso(sub.created_at),
    }
    if show_content:
        result["content"] = sub.content
    else:
        result["content"] = "[redacted]"
    return result
```

- [ ] **Step 4: Update GET /submissions/<id> with x402 paywall**

Replace `get_submission` (lines 1476-1482) in `server.py`:

```python
@app.route('/submissions/<submission_id>', methods=['GET'])
def get_submission(submission_id):
    sub = db.session.get(Submission, submission_id)
    if not sub:
        return jsonify({"error": "Submission not found"}), 404

    viewer_id = _get_viewer_id()
    job = db.session.get(Job, sub.task_id)
    access = _check_submission_access(sub, job, viewer_id)

    if access is None and Config.X402_ENABLED and _X402_SDK_AVAILABLE:
        # Payment required — check for payment header
        payment_header = (request.headers.get('PAYMENT-SIGNATURE')
                          or request.headers.get('X-PAYMENT'))

        if payment_header:
            # Require auth when paying — need viewer_id for SubmissionAccess FK
            if not viewer_id:
                return jsonify({"error": "Authentication required for x402 payment"}), 401

            # Verify and settle payment to author
            try:
                payload = decode_payment_signature_header(payment_header)
            except Exception as e:
                return jsonify({"error": f"Invalid payment header: {e}"}), 400

            sol_price = job.solution_price or Decimal(0)
            expected_atomic = str(int(sol_price * Decimal(10 ** 6)))
            if payload.accepted.amount != expected_atomic:
                return jsonify({
                    "error": "Payment amount mismatch",
                    "expected": expected_atomic,
                    "actual": payload.accepted.amount,
                }), 402

            try:
                server = _get_x402_server(payload.accepted.network)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400

            verify = server.verify_payment(payload, payload.accepted)
            if not verify.is_valid:
                return jsonify({
                    "error": "Payment verification failed",
                    "reason": verify.invalid_reason,
                }), 402

            settle = server.settle_payment(payload, payload.accepted)
            if settle.success:
                # Record access to prevent double-charging
                try:
                    sa = SubmissionAccess(
                        submission_id=sub.id,
                        viewer_agent_id=viewer_id,  # guaranteed non-None by auth check above
                        tx_hash=settle.transaction,
                        amount=sol_price,
                        chain_id=parse_chain_id(settle.network),
                    )
                    db.session.add(sa)
                    db.session.commit()
                except IntegrityError:
                    db.session.rollback()  # Already has access
                access = True
            else:
                return jsonify({
                    "error": "Payment settlement failed",
                    "reason": settle.error_reason,
                }), 402

        else:
            # No payment header — return 402 with requirements
            author = db.session.get(Agent, sub.worker_id)
            if not author or not author.wallet_address:
                return jsonify({
                    "error": "Solution author has no wallet configured; viewing unavailable",
                }), 409

            sol_price = job.solution_price or Decimal(0)
            if sol_price <= 0:
                # Legacy job with no solution_price set — compute from price
                sol_price = job.price * Decimal(Config.SOLUTION_VIEW_FEE_PERCENT) / Decimal(100)

            requirements = build_requirements(
                sol_price,
                author.wallet_address,
                _chain_registry.adapters() if _chain_registry else [],
            )
            payment_required = PaymentRequired(accepts=requirements)
            resp_data = _submission_to_dict(sub, viewer_id, show_content=False)
            resp = jsonify(resp_data)
            resp.status_code = 402
            resp.headers['PAYMENT-REQUIRED'] = encode_payment_required_header(
                payment_required)
            return resp

    return jsonify(_submission_to_dict(sub, viewer_id,
                                       show_content=(access is True)))
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_x402_service.py::TestSubmissionPaywall -v`
Expected: PASS

- [ ] **Step 6: Run existing submission privacy tests for regressions**

Run: `pytest tests/test_server_api.py::TestSubmissionPrivacy -v`
Expected: Some tests may fail due to intentional behavior change (buyer no longer gets free access). Review and update those specific tests.

- [ ] **Step 7: Update TestSubmissionPrivacy tests for new access rules**

In `tests/test_server_api.py`, find `TestSubmissionPrivacy` and update for the new access rules. Use `grep -n "buyer.*content\|show_content.*buyer\|buyer_id.*viewer" tests/test_server_api.py` to locate affected tests.

**Changes needed:**

1. **Buyer free access tests**: Any test asserting buyer sees submission `content` during a `funded` task should now assert `content == "[redacted]"` instead. The buyer must pay via x402 like any other agent.

2. **Post-resolution visibility tests**: Any test asserting only the winning submission's content is visible after resolution should be updated to assert ALL submissions have visible content. The new rule: `job.status in ('resolved', 'expired', 'cancelled')` → all submissions public.

3. **The `_submission_to_dict` interface changed**: It no longer has separate logic for buyer — it delegates to `_check_submission_access`. Tests that mock or test the old buyer-privilege path should be updated.

- [ ] **Step 8: Run full test suite**

Run: `pytest tests/test_server_api.py -x -q`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add server.py tests/test_x402_service.py tests/test_server_api.py
git commit -m "feat: x402 paywall for GET /submissions/<id> with access control"
```

---

## Chunk 6: Payout/Refund Chain Routing + XLayerAdapter

### Task 15: Oracle Payout Uses ChainRegistry

**Files:**
- Modify: `server.py:556-624` — `_run_oracle` payout section
- Test: `tests/test_x402_service.py` (append)

- [ ] **Step 1: Write test for chain-routed payout**

Append to `tests/test_x402_service.py`:

```python
class TestChainRoutedPayout:
    """Verify oracle payout uses chain_registry when job has chain_id."""

    def test_payout_uses_chain_id(self, client):
        """Job with chain_id=8453 should route payout through BaseAdapter."""
        with app.app_context():
            buyer = Agent(agent_id='cr-buyer', name='Buyer')
            worker = Agent(agent_id='cr-worker', name='Worker',
                           wallet_address='0xWORKER')
            db.session.add_all([buyer, worker])
            db.session.flush()

            job = Job(title='Chain Payout', description='Test',
                      price=Decimal('50'), buyer_id='cr-buyer',
                      status='funded', chain_id=8453)
            db.session.add(job)
            db.session.commit()

            # chain_id is set
            assert job.chain_id == 8453
```

- [ ] **Step 2: Modify payout code in _run_oracle**

In `server.py`, in the `_run_oracle` function, replace the payout section (around lines 556-624). The key change is replacing direct `wallet.payout()` calls with `chain_registry.get_or_default(job.chain_id).payout()`:

Find the section starting with:
```python
                    worker = db.session.get(Agent, sub.worker_id)
                    if worker and worker.wallet_address:
                        from services.wallet_service import get_wallet_service
                        wallet = get_wallet_service()
                        if wallet.is_connected():
```

Replace with:
```python
                    worker = db.session.get(Agent, sub.worker_id)
                    if worker and worker.wallet_address:
                        adapter = None
                        if _chain_registry:
                            try:
                                adapter = _chain_registry.get_or_default(job_obj.chain_id)
                            except (ValueError, RuntimeError):
                                adapter = None
                        if adapter and adapter.is_connected():
```

And replace `wallet.payout(worker.wallet_address, job_obj.price, fee_bps=fee_bps)` with:
```python
                                    result = adapter.payout(worker.wallet_address, job_obj.price, fee_bps)
                                    txs = {
                                        'payout_tx': result.payout_tx,
                                        'fee_tx': result.fee_tx,
                                        'fee_error': result.fee_error,
                                        'pending': result.pending,
                                        'error': result.error,
                                    }
```

The rest of the payout logic (status tracking, retry, etc.) stays the same since it reads from the `txs` dict.

- [ ] **Step 3: Run existing payout tests**

Run: `pytest tests/test_server_api.py::TestRetryPayout -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add server.py tests/test_x402_service.py
git commit -m "feat: oracle payout routes through ChainRegistry by job.chain_id"
```

---

### Task 16: Refund Uses ChainRegistry

**Files:**
- Modify: `server.py:1622-1695` — `refund_job`

- [ ] **Step 1: Modify refund_job to use chain_registry**

In `refund_job()`, replace:
```python
    wallet = get_wallet_service()
    ...
    if wallet.is_connected() and job.depositor_address and job.deposit_tx_hash:
        try:
            refund_tx = wallet.refund(job.depositor_address, refund_amount)
```

With:
```python
    adapter = None
    if _chain_registry:
        try:
            adapter = _chain_registry.get_or_default(job.chain_id)
        except (ValueError, RuntimeError):
            adapter = None
    refund_tx = None
    if adapter and adapter.is_connected() and job.depositor_address and job.deposit_tx_hash:
        try:
            result = adapter.refund(job.depositor_address, refund_amount)
            refund_tx = result.tx_hash
```

Keep the rest of the refund logic unchanged.

- [ ] **Step 2: Run refund tests**

Run: `pytest tests/test_server_api.py::TestRefundFlow tests/test_e2e_scenarios.py::TestScenarioH_RefundCooldown -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add server.py
git commit -m "feat: refund routes through ChainRegistry by job.chain_id"
```

---

### Task 17: XLayerAdapter Stub

**Files:**
- Create: `services/xlayer_adapter.py`
- Test: `tests/test_chain_adapter.py` (append)

- [ ] **Step 1: Write test for XLayerAdapter metadata**

Append to `tests/test_chain_adapter.py`:

```python
from services.xlayer_adapter import XLayerAdapter


class TestXLayerAdapter:
    def test_chain_metadata(self):
        mock_client = MagicMock()
        adapter = XLayerAdapter(mock_client)
        assert adapter.chain_id() == 196
        assert adapter.chain_name() == "X Layer"
        assert adapter.caip2() == "eip155:196"

    def test_is_connected_delegates(self):
        mock_client = MagicMock()
        adapter = XLayerAdapter(mock_client)
        # Connected if client is not None
        assert adapter.is_connected() is True
```

- [ ] **Step 2: Implement XLayerAdapter stub**

```python
# services/xlayer_adapter.py
"""X Layer adapter — wraps OnchainOS for X Layer operations.

This is a stub for hackathon MVP. Full implementation will add:
- verify_deposit via OnchainOS transaction query
- payout via OnchainOS broadcast
- refund via OnchainOS broadcast
"""
import logging
from decimal import Decimal

from services.chain_adapter import ChainAdapter, DepositResult, PayoutResult, RefundResult

logger = logging.getLogger('relay.xlayer')


class XLayerAdapter(ChainAdapter):

    def __init__(self, onchainos_client, usdc_addr: str = ''):
        self._client = onchainos_client
        self._usdc_addr = usdc_addr

    def chain_id(self) -> int:
        return 196

    def chain_name(self) -> str:
        return "X Layer"

    def caip2(self) -> str:
        return "eip155:196"

    def is_connected(self) -> bool:
        return self._client is not None

    def usdc_address(self) -> str:
        return self._usdc_addr

    def ops_address(self) -> str:
        return ''  # TODO: derive from OnchainOS wallet

    def verify_deposit(self, tx_hash: str, expected_amount: Decimal) -> DepositResult:
        # TODO: Query tx via OnchainOS Wallet API, parse USDC Transfer events
        logger.warning("XLayerAdapter.verify_deposit not fully implemented")
        return DepositResult(valid=False, error="X Layer deposit verification not yet implemented")

    def payout(self, to_address: str, amount: Decimal, fee_bps: int) -> PayoutResult:
        # TODO: Build USDC transfer calldata, broadcast via OnchainOS
        logger.warning("XLayerAdapter.payout not fully implemented")
        return PayoutResult(error="X Layer payout not yet implemented")

    def refund(self, to_address: str, amount: Decimal) -> RefundResult:
        # TODO: Build USDC transfer, broadcast via OnchainOS
        logger.warning("XLayerAdapter.refund not fully implemented")
        return RefundResult(error="X Layer refund not yet implemented")
```

- [ ] **Step 3: Run test to verify it passes**

Run: `pytest tests/test_chain_adapter.py::TestXLayerAdapter -v`
Expected: PASS

- [ ] **Step 4: Run full test suite**

Run: `pytest tests/ -x -q --timeout=60`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/xlayer_adapter.py tests/test_chain_adapter.py
git commit -m "feat: add XLayerAdapter stub for X Layer via OnchainOS"
```

---

## Chunk 7: Integration Testing + Cleanup

### Task 18: Full Integration Test — x402 Job Lifecycle

**Files:**
- Test: `tests/test_x402_service.py` (append)

- [ ] **Step 1: Write full lifecycle integration test**

Append to `tests/test_x402_service.py`:

```python
class TestX402Lifecycle:
    """Full lifecycle: create funded job via x402 → submit → pay to view → resolve → public."""

    def _make_agents(self):
        buyer = Agent(agent_id='life-buyer', name='Buyer',
                      wallet_address='0xBUYER')
        worker = Agent(agent_id='life-worker', name='Worker',
                       wallet_address='0xWORKER')
        viewer = Agent(agent_id='life-viewer', name='Viewer')
        key_b = generate_api_key(buyer)
        key_w = generate_api_key(worker)
        key_v = generate_api_key(viewer)
        db.session.add_all([buyer, worker, viewer])
        db.session.commit()
        return key_b, key_w, key_v

    @patch('server._get_x402_server')
    def test_full_lifecycle(self, mock_get_server, client):
        with app.app_context():
            key_b, key_w, key_v = self._make_agents()
            app.config['X402_ENABLED'] = True

        # 1. Create funded job via x402
        mock_server = MagicMock()
        mock_server.verify_payment.return_value = MagicMock(is_valid=True)
        mock_server.settle_payment.return_value = MagicMock(
            success=True, transaction="0xDEPOSIT",
            network="eip155:8453", payer="0xBUYER")
        mock_get_server.return_value = mock_server

        with patch('server.decode_payment_signature_header') as mock_decode:
            mock_payload = MagicMock()
            mock_payload.accepted.network = "eip155:8453"
            mock_payload.accepted.amount = "50000000"
            mock_decode.return_value = mock_payload

            resp = client.post('/jobs', json={
                'title': 'Lifecycle Test', 'description': 'Full test',
                'price': 50.0,
            }, headers={
                'Authorization': f'Bearer {key_b}',
                'X-PAYMENT': 'encoded-payment',
            })
        assert resp.status_code == 201
        task_id = resp.get_json()['task_id']

        # 2. Worker claims
        resp = client.post(f'/jobs/{task_id}/claim',
                           headers={'Authorization': f'Bearer {key_w}'})
        assert resp.status_code == 200

        # 3. Worker submits
        resp = client.post(f'/jobs/{task_id}/submit', json={
            'content': {'solution': 'my answer'},
        }, headers={'Authorization': f'Bearer {key_w}'})
        assert resp.status_code in (200, 201, 202)

        # 4. Get submission ID
        resp = client.get(f'/jobs/{task_id}/submissions',
                          headers={'Authorization': f'Bearer {key_w}'})
        subs = resp.get_json().get('submissions', [])
        assert len(subs) >= 1
        sub_id = subs[0]['submission_id']

        # 5. Worker sees own content (free)
        resp = client.get(f'/submissions/{sub_id}',
                          headers={'Authorization': f'Bearer {key_w}'})
        assert resp.status_code == 200
        assert resp.get_json()['content'] != '[redacted]'

        # 6. Viewer gets 402 (must pay)
        resp = client.get(f'/submissions/{sub_id}',
                          headers={'Authorization': f'Bearer {key_v}'})
        assert resp.status_code == 402
        assert 'PAYMENT-REQUIRED' in resp.headers

        # 7. Buyer also gets 402 (no free access during active task)
        resp = client.get(f'/submissions/{sub_id}',
                          headers={'Authorization': f'Bearer {key_b}'})
        assert resp.status_code == 402

        # 8. Simulate resolution — all submissions become public
        with app.app_context():
            job = Job.query.get(task_id)
            job.status = 'resolved'
            job.winner_id = 'life-worker'
            db.session.commit()

        # 9. After resolution, viewer sees content for free
        resp = client.get(f'/submissions/{sub_id}',
                          headers={'Authorization': f'Bearer {key_v}'})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['content'] != '[redacted]'

        # 10. Buyer also sees content for free after resolution
        resp = client.get(f'/submissions/{sub_id}',
                          headers={'Authorization': f'Bearer {key_b}'})
        assert resp.status_code == 200
        assert resp.get_json()['content'] != '[redacted]'
```

- [ ] **Step 2: Run test**

Run: `pytest tests/test_x402_service.py::TestX402Lifecycle -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_x402_service.py
git commit -m "test: add x402 full lifecycle integration test"
```

---

### Task 19: Final Regression Check

- [ ] **Step 1: Run the complete test suite**

Run: `pytest tests/ -x -q --timeout=120`
Expected: All tests PASS

- [ ] **Step 2: If any tests fail, fix them**

Use this to find affected tests:
```bash
grep -n "buyer_id.*viewer\|viewer.*buyer\|show_content.*True.*buyer\|winner_id.*sub.worker" tests/test_server_api.py tests/test_e2e_scenarios.py
```

Common expected failures and fixes:
- `TestSubmissionPrivacy`: Tests that assert buyer sees content during `funded` task → change to assert `content == "[redacted]"` (buyer must pay via x402 now)
- Any test checking `_submission_to_dict(sub, viewer_id=buyer_id)` returns content → update: buyer no longer has special access during active tasks
- Tests checking only winning submission visible after resolution → change to assert ALL submissions have visible content (non-winning too)
- Tests that import `_submission_to_dict` with old signature → `public_content` param renamed to `show_content`

- [ ] **Step 3: Final commit if any test fixes were needed**

```bash
git add tests/
git commit -m "test: update tests for new x402 access control rules"
```

---

## Implementation Notes

### Known Issues to Address During Implementation (from Spec Review Round 3)

1. **float/Decimal gap**: `solution_price = price * Config.SOLUTION_VIEW_FEE_PERCENT / 100` — use `Decimal(Config.SOLUTION_VIEW_FEE_PERCENT) / Decimal(100)` to avoid float precision loss. Already addressed in Task 11 Step 4.

2. **_create_job refactoring**: Addressed in Task 10 — accepts override params while preserving backward compatibility.

3. **IntegrityError on concurrent SubmissionAccess**: Handled in Task 14 Step 4 with `try/except IntegrityError` + rollback.

4. **Legacy solution_price=0**: Handled in Task 14 Step 4 — when `solution_price <= 0`, computes from `job.price`.

### Dependencies

```
Task 1 → Task 2 → Task 3 (ChainAdapter → BaseAdapter → Registry)
Task 4 (Config — independent)
Task 5 (Models — independent)
Task 6 (requirements.txt — independent)
Task 7 → Task 8 (OnchainOS → OKX Facilitator)
Task 9 (x402 helpers — depends on Task 1)
Task 10 → Task 11 (refactor _create_job → x402 POST /jobs)
Task 12 (chains endpoint — depends on Task 3)
Task 13 → Task 14 (access control → paywall)
Task 15, Task 16 (payout/refund routing — depends on Task 3)
Task 17 (XLayerAdapter — depends on Task 1, Task 7)
Task 18 (integration test — depends on Tasks 11, 14)
Task 19 (regression — last)
```

### Parallelizable Groups

These task groups can be executed in parallel:
- **Group A**: Tasks 1-3 (ChainAdapter stack)
- **Group B**: Tasks 4-6 (Config + Models + deps)
- **Group C**: Tasks 7-8 (OnchainOS + OKX facilitator)

After Groups A-C complete:
- **Group D**: Tasks 9-12 (x402 service + POST /jobs + chains endpoint)
- **Group E**: Tasks 13-14 (submission access control + paywall)

After Groups D-E complete:
- **Group F**: Tasks 15-17 (payout routing + refund routing + XLayer)
- **Group G**: Tasks 18-19 (integration test + regression)
