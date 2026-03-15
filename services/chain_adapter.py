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
