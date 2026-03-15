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
        return ''

    def verify_deposit(self, tx_hash: str, expected_amount: Decimal) -> DepositResult:
        logger.warning("XLayerAdapter.verify_deposit not fully implemented")
        return DepositResult(valid=False, error="X Layer deposit verification not yet implemented")

    def payout(self, to_address: str, amount: Decimal, fee_bps: int) -> PayoutResult:
        logger.warning("XLayerAdapter.payout not fully implemented")
        return PayoutResult(error="X Layer payout not yet implemented")

    def refund(self, to_address: str, amount: Decimal) -> RefundResult:
        logger.warning("XLayerAdapter.refund not fully implemented")
        return RefundResult(error="X Layer refund not yet implemented")
