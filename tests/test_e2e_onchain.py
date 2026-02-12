"""
Phase 3: On-chain E2E Happy Path test.

Runs the full Scenario A: Buyer deposits real USDC → Worker submits →
Oracle evaluates (real GPT-4o) → Payout to worker + fee.

Run with: pytest tests/test_e2e_onchain.py -v -m onchain -s
Requires: .env with RPC_URL, all wallet keys, ORACLE_LLM_API_KEY

WARNING: This test interacts with REAL Base L2 mainnet and calls
REAL OpenRouter API. Costs ~0.10 USDC + OpenRouter API credits.
"""
import os
import time
import json
import logging
import functools
import threading
import pytest
from decimal import Decimal
from dotenv import load_dotenv

from tests.helpers.chain_helpers import (
    get_web3, get_usdc_contract, query_usdc_balance,
    send_usdc_from_agent, wait_confirmations,
)

# Suppress wallet logger to prevent key leakage
logging.getLogger("relay.wallet").setLevel(logging.WARNING)

pytestmark = pytest.mark.onchain

# Job config
JOB_PRICE = Decimal("0.10")  # 0.10 USDC
FEE_BPS = 2000  # 20%
WORKER_SHARE = JOB_PRICE * Decimal("0.80")  # 0.08 USDC
FEE_SHARE = JOB_PRICE * Decimal("0.20")    # 0.02 USDC
ORACLE_POLL_INTERVAL = 5  # seconds
ORACLE_POLL_TIMEOUT = 180  # seconds (3 min, generous for GPT-4o)


# ──────────────────────────────────────────────────────────────────
# Tracing infrastructure: intercept service methods to log call chain
# ──────────────────────────────────────────────────────────────────

_trace_log = []           # Collected trace entries
_trace_lock = threading.Lock()
_trace_indent = threading.local()  # Per-thread nesting depth


def _get_indent():
    return getattr(_trace_indent, 'depth', 0)


def _trace_print(msg):
    """Thread-safe print with trace prefix."""
    indent = "  " * (_get_indent() + 1)
    line = f"  [TRACE] {indent}{msg}"
    print(line)
    with _trace_lock:
        _trace_log.append(line)


def _make_tracer(cls_name, method_name, original, caller_label):
    """Create a wrapper that logs entry/exit/result for a method."""
    @functools.wraps(original)
    def wrapper(*args, **kwargs):
        _trace_indent.depth = _get_indent() + 1

        # Build arg summary (skip self)
        sig_parts = []
        param_args = args[1:] if args else ()
        for a in param_args:
            s = str(a)
            if len(s) > 80:
                s = s[:77] + "..."
            sig_parts.append(s)
        for k, v in kwargs.items():
            s = f"{k}={v}"
            if len(s) > 60:
                s = s[:57] + "..."
            sig_parts.append(s)
        sig = ", ".join(sig_parts)

        _trace_print(f"→ {caller_label} calls {cls_name}.{method_name}({sig})")
        t = time.time()
        try:
            result = original(*args, **kwargs)
            elapsed = time.time() - t
            # Summarise result
            if isinstance(result, dict):
                summary = {k: (str(v)[:60] + "..." if len(str(v)) > 60 else v)
                           for k, v in result.items()}
                _trace_print(f"← {cls_name}.{method_name} returned dict in {elapsed:.2f}s: {summary}")
            elif isinstance(result, str):
                _trace_print(f"← {cls_name}.{method_name} returned '{result[:80]}' in {elapsed:.2f}s")
            else:
                _trace_print(f"← {cls_name}.{method_name} returned {type(result).__name__} in {elapsed:.2f}s")
            _trace_indent.depth = max(0, _get_indent() - 1)
            return result
        except Exception as e:
            elapsed = time.time() - t
            _trace_print(f"✗ {cls_name}.{method_name} RAISED {type(e).__name__}: {e} ({elapsed:.2f}s)")
            _trace_indent.depth = max(0, _get_indent() - 1)
            raise
    return wrapper


def _install_tracers():
    """Monkey-patch key service methods to emit trace logs.

    Returns a cleanup function to restore originals.
    """
    originals = []

    # --- WalletService ---
    from services.wallet_service import WalletService
    for method_name in ('verify_deposit', 'estimate_gas', 'send_usdc', 'payout', 'refund', 'is_connected'):
        orig = getattr(WalletService, method_name)
        caller = "Platform/Operator"
        if method_name == 'verify_deposit':
            caller = "Platform (for Buyer)"
        elif method_name in ('estimate_gas',):
            caller = "Platform/API"
        elif method_name == 'payout':
            caller = "Platform/Oracle (auto-settle)"
        elif method_name == 'refund':
            caller = "Platform (for Buyer)"
        wrapped = _make_tracer("WalletService", method_name, orig, caller)
        setattr(WalletService, method_name, wrapped)
        originals.append((WalletService, method_name, orig))

    # --- OracleGuard ---
    from services.oracle_guard import OracleGuard
    for method_name in ('check', 'programmatic_scan', 'llm_scan'):
        orig = getattr(OracleGuard, method_name)
        wrapped = _make_tracer("OracleGuard", method_name, orig, "Oracle/Guard")
        setattr(OracleGuard, method_name, wrapped)
        originals.append((OracleGuard, method_name, orig))

    # --- OracleService ---
    from services.oracle_service import OracleService
    for method_name in ('evaluate', '_call_llm', '_build_result'):
        orig = getattr(OracleService, method_name)
        wrapped = _make_tracer("OracleService", method_name, orig, "Oracle/Evaluator")
        setattr(OracleService, method_name, wrapped)
        originals.append((OracleService, method_name, orig))

    # --- AgentService ---
    from services.agent_service import AgentService
    for method_name in ('register', 'get_profile', 'update_reputation'):
        if hasattr(AgentService, method_name):
            orig = getattr(AgentService, method_name)
            wrapped = _make_tracer("AgentService", method_name, orig, "Platform")
            setattr(AgentService, method_name, wrapped)
            originals.append((AgentService, method_name, orig))

    # --- JobService ---
    from services.job_service import JobService
    for method_name in ('get_job', 'list_jobs', 'to_dict', 'check_expiry'):
        if hasattr(JobService, method_name):
            orig = getattr(JobService, method_name)
            wrapped = _make_tracer("JobService", method_name, orig, "Platform")
            setattr(JobService, method_name, wrapped)
            originals.append((JobService, method_name, orig))

    # --- AuthService ---
    from services.auth_service import generate_api_key
    import services.auth_service as auth_mod
    orig_gen = auth_mod.generate_api_key
    def _traced_gen():
        _trace_print("→ Platform calls auth_service.generate_api_key()")
        result = orig_gen()
        _trace_print(f"← auth_service.generate_api_key() returned key={_mask(result[0])}")
        return result
    auth_mod.generate_api_key = _traced_gen
    originals.append((auth_mod, 'generate_api_key', orig_gen))

    def _cleanup():
        for obj, name, orig in originals:
            setattr(obj, name, orig)

    return _cleanup


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────

def _mask(s, show=6):
    """Mask a sensitive string, showing first `show` chars."""
    if not s:
        return "(empty)"
    if len(s) <= show + 4:
        return s[:show] + "..."
    return s[:show] + "..." + s[-4:]


def _pp(label, data):
    """Pretty-print a dict with label."""
    print(f"  {label}:")
    if isinstance(data, dict):
        for k, v in data.items():
            print(f"    {k}: {v}")
    else:
        print(f"    {data}")


def _elapsed(t0):
    """Return elapsed seconds since t0."""
    return f"{time.time() - t0:.1f}s"


def _api(caller, method, endpoint):
    """Log an API call with caller identity."""
    print(f"\n  ▶ {caller} → {method} {endpoint}")


# ──────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def w3():
    _w3 = get_web3()
    assert _w3.is_connected()
    return _w3


@pytest.fixture(scope="module")
def app_client():
    """Flask test client with real chain config (DEV_MODE=true for SQLite test DB)."""
    load_dotenv()
    os.environ['DEV_MODE'] = 'true'

    from server import app, db
    app.config['TESTING'] = True
    app.config['DEV_MODE'] = True
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'

    with app.app_context():
        db.create_all()
        yield app.test_client()
        from server import _oracle_executor
        _oracle_executor.shutdown(wait=True)
        db.session.remove()
        db.drop_all()


@pytest.fixture(scope="module")
def buyer(app_client):
    """Register buyer agent (Agent1)."""
    rv = app_client.post('/agents', json={
        "agent_id": "e2e-buyer-001",
        "name": "E2E Buyer",
        "wallet_address": os.environ["TEST_AGENT_WALLET_ADDRESS_1"],
    })
    assert rv.status_code == 201
    data = rv.get_json()
    return {"agent_id": "e2e-buyer-001", "api_key": data["api_key"]}


@pytest.fixture(scope="module")
def worker(app_client):
    """Register worker agent (Agent2 — receives payout)."""
    rv = app_client.post('/agents', json={
        "agent_id": "e2e-worker-001",
        "name": "E2E Worker",
        "wallet_address": os.environ["TEST_AGENT_WALLET_ADDRESS_2"],
    })
    assert rv.status_code == 201
    data = rv.get_json()
    return {"agent_id": "e2e-worker-001", "api_key": data["api_key"]}


def auth_header(agent):
    return {"Authorization": f"Bearer {agent['api_key']}"}


# ──────────────────────────────────────────────────────────────────
# Test
# ──────────────────────────────────────────────────────────────────

class TestE2EHappyPath:
    """Scenario A: Full on-chain Happy Path with real Oracle and real USDC."""

    def test_full_happy_path(self, app_client, w3, buyer, worker):
        t0 = time.time()

        # Install method tracers — will be cleaned up at the end
        cleanup_tracers = _install_tracers()
        try:
            self._run(app_client, w3, buyer, worker, t0)
        finally:
            cleanup_tracers()
            # Print collected background traces (oracle thread)
            bg_traces = [t for t in _trace_log if "Oracle" in t or "payout" in t.lower() or "auto-settle" in t.lower()]
            if bg_traces:
                print(f"\n{'═' * 70}")
                print(f"  BACKGROUND THREAD TRACES (Oracle + Auto-Payout)")
                print(f"{'═' * 70}")
                for t in bg_traces:
                    print(t)

    def _run(self, app_client, w3, buyer, worker, t0):
        ops_addr = os.environ["OPERATIONS_WALLET_ADDRESS"]
        worker_addr = os.environ["TEST_AGENT_WALLET_ADDRESS_2"]
        fee_addr = os.environ["FEE_WALLET_ADDRESS"]
        buyer_addr = os.environ["TEST_AGENT_WALLET_ADDRESS_1"]

        print("\n" + "=" * 70)
        print("  SYNAI RELAY — E2E HAPPY PATH TEST (with full call tracing)")
        print("=" * 70)

        # ── Environment ──
        print(f"\n[ENV] Environment & Configuration")
        print(f"  Chain:           Base L2 Mainnet (chain_id={w3.eth.chain_id})")
        print(f"  RPC URL:         {_mask(os.environ.get('RPC_URL', ''), 30)}")
        print(f"  Block number:    {w3.eth.block_number}")
        print(f"  Gas price:       {w3.eth.gas_price} wei ({float(w3.from_wei(w3.eth.gas_price, 'gwei')):.6f} Gwei)")
        print(f"  USDC contract:   {os.environ.get('USDC_CONTRACT', '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913')}")
        print(f"  DEV_MODE:        {os.environ.get('DEV_MODE', 'not set')}")
        print(f"  Oracle model:    {os.environ.get('ORACLE_LLM_MODEL', 'not set')}")
        print(f"  Oracle base URL: {os.environ.get('ORACLE_LLM_BASE_URL', 'not set')}")
        print(f"  Oracle API key:  {_mask(os.environ.get('ORACLE_LLM_API_KEY', ''))}")
        print(f"  Pass threshold:  {os.environ.get('ORACLE_PASS_THRESHOLD', '80')}")
        print(f"  Oracle timeout:  {os.environ.get('ORACLE_TIMEOUT_SECONDS', '120')}s")
        print(f"  Fee BPS:         {os.environ.get('PLATFORM_FEE_BPS', '2000')}")

        print(f"\n[ENV] Wallet Addresses")
        print(f"  Buyer  (Agent1): {buyer_addr}")
        print(f"  Worker (Agent2): {worker_addr}")
        print(f"  Operator (Ops):  {ops_addr}")
        print(f"  Fee wallet:      {fee_addr}")

        print(f"\n[ENV] ETH Balances (for gas)")
        for label, addr in [("Buyer", buyer_addr), ("Ops", ops_addr)]:
            eth_bal = w3.eth.get_balance(addr)
            print(f"  {label}: {float(w3.from_wei(eth_bal, 'ether')):.6f} ETH")

        ops_before = query_usdc_balance(w3, ops_addr)
        worker_before = query_usdc_balance(w3, worker_addr)
        fee_before = query_usdc_balance(w3, fee_addr)
        buyer_before = query_usdc_balance(w3, buyer_addr)

        print(f"\n[ENV] USDC Balances (pre-flight)")
        print(f"  Buyer  (Agent1): {buyer_before} USDC")
        print(f"  Operator (Ops):  {ops_before} USDC")
        print(f"  Worker (Agent2): {worker_before} USDC")
        print(f"  Fee wallet:      {fee_before} USDC")

        if buyer_before < JOB_PRICE:
            pytest.skip(f"Buyer USDC too low: {buyer_before} < {JOB_PRICE}")

        print(f"\n[ENV] Test Parameters")
        print(f"  Job price:       {JOB_PRICE} USDC")
        print(f"  Fee rate:        {FEE_BPS} bps ({FEE_BPS/100}%)")
        print(f"  Worker share:    {WORKER_SHARE} USDC")
        print(f"  Fee share:       {FEE_SHARE} USDC")

        # ── Deposit-info ──
        print(f"\n{'─' * 70}")
        _api("Buyer", "GET", "/platform/deposit-info")
        print(f"  Purpose: Buyer queries chain info and gas estimate before depositing")
        rv = app_client.get('/platform/deposit-info')
        print(f"  HTTP {rv.status_code}")
        dep_info = rv.get_json()
        _pp("Response", dep_info)

        # ── Agent Registration ──
        print(f"\n{'─' * 70}")
        print(f"[T+{_elapsed(t0)}] Agent Registration (already done via fixtures)")
        _api("Buyer", "GET", f"/agents/{buyer['agent_id']}")
        rv = app_client.get(f'/agents/{buyer["agent_id"]}', headers=auth_header(buyer))
        _pp("Buyer profile", rv.get_json())

        _api("Worker", "GET", f"/agents/{worker['agent_id']}")
        rv = app_client.get(f'/agents/{worker["agent_id"]}', headers=auth_header(worker))
        _pp("Worker profile", rv.get_json())

        # ══════════════════════════════════════════════════════════════
        # Step 1: Buyer creates job
        # ══════════════════════════════════════════════════════════════
        print(f"\n{'═' * 70}")
        print(f"[T+{_elapsed(t0)}] STEP 1: Buyer creates job")
        print(f"{'═' * 70}")

        job_payload = {
            "title": "E2E Test: Write a haiku about blockchain",
            "description": "Write a creative haiku (5-7-5 syllable pattern) about "
                           "blockchain technology. The haiku should be original, "
                           "meaningful, and demonstrate understanding of the topic.",
            "rubric": "Score 80+ if: (1) correct 5-7-5 syllable pattern, "
                      "(2) relates to blockchain/crypto, (3) is creative and original. "
                      "Score below 80 if syllable count is wrong or content is generic.",
            "price": float(JOB_PRICE),
            "fee_bps": FEE_BPS,
            "max_retries": 3,
        }
        _api("Buyer", "POST", "/jobs")
        print(f"  Call chain: Buyer → POST /jobs → require_auth() → _create_job()")
        print(f"              → Job(...) → db.session.add() → db.session.commit()")
        _pp("Payload", job_payload)

        rv = app_client.post('/jobs', json=job_payload, headers=auth_header(buyer))
        print(f"  HTTP {rv.status_code}")
        job_data = rv.get_json()
        _pp("Response", job_data)
        assert rv.status_code == 201, f"Job creation failed: {job_data}"
        task_id = job_data["task_id"]

        _api("Buyer", "GET", f"/jobs/{task_id}")
        rv = app_client.get(f'/jobs/{task_id}', headers=auth_header(buyer))
        full_job = rv.get_json()
        _pp("Full job state", full_job)

        # ══════════════════════════════════════════════════════════════
        # Step 2: Buyer deposits USDC on-chain
        # ══════════════════════════════════════════════════════════════
        print(f"\n{'═' * 70}")
        print(f"[T+{_elapsed(t0)}] STEP 2: Buyer deposits USDC on-chain")
        print(f"{'═' * 70}")
        print(f"  Call chain: Buyer wallet → USDC.transfer(ops, amount)")
        print(f"              → ERC-20 Transfer event emitted → wait 12 confirmations")

        print(f"\n  From:   {buyer_addr} (Buyer/Agent1)")
        print(f"  To:     {ops_addr} (Operator)")
        print(f"  Amount: {JOB_PRICE} USDC ({int(JOB_PRICE * Decimal(10**6))} raw units)")

        usdc = get_usdc_contract(w3)
        from web3 import Web3
        raw_amount = int(JOB_PRICE * Decimal(10**6))
        try:
            gas_est = usdc.functions.transfer(
                Web3.to_checksum_address(ops_addr), raw_amount
            ).estimate_gas({"from": buyer_addr})
            gas_price = w3.eth.gas_price
            print(f"\n  Buyer calls USDC.transfer.estimateGas():")
            print(f"    Gas estimate:  {gas_est} units")
            print(f"    Gas limit:     {int(gas_est * 1.2)} units (with 20% buffer)")
            print(f"    Gas price:     {gas_price} wei ({float(Web3.from_wei(gas_price, 'gwei')):.6f} Gwei)")
            print(f"    Est. gas cost: {float(Web3.from_wei(int(gas_est * 1.2) * gas_price, 'ether')):.10f} ETH")
        except Exception as e:
            print(f"  Gas estimate failed: {e}")

        print(f"\n  Buyer calls USDC.transfer() and signs with private key...")
        t_deposit = time.time()
        agent1_key = os.environ["TEST_AGENT_WALLET_KEY_1"]
        tx_hash = send_usdc_from_agent(w3, agent1_key, ops_addr, JOB_PRICE)
        print(f"  Buyer wallet → eth.sendRawTransaction() — sent in {time.time() - t_deposit:.1f}s")
        print(f"  TX hash: {tx_hash}")

        # RPC node may briefly return "not found" after send — retry with backoff
        from web3.exceptions import TransactionNotFound
        for _attempt in range(5):
            try:
                receipt = w3.eth.get_transaction_receipt(tx_hash)
                break
            except TransactionNotFound:
                time.sleep(2)
        else:
            raise TransactionNotFound(f"Receipt still unavailable after retries: {tx_hash}")
        print(f"\n  Buyer reads eth.getTransactionReceipt():")
        print(f"    Block number:  {receipt['blockNumber']}")
        print(f"    Gas used:      {receipt['gasUsed']}")
        print(f"    Status:        {'SUCCESS' if receipt['status'] == 1 else 'REVERTED'}")
        print(f"    Logs count:    {len(receipt['logs'])}")

        transfers = usdc.events.Transfer().process_receipt(receipt)
        for i, t in enumerate(transfers):
            print(f"    Transfer event #{i}: {t['args']['from']} → {t['args']['to']} "
                  f"= {Decimal(t['args']['value']) / Decimal(10**6)} USDC")

        print(f"\n  Waiting for 12 block confirmations...")
        t_confirm = time.time()
        deadline_confirm = time.time() + 120
        while time.time() < deadline_confirm:
            current_block = w3.eth.block_number
            confirms = current_block - receipt['blockNumber']
            if confirms >= 12:
                print(f"  Confirmations: {confirms}/12 — REACHED ({time.time() - t_confirm:.1f}s)")
                break
            print(f"  Confirmations: {confirms}/12 (block {current_block})...")
            time.sleep(3)
        else:
            pytest.fail("Confirmation timeout after 120s")

        buyer_after_deposit = query_usdc_balance(w3, buyer_addr)
        ops_after_deposit = query_usdc_balance(w3, ops_addr)
        print(f"\n  Balances after deposit:")
        print(f"    Buyer:    {buyer_before} → {buyer_after_deposit} (delta: {buyer_after_deposit - buyer_before})")
        print(f"    Operator: {ops_before} → {ops_after_deposit} (delta: {ops_after_deposit - ops_before})")

        # ══════════════════════════════════════════════════════════════
        # Step 3: Buyer calls /fund
        # ══════════════════════════════════════════════════════════════
        print(f"\n{'═' * 70}")
        print(f"[T+{_elapsed(t0)}] STEP 3: Buyer calls POST /fund")
        print(f"{'═' * 70}")

        _api("Buyer", "POST", f"/jobs/{task_id}/fund")
        print(f"  Call chain: Buyer → POST /fund → require_auth() → require_buyer()")
        print(f"              → WalletService.verify_deposit(tx_hash, expected_amount)")
        print(f"                → eth.getTransactionReceipt()")
        print(f"                → check block confirmations >= 12")
        print(f"                → parse USDC Transfer events")
        print(f"                → verify recipient == ops_address && amount >= price")
        print(f"              → job.status = 'funded' → db.session.commit()")
        print(f"  DEV_MODE=false → real chain verification")

        rv = app_client.post(f'/jobs/{task_id}/fund', json={
            "tx_hash": tx_hash,
        }, headers=auth_header(buyer))
        print(f"  HTTP {rv.status_code}")
        fund_data = rv.get_json()
        _pp("Response", fund_data)
        assert rv.status_code == 200, f"Fund failed: {fund_data}"
        assert fund_data["status"] == "funded"

        _api("Buyer", "GET", f"/jobs/{task_id}")
        rv = app_client.get(f'/jobs/{task_id}', headers=auth_header(buyer))
        job_after_fund = rv.get_json()
        _pp("Job state after funding", job_after_fund)

        # ══════════════════════════════════════════════════════════════
        # Step 4: Worker claims job
        # ══════════════════════════════════════════════════════════════
        print(f"\n{'═' * 70}")
        print(f"[T+{_elapsed(t0)}] STEP 4: Worker claims job")
        print(f"{'═' * 70}")

        _api("Worker", "POST", f"/jobs/{task_id}/claim")
        print(f"  Call chain: Worker → POST /claim → require_auth()")
        print(f"              → check job.status == 'funded'")
        print(f"              → check worker != buyer (anti-self-dealing)")
        print(f"              → check worker registered & min_reputation")
        print(f"              → JobParticipant(task_id, worker_id) → db.session.commit()")

        rv = app_client.post(f'/jobs/{task_id}/claim', json={},
                             headers=auth_header(worker))
        print(f"  HTTP {rv.status_code}")
        claim_data = rv.get_json()
        _pp("Response", claim_data)
        assert rv.status_code == 200, f"Claim failed: {claim_data}"

        # ══════════════════════════════════════════════════════════════
        # Step 5: Worker submits content
        # ══════════════════════════════════════════════════════════════
        print(f"\n{'═' * 70}")
        print(f"[T+{_elapsed(t0)}] STEP 5: Worker submits content")
        print(f"{'═' * 70}")

        submission_content = (
            "Blocks chain together,\n"
            "Trustless ledger never sleeps—\n"
            "Consensus is reached."
        )
        content_bytes = len(submission_content.encode('utf-8'))

        _api("Worker", "POST", f"/jobs/{task_id}/submit")
        print(f"  Call chain: Worker → POST /submit → require_auth() → rate_limit()")
        print(f"              → check job.status == 'funded'")
        print(f"              → check content size <= 50KB ({content_bytes} bytes)")
        print(f"              → check worker is participant (JobParticipant)")
        print(f"              → check max_submissions & max_retries")
        print(f"              → Submission(status='judging') → db.session.commit()")
        print(f"              → _launch_oracle_with_timeout(submission_id)")
        print(f"                → spawns background Thread('oracle')")
        print(f"  Content ({content_bytes} bytes):")
        for line in submission_content.split('\n'):
            print(f"    | {line}")

        rv = app_client.post(f'/jobs/{task_id}/submit', json={
            "content": submission_content,
        }, headers=auth_header(worker))
        print(f"  HTTP {rv.status_code}")
        submit_data = rv.get_json()
        _pp("Response", submit_data)
        assert rv.status_code == 202, f"Submit failed: {submit_data}"
        submission_id = submit_data["submission_id"]

        print(f"\n  NOTE: HTTP 202 returned immediately. Oracle runs in background thread.")
        print(f"  Background call chain (oracle thread):")
        print(f"    _oracle_with_timeout()")
        print(f"      → _run_oracle(app, submission_id)")
        print(f"        → Submission.query.with_for_update()  (row lock)")
        print(f"        → OracleGuard.check(content)")
        print(f"          → OracleGuard.programmatic_scan()   (17 regex patterns)")
        print(f"          → OracleGuard.llm_scan()            (GPT-4o injection check)")
        print(f"        → OracleService.evaluate(title, desc, rubric, content)")
        print(f"          → Step 2: _call_llm(COMPREHENSION)")
        print(f"          → Step 3: _call_llm(COMPLETENESS)")
        print(f"          → Step 4: _call_llm(QUALITY)")
        print(f"          → Step 5: _call_llm(DEVILS_ADVOCATE)  [may skip if CLEAR_PASS]")
        print(f"          → Step 6: _call_llm(VERDICT)")
        print(f"        → if verdict == RESOLVED:")
        print(f"            → Job.update(status='resolved', winner_id=worker_id)")
        print(f"            → WalletService.payout(worker_addr, price, fee_bps)")
        print(f"              → WalletService.estimate_gas()  (real-time)")
        print(f"              → WalletService.send_usdc(worker_addr, 0.08)")
        print(f"              → WalletService.estimate_gas()  (real-time)")
        print(f"              → WalletService.send_usdc(fee_addr, 0.02)")
        print(f"            → worker.total_earned += worker_share")
        print(f"            → AgentService.update_reputation()")
        print(f"            → webhook fire_event('job.resolved')")

        # ══════════════════════════════════════════════════════════════
        # Step 6: Poll for Oracle result
        # ══════════════════════════════════════════════════════════════
        print(f"\n{'═' * 70}")
        print(f"[T+{_elapsed(t0)}] STEP 6: Polling for Oracle result")
        print(f"{'═' * 70}")
        print(f"  Oracle workflow: Guard → Comprehension → Completeness → Quality → Devil's Advocate → Verdict")
        print(f"  Submission ID: {submission_id}")
        print(f"  Timeout: {ORACLE_POLL_TIMEOUT}s, poll every {ORACLE_POLL_INTERVAL}s")

        t_oracle = time.time()
        deadline = time.time() + ORACLE_POLL_TIMEOUT
        final_status = None
        poll_count = 0
        while time.time() < deadline:
            poll_count += 1
            _api("Worker", "GET", f"/submissions/{submission_id}")
            rv = app_client.get(f'/submissions/{submission_id}',
                                headers=auth_header(worker))
            assert rv.status_code == 200
            sub = rv.get_json()
            status = sub.get("status")

            if status in ("passed", "failed"):
                oracle_elapsed = time.time() - t_oracle
                final_status = status
                print(f"\n  [Poll #{poll_count}] T+{_elapsed(t0)} — Oracle DONE ({oracle_elapsed:.1f}s)")
                print(f"  ┌────────────────────────────────────────")
                print(f"  │ Status:      {status.upper()}")
                print(f"  │ Score:       {sub.get('oracle_score')}")
                print(f"  │ Reason:      {sub.get('oracle_reason')}")
                print(f"  │ Attempt:     {sub.get('attempt')}")
                print(f"  │ Worker:      {sub.get('worker_id')}")
                print(f"  │ Task:        {sub.get('task_id')}")
                print(f"  │ Created at:  {sub.get('created_at')}")
                steps = sub.get("oracle_steps") or []
                print(f"  │ Oracle steps ({len(steps)}):")
                for s in steps:
                    step_name = s.get("name", "?")
                    step_num = s.get("step", "?")
                    passed = s.get("passed")
                    passed_str = "PASS" if passed else ("FAIL" if passed is False else "N/A")
                    print(f"  │   Step {step_num} ({step_name}): {passed_str}")
                print(f"  └────────────────────────────────────────")
                content_visible = sub.get("content")
                if content_visible and content_visible != "[redacted]":
                    print(f"  Content visible to worker: YES ({len(str(content_visible))} chars)")
                else:
                    print(f"  Content visible to worker: {content_visible}")
                break
            else:
                elapsed = time.time() - t_oracle
                print(f"    [Poll #{poll_count}] status={status} (waiting... {elapsed:.0f}s)")
            time.sleep(ORACLE_POLL_INTERVAL)
        else:
            pytest.fail(f"Oracle timed out after {ORACLE_POLL_TIMEOUT}s — last status: {status}")

        assert final_status == "passed", (
            f"Oracle rejected submission: score={sub.get('oracle_score')}, "
            f"reason={sub.get('oracle_reason')}"
        )

        # ══════════════════════════════════════════════════════════════
        # Step 7: Verify job resolved + payout
        # ══════════════════════════════════════════════════════════════
        print(f"\n{'═' * 70}")
        print(f"[T+{_elapsed(t0)}] STEP 7: Verify job resolved & payout")
        print(f"{'═' * 70}")
        print(f"  Waiting 3s for payout transactions to finalize...")
        time.sleep(3)

        _api("Buyer", "GET", f"/jobs/{task_id}")
        rv = app_client.get(f'/jobs/{task_id}', headers=auth_header(buyer))
        assert rv.status_code == 200
        job = rv.get_json()
        _pp("Full job state after resolution", job)

        assert job["status"] == "resolved", f"Job not resolved: {job['status']}"
        assert job["winner_id"] == worker["agent_id"]

        print(f"\n  Settlement details:")
        print(f"    Job status:      {job['status']}")
        print(f"    Winner:          {job['winner_id']}")
        print(f"    Payout status:   {job['payout_status']}")
        print(f"    Payout TX hash:  {job.get('payout_tx_hash')}")
        print(f"    Fee TX hash:     {job.get('fee_tx_hash')}")
        print(f"    Deposit TX hash: {job.get('deposit_tx_hash')}")
        print(f"    Fee BPS:         {job.get('fee_bps')}")

        assert job["payout_status"] == "success", f"Payout not successful: {job['payout_status']}"
        assert job["payout_tx_hash"] is not None
        assert job["fee_tx_hash"] is not None

        payout_tx = job["payout_tx_hash"]
        fee_tx = job["fee_tx_hash"]

        print(f"\n  On-chain payout TX verification (Platform → Worker):")
        try:
            p_receipt = w3.eth.get_transaction_receipt(payout_tx)
            print(f"    Block: {p_receipt['blockNumber']}, Gas used: {p_receipt['gasUsed']}, "
                  f"Status: {'OK' if p_receipt['status'] == 1 else 'REVERTED'}")
            p_transfers = usdc.events.Transfer().process_receipt(p_receipt)
            for t in p_transfers:
                print(f"    USDC Transfer: {t['args']['from']} → {t['args']['to']} "
                      f"= {Decimal(t['args']['value']) / Decimal(10**6)} USDC")
        except Exception as e:
            print(f"    Error: {e}")

        print(f"\n  On-chain fee TX verification (Platform → Fee Wallet):")
        try:
            f_receipt = w3.eth.get_transaction_receipt(fee_tx)
            print(f"    Block: {f_receipt['blockNumber']}, Gas used: {f_receipt['gasUsed']}, "
                  f"Status: {'OK' if f_receipt['status'] == 1 else 'REVERTED'}")
            f_transfers = usdc.events.Transfer().process_receipt(f_receipt)
            for t in f_transfers:
                print(f"    USDC Transfer: {t['args']['from']} → {t['args']['to']} "
                      f"= {Decimal(t['args']['value']) / Decimal(10**6)} USDC")
        except Exception as e:
            print(f"    Error: {e}")

        # ══════════════════════════════════════════════════════════════
        # Step 8: Verify on-chain balances
        # ══════════════════════════════════════════════════════════════
        print(f"\n{'═' * 70}")
        print(f"[T+{_elapsed(t0)}] STEP 8: Verify on-chain USDC balances")
        print(f"{'═' * 70}")

        ops_after = query_usdc_balance(w3, ops_addr)
        worker_after = query_usdc_balance(w3, worker_addr)
        fee_after = query_usdc_balance(w3, fee_addr)
        buyer_after = query_usdc_balance(w3, buyer_addr)

        print(f"\n  {'Wallet':<20} {'Before':>12} {'After':>12} {'Delta':>12} {'Expected':>12} {'Match':>6}")
        print(f"  {'─' * 74}")

        checks = [
            ("Buyer (Agent1)",  buyer_before,  buyer_after,  -JOB_PRICE),
            ("Operator (Ops)",  ops_before,    ops_after,    Decimal("0")),
            ("Worker (Agent2)", worker_before, worker_after, WORKER_SHARE),
            ("Fee wallet",      fee_before,    fee_after,    FEE_SHARE),
        ]
        all_match = True
        for label, before, after, expected_delta in checks:
            actual_delta = after - before
            match = actual_delta == expected_delta
            if not match:
                all_match = False
            print(f"  {label:<20} {str(before):>12} {str(after):>12} "
                  f"{str(actual_delta):>12} {str(expected_delta):>12} "
                  f"{'OK' if match else 'FAIL':>6}")

        print(f"\n  All balances match: {'YES' if all_match else 'NO'}")

        assert buyer_after == buyer_before - JOB_PRICE
        assert worker_after == worker_before + WORKER_SHARE
        assert fee_after == fee_before + FEE_SHARE
        assert ops_after == ops_before

        # ══════════════════════════════════════════════════════════════
        # Step 9: Verify worker earnings & final state
        # ══════════════════════════════════════════════════════════════
        print(f"\n{'═' * 70}")
        print(f"[T+{_elapsed(t0)}] STEP 9: Verify worker earnings & final state")
        print(f"{'═' * 70}")

        _api("Worker", "GET", f"/agents/{worker['agent_id']}")
        rv = app_client.get(f'/agents/{worker["agent_id"]}', headers=auth_header(worker))
        assert rv.status_code == 200
        worker_profile = rv.get_json()
        _pp("Worker profile (final)", worker_profile)

        earned = Decimal(str(worker_profile.get("total_earned", 0)))
        print(f"\n  total_earned: {earned} USDC (expected: {WORKER_SHARE})")
        assert earned == WORKER_SHARE

        _api("Buyer", "GET", f"/agents/{buyer['agent_id']}")
        rv = app_client.get(f'/agents/{buyer["agent_id"]}', headers=auth_header(buyer))
        _pp("Buyer profile (final)", rv.get_json())

        _api("Buyer", "GET", f"/jobs/{task_id}/submissions")
        rv = app_client.get(f'/jobs/{task_id}/submissions', headers=auth_header(buyer))
        subs_data = rv.get_json()
        print(f"\n  Submissions for this job: {subs_data.get('total', 0)}")
        for s in subs_data.get("submissions", []):
            print(f"    [{s['submission_id'][:8]}...] worker={s['worker_id']} "
                  f"status={s['status']} score={s.get('oracle_score')} "
                  f"attempt={s.get('attempt')}")

        # ══════════════════════════════════════════════════════════════
        # Summary
        # ══════════════════════════════════════════════════════════════
        total_time = time.time() - t0
        print(f"\n{'═' * 70}")
        print(f"  E2E HAPPY PATH COMPLETE")
        print(f"{'═' * 70}")
        print(f"  Total time:      {total_time:.1f}s")
        print(f"  USDC consumed:   {JOB_PRICE} (Buyer → Ops → Worker+Fee)")
        print(f"  Oracle score:    {sub.get('oracle_score')}/100")
        print(f"  Payout status:   {job['payout_status']}")
        print(f"  Balance check:   {'ALL PASS' if all_match else 'MISMATCH DETECTED'}")
        print(f"  Transactions:")
        print(f"    Deposit: {tx_hash}")
        print(f"    Payout:  {payout_tx}")
        print(f"    Fee:     {fee_tx}")
        print(f"{'═' * 70}\n")
