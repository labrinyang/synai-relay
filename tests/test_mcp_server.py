"""Tests for SYNAI Relay MCP server tool handlers."""
import os
import json
import pytest

os.environ.setdefault('DATABASE_URL', 'sqlite://')
os.environ.setdefault('SYNAI_API_KEY', 'test')
os.environ.setdefault('SYNAI_BASE_URL', 'http://localhost')

from server import app
from models import db
from synai_client import SynaiClient
from tests.test_synai_client import _FlaskAdapter


@pytest.fixture(scope='module')
def mcp_setup():
    """Set up Flask app and patch MCP server's client."""
    app.config['TESTING'] = True
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
    app.config['X402_ENABLED'] = False

    with app.app_context():
        db.create_all()

        import mcp_server

        tc = app.test_client()
        client = SynaiClient("http://localhost", "dummy")
        client._session = _FlaskAdapter(tc)
        mcp_server._client = client

        # Register a test agent
        client.register("mcp-test", name="MCP Test Agent",
                        wallet_address="0x" + "cd" * 20)

        yield mcp_server

        from server import _shutdown_event, _oracle_executor
        _shutdown_event.set()
        try:
            _oracle_executor.shutdown(wait=True)
        except Exception:
            pass
        db.drop_all()


class TestMCPTools:

    def test_browse_jobs_empty(self, mcp_setup):
        result = mcp_setup.synai_browse_jobs()
        assert "No jobs found" in result

    def test_list_chains(self, mcp_setup):
        result = mcp_setup.synai_list_chains()
        chains = json.loads(result)
        assert isinstance(chains, list)

    def test_my_profile(self, mcp_setup):
        # synai_my_profile auto-detects agent_id from wallet.
        # Test client doesn't have a real wallet — mock agent_id property.
        from unittest.mock import PropertyMock, patch
        with patch.object(type(mcp_setup._client), 'agent_id',
                          new_callable=PropertyMock, return_value='mcp-test'):
            result = mcp_setup.synai_my_profile()
        data = json.loads(result)
        assert data["agent_id"] == "mcp-test"

    def test_create_and_get_job(self, mcp_setup):
        # Create a job via MCP tool
        result = mcp_setup.synai_create_funded_job(
            title="MCP Test Job",
            description="A job created via MCP tool",
            price=0.5,
        )
        data = json.loads(result)
        assert data["status"] == "open"
        task_id = data["task_id"]

        # Get job details
        result2 = mcp_setup.synai_get_job(task_id)
        data2 = json.loads(result2)
        assert data2["title"] == "MCP Test Job"
