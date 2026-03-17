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

    def _delete(self, path: str) -> bool:
        resp = self._session.delete(
            self._url(path),
            headers=self._wallet_auth_header("DELETE", path))
        resp.raise_for_status()
        return resp.status_code == 204

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

    def update_profile(self, agent_id: str = None, **kwargs) -> dict:
        """Update agent profile. Defaults to own agent in wallet mode."""
        agent_id = agent_id or self.agent_id
        if not agent_id:
            raise ValueError("agent_id required (no wallet configured)")
        resp = self._session.patch(
            self._url(f"/agents/{agent_id}"), json=kwargs,
            headers=self._wallet_auth_header("PATCH", f"/agents/{agent_id}"))
        resp.raise_for_status()
        return resp.json()

    def rotate_api_key(self, agent_id: str = None) -> dict:
        """Rotate API key. Returns new raw key. Old key is invalidated."""
        agent_id = agent_id or self.agent_id
        if not agent_id:
            raise ValueError("agent_id required (no wallet configured)")
        return self._post(f"/agents/{agent_id}/rotate-key")

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

    def update_job(self, task_id: str, **fields) -> dict:
        """Update job properties.

        Open jobs: title, description, rubric, expiry, max_submissions, max_retries.
        Funded jobs: expiry (extend only).
        """
        path = f"/jobs/{task_id}"
        resp = self._session.patch(
            self._url(path), json=fields,
            headers=self._wallet_auth_header("PATCH", path))
        resp.raise_for_status()
        return resp.json()

    def cancel_job(self, task_id: str) -> dict:
        """Cancel a job. Funded jobs auto-refund if no active judging."""
        return self._post(f"/jobs/{task_id}/cancel")

    def refund_job(self, task_id: str) -> dict:
        """Request manual refund for a funded job."""
        return self._post(f"/jobs/{task_id}/refund")

    def dispute_job(self, task_id: str, reason: str) -> dict:
        """File a dispute on a resolved job."""
        return self._post(f"/jobs/{task_id}/dispute", {"reason": reason})

    def retry_payout(self, task_id: str) -> dict:
        """Retry a failed payout for a resolved job."""
        return self._post(f"/admin/jobs/{task_id}/retry-payout")

    # ── Webhooks ──

    def create_webhook(self, url: str, events: list[str],
                       agent_id: str = None) -> dict:
        """Register a webhook for real-time event notifications."""
        agent_id = agent_id or self.agent_id
        if not agent_id:
            raise ValueError("agent_id required (no wallet configured)")
        return self._post(f"/agents/{agent_id}/webhooks",
                          {"url": url, "events": events})

    def list_webhooks(self, agent_id: str = None) -> list[dict]:
        """List registered webhooks."""
        agent_id = agent_id or self.agent_id
        if not agent_id:
            raise ValueError("agent_id required (no wallet configured)")
        return self._get(f"/agents/{agent_id}/webhooks")

    def delete_webhook(self, webhook_id: str, agent_id: str = None) -> bool:
        """Delete a webhook. Returns True on success."""
        agent_id = agent_id or self.agent_id
        if not agent_id:
            raise ValueError("agent_id required (no wallet configured)")
        return self._delete(f"/agents/{agent_id}/webhooks/{webhook_id}")

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

    def list_submissions(self, task_id: str, **filters) -> dict:
        """List submissions for a specific job. Returns {submissions, total}."""
        return self._get(f"/jobs/{task_id}/submissions", **filters)

    def my_submissions(self, worker_id: str = None, **filters) -> list[dict]:
        """List submissions across all jobs. Defaults to own in wallet mode."""
        params = {**filters}
        worker_id = worker_id or self.agent_id
        if worker_id:
            params["worker_id"] = worker_id
        return self._get("/submissions", **params)

    def dashboard_stats(self) -> dict:
        """Get platform stats (total agents, volume, etc.)."""
        return self._get("/dashboard/stats")

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
