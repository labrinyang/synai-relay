"""
Base L2 USDC transfer service.
Handles: deposit verification, payout, fee transfer, refund.
Gracefully degrades when RPC/keys not configured (off-chain dev mode).
"""
import logging
import os
import threading
from decimal import Decimal

logger = logging.getLogger('relay.wallet')

# Standard USDC ERC-20 ABI (only Transfer event + transfer function needed)
USDC_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "from", "type": "address"},
            {"indexed": True, "name": "to", "type": "address"},
            {"indexed": False, "name": "value", "type": "uint256"}
        ],
        "name": "Transfer",
        "type": "event"
    },
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"}
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function"
    }
]


class WalletService:
    def __init__(self, rpc_url=None, usdc_address=None, ops_key=None, fee_address=None):
        self.rpc_url = rpc_url or os.environ.get('RPC_URL', '')
        self.usdc_address = usdc_address or os.environ.get('USDC_CONTRACT', '')
        self.ops_key = ops_key or os.environ.get('OPERATIONS_WALLET_KEY', '')
        self.fee_address = fee_address or os.environ.get('FEE_WALLET_ADDRESS', '')

        self.w3 = None
        self.usdc_contract = None
        self.ops_address = os.environ.get('OPERATIONS_WALLET_ADDRESS', '')
        self.usdc_decimals = 6
        # H5: Nonce lock for concurrent transactions
        self._tx_lock = threading.Lock()

        if self.rpc_url and self.usdc_address:
            try:
                from web3 import Web3
                self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))
                self.usdc_contract = self.w3.eth.contract(
                    address=Web3.to_checksum_address(self.usdc_address),
                    abi=USDC_ABI,
                )
                self.usdc_decimals = self.usdc_contract.functions.decimals().call()
                if self.ops_key:
                    acct = self.w3.eth.account.from_key(self.ops_key)
                    self.ops_address = acct.address
                logger.info("Connected to %s, ops=%s", self.rpc_url, self.ops_address)
            except Exception as e:
                logger.warning("Init failed: %s. Running in off-chain mode.", e)
                self.w3 = None

    def is_connected(self) -> bool:
        return self.w3 is not None and self.ops_key and self.w3.is_connected()

    def get_ops_address(self) -> str:
        return self.ops_address or ''

    def verify_deposit(self, tx_hash: str, expected_amount: Decimal) -> dict:
        """Verify a USDC deposit tx. Returns {valid, depositor, amount, error}."""
        if not self.is_connected():
            return {"valid": False, "error": "Chain not connected"}

        try:
            receipt = self.w3.eth.get_transaction_receipt(tx_hash)
            if receipt['status'] != 1:
                return {"valid": False, "error": "Transaction reverted"}

            # M13: Require minimum 12 block confirmations
            block_number = receipt.get('blockNumber', 0)
            current_block = self.w3.eth.block_number
            confirmations = current_block - block_number
            if confirmations < 12:
                return {"valid": False, "error": f"Insufficient confirmations: {confirmations}/12"}

            transfers = self.usdc_contract.events.Transfer().process_receipt(receipt)
            for t in transfers:
                to_addr = t['args']['to']
                if to_addr.lower() == self.ops_address.lower():
                    raw_amount = t['args']['value']
                    amount = Decimal(raw_amount) / Decimal(10 ** self.usdc_decimals)
                    if amount >= expected_amount:
                        result = {
                            "valid": True,
                            "depositor": t['args']['from'],
                            "amount": amount,
                        }
                        # G22: Flag overpayment
                        if amount > expected_amount:
                            overpayment = amount - expected_amount
                            result["overpayment"] = float(overpayment)
                            logger.warning(
                                "Overpayment detected: tx=%s amount=%s expected=%s excess=%s",
                                tx_hash, amount, expected_amount, overpayment,
                            )
                        return result
                    else:
                        return {"valid": False, "error": f"Amount {amount} < {expected_amount}"}

            return {"valid": False, "error": "No USDC transfer to operations wallet found"}
        except Exception as e:
            return {"valid": False, "error": str(e)}

    def send_usdc(self, to_address: str, amount: Decimal) -> str:
        """Send USDC from operations wallet. Returns tx_hash."""
        if not self.is_connected() or not self.ops_key:
            raise RuntimeError("Chain not connected or ops key missing")

        from web3 import Web3
        raw_amount = int(amount * Decimal(10 ** self.usdc_decimals))
        to_addr = Web3.to_checksum_address(to_address)

        # H5: Lock to prevent nonce collisions on concurrent transactions
        with self._tx_lock:
            tx = self.usdc_contract.functions.transfer(to_addr, raw_amount).build_transaction({
                'from': self.ops_address,
                'nonce': self.w3.eth.get_transaction_count(self.ops_address),
                'gas': 100_000,
                'gasPrice': self.w3.eth.gas_price,
            })
            signed = self.w3.eth.account.sign_transaction(tx, self.ops_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        if receipt['status'] != 1:
            raise RuntimeError(f"USDC transfer reverted: {tx_hash.hex()}")

        return tx_hash.hex()

    def payout(self, worker_address: str, task_price: Decimal, fee_bps: int = 2000) -> dict:
        """Send worker share to worker, fee share to fee wallet. Returns tx hashes.
        fee_bps: fee in basis points (2000 = 20%, 500 = 5%)."""
        fee_rate = Decimal(fee_bps) / Decimal(10000)
        worker_amount = task_price * (Decimal(1) - fee_rate)
        fee_amount = task_price * fee_rate

        payout_tx = self.send_usdc(worker_address, worker_amount)
        try:
            fee_tx = self.send_usdc(self.fee_address, fee_amount)
        except Exception as e:
            # Worker paid but fee failed — log and return partial result
            logger.warning("Fee transfer failed after payout: %s", e)
            return {"payout_tx": payout_tx, "fee_tx": None, "fee_error": str(e)}

        return {"payout_tx": payout_tx, "fee_tx": fee_tx}

    def refund(self, depositor_address: str, amount: Decimal) -> str:
        """Refund full amount to depositor. Returns tx_hash."""
        return self.send_usdc(depositor_address, amount)


# Singleton
_wallet_service = None

def get_wallet_service() -> WalletService:
    global _wallet_service
    if _wallet_service is None:
        _wallet_service = WalletService()
    return _wallet_service
