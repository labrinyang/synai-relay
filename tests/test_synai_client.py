"""Tests for SYNAI Relay Python SDK."""
import os
import pytest

os.environ.setdefault('DATABASE_URL', 'sqlite://')

from server import app
from models import db
from synai_client import SynaiClient


class _WrappedResponse:
    """Wraps Flask WrapperTestResponse to match requests.Response API."""

    def __init__(self, flask_resp):
        self._r = flask_resp
        self.status_code = flask_resp.status_code
        self.headers = flask_resp.headers

    def raise_for_status(self):
        if self.status_code >= 400:
            from requests.exceptions import HTTPError
            raise HTTPError(f"{self.status_code}", response=self)

    def json(self):
        return self._r.get_json()


class _FlaskAdapter:
    """Adapts Flask test client to look like requests.Session for SDK testing."""

    def __init__(self, test_client):
        self._tc = test_client
        self.headers = {}

    def _path(self, url):
        if "//localhost" in url:
            return url.split("//localhost", 1)[-1]
        return url

    def get(self, url, params=None, **kw):
        return _WrappedResponse(
            self._tc.get(self._path(url), query_string=params,
                         headers=self.headers))

    def post(self, url, json=None, headers=None, **kw):
        h = {**self.headers, **(headers or {})}
        return _WrappedResponse(
            self._tc.post(self._path(url), json=json, headers=h))

    def patch(self, url, json=None, **kw):
        return _WrappedResponse(
            self._tc.patch(self._path(url), json=json,
                           headers=self.headers))


@pytest.fixture(scope='module')
def sdk():
    """Create SDK client wired to Flask test client."""
    app.config['TESTING'] = True
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
    app.config['X402_ENABLED'] = False

    with app.app_context():
        db.create_all()
        tc = app.test_client()
        client = SynaiClient("http://localhost", api_key="dummy")
        client._session = _FlaskAdapter(tc)
        yield client
        from server import _shutdown_event, _oracle_executor
        _shutdown_event.set()
        try:
            _oracle_executor.shutdown(wait=True)
        except Exception:
            pass
        db.drop_all()


class TestSDKAgent:

    def test_register(self, sdk):
        result = sdk.register("sdk-worker", name="SDK Worker",
                              wallet_address="0x" + "ab" * 20)
        assert result["agent_id"] == "sdk-worker"
        assert "api_key" in result

    def test_get_profile(self, sdk):
        profile = sdk.get_profile("sdk-worker")
        assert profile["agent_id"] == "sdk-worker"
        assert profile["name"] == "SDK Worker"


class TestSDKPlatform:

    def test_health(self, sdk):
        h = sdk.health()
        assert h["status"] == "healthy"

    def test_list_chains(self, sdk):
        chains = sdk.list_chains()
        assert isinstance(chains, list)

    def test_leaderboard(self, sdk):
        result = sdk.leaderboard()
        assert isinstance(result, dict)


class TestWalletAuth:
    """Test wallet signature authentication (no API key)."""

    def test_wallet_auth_claim(self, sdk):
        """Wallet signature auth works for claim (free operation)."""
        from eth_account import Account
        from eth_account.messages import encode_defunct
        import time as _time

        # Use a test wallet
        account = Account.create()
        wallet_addr = account.address

        # Register agent with this wallet using existing SDK
        sdk.register("wallet-test-agent", name="Wallet Test",
                     wallet_address=wallet_addr)

        # Create a job to claim
        result = sdk.create_job("Wallet Auth Test", "Test description", 0.5)
        task_id = result["task_id"]

        # Create a wallet-auth SDK (no API key)
        wallet_sdk = SynaiClient("http://localhost",
                                 wallet_key=account.key.hex())
        wallet_sdk._session = sdk._session  # reuse Flask adapter

        # Override _wallet_auth_header for Flask test client
        # (wallet auth creates Authorization headers per-request)
        ts = str(int(_time.time()))
        path = f"/jobs/{task_id}/claim"
        message = f"SYNAI:POST:{path}:{ts}"
        msg = encode_defunct(text=message)
        sig = account.sign_message(msg)
        auth_val = f"Wallet {account.address}:{ts}:{sig.signature.hex()}"

        # Direct call with wallet auth header
        from server import app
        tc = app.test_client()
        resp = tc.post(path, headers={"Authorization": auth_val})
        # Could be 200 (claimed) or 409 (already claimed) or 400 (not funded)
        assert resp.status_code in (200, 400, 409), \
            f"Wallet auth failed: {resp.status_code} {resp.get_json()}"

    def test_auto_registration(self, sdk):
        """New wallet auto-registers an agent on first wallet auth."""
        from eth_account import Account
        from eth_account.messages import encode_defunct
        import time as _time

        account = Account.create()

        # Create a job first (to have something to claim)
        result = sdk.create_job("Auto-reg Test", "Test", 0.5)
        task_id = result["task_id"]

        # Claim with a brand-new wallet — should auto-register
        ts = str(int(_time.time()))
        path = f"/jobs/{task_id}/claim"
        message = f"SYNAI:POST:{path}:{ts}"
        msg = encode_defunct(text=message)
        sig = account.sign_message(msg)
        auth_val = f"Wallet {account.address}:{ts}:{sig.signature.hex()}"

        from server import app
        tc = app.test_client()
        resp = tc.post(path, headers={"Authorization": auth_val})
        # 400 (not funded) is expected — but agent should be auto-created
        assert resp.status_code in (200, 400)

        # Verify agent was auto-created with wallet address as ID
        profile_resp = tc.get(f"/agents/{account.address.lower()}")
        assert profile_resp.status_code == 200
        data = profile_resp.get_json()
        assert data["wallet_address"] == account.address


class TestSDKWorkerFlow:

    def test_browse_empty(self, sdk):
        jobs = sdk.browse_jobs()
        assert jobs == []

    def test_create_and_browse(self, sdk):
        # Register a buyer
        sdk.register("sdk-buyer", name="SDK Buyer")

        # Create a job (classic, no x402)
        result = sdk.create_job("SDK Test Job", "Test description", 0.5)
        assert result["status"] == "open"
        task_id = result["task_id"]
        sdk._test_task_id = task_id

    def test_get_job(self, sdk):
        job = sdk.get_job(sdk._test_task_id)
        assert job["title"] == "SDK Test Job"
        assert job["status"] == "open"
