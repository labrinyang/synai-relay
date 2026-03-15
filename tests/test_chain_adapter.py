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
