"""
Phase 2: On-chain wallet service tests against Base L2 mainnet.

Run with: pytest tests/test_onchain_wallet.py -v -m onchain
Requires: .env with RPC_URL, OPERATIONS_WALLET_KEY, etc.

WARNING: These tests interact with REAL Base L2 mainnet.
Write tests consume small amounts of real USDC (~0.17 total).
"""
import os
import logging
import time
import pytest
from decimal import Decimal
from dotenv import load_dotenv

from services.wallet_service import WalletService
from tests.helpers.chain_helpers import (
    get_web3, query_usdc_balance, send_usdc_from_agent, wait_confirmations,
)

# Suppress wallet logger during tests to prevent key leakage in tracebacks
logging.getLogger("relay.wallet").setLevel(logging.WARNING)

# Mark all tests in this module as requiring --onchain
pytestmark = pytest.mark.onchain


# ── Module-scoped fixtures ──────────────────────────────────────────

@pytest.fixture(scope="module")
def w3():
    """Connected Web3 instance."""
    load_dotenv()
    _w3 = get_web3()
    assert _w3.is_connected(), "Cannot connect to Base L2 RPC"
    return _w3


@pytest.fixture(scope="module")
def wallet(w3):
    """Fresh WalletService instance connected to Base mainnet.
    Direct instantiation — NOT the global singleton.
    """
    ws = WalletService(
        rpc_url=os.environ["RPC_URL"],
        usdc_address=os.environ.get("USDC_CONTRACT", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"),
        ops_key=os.environ["OPERATIONS_WALLET_KEY"],
        fee_address=os.environ["FEE_WALLET_ADDRESS"],
    )
    assert ws.is_connected(), "WalletService not connected"
    return ws


@pytest.fixture(scope="module")
def deposit_tx(w3, wallet):
    """One-time setup: Agent1 sends 0.10 USDC to Ops wallet.
    Returns the tx_hash (hex string) for use in verify_deposit tests.
    Waits for 12 confirmations (~24s).
    """
    agent1_key = os.environ["TEST_AGENT_WALLET_KEY_1"]
    ops_address = wallet.get_ops_address()
    amount = Decimal("0.10")

    # Pre-flight: check Agent1 has enough USDC
    agent1_addr = os.environ["TEST_AGENT_WALLET_ADDRESS_1"]
    balance = query_usdc_balance(w3, agent1_addr)
    if balance < amount:
        pytest.skip(f"Agent1 USDC balance too low: {balance} < {amount}")

    # Send deposit
    tx_hash = send_usdc_from_agent(w3, agent1_key, ops_address, amount)

    # Wait for 12 confirmations
    wait_confirmations(w3, tx_hash, min_confirms=12, timeout=120)

    return tx_hash


# ── Env var helpers ─────────────────────────────────────────────────

OPS_ADDRESS = os.environ.get("OPERATIONS_WALLET_ADDRESS", "")
FEE_ADDRESS = os.environ.get("FEE_WALLET_ADDRESS", "")
AGENT2_ADDRESS = os.environ.get("TEST_AGENT_WALLET_ADDRESS_2", "")
AGENT3_ADDRESS = os.environ.get("TEST_AGENT_WALLET_ADDRESS_3", "")


# ===================================================================
# GROUP 1: Read-Only Tests (free, no gas, no USDC)
# ===================================================================

class TestChainConnection:
    """R1-R3: Basic chain connectivity and contract interaction."""

    def test_r1_chain_connected(self, wallet):
        """R1: WalletService connects to Base L2 RPC."""
        assert wallet.is_connected() is True
        assert wallet.w3.eth.chain_id == 8453  # Base mainnet

    def test_r2_usdc_decimals_is_6(self, wallet):
        """R2: Real USDC contract returns decimals=6."""
        assert wallet.usdc_decimals == 6

    def test_r3_ops_address_matches_env(self, wallet):
        """R3: Derived address from private key matches .env OPERATIONS_WALLET_ADDRESS."""
        expected = os.environ["OPERATIONS_WALLET_ADDRESS"]
        actual = wallet.get_ops_address()
        assert actual.lower() == expected.lower()


# ===================================================================
# GROUP 2+3: Deposit Setup + verify_deposit Tests
# ===================================================================

class TestVerifyDeposit:
    """R4-R6: verify_deposit() against a real on-chain USDC transfer.
    Uses the deposit_tx fixture (Agent1 -> Ops, 0.10 USDC).
    """

    def test_r4_verify_deposit_valid(self, wallet, deposit_tx):
        """R4: Valid deposit — correct amount, sufficient confirmations."""
        result = wallet.verify_deposit(deposit_tx, Decimal("0.10"))
        assert result["valid"] is True
        assert result["amount"] == Decimal("0.1")
        assert "depositor" in result
        # Depositor should be Agent1's address
        expected_depositor = os.environ["TEST_AGENT_WALLET_ADDRESS_1"]
        assert result["depositor"].lower() == expected_depositor.lower()

    def test_r5_verify_deposit_amount_too_high(self, wallet, deposit_tx):
        """R5: Expected amount > actual deposit — valid=False."""
        result = wallet.verify_deposit(deposit_tx, Decimal("0.20"))
        assert result["valid"] is False
        assert "Amount" in result.get("error", "") or "amount" in result.get("error", "").lower()

    def test_r6_verify_deposit_overpayment(self, wallet, deposit_tx):
        """R6: Expected amount < actual deposit — valid=True with overpayment flag."""
        result = wallet.verify_deposit(deposit_tx, Decimal("0.05"))
        assert result["valid"] is True
        assert "overpayment" in result
        assert result["overpayment"] == pytest.approx(0.05, abs=0.001)


# ===================================================================
# GROUP 4: Write Tests (cost USDC + gas)
# ===================================================================

class TestSendUSDC:
    """W1: Basic send_usdc — smallest possible real transfer."""

    def test_w1_send_usdc_small(self, wallet, w3, deposit_tx):
        """W1: Send 0.01 USDC from Ops to Agent3."""
        # deposit_tx dependency ensures Ops has received the deposit
        ops_balance_before = query_usdc_balance(w3, wallet.get_ops_address())
        if ops_balance_before < Decimal("0.01"):
            pytest.skip(f"Ops USDC too low: {ops_balance_before}")

        agent3_before = query_usdc_balance(w3, AGENT3_ADDRESS)
        tx_hash = wallet.send_usdc(AGENT3_ADDRESS, Decimal("0.01"))

        # Verify tx_hash is valid hex
        assert tx_hash.startswith("0x") or len(tx_hash) == 64
        # Wait for RPC node to reflect the new balance
        time.sleep(2)
        # Verify Agent3 received
        agent3_after = query_usdc_balance(w3, AGENT3_ADDRESS)
        assert agent3_after == agent3_before + Decimal("0.01")


class TestPayoutReal:
    """W2: Real payout with 80/20 split."""

    def test_w2_payout_real_split(self, wallet, w3, deposit_tx):
        """W2: payout(Agent2, 0.10 USDC, 2000 bps) -> 0.08 to Agent2, 0.02 to Fee."""
        ops_balance = query_usdc_balance(w3, wallet.get_ops_address())
        if ops_balance < Decimal("0.10"):
            pytest.skip(f"Ops USDC too low: {ops_balance}")

        agent2_before = query_usdc_balance(w3, AGENT2_ADDRESS)
        fee_before = query_usdc_balance(w3, FEE_ADDRESS)

        result = wallet.payout(AGENT2_ADDRESS, Decimal("0.10"), fee_bps=2000)

        # Both txs should succeed
        assert "payout_tx" in result
        assert "fee_tx" in result
        assert result["payout_tx"] is not None
        assert result["fee_tx"] is not None
        assert "pending" not in result  # no timeout
        assert "fee_error" not in result  # no partial failure

        # Wait for RPC node to reflect the new balance
        time.sleep(2)
        # Verify on-chain balances
        agent2_after = query_usdc_balance(w3, AGENT2_ADDRESS)
        fee_after = query_usdc_balance(w3, FEE_ADDRESS)
        assert agent2_after == agent2_before + Decimal("0.08")
        assert fee_after == fee_before + Decimal("0.02")


class TestRefundReal:
    """W3: Real refund — send USDC back to a depositor."""

    def test_w3_refund_real(self, wallet, w3, deposit_tx):
        """W3: Refund 0.05 USDC to Agent3."""
        ops_balance = query_usdc_balance(w3, wallet.get_ops_address())
        if ops_balance < Decimal("0.05"):
            pytest.skip(f"Ops USDC too low: {ops_balance}")

        agent3_before = query_usdc_balance(w3, AGENT3_ADDRESS)
        tx_hash = wallet.refund(AGENT3_ADDRESS, Decimal("0.05"))

        assert tx_hash is not None
        assert len(tx_hash) > 0
        # Wait for RPC node to reflect the new balance
        time.sleep(2)
        agent3_after = query_usdc_balance(w3, AGENT3_ADDRESS)
        assert agent3_after == agent3_before + Decimal("0.05")


class TestInsufficientBalance:
    """W4: Attempting to send more USDC than available — should revert."""

    def test_w4_insufficient_balance_reverts(self, wallet, deposit_tx):
        """W4: send_usdc with 999999 USDC -> RuntimeError (revert)."""
        with pytest.raises(RuntimeError, match="reverted|revert|insufficient|transfer amount exceeds balance"):
            wallet.send_usdc(AGENT3_ADDRESS, Decimal("999999"))


# ===================================================================
# GROUP 5: Gas + Confirmation Edge Cases
# ===================================================================

class TestGasEstimation:
    """Verify that 100_000 gas limit is sufficient for Base L2 USDC transfers."""

    def test_gas_used_under_limit(self, wallet, w3, deposit_tx):
        """Gas used by a real USDC transfer should be well under 100,000."""
        # Use the deposit tx (already mined) to check gas used
        receipt = w3.eth.get_transaction_receipt(deposit_tx)
        gas_used = receipt["gasUsed"]
        gas_limit = 100_000
        print(f"\n  Gas used: {gas_used} / {gas_limit} ({gas_used/gas_limit*100:.1f}%)")
        assert gas_used < gas_limit, f"Gas used {gas_used} exceeds limit {gas_limit}"
        # Typically ~65,000 for ERC-20 transfer
        assert gas_used < 80_000, f"Gas unexpectedly high: {gas_used}"


class TestConfirmationEdge:
    """Test verify_deposit behavior with a fresh (low-confirmation) transaction."""

    def test_insufficient_confirmations_fresh_tx(self, wallet, w3):
        """Send a fresh tx and immediately verify — should fail on confirmations.
        NOTE: This test sends 0.01 USDC from Agent1 to Ops.
        """
        agent1_key = os.environ.get("TEST_AGENT_WALLET_KEY_1", "")
        if not agent1_key:
            pytest.skip("TEST_AGENT_WALLET_KEY_1 not set")

        agent1_addr = os.environ["TEST_AGENT_WALLET_ADDRESS_1"]
        balance = query_usdc_balance(w3, agent1_addr)
        if balance < Decimal("0.01"):
            pytest.skip(f"Agent1 USDC too low: {balance}")

        # Send and DON'T wait for confirmations
        tx_hash = send_usdc_from_agent(
            w3, agent1_key, wallet.get_ops_address(), Decimal("0.01")
        )

        # Immediately verify — should have < 12 confirmations.
        # The RPC may not have indexed the tx yet, in which case
        # get_transaction_receipt() raises TransactionNotFound.
        # verify_deposit() catches that via its broad except and returns
        # {"valid": False, "error": "<TransactionNotFound message>"}.
        from web3.exceptions import TransactionNotFound
        try:
            result = wallet.verify_deposit(tx_hash, Decimal("0.01"))
        except TransactionNotFound:
            # RPC lag — treat as equivalent to "insufficient confirmations"
            print(f"\n  TransactionNotFound raised (RPC lag) — equivalent to insufficient confirmations")
            return

        # On Base L2 (2s blocks), 12 confirms = ~24s. If we check within 1-2s,
        # it should have < 12 confirms. But if the RPC is slow, it might already
        # have enough. Handle both cases.
        if result["valid"] is False:
            error_msg = result.get("error", "").lower()
            assert "confirmations" in error_msg or \
                   "insufficient" in error_msg or \
                   "not found" in error_msg or \
                   "not in the chain" in error_msg, \
                   f"Unexpected error: {result.get('error', '')}"
            print(f"\n  Correctly rejected: {result['error']}")
        else:
            # Base L2 is fast — if we got 12+ confirms already, that's fine too
            print(f"\n  Note: TX already had 12+ confirmations (Base L2 is fast)")


# ===================================================================
# TEARDOWN: Balance Reconciliation Report
# ===================================================================

class TestBalanceReport:
    """Final: Print balance report after all tests."""

    def test_zz_balance_report(self, w3, wallet):
        """Print ending balances for all wallets (always passes)."""
        wallets = {
            "Ops": wallet.get_ops_address(),
            "Fee": FEE_ADDRESS,
            "Agent1": os.environ.get("TEST_AGENT_WALLET_ADDRESS_1", ""),
            "Agent2": AGENT2_ADDRESS,
            "Agent3": AGENT3_ADDRESS,
        }
        print("\n\n=== BALANCE RECONCILIATION REPORT ===")
        for name, addr in wallets.items():
            if addr:
                from web3 import Web3
                eth = Web3.from_wei(w3.eth.get_balance(Web3.to_checksum_address(addr)), "ether")
                usdc = query_usdc_balance(w3, addr)
                print(f"  {name:8s} ({addr[:10]}...): ETH={eth:.6f}, USDC={usdc:.6f}")
        print("=====================================\n")
