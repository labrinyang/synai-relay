"""x402 route-level helpers: build requirements, parse chain IDs, record access."""
import logging
from decimal import Decimal

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
    except ValueError:
        raise ValueError(f"Invalid CAIP-2 network: {network!r}")


def build_requirements(amount_usdc: Decimal, pay_to: str,
                       adapters: list) -> list:
    """Build PaymentRequirements for all supported chains."""
    try:
        from x402 import PaymentRequirements
    except ImportError:
        raise RuntimeError("x402 SDK required for build_requirements")
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
