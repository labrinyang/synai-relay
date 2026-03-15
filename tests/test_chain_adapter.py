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
