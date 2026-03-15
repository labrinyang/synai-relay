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
