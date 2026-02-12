"""Unit tests for WalletService with mocked web3."""
import pytest
from unittest.mock import MagicMock, patch
from decimal import Decimal


def _make_ws():
    """Create a WalletService with mocked internals (skipping __init__)."""
    from services.wallet_service import WalletService
    ws = WalletService.__new__(WalletService)
    ws.w3 = MagicMock()
    ws.usdc_contract = MagicMock()
    ws.ops_address = '0xOPS'
    ws.ops_key = 'mock_key'
    ws.usdc_decimals = 6
    ws.w3.is_connected.return_value = True
    return ws


def test_verify_deposit_valid():
    """Valid USDC transfer to operations wallet is accepted."""
    ws = _make_ws()
    ws.w3.eth.get_transaction_receipt.return_value = {'status': 1, 'blockNumber': 100}
    ws.w3.eth.block_number = 200  # 100 confirmations (>= 12)
    ws.usdc_contract.events.Transfer.return_value.process_receipt.return_value = [
        {'args': {'from': '0xBOSS', 'to': '0xOPS', 'value': 10_000_000}}
    ]

    result = ws.verify_deposit('0xtxhash', Decimal('10.0'))
    assert result['valid'] is True
    assert result['depositor'] == '0xBOSS'
    assert result['amount'] == Decimal('10.0')


def test_verify_deposit_wrong_recipient():
    """USDC transfer to wrong address is rejected."""
    ws = _make_ws()
    ws.w3.eth.get_transaction_receipt.return_value = {'status': 1, 'blockNumber': 100}
    ws.w3.eth.block_number = 200  # 100 confirmations (>= 12)
    ws.usdc_contract.events.Transfer.return_value.process_receipt.return_value = [
        {'args': {'from': '0xBOSS', 'to': '0xWRONG', 'value': 10_000_000}}
    ]

    result = ws.verify_deposit('0xtxhash', Decimal('10.0'))
    assert result['valid'] is False


def test_verify_deposit_insufficient_amount():
    """USDC amount less than task price is rejected."""
    ws = _make_ws()
    ws.w3.eth.get_transaction_receipt.return_value = {'status': 1, 'blockNumber': 100}
    ws.w3.eth.block_number = 200  # 100 confirmations (>= 12)
    ws.usdc_contract.events.Transfer.return_value.process_receipt.return_value = [
        {'args': {'from': '0xBOSS', 'to': '0xOPS', 'value': 5_000_000}}
    ]

    result = ws.verify_deposit('0xtxhash', Decimal('10.0'))
    assert result['valid'] is False
