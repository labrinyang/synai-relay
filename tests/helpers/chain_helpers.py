"""
Chain helper utilities for on-chain tests.
Provides: send_usdc_from_agent(), query_usdc_balance(), wait_confirmations().
"""
import os
import time
from decimal import Decimal
from web3 import Web3
from web3.exceptions import TransactionNotFound

USDC_ABI = [
    {
        "inputs": [{"name": "to", "type": "address"}, {"name": "value", "type": "uint256"}],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "from", "type": "address"},
            {"indexed": True, "name": "to", "type": "address"},
            {"indexed": False, "name": "value", "type": "uint256"},
        ],
        "name": "Transfer",
        "type": "event",
    },
]


def get_web3():
    """Get a connected Web3 instance from .env RPC_URL."""
    rpc_url = os.environ.get("RPC_URL", "")
    if not rpc_url:
        raise RuntimeError("RPC_URL not set")
    return Web3(Web3.HTTPProvider(rpc_url))


def get_usdc_contract(w3):
    """Get the USDC contract instance."""
    usdc_addr = os.environ.get("USDC_CONTRACT", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
    return w3.eth.contract(address=Web3.to_checksum_address(usdc_addr), abi=USDC_ABI)


def query_usdc_balance(w3, address: str) -> Decimal:
    """Query USDC balance for an address. Returns human-readable Decimal (e.g., 1.5)."""
    usdc = get_usdc_contract(w3)
    raw = usdc.functions.balanceOf(Web3.to_checksum_address(address)).call()
    return Decimal(raw) / Decimal(10**6)  # USDC has 6 decimals on Base


def send_usdc_from_agent(w3, agent_key: str, to_address: str, amount: Decimal) -> str:
    """Send USDC from an agent wallet (not ops). Returns tx_hash hex string.
    Used for test setup (e.g., Agent1 deposits to Ops).
    """
    usdc = get_usdc_contract(w3)
    acct = w3.eth.account.from_key(agent_key)
    raw_amount = int(amount * Decimal(10**6))  # USDC has 6 decimals on Base

    nonce = w3.eth.get_transaction_count(acct.address, "pending")
    gas_estimate = usdc.functions.transfer(
        Web3.to_checksum_address(to_address), raw_amount
    ).estimate_gas({"from": acct.address})
    gas_limit = int(gas_estimate * 1.2)  # 20% buffer
    gas_price = w3.eth.gas_price
    tx = usdc.functions.transfer(
        Web3.to_checksum_address(to_address), raw_amount
    ).build_transaction({
        "from": acct.address,
        "nonce": nonce,
        "gas": gas_limit,
        "gasPrice": gas_price,
    })
    signed = w3.eth.account.sign_transaction(tx, agent_key)
    tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    if receipt["status"] != 1:
        raise RuntimeError(f"Agent USDC transfer reverted: {tx_hash.hex()}")
    return tx_hash.hex()


def wait_confirmations(w3, tx_hash: str, min_confirms: int = 12, timeout: int = 60):
    """Wait until a tx has at least min_confirms confirmations.
    Base L2 block time ~2s, so 12 confirms ~24s.
    """
    if isinstance(tx_hash, str):
        tx_hash_bytes = bytes.fromhex(tx_hash.replace("0x", ""))
    else:
        tx_hash_bytes = tx_hash

    deadline = time.time() + timeout
    confirms = 0
    while time.time() < deadline:
        try:
            receipt = w3.eth.get_transaction_receipt(tx_hash_bytes)
        except TransactionNotFound:
            time.sleep(2)
            continue
        if receipt is None:
            time.sleep(2)
            continue
        current = w3.eth.block_number
        confirms = current - receipt["blockNumber"]
        if confirms >= min_confirms:
            return confirms
        time.sleep(2)
    raise TimeoutError(f"Tx {tx_hash} only has {confirms}/{min_confirms} confirmations after {timeout}s")
