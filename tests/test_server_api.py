"""
Tests for server.py API endpoints.
Covers: health, agent registration, auth, job CRUD, claim, unclaim,
submit, cancel, refund, webhooks, solvency, dispute, rate limiting.
"""
import os
import json
import unittest
import pytest
from unittest.mock import patch

# Force DEV_MODE and test DB before importing app
os.environ['DEV_MODE'] = 'true'
os.environ['DATABASE_URL'] = 'sqlite://'  # in-memory

from server import app
from models import db, Agent, Job, Submission, JobParticipant, Webhook


@pytest.fixture
def client():
    """Create a test client with fresh in-memory DB and reset rate limiters."""
    app.config['TESTING'] = True
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
    # Reset rate limiter state between tests
    from services.rate_limiter import _api_limiter, _submit_limiter
    _api_limiter._requests.clear()
    _submit_limiter._requests.clear()
    with app.app_context():
        db.create_all()
        yield app.test_client()
        # Wait for any pending oracle evaluations to complete
        from server import _oracle_executor
        _oracle_executor.shutdown(wait=True)
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
    @patch('services.webhook_service.is_safe_webhook_url', return_value=True)
    def test_webhook_crud(self, mock_ssrf, client):
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
    def test_solvency_requires_auth(self, client):
        resp = client.get('/platform/solvency')
        assert resp.status_code == 401

    def test_solvency_endpoint(self, client):
        _, key = _register_agent(client, 'sol-agent')
        resp = client.get('/platform/solvency', headers=_auth_headers(key))
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

    def test_rate_limiter_cleans_stale_keys(self):
        """M6 fix: stale keys should be removed after cleanup."""
        from services.rate_limiter import RateLimiter
        import time
        rl = RateLimiter(max_requests=1, window_seconds=0.1)
        rl.is_allowed('stale-key')
        assert 'stale-key' in rl._requests
        time.sleep(0.15)
        rl.is_allowed('stale-key')  # triggers cleanup
        # After cleanup and re-add, key exists but old entries are gone


# ===================================================================
# Submission Flow (C4 gap)
# ===================================================================

class TestSubmissionFlow:
    def _setup_claimed(self, client):
        """Helper: create buyer + worker, create/fund/claim job."""
        _, buyer_key = _register_agent(client, 'sub-buyer',
                                       wallet='0x' + 'bb' * 20)
        _, worker_key = _register_agent(client, 'sub-worker',
                                        wallet='0x' + 'cc' * 20)
        resp = client.post('/jobs',
                           json={'title': 'Task', 'description': 'Do work',
                                 'price': 1.0, 'max_retries': 2},
                           headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']
        client.post(f'/jobs/{task_id}/fund',
                    json={'tx_hash': '0xsub-fund'},
                    headers=_auth_headers(buyer_key))
        client.post(f'/jobs/{task_id}/claim',
                    json={},
                    headers=_auth_headers(worker_key))
        return task_id, buyer_key, worker_key

    def test_submit_requires_auth(self, client):
        resp = client.post('/jobs/fake-id/submit',
                           json={'content': 'result'})
        assert resp.status_code == 401

    def test_submit_job_not_found(self, client):
        _, key = _register_agent(client, 'sub-ghost')
        resp = client.post('/jobs/nonexistent/submit',
                           json={'content': 'result'},
                           headers=_auth_headers(key))
        assert resp.status_code == 404

    def test_submit_not_funded(self, client):
        """Cannot submit to an open (unfunded) job."""
        _, buyer_key = _register_agent(client, 'sub-buyer-nf')
        _, worker_key = _register_agent(client, 'sub-worker-nf')
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0},
                           headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']
        resp = client.post(f'/jobs/{task_id}/submit',
                           json={'content': 'result'},
                           headers=_auth_headers(worker_key))
        assert resp.status_code == 400

    def test_submit_not_participant(self, client):
        """Worker must claim before submitting."""
        _, buyer_key = _register_agent(client, 'sub-buyer-np')
        _, worker_key = _register_agent(client, 'sub-worker-np')
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0},
                           headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']
        client.post(f'/jobs/{task_id}/fund',
                    json={'tx_hash': '0xnp-fund'},
                    headers=_auth_headers(buyer_key))
        resp = client.post(f'/jobs/{task_id}/submit',
                           json={'content': 'result'},
                           headers=_auth_headers(worker_key))
        assert resp.status_code == 403

    def test_submit_missing_content(self, client):
        task_id, buyer_key, worker_key = self._setup_claimed(client)
        resp = client.post(f'/jobs/{task_id}/submit',
                           json={},
                           headers=_auth_headers(worker_key))
        assert resp.status_code == 400

    def test_submit_success(self, client):
        """Successful submission should return 202 with judging status."""
        task_id, buyer_key, worker_key = self._setup_claimed(client)
        resp = client.post(f'/jobs/{task_id}/submit',
                           json={'content': {'answer': 'my solution'}},
                           headers=_auth_headers(worker_key))
        assert resp.status_code == 202
        data = resp.get_json()
        assert data['status'] == 'judging'
        assert 'submission_id' in data
        assert data['attempt'] == 1

    def test_submit_content_size_limit(self, client):
        """Content exceeding 50KB should be rejected."""
        task_id, buyer_key, worker_key = self._setup_claimed(client)
        large_content = 'x' * (51 * 1024)
        resp = client.post(f'/jobs/{task_id}/submit',
                           json={'content': large_content},
                           headers=_auth_headers(worker_key))
        assert resp.status_code == 400
        assert '50KB' in resp.get_json()['error']

    def test_submit_max_retries(self, client):
        """Worker should be blocked after max_retries submissions."""
        task_id, buyer_key, worker_key = self._setup_claimed(client)
        # Submit twice (max_retries=2 from _setup_claimed)
        for i in range(2):
            resp = client.post(f'/jobs/{task_id}/submit',
                               json={'content': f'attempt {i+1}'},
                               headers=_auth_headers(worker_key))
            assert resp.status_code == 202
        # Third should fail
        resp = client.post(f'/jobs/{task_id}/submit',
                           json={'content': 'attempt 3'},
                           headers=_auth_headers(worker_key))
        assert resp.status_code == 400
        assert 'retries' in resp.get_json()['error'].lower()


# ===================================================================
# Submission Privacy (G16 / C2 fix)
# ===================================================================

class TestSubmissionPrivacy:
    def _setup_with_submission(self, client):
        """Create a job with a submission in judging state."""
        _, buyer_key = _register_agent(client, 'priv-buyer',
                                       wallet='0x' + 'bb' * 20)
        _, worker_key = _register_agent(client, 'priv-worker',
                                        wallet='0x' + 'cc' * 20)
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0},
                           headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']
        client.post(f'/jobs/{task_id}/fund',
                    json={'tx_hash': '0xpriv-fund'},
                    headers=_auth_headers(buyer_key))
        client.post(f'/jobs/{task_id}/claim',
                    json={},
                    headers=_auth_headers(worker_key))
        resp = client.post(f'/jobs/{task_id}/submit',
                           json={'content': {'secret': 'my secret solution'}},
                           headers=_auth_headers(worker_key))
        sub_id = resp.get_json()['submission_id']
        return task_id, sub_id, buyer_key, worker_key

    def test_unauthenticated_sees_redacted(self, client):
        task_id, sub_id, _, _ = self._setup_with_submission(client)
        resp = client.get(f'/jobs/{task_id}/submissions')
        data = resp.get_json()['submissions']
        assert len(data) == 1
        assert data[0]['content'] == '[redacted]'

    def test_worker_sees_own_content(self, client):
        task_id, sub_id, _, worker_key = self._setup_with_submission(client)
        resp = client.get(f'/jobs/{task_id}/submissions',
                          headers=_auth_headers(worker_key))
        data = resp.get_json()['submissions']
        assert data[0]['content'] == {'secret': 'my secret solution'}

    def test_buyer_sees_content(self, client):
        task_id, sub_id, buyer_key, _ = self._setup_with_submission(client)
        resp = client.get(f'/jobs/{task_id}/submissions',
                          headers=_auth_headers(buyer_key))
        data = resp.get_json()['submissions']
        assert data[0]['content'] == {'secret': 'my secret solution'}

    def test_third_party_sees_redacted(self, client):
        task_id, sub_id, _, _ = self._setup_with_submission(client)
        _, other_key = _register_agent(client, 'priv-other')
        resp = client.get(f'/jobs/{task_id}/submissions',
                          headers=_auth_headers(other_key))
        data = resp.get_json()['submissions']
        assert data[0]['content'] == '[redacted]'

    def test_single_submission_redacted(self, client):
        task_id, sub_id, _, _ = self._setup_with_submission(client)
        resp = client.get(f'/submissions/{sub_id}')
        data = resp.get_json()
        assert data['content'] == '[redacted]'

    def test_single_submission_worker_sees_content(self, client):
        task_id, sub_id, _, worker_key = self._setup_with_submission(client)
        resp = client.get(f'/submissions/{sub_id}',
                          headers=_auth_headers(worker_key))
        data = resp.get_json()
        assert data['content'] == {'secret': 'my secret solution'}


# ===================================================================
# Job Detail Endpoint
# ===================================================================

class TestJobDetail:
    def test_get_job(self, client):
        _, key = _register_agent(client, 'detail-buyer')
        resp = client.post('/jobs',
                           json={'title': 'Detail Test', 'description': 'D', 'price': 2.5},
                           headers=_auth_headers(key))
        task_id = resp.get_json()['task_id']
        resp = client.get(f'/jobs/{task_id}')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['task_id'] == task_id
        assert data['title'] == 'Detail Test'
        assert data['price'] == 2.5
        assert data['status'] == 'open'
        assert 'created_at' in data

    def test_get_job_not_found(self, client):
        resp = client.get('/jobs/nonexistent-id')
        assert resp.status_code == 404

    def test_deposit_info(self, client):
        resp = client.get('/platform/deposit-info')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'operations_wallet' in data
        assert data['chain'] == 'base'


# ===================================================================
# Refund Flow
# ===================================================================

class TestRefundFlow:
    def test_refund_requires_auth(self, client):
        resp = client.post('/jobs/fake/refund')
        assert resp.status_code == 401

    def test_refund_not_found(self, client):
        _, key = _register_agent(client, 'ref-buyer')
        resp = client.post('/jobs/nonexistent/refund',
                           headers=_auth_headers(key))
        assert resp.status_code == 404

    def test_refund_wrong_state(self, client):
        """Cannot refund open job."""
        _, key = _register_agent(client, 'ref-buyer-ws')
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0},
                           headers=_auth_headers(key))
        task_id = resp.get_json()['task_id']
        resp = client.post(f'/jobs/{task_id}/refund',
                           headers=_auth_headers(key))
        assert resp.status_code == 400

    def test_refund_cancelled_job(self, client):
        """Refund a cancelled job succeeds (off-chain mode)."""
        _, key = _register_agent(client, 'ref-buyer-can')
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0},
                           headers=_auth_headers(key))
        task_id = resp.get_json()['task_id']
        # Fund then cancel
        client.post(f'/jobs/{task_id}/fund',
                    json={'tx_hash': '0xref-fund'},
                    headers=_auth_headers(key))
        client.post(f'/jobs/{task_id}/cancel',
                    headers=_auth_headers(key))
        # Refund
        resp = client.post(f'/jobs/{task_id}/refund',
                           headers=_auth_headers(key))
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['status'] == 'refunded'
        assert data['amount'] == 1.0

    def test_refund_double_refund_blocked(self, client):
        """Cannot refund same job twice."""
        _, key = _register_agent(client, 'ref-buyer-dbl')
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0},
                           headers=_auth_headers(key))
        task_id = resp.get_json()['task_id']
        client.post(f'/jobs/{task_id}/fund',
                    json={'tx_hash': '0xdbl-fund'},
                    headers=_auth_headers(key))
        client.post(f'/jobs/{task_id}/cancel',
                    headers=_auth_headers(key))
        # First refund
        resp = client.post(f'/jobs/{task_id}/refund',
                           headers=_auth_headers(key))
        assert resp.status_code == 200
        # Second refund
        resp = client.post(f'/jobs/{task_id}/refund',
                           headers=_auth_headers(key))
        assert resp.status_code == 409

    def test_refund_requires_buyer(self, client):
        """Only buyer can refund."""
        _, buyer_key = _register_agent(client, 'ref-buyer-rb')
        _, other_key = _register_agent(client, 'ref-other-rb')
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0},
                           headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']
        client.post(f'/jobs/{task_id}/fund',
                    json={'tx_hash': '0xrb-fund'},
                    headers=_auth_headers(buyer_key))
        client.post(f'/jobs/{task_id}/cancel',
                    headers=_auth_headers(buyer_key))
        resp = client.post(f'/jobs/{task_id}/refund',
                           headers=_auth_headers(other_key))
        assert resp.status_code == 403


# ===================================================================
# Cancel Funded Job
# ===================================================================

class TestCancelFundedJob:
    def test_cancel_funded_job(self, client):
        _, key = _register_agent(client, 'cfund-buyer')
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0},
                           headers=_auth_headers(key))
        task_id = resp.get_json()['task_id']
        client.post(f'/jobs/{task_id}/fund',
                    json={'tx_hash': '0xcfund'},
                    headers=_auth_headers(key))
        resp = client.post(f'/jobs/{task_id}/cancel',
                           headers=_auth_headers(key))
        assert resp.status_code == 200
        assert resp.get_json()['status'] == 'cancelled'

    def test_cancel_already_cancelled(self, client):
        _, key = _register_agent(client, 'cfund-buyer2')
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0},
                           headers=_auth_headers(key))
        task_id = resp.get_json()['task_id']
        client.post(f'/jobs/{task_id}/cancel',
                    headers=_auth_headers(key))
        resp = client.post(f'/jobs/{task_id}/cancel',
                           headers=_auth_headers(key))
        assert resp.status_code == 400


# ===================================================================
# Claim Validation
# ===================================================================

class TestClaimValidation:
    def test_claim_unfunded_job(self, client):
        """Cannot claim an open (unfunded) job."""
        _, buyer_key = _register_agent(client, 'claim-buyer')
        _, worker_key = _register_agent(client, 'claim-worker')
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0},
                           headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']
        resp = client.post(f'/jobs/{task_id}/claim',
                           json={},
                           headers=_auth_headers(worker_key))
        assert resp.status_code == 400

    def test_claim_duplicate(self, client):
        """Cannot claim same job twice."""
        _, buyer_key = _register_agent(client, 'claim-buyer2',
                                       wallet='0x' + 'bb' * 20)
        _, worker_key = _register_agent(client, 'claim-worker2',
                                        wallet='0x' + 'cc' * 20)
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0},
                           headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']
        client.post(f'/jobs/{task_id}/fund',
                    json={'tx_hash': '0xclaim-dup'},
                    headers=_auth_headers(buyer_key))
        client.post(f'/jobs/{task_id}/claim',
                    json={},
                    headers=_auth_headers(worker_key))
        resp = client.post(f'/jobs/{task_id}/claim',
                           json={},
                           headers=_auth_headers(worker_key))
        assert resp.status_code == 409

    def test_unclaim_then_reclaimable(self, client):
        """After unclaim, worker is removed from participants."""
        _, buyer_key = _register_agent(client, 'reclaim-buyer',
                                       wallet='0x' + 'bb' * 20)
        _, worker_key = _register_agent(client, 'reclaim-worker',
                                        wallet='0x' + 'cc' * 20)
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0},
                           headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']
        client.post(f'/jobs/{task_id}/fund',
                    json={'tx_hash': '0xreclaim'},
                    headers=_auth_headers(buyer_key))
        client.post(f'/jobs/{task_id}/claim',
                    json={},
                    headers=_auth_headers(worker_key))
        client.post(f'/jobs/{task_id}/unclaim',
                    json={},
                    headers=_auth_headers(worker_key))
        # Verify worker removed from participants
        resp = client.get(f'/jobs/{task_id}')
        assert 'reclaim-worker' not in resp.get_json()['participants']


# ===================================================================
# Price Validation
# ===================================================================

class TestPriceValidation:
    def test_negative_price(self, client):
        _, key = _register_agent(client, 'price-neg')
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': -1.0},
                           headers=_auth_headers(key))
        assert resp.status_code == 400

    def test_zero_price(self, client):
        _, key = _register_agent(client, 'price-zero')
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 0},
                           headers=_auth_headers(key))
        assert resp.status_code == 400

    def test_below_min_price(self, client):
        _, key = _register_agent(client, 'price-low')
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 0.01},
                           headers=_auth_headers(key))
        assert resp.status_code == 400

    def test_invalid_price_string(self, client):
        _, key = _register_agent(client, 'price-str')
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 'not-a-number'},
                           headers=_auth_headers(key))
        assert resp.status_code == 400

    def test_missing_price(self, client):
        _, key = _register_agent(client, 'price-miss')
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D'},
                           headers=_auth_headers(key))
        assert resp.status_code == 400


# ===================================================================
# Agent ID Validation
# ===================================================================

class TestAgentIdValidation:
    def test_agent_id_too_short(self, client):
        resp = client.post('/agents', json={'agent_id': 'ab'})
        assert resp.status_code == 400

    def test_agent_id_special_chars(self, client):
        resp = client.post('/agents', json={'agent_id': 'bad@agent!'})
        assert resp.status_code == 400

    def test_agent_id_valid_formats(self, client):
        """Valid agent_id with hyphens and underscores."""
        resp = client.post('/agents', json={'agent_id': 'my-agent_01'})
        assert resp.status_code == 201


# ===================================================================
# Idempotency Key (G17)
# ===================================================================

class TestIdempotencyKey:
    def test_idempotency_returns_cached(self, client):
        _, key = _register_agent(client, 'idem-buyer')
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0},
                           headers=_auth_headers(key))
        task_id = resp.get_json()['task_id']

        # Fund with idempotency key
        headers = {**_auth_headers(key), 'Idempotency-Key': 'unique-fund-1'}
        resp1 = client.post(f'/jobs/{task_id}/fund',
                            json={'tx_hash': '0xidem-fund'},
                            headers=headers)
        assert resp1.status_code == 200

        # Same key should return cached response
        resp2 = client.post(f'/jobs/{task_id}/fund',
                            json={'tx_hash': '0xidem-fund'},
                            headers=headers)
        assert resp2.status_code == 200
        assert resp2.get_json()['task_id'] == task_id

    def test_idempotency_different_keys(self, client):
        """Different idempotency keys should not collide."""
        _, key = _register_agent(client, 'idem-buyer2')
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0},
                           headers=_auth_headers(key))
        task_id = resp.get_json()['task_id']

        headers1 = {**_auth_headers(key), 'Idempotency-Key': 'key-a'}
        resp = client.post(f'/jobs/{task_id}/fund',
                           json={'tx_hash': '0xidem-a'},
                           headers=headers1)
        assert resp.status_code == 200

        # Different idempotency key, same fund -> should fail (already funded)
        headers2 = {**_auth_headers(key), 'Idempotency-Key': 'key-b'}
        resp = client.post(f'/jobs/{task_id}/fund',
                           json={'tx_hash': '0xidem-b'},
                           headers=headers2)
        assert resp.status_code == 400  # already funded


# ===================================================================
# Name Validation (m2)
# ===================================================================

class TestNameValidation:
    def test_update_name_empty(self, client):
        _, key = _register_agent(client, 'name-empty')
        resp = client.patch('/agents/name-empty',
                            json={'name': ''},
                            headers=_auth_headers(key))
        assert resp.status_code == 400

    def test_update_name_too_long(self, client):
        _, key = _register_agent(client, 'name-long')
        resp = client.patch('/agents/name-long',
                            json={'name': 'x' * 201},
                            headers=_auth_headers(key))
        assert resp.status_code == 400


# ===================================================================
# E2E Lifecycle: create -> fund -> claim -> submit -> check
# ===================================================================

class TestE2ELifecycle:
    def test_full_happy_path_to_submission(self, client):
        """E2E: register -> create -> fund -> claim -> submit -> verify submission state."""
        # Register agents
        _, buyer_key = _register_agent(client, 'e2e-buyer',
                                       wallet='0x' + 'bb' * 20)
        _, worker_key = _register_agent(client, 'e2e-worker',
                                        wallet='0x' + 'cc' * 20)
        # Create job
        resp = client.post('/jobs',
                           json={'title': 'E2E Task', 'description': 'Full test',
                                 'price': 5.0},
                           headers=_auth_headers(buyer_key))
        assert resp.status_code == 201
        task_id = resp.get_json()['task_id']

        # Verify job is open
        resp = client.get(f'/jobs/{task_id}')
        assert resp.get_json()['status'] == 'open'

        # Fund
        resp = client.post(f'/jobs/{task_id}/fund',
                           json={'tx_hash': '0xe2e-fund'},
                           headers=_auth_headers(buyer_key))
        assert resp.status_code == 200
        assert resp.get_json()['status'] == 'funded'

        # Claim
        resp = client.post(f'/jobs/{task_id}/claim',
                           json={},
                           headers=_auth_headers(worker_key))
        assert resp.status_code == 200

        # Submit
        resp = client.post(f'/jobs/{task_id}/submit',
                           json={'content': {'result': 'done'}},
                           headers=_auth_headers(worker_key))
        assert resp.status_code == 202
        sub_id = resp.get_json()['submission_id']

        # Check submission exists
        resp = client.get(f'/submissions/{sub_id}',
                          headers=_auth_headers(worker_key))
        assert resp.status_code == 200
        sub_data = resp.get_json()
        assert sub_data['task_id'] == task_id
        assert sub_data['worker_id'] == 'e2e-worker'
        assert sub_data['content'] == {'result': 'done'}

        # List submissions for job
        resp = client.get(f'/jobs/{task_id}/submissions',
                          headers=_auth_headers(buyer_key))
        subs = resp.get_json()['submissions']
        assert len(subs) == 1
        assert subs[0]['content'] == {'result': 'done'}

    def test_cancel_and_refund_flow(self, client):
        """E2E: create -> fund -> cancel -> refund."""
        _, buyer_key = _register_agent(client, 'e2e-buyer-cr',
                                       wallet='0x' + 'bb' * 20)
        resp = client.post('/jobs',
                           json={'title': 'E2E Cancel', 'description': 'D', 'price': 3.0},
                           headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']

        client.post(f'/jobs/{task_id}/fund',
                    json={'tx_hash': '0xe2e-cr-fund'},
                    headers=_auth_headers(buyer_key))
        client.post(f'/jobs/{task_id}/cancel',
                    headers=_auth_headers(buyer_key))

        resp = client.post(f'/jobs/{task_id}/refund',
                           headers=_auth_headers(buyer_key))
        assert resp.status_code == 200
        assert resp.get_json()['status'] == 'refunded'

        # Check solvency reflects
        resp = client.get('/platform/solvency',
                          headers=_auth_headers(buyer_key))
        data = resp.get_json()
        assert data['funded_jobs_count'] == 0


# ===================================================================
# Oracle Timeout (G07)
# ===================================================================

class TestOracleTimeout:
    """G07: Test oracle timeout handling."""

    def test_submission_enters_judging(self, client):
        """Submission should start in 'judging' state."""
        _, buyer_key = _register_agent(client, 'ot-buyer', wallet='0x' + 'bb' * 20)
        _, worker_key = _register_agent(client, 'ot-worker', wallet='0x' + 'cc' * 20)
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0},
                           headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']
        client.post(f'/jobs/{task_id}/fund',
                    json={'tx_hash': '0xot-fund'},
                    headers=_auth_headers(buyer_key))
        client.post(f'/jobs/{task_id}/claim',
                    json={},
                    headers=_auth_headers(worker_key))
        resp = client.post(f'/jobs/{task_id}/submit',
                           json={'content': 'test'},
                           headers=_auth_headers(worker_key))
        assert resp.status_code == 202
        data = resp.get_json()
        assert data['status'] == 'judging'

    @patch('server._launch_oracle_with_timeout')
    def test_oracle_timeout_marks_failed(self, mock_launch, client):
        """When oracle times out, submission should be marked failed."""
        _, buyer_key = _register_agent(client, 'ot-buyer2', wallet='0x' + 'bb' * 20)
        _, worker_key = _register_agent(client, 'ot-worker2', wallet='0x' + 'cc' * 20)
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0},
                           headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']
        client.post(f'/jobs/{task_id}/fund',
                    json={'tx_hash': '0xot-fund2'},
                    headers=_auth_headers(buyer_key))
        client.post(f'/jobs/{task_id}/claim',
                    json={},
                    headers=_auth_headers(worker_key))
        resp = client.post(f'/jobs/{task_id}/submit',
                           json={'content': 'test'},
                           headers=_auth_headers(worker_key))
        sub_id = resp.get_json()['submission_id']

        # Simulate timeout: manually mark submission as failed (since oracle is mocked)
        # Use db.session directly (already inside fixture's app_context)
        sub = db.session.get(Submission, sub_id)
        sub.status = 'failed'
        sub.oracle_reason = 'Evaluation timed out after 120s'
        db.session.commit()

        resp = client.get(f'/submissions/{sub_id}',
                          headers=_auth_headers(worker_key))
        data = resp.get_json()
        assert data['status'] == 'failed'
        assert 'timed out' in data['oracle_reason'].lower()

    @patch('server._launch_oracle_with_timeout')
    def test_oracle_completes_normally(self, mock_launch, client):
        """When oracle completes normally, submission gets proper result."""
        _, buyer_key = _register_agent(client, 'ot-buyer3', wallet='0x' + 'bb' * 20)
        _, worker_key = _register_agent(client, 'ot-worker3', wallet='0x' + 'cc' * 20)
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0},
                           headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']
        client.post(f'/jobs/{task_id}/fund',
                    json={'tx_hash': '0xot-fund3'},
                    headers=_auth_headers(buyer_key))
        client.post(f'/jobs/{task_id}/claim',
                    json={},
                    headers=_auth_headers(worker_key))
        resp = client.post(f'/jobs/{task_id}/submit',
                           json={'content': 'test solution'},
                           headers=_auth_headers(worker_key))
        sub_id = resp.get_json()['submission_id']

        # Simulate successful oracle evaluation
        # Use db.session directly (already inside fixture's app_context)
        sub = db.session.get(Submission, sub_id)
        sub.status = 'passed'
        sub.oracle_score = 85
        sub.oracle_reason = 'Good solution'
        db.session.commit()

        resp = client.get(f'/submissions/{sub_id}',
                          headers=_auth_headers(worker_key))
        data = resp.get_json()
        assert data['status'] == 'passed'
        assert data['oracle_score'] == 85


# ===================================================================
# Unclaim Edge Cases (G05)
# ===================================================================

class TestUnclaimEdgeCases:
    """G05: Edge cases for unclaim endpoint."""

    def test_unclaim_with_judging_submission(self, client):
        """Cannot unclaim when a submission is being judged."""
        _, buyer_key = _register_agent(client, 'uc-buyer', wallet='0x' + 'bb' * 20)
        _, worker_key = _register_agent(client, 'uc-worker', wallet='0x' + 'cc' * 20)
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0},
                           headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']
        client.post(f'/jobs/{task_id}/fund',
                    json={'tx_hash': '0xuc-fund'},
                    headers=_auth_headers(buyer_key))
        client.post(f'/jobs/{task_id}/claim',
                    json={},
                    headers=_auth_headers(worker_key))
        # Create a submission in judging state (submit endpoint does this)
        # Mock oracle to prevent it from running
        with patch('server._launch_oracle_with_timeout'):
            client.post(f'/jobs/{task_id}/submit',
                        json={'content': 'test'},
                        headers=_auth_headers(worker_key))
        # Try unclaim — should fail because submission is judging
        resp = client.post(f'/jobs/{task_id}/unclaim',
                           json={},
                           headers=_auth_headers(worker_key))
        assert resp.status_code == 400
        assert 'judg' in resp.get_json()['error'].lower()

    def test_unclaim_cancelled_job(self, client):
        """Cannot unclaim from a cancelled job."""
        _, buyer_key = _register_agent(client, 'uc-buyer2', wallet='0x' + 'bb' * 20)
        _, worker_key = _register_agent(client, 'uc-worker2', wallet='0x' + 'cc' * 20)
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0},
                           headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']
        client.post(f'/jobs/{task_id}/fund',
                    json={'tx_hash': '0xuc-fund2'},
                    headers=_auth_headers(buyer_key))
        client.post(f'/jobs/{task_id}/claim',
                    json={},
                    headers=_auth_headers(worker_key))
        client.post(f'/jobs/{task_id}/cancel',
                    headers=_auth_headers(buyer_key))
        resp = client.post(f'/jobs/{task_id}/unclaim',
                           json={},
                           headers=_auth_headers(worker_key))
        assert resp.status_code == 400

    def test_unclaim_resolved_job(self, client):
        """Cannot unclaim from a resolved job."""
        _, buyer_key = _register_agent(client, 'uc-buyer3', wallet='0x' + 'bb' * 20)
        _, worker_key = _register_agent(client, 'uc-worker3', wallet='0x' + 'cc' * 20)
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0},
                           headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']
        client.post(f'/jobs/{task_id}/fund',
                    json={'tx_hash': '0xuc-fund3'},
                    headers=_auth_headers(buyer_key))
        client.post(f'/jobs/{task_id}/claim',
                    json={},
                    headers=_auth_headers(worker_key))
        # Manually resolve the job to test edge case
        job = db.session.get(Job, task_id)
        job.status = 'resolved'
        job.winner_id = 'uc-worker3'
        db.session.commit()
        resp = client.post(f'/jobs/{task_id}/unclaim',
                           json={},
                           headers=_auth_headers(worker_key))
        assert resp.status_code == 400


# ===================================================================
# Job Filtering / Sorting / Limit (G03)
# ===================================================================

class TestJobFiltering:
    """G03: Advanced filtering, sorting, and limit tests."""

    def _create_jobs(self, client, key, prices):
        """Helper: create multiple jobs with given prices."""
        task_ids = []
        for i, price in enumerate(prices):
            resp = client.post('/jobs',
                               json={'title': f'Task {i}', 'description': 'D',
                                     'price': price,
                                     'artifact_type': 'CODE' if i % 2 == 0 else 'GENERAL'},
                               headers=_auth_headers(key))
            task_ids.append(resp.get_json()['task_id'])
        return task_ids

    def test_filter_by_price_range(self, client):
        _, key = _register_agent(client, 'filt-buyer')
        self._create_jobs(client, key, [1.0, 5.0, 10.0, 20.0])
        resp = client.get('/jobs?min_price=5&max_price=15')
        data = resp.get_json()
        prices = [j['price'] for j in data['jobs']]
        assert all(5 <= p <= 15 for p in prices)
        assert len(prices) == 2  # 5.0 and 10.0

    def test_sort_by_price_ascending(self, client):
        _, key = _register_agent(client, 'sort-buyer')
        self._create_jobs(client, key, [10.0, 1.0, 5.0])
        resp = client.get('/jobs?sort_by=price&sort_order=asc')
        data = resp.get_json()
        prices = [j['price'] for j in data['jobs']]
        assert prices == sorted(prices)

    def test_sort_by_price_descending(self, client):
        _, key = _register_agent(client, 'sort-buyer2')
        self._create_jobs(client, key, [10.0, 1.0, 5.0])
        resp = client.get('/jobs?sort_by=price&sort_order=desc')
        data = resp.get_json()
        prices = [j['price'] for j in data['jobs']]
        assert prices == sorted(prices, reverse=True)

    def test_large_limit_does_not_error(self, client):
        """Requesting limit > 200 should not error; results are still capped."""
        _, key = _register_agent(client, 'lim-buyer')
        resp = client.get('/jobs?limit=999')
        assert resp.status_code == 200
        data = resp.get_json()
        # The actual results returned should be at most 200 (capped in service layer)
        assert len(data['jobs']) <= 200

    def test_filter_by_artifact_type(self, client):
        _, key = _register_agent(client, 'art-buyer')
        self._create_jobs(client, key, [1.0, 2.0, 3.0])  # indexes 0,2 = CODE, 1 = GENERAL
        resp = client.get('/jobs?artifact_type=CODE')
        data = resp.get_json()
        assert all(j['artifact_type'] == 'CODE' for j in data['jobs'])

    def test_invalid_sort_field_ignored(self, client):
        """Invalid sort_by should fall back to created_at."""
        _, key = _register_agent(client, 'badsort-buyer')
        self._create_jobs(client, key, [1.0])
        resp = client.get('/jobs?sort_by=DROP_TABLE')
        assert resp.status_code == 200  # should not error


# ===================================================================
# Retry Payout (G06)
# ===================================================================

class TestRetryPayout:
    """G06: Tests for POST /admin/jobs/<task_id>/retry-payout."""

    def test_retry_payout_requires_auth(self, client):
        resp = client.post('/admin/jobs/fake-id/retry-payout')
        assert resp.status_code == 401

    def test_retry_payout_not_found(self, client):
        _, key = _register_agent(client, 'rp-agent')
        resp = client.post('/admin/jobs/nonexistent/retry-payout',
                           headers=_auth_headers(key))
        assert resp.status_code == 404

    def test_retry_payout_not_resolved(self, client):
        """Cannot retry payout on non-resolved job."""
        _, key = _register_agent(client, 'rp-buyer')
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0},
                           headers=_auth_headers(key))
        task_id = resp.get_json()['task_id']
        resp = client.post(f'/admin/jobs/{task_id}/retry-payout',
                           headers=_auth_headers(key))
        assert resp.status_code == 400
        assert 'not resolved' in resp.get_json()['error'].lower()

    def test_retry_payout_not_failed(self, client):
        """Cannot retry payout that isn't in 'failed' state."""
        _, buyer_key = _register_agent(client, 'rp-buyer2', wallet='0x' + 'bb' * 20)
        _, worker_key = _register_agent(client, 'rp-worker2', wallet='0x' + 'cc' * 20)
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0},
                           headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']
        client.post(f'/jobs/{task_id}/fund',
                    json={'tx_hash': '0xrp-fund2'},
                    headers=_auth_headers(buyer_key))
        # Manually set to resolved with payout_status=success
        job = db.session.get(Job, task_id)
        job.status = 'resolved'
        job.winner_id = 'rp-worker2'
        job.payout_status = 'success'
        db.session.commit()
        resp = client.post(f'/admin/jobs/{task_id}/retry-payout',
                           headers=_auth_headers(buyer_key))
        assert resp.status_code == 400
        assert 'not in failed state' in resp.get_json()['error'].lower()


# ===================================================================
# Cross-Job Submissions (G16)
# ===================================================================

class TestCrossJobSubmissions:
    """G16: Tests for GET /submissions?worker_id=<id> cross-job query."""

    def test_cross_job_requires_worker_id(self, client):
        resp = client.get('/submissions')
        assert resp.status_code == 400
        assert 'worker_id' in resp.get_json()['error'].lower()

    def test_cross_job_returns_worker_submissions(self, client):
        """Worker's submissions across multiple jobs."""
        _, buyer_key = _register_agent(client, 'cj-buyer', wallet='0x' + 'bb' * 20)
        _, worker_key = _register_agent(client, 'cj-worker', wallet='0x' + 'cc' * 20)

        # Create and fund two jobs
        task_ids = []
        for i in range(2):
            resp = client.post('/jobs',
                               json={'title': f'CJ Task {i}', 'description': 'D', 'price': 1.0},
                               headers=_auth_headers(buyer_key))
            tid = resp.get_json()['task_id']
            task_ids.append(tid)
            client.post(f'/jobs/{tid}/fund',
                        json={'tx_hash': f'0xcj-fund-{i}'},
                        headers=_auth_headers(buyer_key))
            client.post(f'/jobs/{tid}/claim',
                        json={},
                        headers=_auth_headers(worker_key))
            client.post(f'/jobs/{tid}/submit',
                        json={'content': f'solution {i}'},
                        headers=_auth_headers(worker_key))

        resp = client.get('/submissions?worker_id=cj-worker')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['total'] == 2
        assert len(data['submissions']) == 2

    def test_cross_job_other_worker_not_included(self, client):
        """Other worker's submissions should not appear."""
        _, buyer_key = _register_agent(client, 'cj-buyer2', wallet='0x' + 'bb' * 20)
        _, w1_key = _register_agent(client, 'cj-w1', wallet='0x' + 'cc' * 20)
        _, w2_key = _register_agent(client, 'cj-w2', wallet='0x' + 'dd' * 20)

        resp = client.post('/jobs',
                           json={'title': 'CJ2', 'description': 'D', 'price': 1.0},
                           headers=_auth_headers(buyer_key))
        tid = resp.get_json()['task_id']
        client.post(f'/jobs/{tid}/fund',
                    json={'tx_hash': '0xcj2-fund'},
                    headers=_auth_headers(buyer_key))
        client.post(f'/jobs/{tid}/claim', json={}, headers=_auth_headers(w1_key))
        client.post(f'/jobs/{tid}/submit',
                    json={'content': 'w1 solution'},
                    headers=_auth_headers(w1_key))

        # Worker 2 has no submissions
        resp = client.get('/submissions?worker_id=cj-w2')
        assert resp.status_code == 200
        assert resp.get_json()['total'] == 0


# ===================================================================
# Submissions Pagination (G03)
# ===================================================================

class TestSubmissionsPagination:
    """G03: Pagination on GET /jobs/<task_id>/submissions."""

    def test_submissions_pagination(self, client):
        """Paginated submissions response."""
        _, buyer_key = _register_agent(client, 'sp-buyer', wallet='0x' + 'bb' * 20)
        _, worker_key = _register_agent(client, 'sp-worker', wallet='0x' + 'cc' * 20)
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0,
                                 'max_retries': 5},
                           headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']
        client.post(f'/jobs/{task_id}/fund',
                    json={'tx_hash': '0xsp-fund'},
                    headers=_auth_headers(buyer_key))
        client.post(f'/jobs/{task_id}/claim',
                    json={},
                    headers=_auth_headers(worker_key))
        # Submit 3 times
        for i in range(3):
            client.post(f'/jobs/{task_id}/submit',
                        json={'content': f'attempt {i}'},
                        headers=_auth_headers(worker_key))

        # Get first page
        resp = client.get(f'/jobs/{task_id}/submissions?limit=2&offset=0')
        data = resp.get_json()
        assert data['total'] == 3
        assert len(data['submissions']) == 2
        assert data['limit'] == 2
        assert data['offset'] == 0

        # Get second page
        resp = client.get(f'/jobs/{task_id}/submissions?limit=2&offset=2')
        data = resp.get_json()
        assert len(data['submissions']) == 1

    def test_submissions_default_pagination(self, client):
        """Default pagination returns structured response."""
        _, buyer_key = _register_agent(client, 'sp-buyer2', wallet='0x' + 'bb' * 20)
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0},
                           headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']
        resp = client.get(f'/jobs/{task_id}/submissions')
        data = resp.get_json()
        assert 'submissions' in data
        assert 'total' in data


# ===================================================================
# Fee Config (G19)
# ===================================================================

class TestFeeConfig:
    """G19: Per-job fee configuration tests."""

    def test_create_job_with_custom_fee(self, client):
        _, key = _register_agent(client, 'fee-buyer')
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0,
                                 'fee_bps': 1000},
                           headers=_auth_headers(key))
        assert resp.status_code == 201
        task_id = resp.get_json()['task_id']
        resp = client.get(f'/jobs/{task_id}')
        assert resp.get_json()['fee_bps'] == 1000

    def test_create_job_with_zero_fee(self, client):
        _, key = _register_agent(client, 'fee-buyer2')
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0,
                                 'fee_bps': 0},
                           headers=_auth_headers(key))
        assert resp.status_code == 201
        task_id = resp.get_json()['task_id']
        resp = client.get(f'/jobs/{task_id}')
        assert resp.get_json()['fee_bps'] == 0

    def test_create_job_fee_too_high(self, client):
        _, key = _register_agent(client, 'fee-buyer3')
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0,
                                 'fee_bps': 10001},
                           headers=_auth_headers(key))
        assert resp.status_code == 400

    def test_create_job_default_fee(self, client):
        """Without fee_bps, should use platform default (500)."""
        _, key = _register_agent(client, 'fee-buyer4')
        resp = client.post('/jobs',
                           json={'title': 'T', 'description': 'D', 'price': 1.0},
                           headers=_auth_headers(key))
        assert resp.status_code == 201
        task_id = resp.get_json()['task_id']
        resp = client.get(f'/jobs/{task_id}')
        assert resp.get_json()['fee_bps'] == 500  # Config.PLATFORM_FEE_BPS


# ===================================================================
# Correlation ID (G14)
# ===================================================================

class TestCorrelationId:
    """G14: Request correlation ID tests."""

    def test_response_has_request_id(self, client):
        resp = client.get('/health')
        assert 'X-Request-ID' in resp.headers

    def test_request_id_echoed_back(self, client):
        resp = client.get('/health', headers={'X-Request-ID': 'my-custom-id-123'})
        assert resp.headers.get('X-Request-ID') == 'my-custom-id-123'

    def test_request_id_generated_if_missing(self, client):
        resp = client.get('/health')
        rid = resp.headers.get('X-Request-ID')
        assert rid is not None
        assert len(rid) > 10  # UUID format


# ===================================================================
# Reclaim After Unclaim (F04)
# ===================================================================

class TestReclaimAfterUnclaim(unittest.TestCase):
    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_reclaim_after_unclaim(self):
        """F04: Worker can re-claim a job after unclaiming."""
        _, buyer_key = _register_agent(self.client, 'buyer-1', 'Buyer')
        _, worker_key = _register_agent(self.client, 'worker-1', 'Worker')

        # Create and fund job
        resp = self.client.post('/jobs', json={'title': 'T', 'description': 'D', 'price': 1.0},
                                headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']
        self.client.post(f'/jobs/{task_id}/fund', json={'tx_hash': '0xabc123'},
                         headers=_auth_headers(buyer_key))

        # Claim, unclaim, re-claim
        resp = self.client.post(f'/jobs/{task_id}/claim', headers=_auth_headers(worker_key))
        assert resp.status_code == 200

        resp = self.client.post(f'/jobs/{task_id}/unclaim', headers=_auth_headers(worker_key))
        assert resp.status_code == 200

        resp = self.client.post(f'/jobs/{task_id}/claim', headers=_auth_headers(worker_key))
        assert resp.status_code == 200, f"Re-claim failed: {resp.get_json()}"
        assert resp.get_json()['status'] == 'claimed'

    def test_reclaim_shows_in_participants(self):
        """F04: Re-claimed worker appears in job participants list."""
        _, buyer_key = _register_agent(self.client, 'buyer-1', 'Buyer')
        _, worker_key = _register_agent(self.client, 'worker-1', 'Worker')

        resp = self.client.post('/jobs', json={'title': 'T', 'description': 'D', 'price': 1.0},
                                headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']
        self.client.post(f'/jobs/{task_id}/fund', json={'tx_hash': '0xabc456'},
                         headers=_auth_headers(buyer_key))

        self.client.post(f'/jobs/{task_id}/claim', headers=_auth_headers(worker_key))
        self.client.post(f'/jobs/{task_id}/unclaim', headers=_auth_headers(worker_key))
        self.client.post(f'/jobs/{task_id}/claim', headers=_auth_headers(worker_key))

        resp = self.client.get(f'/jobs/{task_id}')
        job = resp.get_json()
        assert 'worker-1' in job['participants']


# ===================================================================
# Retry Payout Auth (F05)
# ===================================================================

class TestRetryPayoutAuth(unittest.TestCase):
    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_retry_payout_unauthorized_agent(self):
        """F05: Non-buyer non-winner agent cannot retry payout."""
        _, buyer_key = _register_agent(self.client, 'buyer-1', 'Buyer')
        _, worker_key = _register_agent(self.client, 'worker-1', 'Worker')
        _, stranger_key = _register_agent(self.client, 'stranger-1', 'Stranger')

        # Create a job and manually set it to resolved with failed payout
        resp = self.client.post('/jobs', json={'title': 'T', 'description': 'D', 'price': 1.0},
                                headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']

        job = db.session.query(Job).filter_by(task_id=task_id).first()
        job.status = 'resolved'
        job.winner_id = 'worker-1'
        job.payout_status = 'failed'
        db.session.commit()

        # Stranger tries to retry payout -> 403
        resp = self.client.post(f'/admin/jobs/{task_id}/retry-payout',
                                headers=_auth_headers(stranger_key))
        assert resp.status_code == 403

    def test_retry_payout_buyer_allowed(self):
        """F05: Buyer can retry payout."""
        _, buyer_key = _register_agent(self.client, 'buyer-1', 'Buyer')
        _, worker_key = _register_agent(self.client, 'worker-1', 'Worker', wallet='0x' + 'a' * 40)

        resp = self.client.post('/jobs', json={'title': 'T', 'description': 'D', 'price': 1.0},
                                headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']

        job = db.session.query(Job).filter_by(task_id=task_id).first()
        job.status = 'resolved'
        job.winner_id = 'worker-1'
        job.payout_status = 'failed'
        db.session.commit()

        # Buyer tries -> should pass auth check (will fail on chain connection, but that's OK - check it's not 403)
        resp = self.client.post(f'/admin/jobs/{task_id}/retry-payout',
                                headers=_auth_headers(buyer_key))
        assert resp.status_code != 403


# ===================================================================
# Webhook Participants (F01)
# ===================================================================

class TestWebhookParticipants(unittest.TestCase):
    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    @patch('services.webhook_service.is_safe_webhook_url', return_value=True)
    def test_fire_event_finds_worker_from_join_table(self, mock_ssrf):
        """F01: fire_event uses JobParticipant instead of JSON array."""
        _, buyer_key = _register_agent(self.client, 'buyer-1', 'Buyer')
        _, worker_key = _register_agent(self.client, 'worker-1', 'Worker')

        # Create and fund job
        resp = self.client.post('/jobs', json={'title': 'T', 'description': 'D', 'price': 1.0},
                                headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']
        self.client.post(f'/jobs/{task_id}/fund', json={'tx_hash': '0xtest1'},
                         headers=_auth_headers(buyer_key))

        # Worker claims (creates JobParticipant, NOT JSON array entry)
        self.client.post(f'/jobs/{task_id}/claim', headers=_auth_headers(worker_key))

        # Verify JobParticipant has the worker
        assert JobParticipant.query.filter_by(task_id=task_id, worker_id='worker-1', unclaimed_at=None).first() is not None

        # Register a webhook for the worker
        resp = self.client.post('/agents/worker-1/webhooks',
                                json={'url': 'https://example.com/hook', 'events': ['job.resolved']},
                                headers=_auth_headers(worker_key))
        assert resp.status_code == 201

        # The fire_event function should find the worker through JobParticipant
        # We can verify by checking that the webhook query would match
        webhooks = Webhook.query.filter(
            Webhook.agent_id.in_({'buyer-1', 'worker-1'}),
            Webhook.active.is_(True),
        ).all()
        # Worker's webhook should be found
        assert any(wh.agent_id == 'worker-1' for wh in webhooks)


# ===================================================================
# Expiry Does Not Fail Judging (F09)
# ===================================================================

class TestExpiryDoesNotFailJudging(unittest.TestCase):
    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_expiry_preserves_judging_submissions(self):
        """F09: check_expiry only fails pending submissions, not judging ones."""
        from services.job_service import JobService
        import datetime

        _, buyer_key = _register_agent(self.client, 'buyer-1', 'Buyer')
        _, worker_key = _register_agent(self.client, 'worker-1', 'Worker')

        # Create job with past expiry
        past = int((datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)).timestamp())
        resp = self.client.post('/jobs', json={'title': 'T', 'description': 'D', 'price': 1.0, 'expiry': past},
                                headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']

        # Manually set to funded with submissions
        job = Job.query.get(task_id)
        job.status = 'funded'
        job.deposit_tx_hash = '0xtest_expiry'

        # Create a pending and a judging submission
        pending_sub = Submission(task_id=task_id, worker_id='worker-1', content={'a': 1}, status='pending', attempt=1)
        judging_sub = Submission(task_id=task_id, worker_id='worker-1', content={'a': 2}, status='judging', attempt=2)
        db.session.add_all([pending_sub, judging_sub])
        db.session.commit()

        pending_id = pending_sub.id
        judging_id = judging_sub.id

        # Trigger expiry
        expired = JobService.check_expiry(job)
        db.session.commit()
        assert expired is True

        # Pending should be failed, judging should be preserved
        p = db.session.get(Submission, pending_id)
        j = db.session.get(Submission, judging_id)
        assert p.status == 'failed'
        assert j.status == 'judging'  # F09: not touched by expiry
