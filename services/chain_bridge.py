"""
ChainBridge: web3.py wrapper for TaskEscrow and CVSOracle contract interaction.
Handles tx signing, event reading, and state sync.
Gracefully degrades when env vars are missing (off-chain mode).
"""
import os
import json
from web3 import Web3
from eth_account import Account


class ChainBridge:
    def __init__(self):
        self.rpc_url = os.getenv('RPC_URL', 'http://127.0.0.1:8545')
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))

        self.escrow_address = os.getenv('TASK_ESCROW_ADDRESS')
        self.oracle_address = os.getenv('CVS_ORACLE_ADDRESS')
        self.oracle_private_key = os.getenv('ORACLE_PRIVATE_KEY')

        self._escrow_abi = self._load_abi('TaskEscrow')
        self._oracle_abi = self._load_abi('CVSOracle')

        self.escrow = None
        self.oracle = None

        if self.escrow_address and self._escrow_abi:
            self.escrow = self.w3.eth.contract(
                address=self.escrow_address,
                abi=self._escrow_abi
            )
        if self.oracle_address and self._oracle_abi:
            self.oracle = self.w3.eth.contract(
                address=self.oracle_address,
                abi=self._oracle_abi
            )

    def _load_abi(self, contract_name):
        """Load ABI from Foundry output."""
        abi_path = os.path.join(
            os.path.dirname(__file__), '..', 'contracts', 'out',
            f'{contract_name}.sol', f'{contract_name}.json'
        )
        if not os.path.exists(abi_path):
            return None
        with open(abi_path) as f:
            data = json.load(f)
        return data.get('abi', [])

    def is_connected(self):
        """Check if RPC is reachable and contracts are configured."""
        try:
            return (
                self.w3.is_connected()
                and self.escrow is not None
                and self.oracle is not None
            )
        except Exception:
            return False

    # --- Read functions ---

    def get_task(self, chain_task_id: str) -> dict:
        task_id_bytes = bytes.fromhex(chain_task_id.replace('0x', ''))
        result = self.escrow.functions.getTask(task_id_bytes).call()
        return {
            'boss': result[0],
            'expiry': result[1],
            'status': result[2],
            'maxRetries': result[3],
            'retryCount': result[4],
            'worker': result[5],
            'amount': result[6],
            'contentHash': result[7].hex(),
        }

    def get_pending_withdrawal(self, address: str) -> int:
        return self.escrow.functions.pendingWithdrawals(address).call()

    # --- Write functions ---

    def _send_tx(self, private_key, fn, value=0):
        account = Account.from_key(private_key)
        nonce = self.w3.eth.get_transaction_count(account.address)
        tx = fn.build_transaction({
            'from': account.address,
            'nonce': nonce,
            'gas': 500_000,
            'gasPrice': self.w3.eth.gas_price,
            'value': value,
        })
        signed = self.w3.eth.account.sign_transaction(tx, private_key)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        return receipt

    def create_task(self, boss_key, amount, expiry, content_hash, max_retries=3):
        fn = self.escrow.functions.createTask(amount, expiry, content_hash, max_retries)
        receipt = self._send_tx(boss_key, fn)
        logs = self.escrow.events.TaskCreated().process_receipt(receipt)
        if logs:
            return '0x' + logs[0]['args']['taskId'].hex()
        raise RuntimeError("TaskCreated event not found in receipt")

    def fund_task(self, boss_key, chain_task_id):
        task_id_bytes = bytes.fromhex(chain_task_id.replace('0x', ''))
        fn = self.escrow.functions.fundTask(task_id_bytes)
        receipt = self._send_tx(boss_key, fn)
        return receipt.transactionHash.hex()

    def claim_task(self, worker_key, chain_task_id):
        task_id_bytes = bytes.fromhex(chain_task_id.replace('0x', ''))
        fn = self.escrow.functions.claimTask(task_id_bytes)
        receipt = self._send_tx(worker_key, fn)
        return receipt.transactionHash.hex()

    def submit_result(self, worker_key, chain_task_id, result_hash):
        task_id_bytes = bytes.fromhex(chain_task_id.replace('0x', ''))
        fn = self.escrow.functions.submitResult(task_id_bytes, result_hash)
        receipt = self._send_tx(worker_key, fn)
        return receipt.transactionHash.hex()

    def submit_verdict(self, chain_task_id, accepted, score, evidence_hash):
        task_id_bytes = bytes.fromhex(chain_task_id.replace('0x', ''))
        fn = self.oracle.functions.submitVerdict(
            task_id_bytes, accepted, score, evidence_hash
        )
        receipt = self._send_tx(self.oracle_private_key, fn)
        return receipt.transactionHash.hex()

    def settle(self, chain_task_id, caller_key):
        task_id_bytes = bytes.fromhex(chain_task_id.replace('0x', ''))
        fn = self.escrow.functions.settle(task_id_bytes)
        receipt = self._send_tx(caller_key, fn)
        return receipt.transactionHash.hex()

    def mark_expired(self, chain_task_id, caller_key):
        task_id_bytes = bytes.fromhex(chain_task_id.replace('0x', ''))
        fn = self.escrow.functions.markExpired(task_id_bytes)
        receipt = self._send_tx(caller_key, fn)
        return receipt.transactionHash.hex()

    def refund(self, boss_key, chain_task_id):
        task_id_bytes = bytes.fromhex(chain_task_id.replace('0x', ''))
        fn = self.escrow.functions.refund(task_id_bytes)
        receipt = self._send_tx(boss_key, fn)
        return receipt.transactionHash.hex()

    def cancel_task(self, boss_key, chain_task_id):
        task_id_bytes = bytes.fromhex(chain_task_id.replace('0x', ''))
        fn = self.escrow.functions.cancelTask(task_id_bytes)
        receipt = self._send_tx(boss_key, fn)
        return receipt.transactionHash.hex()

    def withdraw(self, caller_key):
        fn = self.escrow.functions.withdraw()
        receipt = self._send_tx(caller_key, fn)
        return receipt.transactionHash.hex()


# Singleton — constructed lazily, tolerates missing env vars
_bridge_instance = None

def get_chain_bridge() -> ChainBridge:
    global _bridge_instance
    if _bridge_instance is None:
        _bridge_instance = ChainBridge()
    return _bridge_instance
