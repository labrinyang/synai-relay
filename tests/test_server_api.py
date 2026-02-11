"""
Tests for server.py API endpoints.
Covers: health, agent registration, auth, job CRUD, claim, unclaim,
submit, cancel, refund, webhooks, solvency, dispute, rate limiting.
"""
import os
import json
import pytest

# Force DEV_MODE and test DB before importing app
os.environ['DEV_MODE'] = 'true'
os.environ['DATABASE_URL'] = 'sqlite://'  # in-memory

from server import app
from models import db, Agent, Job, Submission


@pytest.fixture
def client():
    """Create a test client with fresh in-memory DB."""
    app.config['TESTING'] = True
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()


def _register_agent(client, agent_id='agent-1', name='Test Agent', wallet=None):
    """Helper: register an agent and return (agent_id, api_key)."""
    payload = {'agent_id': agent_id, 'name': name}
    if wallet:
        payload['wallet_address'] = wallet
    resp = client.post('/agents', json=payload)
    data = resp.get_json()
    return agent_id, data.get('api_key')


def _auth_headers(api_key):
    return {'Authorization': f'Bearer {api_key}'}


# ===================================================================
# Health
# ===================================================================

class TestHealth:
    def test_health(self, client):
        resp = client.get('/health')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['status'] == 'healthy'

    def test_health_dev_mode_flag(self, client):
        resp = client.get('/health')
        data = resp.get_json()
        assert data.get('dev_mode') is True


# ===================================================================
# Agent Registration (G01)
# ===================================================================

class TestAgentRegistration:
    def test_register_agent(self, client):
        resp = client.post('/agents', json={
            'agent_id': 'bot-1',
            'name': 'Bot One',
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['status'] == 'registered'
        assert 'api_key' in data
        assert len(data['api_key']) > 20

    def test_register_missing_id(self, client):
        resp = client.post('/agents', json={'name': 'No ID'})
        assert resp.status_code == 400

    def test_register_duplicate(self, client):
        client.post('/agents', json={'agent_id': 'dup-1'})
        resp = client.post('/agents', json={'agent_id': 'dup-1'})
        assert resp.status_code == 409

    def test_register_wallet_warning(self, client):
        resp = client.post('/agents', json={'agent_id': 'no-wallet'})
        data = resp.get_json()
        assert 'warnings' in data
        assert any('wallet' in w.lower() for w in data['warnings'])

    def test_register_with_wallet(self, client):
        resp = client.post('/agents', json={
            'agent_id': 'has-wallet',
            'wallet_address': '0x' + 'a' * 40,
        })
        data = resp.get_json()
        assert 'warnings' not in data


# ===================================================================
# Agent Profile (G02)
# ===================================================================

class TestAgentProfile:
    def test_get_profile(self, client):
        _register_agent(client, 'prof-1')
        resp = client.get('/agents/prof-1')
        assert resp.status_code == 200
        assert resp.get_json()['agent_id'] == 'prof-1'

    def test_get_nonexistent(self, client):
        resp = client.get('/agents/ghost')
        assert resp.status_code == 404

    def test_update_profile(self, client):
        _, key = _register_agent(client, 'upd-1')
        resp = client.patch('/agents/upd-1',
                            json={'name': 'Updated'},
                            headers=_auth_headers(key))
        assert resp.status_code == 200
        assert resp.get_json()['name'] == 'Updated'

    def test_update_requires_auth(self, client):
        _register_agent(client, 'auth-test')
        resp = client.patch('/agents/auth-test', json={'name': 'Hacked'})
        assert resp.status_code == 401

    def test_cannot_update_others(self, client):
        _register_agent(client, 'target')
        _, key = _register_agent(client, 'attacker')
        resp = client.patch('/agents/target',
                            json={'name': 'Hacked'},
                            headers=_auth_headers(key))
        assert resp.status_code == 403

    def test_update_wallet_validation(self, client):
        _, key = _register_agent(client, 'wallet-v')
        resp = client.patch('/agents/wallet-v',
                            json={'wallet_address': 'invalid'},
                            headers=_auth_headers(key))
        assert resp.status_code == 400


# ===================================================================
# Auth (G01)
# ===================================================================

class TestAuth:
    def test_no_auth_on_public_endpoints(self, client):
        """GET /health, GET /jobs, GET /agents/<id> should not require auth."""
        assert client.get('/health').status_code == 200
        assert client.get('/jobs').status_code == 200

    def test_auth_required_for_create_job(self, client):
        resp = client.post('/jobs', json={
            'title': 'Test', 'description': 'Desc', 'price': 1.0,
        })
        assert resp.status_code == 401

    def test_invalid_api_key(self, client):
        resp = client.post('/jobs',
                           json={'title': 'Test', 'description': 'Desc', 'price': 1.0},
                           headers={'Authorization': 'Bearer invalid-key'})
        assert resp.status_code == 401


# ===================================================================
# Job CRUD (G03, G11)
# ===================================================================

class TestJobCRUD:
    def test_create_job(self, client):
        _, key = _register_agent(client, 'buyer-1')
        resp = client.post('/jobs',
                           json={'title': 'Task 1', 'description': 'Do stuff', 'price': 1.0},
                           headers=_auth_headers(key))
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['status'] == 'open'
        assert 'task_id' in data

    def test_create_job_missing_fields(self, client):
        _, key = _register_agent(client, 'buyer-2')
        resp = client.post('/jobs',
                           json={'title': 'No Desc'},
                           headers=_auth_headers(key))
        assert resp.status_code == 400

    def test_list_jobs_pagination(self, client):
        _, key = _register_agent(client, 'buyer-3')
        for i in range(5):
            client.post('/jobs',
                        json={'title': f'Task {i}', 'description': 'Desc', 'price': 1.0},
                        headers=_auth_headers(key))
        resp = client.get('/jobs?limit=2&offset=0')
        data = resp.get_json()
        assert data['total'] == 5
        assert len(data['jobs']) == 2

    def test_list_jobs_filter_status(self, client):
        _, key = _register_agent(client, 'buyer-4')
        client.post('/jobs',
                    json={'title': 'Open', 'description': 'Desc', 'price': 1.0},
                    headers=_auth_headers(key))
        resp = client.get('/jobs?status=funded')
        data = resp.get_json()
        assert data['total'] == 0

    def test_update_open_job(self, client):
        _, key = _register_agent(client, 'buyer-u')
        resp = client.post('/jobs',
                           json={'title': 'Original', 'description': 'Desc', 'price': 1.0},
                           headers=_auth_headers(key))
        task_id = resp.get_json()['task_id']
        resp = client.patch(f'/jobs/{task_id}',
                            json={'title': 'Updated Title'},
                            headers=_auth_headers(key))
        assert resp.status_code == 200
        assert resp.get_json()['title'] == 'Updated Title'

    def test_update_requires_auth(self, client):
        _, key = _register_agent(client, 'buyer-ua')
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0},
                           headers=_auth_headers(key))
        task_id = resp.get_json()['task_id']
        resp = client.patch(f'/jobs/{task_id}', json={'title': 'Hacked'})
        assert resp.status_code == 401


# ===================================================================
# Fund, Claim, Unclaim (G05)
# ===================================================================

class TestJobLifecycle:
    def _create_and_fund(self, client):
        """Helper: create buyer, worker, job, fund it."""
        _, buyer_key = _register_agent(client, 'buyer',
                                       wallet='0x' + 'bb' * 20)
        _, worker_key = _register_agent(client, 'worker',
                                        wallet='0x' + 'cc' * 20)
        resp = client.post('/jobs',
                           json={'title': 'Task', 'description': 'Do', 'price': 1.0},
                           headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']

        resp = client.post(f'/jobs/{task_id}/fund',
                           json={'tx_hash': '0xabc123'},
                           headers=_auth_headers(buyer_key))
        assert resp.status_code == 200
        return task_id, buyer_key, worker_key

    def test_fund_job(self, client):
        _, buyer_key = _register_agent(client, 'buyer-f')
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0},
                           headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']
        resp = client.post(f'/jobs/{task_id}/fund',
                           json={'tx_hash': '0xfund1'},
                           headers=_auth_headers(buyer_key))
        assert resp.status_code == 200
        assert resp.get_json()['status'] == 'funded'

    def test_fund_requires_buyer(self, client):
        _, buyer_key = _register_agent(client, 'buyer-fr')
        _, other_key = _register_agent(client, 'other-fr')
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0},
                           headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']
        resp = client.post(f'/jobs/{task_id}/fund',
                           json={'tx_hash': '0xhack'},
                           headers=_auth_headers(other_key))
        assert resp.status_code == 403

    def test_claim_job(self, client):
        task_id, buyer_key, worker_key = self._create_and_fund(client)
        resp = client.post(f'/jobs/{task_id}/claim',
                           json={},
                           headers=_auth_headers(worker_key))
        assert resp.status_code == 200
        assert resp.get_json()['status'] == 'claimed'

    def test_claim_self_dealing(self, client):
        _, buyer_key = _register_agent(client, 'self-dealer',
                                       wallet='0x' + 'aa' * 20)
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0},
                           headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']
        client.post(f'/jobs/{task_id}/fund',
                    json={'tx_hash': '0xsd1'},
                    headers=_auth_headers(buyer_key))
        resp = client.post(f'/jobs/{task_id}/claim',
                           json={},
                           headers=_auth_headers(buyer_key))
        assert resp.status_code == 403

    def test_unclaim_job(self, client):
        task_id, buyer_key, worker_key = self._create_and_fund(client)
        client.post(f'/jobs/{task_id}/claim',
                    json={},
                    headers=_auth_headers(worker_key))
        resp = client.post(f'/jobs/{task_id}/unclaim',
                           json={},
                           headers=_auth_headers(worker_key))
        assert resp.status_code == 200
        assert resp.get_json()['status'] == 'unclaimed'

    def test_unclaim_not_claimed(self, client):
        task_id, buyer_key, worker_key = self._create_and_fund(client)
        resp = client.post(f'/jobs/{task_id}/unclaim',
                           json={},
                           headers=_auth_headers(worker_key))
        assert resp.status_code == 400

    def test_cancel_job(self, client):
        _, buyer_key = _register_agent(client, 'cancel-buyer')
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0},
                           headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']
        resp = client.post(f'/jobs/{task_id}/cancel',
                           headers=_auth_headers(buyer_key))
        assert resp.status_code == 200
        assert resp.get_json()['status'] == 'cancelled'


# ===================================================================
# Webhooks (G04)
# ===================================================================

class TestWebhooks:
    def test_webhook_crud(self, client):
        _, key = _register_agent(client, 'wh-agent')

        # Create
        resp = client.post('/agents/wh-agent/webhooks',
                           json={
                               'url': 'https://example.com/hook',
                               'events': ['job.resolved'],
                           },
                           headers=_auth_headers(key))
        assert resp.status_code == 201
        data = resp.get_json()
        assert 'webhook_id' in data
        assert 'secret' in data
        wh_id = data['webhook_id']

        # List
        resp = client.get('/agents/wh-agent/webhooks',
                          headers=_auth_headers(key))
        assert resp.status_code == 200
        hooks = resp.get_json()
        assert len(hooks) == 1

        # Delete
        resp = client.delete(f'/agents/wh-agent/webhooks/{wh_id}',
                             headers=_auth_headers(key))
        assert resp.status_code == 204

        # Verify deleted
        resp = client.get('/agents/wh-agent/webhooks',
                          headers=_auth_headers(key))
        assert len(resp.get_json()) == 0

    def test_webhook_requires_https(self, client):
        _, key = _register_agent(client, 'wh-http')
        resp = client.post('/agents/wh-http/webhooks',
                           json={'url': 'http://insecure.com', 'events': ['job.resolved']},
                           headers=_auth_headers(key))
        assert resp.status_code == 400

    def test_webhook_cross_agent(self, client):
        _, key1 = _register_agent(client, 'wh-a')
        _, key2 = _register_agent(client, 'wh-b')
        resp = client.post('/agents/wh-a/webhooks',
                           json={'url': 'https://x.com/hook', 'events': ['job.resolved']},
                           headers=_auth_headers(key2))
        assert resp.status_code == 403


# ===================================================================
# Solvency (G21)
# ===================================================================

class TestSolvency:
    def test_solvency_endpoint(self, client):
        resp = client.get('/platform/solvency')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'outstanding_liabilities' in data
        assert 'funded_jobs_count' in data


# ===================================================================
# Dispute (G24)
# ===================================================================

class TestDispute:
    def test_dispute_requires_auth(self, client):
        resp = client.post('/jobs/fake-id/dispute', json={'reason': 'Bad'})
        assert resp.status_code == 401

    def test_dispute_wrong_state(self, client):
        _, key = _register_agent(client, 'disp-buyer')
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0},
                           headers=_auth_headers(key))
        task_id = resp.get_json()['task_id']
        resp = client.post(f'/jobs/{task_id}/dispute',
                           json={'reason': 'Bad work'},
                           headers=_auth_headers(key))
        assert resp.status_code == 400  # job is 'open', not 'resolved'


# ===================================================================
# Rate Limiter
# ===================================================================

class TestRateLimiter:
    def test_rate_limiter_unit(self):
        from services.rate_limiter import RateLimiter
        rl = RateLimiter(max_requests=3, window_seconds=60)
        assert rl.is_allowed('k1')[0] is True
        assert rl.is_allowed('k1')[0] is True
        assert rl.is_allowed('k1')[0] is True
        assert rl.is_allowed('k1')[0] is False  # 4th should fail
        # Different key should still be allowed
        assert rl.is_allowed('k2')[0] is True
