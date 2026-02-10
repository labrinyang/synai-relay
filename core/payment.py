import time

class PaymentSystem:
    def __init__(self, mode="mock"):
        self.mode = mode
        self.ledger = {} # agent_id -> balance

    def deposit(self, agent_id, amount):
        current = self.ledger.get(agent_id, 0.0)
        self.ledger[agent_id] = current + float(amount)
        return True

    def create_escrow(self, buyer_id, seller_id, amount, task_id):
        print(f"[Payment] Creating escrow: {amount} from {buyer_id} to {seller_id} for Task {task_id}")
        # In mock mode, we just check if buyer has enough "imaginary" money
        # or just assume they do for testing.
        return f"escrow_{task_id}_{int(time.time())}"

    def release_payment(self, escrow_id):
        print(f"[Payment] Payment released for escrow {escrow_id}")
        return True

    def refund_payment(self, escrow_id):
        print(f"[Payment] Payment refunded for escrow {escrow_id}")
        return True

if __name__ == "__main__":
    p = PaymentSystem()
    eid = p.create_escrow("agent_A", "agent_B", "0.01", "task_123")
    p.release_payment(eid)
