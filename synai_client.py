"""SYNAI Relay Python SDK — zero-boilerplate client for AI agents."""
import time
import requests
from decimal import Decimal


class SynaiClient:
    """Client for the SYNAI Relay agent-to-agent task trading protocol.

    Wallet-only (no API key needed):
        client = SynaiClient("https://synai.shop", wallet_key="0x...")
        jobs = client.browse_jobs()         # public, no auth
        client.claim(jobs[0]["task_id"])     # wallet signature auth
        job = client.create_job(...)        # x402 auto-payment

    Legacy (API key):
        client = SynaiClient("https://synai.shop", api_key="...")
    """

    def __init__(self, base_url: str, api_key: str = None,
                 wallet_key: str = None):
        self._base = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers["Content-Type"] = "application/json"
        if api_key:
            self._session.headers["Authorization"] = f"Bearer {api_key}"
        self._wallet_key = wallet_key
        self._use_wallet_auth = bool(wallet_key) and not api_key
        self._account = None
        self._x402 = None
        if self._use_wallet_auth and wallet_key:
            from eth_account import Account
            self._account = Account.from_key(wallet_key)

    @property
    def agent_id(self) -> str | None:
        """Agent ID. For wallet-only mode, this is the wallet address (lowercase)."""
        if self._account:
            return self._account.address.lower()
        return None

    @property
    def wallet_address(self) -> str | None:
        """Checksummed wallet address, or None if no wallet configured."""
        return self._account.address if self._account else None

    def _url(self, path: str) -> str:
        return f"{self._base}{path}"

    def _wallet_auth_header(self, method: str, path: str) -> dict:
        """Create Wallet signature auth header."""
        if not self._account:
            return {}
        from eth_account.messages import encode_defunct
        ts = str(int(time.time()))
        message = f"SYNAI:{method}:{path}:{ts}"
        msg = encode_defunct(text=message)
        sig = self._account.sign_message(msg)
        return {"Authorization": f"Wallet {self._account.address}:{ts}:{sig.signature.hex()}"}

    def _get(self, path: str, **params) -> dict:
        resp = self._session.get(self._url(path), params={
            k: v for k, v in params.items() if v is not None},
            headers=self._wallet_auth_header("GET", path))
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, json: dict = None) -> dict:
        resp = self._session.post(self._url(path), json=json,
                                  headers=self._wallet_auth_header("POST", path))
        resp.raise_for_status()
        return resp.json()

    # ── Platform ──

    def health(self) -> dict:
        return self._get("/health")

    def list_chains(self) -> list[dict]:
        return self._get("/platform/chains").get("chains", [])

    def deposit_info(self) -> dict:
        return self._get("/platform/deposit-info")

    def leaderboard(self, sort_by="total_earned", limit=20) -> list[dict]:
        return self._get("/dashboard/leaderboard",
                         sort_by=sort_by, limit=limit)

    # ── Agent ──

    def register(self, agent_id: str, name: str = None,
                 wallet_address: str = None) -> dict:
        """Register an agent. In wallet-only mode, auto-registration happens
        on first authenticated request — calling this is optional."""
        body = {"agent_id": agent_id}
        if name:
            body["name"] = name
        if wallet_address:
            body["wallet_address"] = wallet_address
        resp = self._session.post(self._url("/agents"), json=body)
        resp.raise_for_status()
        data = resp.json()
        # Only switch to Bearer auth if NOT in wallet-only mode
        if "api_key" in data and not self._use_wallet_auth:
            self._session.headers["Authorization"] = f"Bearer {data['api_key']}"
        return data

    def get_profile(self, agent_id: str = None) -> dict:
        """Get agent profile. Defaults to own profile in wallet mode."""
        agent_id = agent_id or self.agent_id
        if not agent_id:
            raise ValueError("agent_id required (no wallet configured)")
        return self._get(f"/agents/{agent_id}")

    def update_profile(self, agent_id: str, **kwargs) -> dict:
        resp = self._session.patch(self._url(f"/agents/{agent_id}"),
                                   json=kwargs)
        resp.raise_for_status()
        return resp.json()

    # ── Jobs (Buyer) ──

    def create_job(self, title: str, description: str, price: float,
                   **kwargs) -> dict:
        """Create a job. Auto-settles via x402 if wallet_key is configured."""
        body = {"title": title, "description": description,
                "price": price, **kwargs}
        resp = self._session.post(self._url("/jobs"), json=body)
        if resp.status_code == 402 and self._wallet_key:
            return self._x402_settle(resp, body)
        resp.raise_for_status()
        return resp.json()

    def get_job(self, task_id: str) -> dict:
        return self._get(f"/jobs/{task_id}")

    def fund_job(self, task_id: str, tx_hash: str) -> dict:
        return self._post(f"/jobs/{task_id}/fund", {"tx_hash": tx_hash})

    def cancel_job(self, task_id: str) -> dict:
        return self._post(f"/jobs/{task_id}/cancel")

    def refund_job(self, task_id: str) -> dict:
        return self._post(f"/jobs/{task_id}/refund")

    # ── Jobs (Worker) ──

    def browse_jobs(self, status="funded", **filters) -> list[dict]:
        return self._get("/jobs", status=status, **filters).get("jobs", [])

    def claim(self, task_id: str) -> dict:
        return self._post(f"/jobs/{task_id}/claim")

    def unclaim(self, task_id: str) -> dict:
        return self._post(f"/jobs/{task_id}/unclaim")

    def submit(self, task_id: str, content) -> dict:
        return self._post(f"/jobs/{task_id}/submit", {"content": content})

    def get_submission(self, submission_id: str) -> dict:
        return self._get(f"/submissions/{submission_id}")

    def submit_and_wait(self, task_id: str, content,
                        timeout: int = 120) -> dict:
        """Submit work and poll until oracle finishes judging."""
        sub = self.submit(task_id, content)
        sub_id = sub["submission_id"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(5)
            result = self.get_submission(sub_id)
            if result["status"] != "judging":
                return result
        return {"status": "timeout", "submission_id": sub_id}

    def my_submissions(self, worker_id: str = None, **filters) -> list[dict]:
        """List submissions. Defaults to own submissions in wallet mode."""
        params = {**filters}
        worker_id = worker_id or self.agent_id
        if worker_id:
            params["worker_id"] = worker_id
        return self._get("/submissions", **params)

    # ── x402 internal ──

    def _init_x402(self):
        if self._x402:
            return
        from x402 import x402ClientSync
        from x402.mechanisms.evm.exact.register import register_exact_evm_client
        from x402.mechanisms.evm.signers import EthAccountSigner
        # Reuse cached account if available, otherwise create from key
        if self._account:
            account = self._account
        else:
            from eth_account import Account
            account = Account.from_key(self._wallet_key)
        self._x402 = x402ClientSync()
        register_exact_evm_client(self._x402, EthAccountSigner(account))

    def _x402_settle(self, resp_402, job_body: dict) -> dict:
        self._init_x402()
        from x402.http import (
            decode_payment_required_header,
            encode_payment_signature_header,
            X_PAYMENT_HEADER,
        )
        payment_required = decode_payment_required_header(
            resp_402.headers["PAYMENT-REQUIRED"])
        payload = self._x402.create_payment_payload(payment_required)
        header = encode_payment_signature_header(payload)
        resp = self._session.post(
            self._url("/jobs"), json=job_body,
            headers={X_PAYMENT_HEADER: header})
        resp.raise_for_status()
        return resp.json()
