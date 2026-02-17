"""
End-to-end test scenarios from Section 3 of docs/test-plan.md.

Scenarios:
  A: Happy Path — Full Resolution
  B: Task Timeout / No Takers
  C: Rejection -> Retry -> Pass
  D: Dispute Flow
  E: Concurrent Claims
"""
import os
import unittest
from unittest.mock import patch

# Force test DB before importing app
os.environ['DATABASE_URL'] = 'sqlite://'  # in-memory

from server import app
from models import db, Agent, Job, Submission, JobParticipant, Dispute


def _register_agent(client, agent_id, name='Test Agent', wallet=None):
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
# Scenario A: Happy Path -- Full Resolution
# ===================================================================

class TestScenarioA_HappyPath(unittest.TestCase):
    """
    Scenario A: Buyer registers -> creates job -> funds -> Worker registers ->
    claims -> submits -> Oracle passes (simulated) -> Job resolved ->
    verify winner_id, payout fields, solvency.
    """

    def setUp(self):
        app.config['TESTING'] = True
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
        # Provide mock wallet service so fund/payout work without a real chain
        import services.wallet_service as ws_mod
        from unittest.mock import MagicMock
        mock_wallet = MagicMock()
        mock_wallet.is_connected.return_value = True
        mock_wallet.get_ops_address.return_value = '0x' + '00' * 20
        mock_wallet.verify_deposit.return_value = {
            'valid': True, 'depositor': '', 'amount': None,
        }
        mock_wallet.payout.return_value = {'payout_tx': '0xpayout_mock', 'fee_tx': '0xfee_mock'}
        mock_wallet.refund.return_value = '0xrefund_mock'
        mock_wallet.estimate_gas.return_value = {"error": "mock"}
        ws_mod._wallet_service = mock_wallet
        self.ctx = app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = app.test_client()
        # Reset rate limiters
        from services.rate_limiter import _api_limiter, _submit_limiter
        _api_limiter._requests.clear()
        _submit_limiter._requests.clear()

    def tearDown(self):
        from server import _shutdown_event, _oracle_executor, _pending_oracles, _pending_lock
        _shutdown_event.set()
        _oracle_executor.shutdown(wait=False)
        from services.webhook_service import shutdown_webhook_pool
        shutdown_webhook_pool(wait=False)
        import time
        time.sleep(0.05)
        db.session.remove()
        db.drop_all()
        _oracle_executor.ensure_pool()
        with _pending_lock:
            _pending_oracles.clear()
        _shutdown_event.clear()
        self.ctx.pop()

    @patch('server._launch_oracle_with_timeout')
    def test_happy_path_full_resolution(self, mock_oracle):
        c = self.client

        # 1. Buyer registers
        buyer_id, buyer_key = _register_agent(c, 'a-buyer', 'Buyer A',
                                               wallet='0x' + 'aa' * 20)

        # 2. Worker registers (with wallet for payout)
        worker_id, worker_key = _register_agent(c, 'a-worker', 'Worker A',
                                                 wallet='0x' + 'bb' * 20)

        # 3. Buyer creates job
        resp = c.post('/jobs', json={
            'title': 'E2E Happy Path Task',
            'description': 'Build a widget',
            'price': 1.0,
            'rubric': 'Must be functional',
        }, headers=_auth_headers(buyer_key))
        self.assertEqual(resp.status_code, 201)
        task_id = resp.get_json()['task_id']

        # 4. Buyer funds job
        resp = c.post(f'/jobs/{task_id}/fund',
                       json={'tx_hash': '0xhappy-fund'},
                       headers=_auth_headers(buyer_key))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['status'], 'funded')

        # 5. Worker sees funded job in list
        resp = c.get('/jobs?status=funded')
        self.assertEqual(resp.status_code, 200)
        job_ids = [j['task_id'] for j in resp.get_json()['jobs']]
        self.assertIn(task_id, job_ids)

        # 6. Worker claims
        resp = c.post(f'/jobs/{task_id}/claim', json={},
                       headers=_auth_headers(worker_key))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['status'], 'claimed')

        # 7. Worker submits (oracle is mocked)
        resp = c.post(f'/jobs/{task_id}/submit',
                       json={'content': {'solution': 'high quality widget'}},
                       headers=_auth_headers(worker_key))
        self.assertEqual(resp.status_code, 202)
        sub_id = resp.get_json()['submission_id']
        self.assertEqual(resp.get_json()['status'], 'judging')
        self.assertEqual(resp.get_json()['attempt'], 1)

        # 8. Simulate oracle passing the submission
        sub = db.session.get(Submission, sub_id)
        sub.status = 'passed'
        sub.oracle_score = 85
        sub.oracle_reason = 'Good solution'

        # Set job to resolved with winner
        job = db.session.get(Job, task_id)
        job.status = 'resolved'
        job.winner_id = worker_id
        job.result_data = {'solution': 'high quality widget'}
        job.payout_status = 'skipped'  # no chain connected in test
        db.session.commit()

        # 9. Verify job is resolved
        resp = c.get(f'/jobs/{task_id}')
        self.assertEqual(resp.status_code, 200)
        job_data = resp.get_json()
        self.assertEqual(job_data['status'], 'resolved')
        self.assertEqual(job_data['winner_id'], worker_id)

        # 10. Verify participants list
        participant_ids = [p['agent_id'] for p in job_data['participants']]
        self.assertIn(worker_id, participant_ids)

        # 11. Verify submission status
        resp = c.get(f'/submissions/{sub_id}',
                      headers=_auth_headers(worker_key))
        self.assertEqual(resp.status_code, 200)
        sub_data = resp.get_json()
        self.assertEqual(sub_data['status'], 'passed')
        self.assertEqual(sub_data['oracle_score'], 85)

        # 12. Verify worker profile
        resp = c.get(f'/agents/{worker_id}')
        self.assertEqual(resp.status_code, 200)


# ===================================================================
# Scenario B: Task Timeout / No Takers
# ===================================================================

class TestScenarioB_TaskTimeout(unittest.TestCase):
    """
    Scenario B: Buyer creates job with short expiry -> funds ->
    trigger expiry -> job becomes expired -> buyer refunds -> verify refund.
    """

    def setUp(self):
        app.config['TESTING'] = True
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
        # Provide mock wallet service so fund/payout work without a real chain
        import services.wallet_service as ws_mod
        from unittest.mock import MagicMock
        mock_wallet = MagicMock()
        mock_wallet.is_connected.return_value = True
        mock_wallet.get_ops_address.return_value = '0x' + '00' * 20
        mock_wallet.verify_deposit.return_value = {
            'valid': True, 'depositor': '', 'amount': None,
        }
        mock_wallet.payout.return_value = {'payout_tx': '0xpayout_mock', 'fee_tx': '0xfee_mock'}
        mock_wallet.refund.return_value = '0xrefund_mock'
        mock_wallet.estimate_gas.return_value = {"error": "mock"}
        ws_mod._wallet_service = mock_wallet
        self.ctx = app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = app.test_client()
        from services.rate_limiter import _api_limiter, _submit_limiter
        _api_limiter._requests.clear()
        _submit_limiter._requests.clear()

    def tearDown(self):
        from server import _shutdown_event, _oracle_executor, _pending_oracles, _pending_lock
        _shutdown_event.set()
        _oracle_executor.shutdown(wait=False)
        from services.webhook_service import shutdown_webhook_pool
        shutdown_webhook_pool(wait=False)
        import time
        time.sleep(0.05)
        db.session.remove()
        db.drop_all()
        _oracle_executor.ensure_pool()
        with _pending_lock:
            _pending_oracles.clear()
        _shutdown_event.clear()
        self.ctx.pop()

    def test_task_timeout_and_refund(self):
        import datetime
        c = self.client

        # 1. Buyer registers
        buyer_id, buyer_key = _register_agent(c, 'b-buyer', 'Buyer B',
                                               wallet='0x' + 'aa' * 20)

        # 2. Create job with past expiry
        past = int((datetime.datetime.now(datetime.timezone.utc)
                     - datetime.timedelta(hours=1)).timestamp())
        resp = c.post('/jobs', json={
            'title': 'Timeout Task',
            'description': 'This will expire',
            'price': 2.0,
            'expiry': past,
        }, headers=_auth_headers(buyer_key))
        self.assertEqual(resp.status_code, 201)
        task_id = resp.get_json()['task_id']

        # 3. Fund the job
        resp = c.post(f'/jobs/{task_id}/fund',
                       json={'tx_hash': '0xtimeout-fund'},
                       headers=_auth_headers(buyer_key))
        self.assertEqual(resp.status_code, 200)

        # 4. Trigger expiry via JobService.check_expiry
        from services.job_service import JobService
        job = db.session.get(Job, task_id)
        expired = JobService.check_expiry(job)
        db.session.commit()
        self.assertTrue(expired)

        # 5. Verify status is expired
        resp = c.get(f'/jobs/{task_id}')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['status'], 'expired')

        # 6. Buyer refunds
        resp = c.post(f'/jobs/{task_id}/refund',
                       headers=_auth_headers(buyer_key))
        self.assertEqual(resp.status_code, 200)
        refund_data = resp.get_json()
        self.assertEqual(refund_data['status'], 'refunded')
        self.assertEqual(refund_data['amount'], 2.0)


# ===================================================================
# Scenario C: Rejection -> Retry -> Pass
# ===================================================================

class TestScenarioC_RejectionRetryPass(unittest.TestCase):
    """
    Scenario C: Buyer creates strict job -> Worker claims -> submits low quality ->
    Oracle fails (simulated) -> Worker resubmits -> Oracle passes (simulated) -> resolved.
    """

    def setUp(self):
        app.config['TESTING'] = True
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
        # Provide mock wallet service so fund/payout work without a real chain
        import services.wallet_service as ws_mod
        from unittest.mock import MagicMock
        mock_wallet = MagicMock()
        mock_wallet.is_connected.return_value = True
        mock_wallet.get_ops_address.return_value = '0x' + '00' * 20
        mock_wallet.verify_deposit.return_value = {
            'valid': True, 'depositor': '', 'amount': None,
        }
        mock_wallet.payout.return_value = {'payout_tx': '0xpayout_mock', 'fee_tx': '0xfee_mock'}
        mock_wallet.refund.return_value = '0xrefund_mock'
        mock_wallet.estimate_gas.return_value = {"error": "mock"}
        ws_mod._wallet_service = mock_wallet
        self.ctx = app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = app.test_client()
        from services.rate_limiter import _api_limiter, _submit_limiter
        _api_limiter._requests.clear()
        _submit_limiter._requests.clear()

    def tearDown(self):
        from server import _shutdown_event, _oracle_executor, _pending_oracles, _pending_lock
        _shutdown_event.set()
        _oracle_executor.shutdown(wait=False)
        from services.webhook_service import shutdown_webhook_pool
        shutdown_webhook_pool(wait=False)
        import time
        time.sleep(0.05)
        db.session.remove()
        db.drop_all()
        _oracle_executor.ensure_pool()
        with _pending_lock:
            _pending_oracles.clear()
        _shutdown_event.clear()
        self.ctx.pop()

    @patch('server._launch_oracle_with_timeout')
    def test_rejection_retry_pass(self, mock_oracle):
        c = self.client

        # 1. Register agents
        buyer_id, buyer_key = _register_agent(c, 'c-buyer', 'Buyer C',
                                               wallet='0x' + 'aa' * 20)
        worker_id, worker_key = _register_agent(c, 'c-worker', 'Worker C',
                                                 wallet='0x' + 'bb' * 20)

        # 2. Create strict job with max_retries=3
        resp = c.post('/jobs', json={
            'title': 'Strict Task',
            'description': 'Must be perfect',
            'price': 5.0,
            'rubric': 'Extremely strict criteria',
            'max_retries': 3,
        }, headers=_auth_headers(buyer_key))
        self.assertEqual(resp.status_code, 201)
        task_id = resp.get_json()['task_id']

        # 3. Fund job
        c.post(f'/jobs/{task_id}/fund',
               json={'tx_hash': '0xstrict-fund'},
               headers=_auth_headers(buyer_key))

        # 4. Worker claims
        resp = c.post(f'/jobs/{task_id}/claim', json={},
                       headers=_auth_headers(worker_key))
        self.assertEqual(resp.status_code, 200)

        # 5. First submission: low quality
        resp = c.post(f'/jobs/{task_id}/submit',
                       json={'content': 'low quality effort'},
                       headers=_auth_headers(worker_key))
        self.assertEqual(resp.status_code, 202)
        sub1_id = resp.get_json()['submission_id']
        self.assertEqual(resp.get_json()['attempt'], 1)

        # 6. Simulate oracle failure for first submission
        sub1 = db.session.get(Submission, sub1_id)
        sub1.status = 'failed'
        sub1.oracle_score = 40
        sub1.oracle_reason = 'Low quality - does not meet rubric criteria'
        # Increment failure count on job
        job = db.session.get(Job, task_id)
        job.failure_count = (job.failure_count or 0) + 1
        db.session.commit()

        # 7. Verify job is still funded (not resolved) after failure
        resp = c.get(f'/jobs/{task_id}')
        job_data = resp.get_json()
        self.assertEqual(job_data['status'], 'funded')

        # 8. Verify failure count
        job = db.session.get(Job, task_id)
        self.assertEqual(job.failure_count, 1)

        # 9. Verify first submission is failed
        resp = c.get(f'/submissions/{sub1_id}',
                      headers=_auth_headers(worker_key))
        sub1_data = resp.get_json()
        self.assertEqual(sub1_data['status'], 'failed')
        self.assertEqual(sub1_data['oracle_score'], 40)
        self.assertIn('Low quality', sub1_data['oracle_reason'])

        # 10. Second submission: improved quality
        resp = c.post(f'/jobs/{task_id}/submit',
                       json={'content': 'much improved high quality solution'},
                       headers=_auth_headers(worker_key))
        self.assertEqual(resp.status_code, 202)
        sub2_id = resp.get_json()['submission_id']
        self.assertEqual(resp.get_json()['attempt'], 2)

        # 11. Simulate oracle passing second submission
        sub2 = db.session.get(Submission, sub2_id)
        sub2.status = 'passed'
        sub2.oracle_score = 85
        sub2.oracle_reason = 'Meets all criteria'

        # Set job to resolved
        job = db.session.get(Job, task_id)
        job.status = 'resolved'
        job.winner_id = worker_id
        job.payout_status = 'skipped'
        db.session.commit()

        # 12. Verify second submission is passed with attempt=2
        resp = c.get(f'/submissions/{sub2_id}',
                      headers=_auth_headers(worker_key))
        sub2_data = resp.get_json()
        self.assertEqual(sub2_data['status'], 'passed')
        self.assertEqual(sub2_data['oracle_score'], 85)
        self.assertEqual(sub2_data['attempt'], 2)

        # 13. Verify job resolved with winner
        resp = c.get(f'/jobs/{task_id}')
        job_data = resp.get_json()
        self.assertEqual(job_data['status'], 'resolved')
        self.assertEqual(job_data['winner_id'], worker_id)


# ===================================================================
# Scenario D: Dispute Flow
# ===================================================================

class TestScenarioD_DisputeFlow(unittest.TestCase):
    """
    Scenario D: Complete Scenario A -> Buyer disputes -> verify dispute created ->
    Worker also disputes -> Third party blocked.
    """

    def setUp(self):
        app.config['TESTING'] = True
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
        # Provide mock wallet service so fund/payout work without a real chain
        import services.wallet_service as ws_mod
        from unittest.mock import MagicMock
        mock_wallet = MagicMock()
        mock_wallet.is_connected.return_value = True
        mock_wallet.get_ops_address.return_value = '0x' + '00' * 20
        mock_wallet.verify_deposit.return_value = {
            'valid': True, 'depositor': '', 'amount': None,
        }
        mock_wallet.payout.return_value = {'payout_tx': '0xpayout_mock', 'fee_tx': '0xfee_mock'}
        mock_wallet.refund.return_value = '0xrefund_mock'
        mock_wallet.estimate_gas.return_value = {"error": "mock"}
        ws_mod._wallet_service = mock_wallet
        self.ctx = app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = app.test_client()
        from services.rate_limiter import _api_limiter, _submit_limiter
        _api_limiter._requests.clear()
        _submit_limiter._requests.clear()

    def tearDown(self):
        from server import _shutdown_event, _oracle_executor, _pending_oracles, _pending_lock
        _shutdown_event.set()
        _oracle_executor.shutdown(wait=False)
        from services.webhook_service import shutdown_webhook_pool
        shutdown_webhook_pool(wait=False)
        import time
        time.sleep(0.05)
        db.session.remove()
        db.drop_all()
        _oracle_executor.ensure_pool()
        with _pending_lock:
            _pending_oracles.clear()
        _shutdown_event.clear()
        self.ctx.pop()

    @patch('server._launch_oracle_with_timeout')
    def test_dispute_flow(self, mock_oracle):
        c = self.client

        # --- Setup: Complete a job (Scenario A mini) ---
        buyer_id, buyer_key = _register_agent(c, 'd-buyer', 'Buyer D',
                                               wallet='0x' + 'aa' * 20)
        worker_id, worker_key = _register_agent(c, 'd-worker', 'Worker D',
                                                 wallet='0x' + 'bb' * 20)

        resp = c.post('/jobs', json={
            'title': 'Dispute Test Task',
            'description': 'Will be disputed',
            'price': 3.0,
        }, headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']

        # Fund
        c.post(f'/jobs/{task_id}/fund',
               json={'tx_hash': '0xdispute-fund'},
               headers=_auth_headers(buyer_key))

        # Claim
        c.post(f'/jobs/{task_id}/claim', json={},
               headers=_auth_headers(worker_key))

        # Submit
        resp = c.post(f'/jobs/{task_id}/submit',
                       json={'content': 'dispute test solution'},
                       headers=_auth_headers(worker_key))
        sub_id = resp.get_json()['submission_id']

        # Simulate oracle pass and resolve
        sub = db.session.get(Submission, sub_id)
        sub.status = 'passed'
        sub.oracle_score = 90
        job = db.session.get(Job, task_id)
        job.status = 'resolved'
        job.winner_id = worker_id
        job.payout_status = 'skipped'
        db.session.commit()

        # --- Dispute tests ---

        # 1. Buyer disputes
        resp = c.post(f'/jobs/{task_id}/dispute',
                       json={'reason': 'Result does not match requirements'},
                       headers=_auth_headers(buyer_key))
        self.assertEqual(resp.status_code, 202)
        dispute_data = resp.get_json()
        self.assertEqual(dispute_data['status'], 'dispute_filed')
        self.assertEqual(dispute_data['filed_by'], buyer_id)
        self.assertIn('dispute_id', dispute_data)

        # 2. Worker also disputes (both parties can dispute)
        resp = c.post(f'/jobs/{task_id}/dispute',
                       json={'reason': 'Buyer feedback was unfair'},
                       headers=_auth_headers(worker_key))
        self.assertEqual(resp.status_code, 202)
        worker_dispute = resp.get_json()
        self.assertEqual(worker_dispute['filed_by'], worker_id)

        # 3. Register third-party agent
        _, third_key = _register_agent(c, 'd-third', 'Third Party')

        # Third party cannot dispute (not buyer or winner)
        resp = c.post(f'/jobs/{task_id}/dispute',
                       json={'reason': 'I want in on this'},
                       headers=_auth_headers(third_key))
        self.assertEqual(resp.status_code, 403)

    @patch('server._launch_oracle_with_timeout')
    def test_dispute_requires_resolved_state(self, mock_oracle):
        """Cannot dispute a job that is not resolved."""
        c = self.client

        buyer_id, buyer_key = _register_agent(c, 'd2-buyer', 'Buyer D2',
                                               wallet='0x' + 'aa' * 20)

        resp = c.post('/jobs', json={
            'title': 'Open Job',
            'description': 'Not resolved',
            'price': 1.0,
        }, headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']

        # Try to dispute an open job
        resp = c.post(f'/jobs/{task_id}/dispute',
                       json={'reason': 'Too early'},
                       headers=_auth_headers(buyer_key))
        self.assertEqual(resp.status_code, 400)


# ===================================================================
# Scenario E: Concurrent Claims
# ===================================================================

class TestScenarioE_ConcurrentClaims(unittest.TestCase):
    """
    Scenario E: Create funded job -> 3 workers all claim -> verify all in
    participants -> Workers A and B submit -> simulate B passes first ->
    Job resolved with winner=B -> Worker C cannot submit (job resolved).
    """

    def setUp(self):
        app.config['TESTING'] = True
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
        # Provide mock wallet service so fund/payout work without a real chain
        import services.wallet_service as ws_mod
        from unittest.mock import MagicMock
        mock_wallet = MagicMock()
        mock_wallet.is_connected.return_value = True
        mock_wallet.get_ops_address.return_value = '0x' + '00' * 20
        mock_wallet.verify_deposit.return_value = {
            'valid': True, 'depositor': '', 'amount': None,
        }
        mock_wallet.payout.return_value = {'payout_tx': '0xpayout_mock', 'fee_tx': '0xfee_mock'}
        mock_wallet.refund.return_value = '0xrefund_mock'
        mock_wallet.estimate_gas.return_value = {"error": "mock"}
        ws_mod._wallet_service = mock_wallet
        self.ctx = app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = app.test_client()
        from services.rate_limiter import _api_limiter, _submit_limiter
        _api_limiter._requests.clear()
        _submit_limiter._requests.clear()

    def tearDown(self):
        from server import _shutdown_event, _oracle_executor, _pending_oracles, _pending_lock
        _shutdown_event.set()
        _oracle_executor.shutdown(wait=False)
        from services.webhook_service import shutdown_webhook_pool
        shutdown_webhook_pool(wait=False)
        import time
        time.sleep(0.05)
        db.session.remove()
        db.drop_all()
        _oracle_executor.ensure_pool()
        with _pending_lock:
            _pending_oracles.clear()
        _shutdown_event.clear()
        self.ctx.pop()

    @patch('server._launch_oracle_with_timeout')
    def test_concurrent_claims_and_resolution(self, mock_oracle):
        c = self.client

        # 1. Register buyer and 3 workers
        buyer_id, buyer_key = _register_agent(c, 'e-buyer', 'Buyer E',
                                               wallet='0x' + 'aa' * 20)
        worker_a_id, worker_a_key = _register_agent(c, 'e-worker-a', 'Worker A',
                                                      wallet='0x' + 'bb' * 20)
        worker_b_id, worker_b_key = _register_agent(c, 'e-worker-b', 'Worker B',
                                                      wallet='0x' + 'cc' * 20)
        worker_c_id, worker_c_key = _register_agent(c, 'e-worker-c', 'Worker C',
                                                      wallet='0x' + 'dd' * 20)

        # 2. Create and fund job
        resp = c.post('/jobs', json={
            'title': 'Concurrent Claims Task',
            'description': 'Multi-worker test',
            'price': 10.0,
            'max_retries': 3,
        }, headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']

        c.post(f'/jobs/{task_id}/fund',
               json={'tx_hash': '0xconcurrent-fund'},
               headers=_auth_headers(buyer_key))

        # 3. All three workers claim
        for key in [worker_a_key, worker_b_key, worker_c_key]:
            resp = c.post(f'/jobs/{task_id}/claim', json={},
                           headers=_auth_headers(key))
            self.assertEqual(resp.status_code, 200)

        # 4. Verify all 3 workers are in participants
        resp = c.get(f'/jobs/{task_id}')
        self.assertEqual(resp.status_code, 200)
        participants = resp.get_json()['participants']
        participant_ids = [p['agent_id'] for p in participants]
        self.assertIn(worker_a_id, participant_ids)
        self.assertIn(worker_b_id, participant_ids)
        self.assertIn(worker_c_id, participant_ids)
        self.assertEqual(len(participants), 3)

        # 5. Worker A submits
        resp = c.post(f'/jobs/{task_id}/submit',
                       json={'content': 'Worker A mediocre solution'},
                       headers=_auth_headers(worker_a_key))
        self.assertEqual(resp.status_code, 202)
        sub_a_id = resp.get_json()['submission_id']

        # 6. Worker B submits
        resp = c.post(f'/jobs/{task_id}/submit',
                       json={'content': 'Worker B excellent solution'},
                       headers=_auth_headers(worker_b_key))
        self.assertEqual(resp.status_code, 202)
        sub_b_id = resp.get_json()['submission_id']

        # 7. Simulate B passes first: B's submission passes, job resolved
        sub_b = db.session.get(Submission, sub_b_id)
        sub_b.status = 'passed'
        sub_b.oracle_score = 92

        # Mark A's submission as failed (discarded since B won)
        sub_a = db.session.get(Submission, sub_a_id)
        sub_a.status = 'failed'
        sub_a.oracle_reason = 'Another submission was accepted first'

        job = db.session.get(Job, task_id)
        job.status = 'resolved'
        job.winner_id = worker_b_id
        job.payout_status = 'skipped'
        db.session.commit()

        # 8. Worker C tries to submit -> should fail (job already resolved)
        resp = c.post(f'/jobs/{task_id}/submit',
                       json={'content': 'Worker C too late'},
                       headers=_auth_headers(worker_c_key))
        self.assertEqual(resp.status_code, 400)

        # 9. Verify only one winner
        resp = c.get(f'/jobs/{task_id}')
        job_data = resp.get_json()
        self.assertEqual(job_data['status'], 'resolved')
        self.assertEqual(job_data['winner_id'], worker_b_id)

        # 10. Verify submission statuses
        resp = c.get(f'/submissions/{sub_b_id}',
                      headers=_auth_headers(worker_b_key))
        self.assertEqual(resp.get_json()['status'], 'passed')

        resp = c.get(f'/submissions/{sub_a_id}',
                      headers=_auth_headers(worker_a_key))
        self.assertEqual(resp.get_json()['status'], 'failed')

    @patch('server._launch_oracle_with_timeout')
    def test_duplicate_claim_rejected(self, mock_oracle):
        """A worker who already claimed cannot claim again."""
        c = self.client

        buyer_id, buyer_key = _register_agent(c, 'e2-buyer', 'Buyer E2',
                                               wallet='0x' + 'aa' * 20)
        worker_id, worker_key = _register_agent(c, 'e2-worker', 'Worker E2',
                                                  wallet='0x' + 'bb' * 20)

        resp = c.post('/jobs', json={
            'title': 'Dup Claim Task',
            'description': 'Desc',
            'price': 1.0,
        }, headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']

        c.post(f'/jobs/{task_id}/fund',
               json={'tx_hash': '0xdup-claim-fund'},
               headers=_auth_headers(buyer_key))

        # First claim succeeds
        resp = c.post(f'/jobs/{task_id}/claim', json={},
                       headers=_auth_headers(worker_key))
        self.assertEqual(resp.status_code, 200)

        # Second claim should be rejected
        resp = c.post(f'/jobs/{task_id}/claim', json={},
                       headers=_auth_headers(worker_key))
        self.assertEqual(resp.status_code, 409)

    @patch('server._launch_oracle_with_timeout')
    def test_resolved_job_blocks_all_new_submissions(self, mock_oracle):
        """After a job is resolved, no further submissions are accepted."""
        c = self.client

        buyer_id, buyer_key = _register_agent(c, 'e3-buyer', 'Buyer E3',
                                               wallet='0x' + 'aa' * 20)
        worker_a_id, worker_a_key = _register_agent(c, 'e3-worker-a', 'Worker A3',
                                                      wallet='0x' + 'bb' * 20)
        worker_b_id, worker_b_key = _register_agent(c, 'e3-worker-b', 'Worker B3',
                                                      wallet='0x' + 'cc' * 20)

        resp = c.post('/jobs', json={
            'title': 'Resolved Block Task',
            'description': 'Desc',
            'price': 2.0,
        }, headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']

        c.post(f'/jobs/{task_id}/fund',
               json={'tx_hash': '0xresolved-block-fund'},
               headers=_auth_headers(buyer_key))

        # Both workers claim
        c.post(f'/jobs/{task_id}/claim', json={},
               headers=_auth_headers(worker_a_key))
        c.post(f'/jobs/{task_id}/claim', json={},
               headers=_auth_headers(worker_b_key))

        # Worker A submits
        resp = c.post(f'/jobs/{task_id}/submit',
                       json={'content': 'solution a'},
                       headers=_auth_headers(worker_a_key))
        sub_a_id = resp.get_json()['submission_id']

        # Simulate A passes -> job resolved
        sub_a = db.session.get(Submission, sub_a_id)
        sub_a.status = 'passed'
        sub_a.oracle_score = 88

        job = db.session.get(Job, task_id)
        job.status = 'resolved'
        job.winner_id = worker_a_id
        job.payout_status = 'skipped'
        db.session.commit()

        # Worker B tries to submit -> 400
        resp = c.post(f'/jobs/{task_id}/submit',
                       json={'content': 'solution b'},
                       headers=_auth_headers(worker_b_key))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('not accepting submissions', resp.get_json()['error'].lower())


# ===================================================================
# Scenario F: Oracle Low Score (REJECTED)
# ===================================================================

class TestScenarioF_OracleLowScore(unittest.TestCase):
    """
    Scenario F: Worker submits low quality -> oracle rejects (score < threshold) ->
    Worker retries with improved submission -> oracle passes -> job resolved.
    Also tests max_retries exhaustion.
    """

    def setUp(self):
        app.config['TESTING'] = True
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
        # Provide mock wallet service so fund/payout work without a real chain
        import services.wallet_service as ws_mod
        from unittest.mock import MagicMock
        mock_wallet = MagicMock()
        mock_wallet.is_connected.return_value = True
        mock_wallet.get_ops_address.return_value = '0x' + '00' * 20
        mock_wallet.verify_deposit.return_value = {
            'valid': True, 'depositor': '', 'amount': None,
        }
        mock_wallet.payout.return_value = {'payout_tx': '0xpayout_mock', 'fee_tx': '0xfee_mock'}
        mock_wallet.refund.return_value = '0xrefund_mock'
        mock_wallet.estimate_gas.return_value = {"error": "mock"}
        ws_mod._wallet_service = mock_wallet
        self.ctx = app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = app.test_client()
        from services.rate_limiter import _api_limiter, _submit_limiter
        _api_limiter._requests.clear()
        _submit_limiter._requests.clear()

    def tearDown(self):
        from server import _shutdown_event, _oracle_executor, _pending_oracles, _pending_lock
        _shutdown_event.set()
        _oracle_executor.shutdown(wait=False)
        from services.webhook_service import shutdown_webhook_pool
        shutdown_webhook_pool(wait=False)
        import time
        time.sleep(0.05)
        db.session.remove()
        db.drop_all()
        _oracle_executor.ensure_pool()
        with _pending_lock:
            _pending_oracles.clear()
        _shutdown_event.clear()
        self.ctx.pop()

    @patch('server._launch_oracle_with_timeout')
    def test_oracle_rejection_then_retry_pass(self, mock_oracle):
        c = self.client

        # Register buyer + worker
        buyer_id, buyer_key = _register_agent(c, 'f-buyer', 'Buyer F',
                                               wallet='0x' + 'aa' * 20)
        worker_id, worker_key = _register_agent(c, 'f-worker', 'Worker F',
                                                 wallet='0x' + 'bb' * 20)

        # Create job with max_retries=3
        resp = c.post('/jobs', json={
            'title': 'Low Score Task',
            'description': 'Needs high quality',
            'price': 1.0,
            'rubric': 'Must score above 70',
            'max_retries': 3,
        }, headers=_auth_headers(buyer_key))
        self.assertEqual(resp.status_code, 201)
        task_id = resp.get_json()['task_id']

        # Fund
        c.post(f'/jobs/{task_id}/fund',
               json={'tx_hash': '0xf-fund'},
               headers=_auth_headers(buyer_key))

        # Worker claims
        resp = c.post(f'/jobs/{task_id}/claim', json={},
                       headers=_auth_headers(worker_key))
        self.assertEqual(resp.status_code, 200)

        # Submit attempt 1
        resp = c.post(f'/jobs/{task_id}/submit',
                       json={'content': 'sloppy work'},
                       headers=_auth_headers(worker_key))
        self.assertEqual(resp.status_code, 202)
        sub1_id = resp.get_json()['submission_id']
        self.assertEqual(resp.get_json()['attempt'], 1)

        # Simulate rejection: score 40
        sub1 = db.session.get(Submission, sub1_id)
        sub1.status = 'failed'
        sub1.oracle_score = 40
        sub1.oracle_reason = 'Below threshold'
        job = db.session.get(Job, task_id)
        job.failure_count = (job.failure_count or 0) + 1
        db.session.commit()

        # Verify: submission failed, job still funded
        resp = c.get(f'/submissions/{sub1_id}',
                      headers=_auth_headers(worker_key))
        self.assertEqual(resp.get_json()['status'], 'failed')

        resp = c.get(f'/jobs/{task_id}')
        self.assertEqual(resp.get_json()['status'], 'funded')

        # Submit attempt 2
        resp = c.post(f'/jobs/{task_id}/submit',
                       json={'content': 'much better work'},
                       headers=_auth_headers(worker_key))
        self.assertEqual(resp.status_code, 202)
        sub2_id = resp.get_json()['submission_id']
        self.assertEqual(resp.get_json()['attempt'], 2)

        # Simulate pass: score 85
        sub2 = db.session.get(Submission, sub2_id)
        sub2.status = 'passed'
        sub2.oracle_score = 85
        sub2.oracle_reason = 'Meets criteria'
        job = db.session.get(Job, task_id)
        job.status = 'resolved'
        job.winner_id = worker_id
        job.payout_status = 'skipped'
        db.session.commit()

        # Verify: job resolved, winner correct, attempt=2
        resp = c.get(f'/jobs/{task_id}')
        job_data = resp.get_json()
        self.assertEqual(job_data['status'], 'resolved')
        self.assertEqual(job_data['winner_id'], worker_id)

        resp = c.get(f'/submissions/{sub2_id}',
                      headers=_auth_headers(worker_key))
        sub2_data = resp.get_json()
        self.assertEqual(sub2_data['status'], 'passed')
        self.assertEqual(sub2_data['oracle_score'], 85)
        self.assertEqual(sub2_data['attempt'], 2)

    @patch('server._launch_oracle_with_timeout')
    def test_oracle_max_retries_exhausted(self, mock_oracle):
        c = self.client

        buyer_id, buyer_key = _register_agent(c, 'f2-buyer', 'Buyer F2',
                                               wallet='0x' + 'aa' * 20)
        worker_id, worker_key = _register_agent(c, 'f2-worker', 'Worker F2',
                                                 wallet='0x' + 'bb' * 20)

        # Create job with max_retries=2
        resp = c.post('/jobs', json={
            'title': 'Max Retry Task',
            'description': 'Limited retries',
            'price': 1.0,
            'max_retries': 2,
        }, headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']

        c.post(f'/jobs/{task_id}/fund',
               json={'tx_hash': '0xf2-fund'},
               headers=_auth_headers(buyer_key))

        c.post(f'/jobs/{task_id}/claim', json={},
               headers=_auth_headers(worker_key))

        # Attempt 1 → fail
        resp = c.post(f'/jobs/{task_id}/submit',
                       json={'content': 'attempt 1'},
                       headers=_auth_headers(worker_key))
        sub1_id = resp.get_json()['submission_id']
        sub1 = db.session.get(Submission, sub1_id)
        sub1.status = 'failed'
        sub1.oracle_score = 30
        job = db.session.get(Job, task_id)
        job.failure_count = 1
        db.session.commit()

        # Attempt 2 → fail
        resp = c.post(f'/jobs/{task_id}/submit',
                       json={'content': 'attempt 2'},
                       headers=_auth_headers(worker_key))
        sub2_id = resp.get_json()['submission_id']
        sub2 = db.session.get(Submission, sub2_id)
        sub2.status = 'failed'
        sub2.oracle_score = 35
        job = db.session.get(Job, task_id)
        job.failure_count = 2
        db.session.commit()

        # Attempt 3 → allowed (max_retries=2 means 2 retries after initial = 3 total)
        resp = c.post(f'/jobs/{task_id}/submit',
                       json={'content': 'attempt 3'},
                       headers=_auth_headers(worker_key))
        self.assertEqual(resp.status_code, 202)
        sub3_id = resp.get_json()['submission_id']
        sub3 = db.session.get(Submission, sub3_id)
        sub3.status = 'failed'
        sub3.oracle_score = 40
        job = db.session.get(Job, task_id)
        job.failure_count = 3
        db.session.commit()

        # Attempt 4 → should be rejected (max_retries exhausted)
        resp = c.post(f'/jobs/{task_id}/submit',
                       json={'content': 'attempt 4'},
                       headers=_auth_headers(worker_key))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('retr', resp.get_json()['error'].lower())


# ===================================================================
# Scenario G: Payout Partial Failure
# ===================================================================

class TestScenarioG_PayoutPartialFailure(unittest.TestCase):
    """
    Scenario G: Job resolved, worker payout succeeds but fee transfer fails ->
    payout_status='partial', payout_tx_hash set, fee_tx_hash is None.
    """

    def setUp(self):
        app.config['TESTING'] = True
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
        # Provide mock wallet service so fund/payout work without a real chain
        import services.wallet_service as ws_mod
        from unittest.mock import MagicMock
        mock_wallet = MagicMock()
        mock_wallet.is_connected.return_value = True
        mock_wallet.get_ops_address.return_value = '0x' + '00' * 20
        mock_wallet.verify_deposit.return_value = {
            'valid': True, 'depositor': '', 'amount': None,
        }
        mock_wallet.payout.return_value = {'payout_tx': '0xpayout_mock', 'fee_tx': '0xfee_mock'}
        mock_wallet.refund.return_value = '0xrefund_mock'
        mock_wallet.estimate_gas.return_value = {"error": "mock"}
        ws_mod._wallet_service = mock_wallet
        self.ctx = app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = app.test_client()
        from services.rate_limiter import _api_limiter, _submit_limiter
        _api_limiter._requests.clear()
        _submit_limiter._requests.clear()

    def tearDown(self):
        from server import _shutdown_event, _oracle_executor, _pending_oracles, _pending_lock
        _shutdown_event.set()
        _oracle_executor.shutdown(wait=False)
        from services.webhook_service import shutdown_webhook_pool
        shutdown_webhook_pool(wait=False)
        import time
        time.sleep(0.05)
        db.session.remove()
        db.drop_all()
        _oracle_executor.ensure_pool()
        with _pending_lock:
            _pending_oracles.clear()
        _shutdown_event.clear()
        self.ctx.pop()

    @patch('server._launch_oracle_with_timeout')
    def test_partial_payout_state(self, mock_oracle):
        c = self.client

        buyer_id, buyer_key = _register_agent(c, 'g-buyer', 'Buyer G',
                                               wallet='0x' + 'aa' * 20)
        worker_id, worker_key = _register_agent(c, 'g-worker', 'Worker G',
                                                 wallet='0x' + 'bb' * 20)

        # Create + fund job
        resp = c.post('/jobs', json={
            'title': 'Partial Payout Task',
            'description': 'Test partial failure',
            'price': 5.0,
        }, headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']

        c.post(f'/jobs/{task_id}/fund',
               json={'tx_hash': '0xg-fund'},
               headers=_auth_headers(buyer_key))

        # Worker claims + submits
        c.post(f'/jobs/{task_id}/claim', json={},
               headers=_auth_headers(worker_key))
        resp = c.post(f'/jobs/{task_id}/submit',
                       json={'content': 'good solution'},
                       headers=_auth_headers(worker_key))
        sub_id = resp.get_json()['submission_id']

        # Simulate oracle pass + partial payout directly in DB
        sub = db.session.get(Submission, sub_id)
        sub.status = 'passed'
        sub.oracle_score = 90

        job = db.session.get(Job, task_id)
        job.status = 'resolved'
        job.winner_id = worker_id
        job.payout_tx_hash = '0xWorkerPaid'
        job.fee_tx_hash = None
        job.payout_status = 'partial'
        db.session.commit()

        # Verify via GET /jobs
        resp = c.get(f'/jobs/{task_id}')
        self.assertEqual(resp.status_code, 200)
        job_data = resp.get_json()
        self.assertEqual(job_data['status'], 'resolved')
        self.assertEqual(job_data['payout_status'], 'partial')
        self.assertEqual(job_data['payout_tx_hash'], '0xWorkerPaid')
        self.assertIsNone(job_data.get('fee_tx_hash'))

        # Job should still be resolved (not rolled back)
        self.assertEqual(job_data['winner_id'], worker_id)


# ===================================================================
# Scenario H: Refund Cooldown
# ===================================================================

class TestScenarioH_RefundCooldown(unittest.TestCase):
    """
    Scenario H: Tests the 1-hour refund cooldown per depositor address.
    """

    def setUp(self):
        app.config['TESTING'] = True
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
        # Provide mock wallet service so fund/payout work without a real chain
        import services.wallet_service as ws_mod
        from unittest.mock import MagicMock
        mock_wallet = MagicMock()
        mock_wallet.is_connected.return_value = True
        mock_wallet.get_ops_address.return_value = '0x' + '00' * 20
        mock_wallet.verify_deposit.return_value = {
            'valid': True, 'depositor': '', 'amount': None,
        }
        mock_wallet.payout.return_value = {'payout_tx': '0xpayout_mock', 'fee_tx': '0xfee_mock'}
        mock_wallet.refund.return_value = '0xrefund_mock'
        mock_wallet.estimate_gas.return_value = {"error": "mock"}
        ws_mod._wallet_service = mock_wallet
        self.ctx = app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = app.test_client()
        from services.rate_limiter import _api_limiter, _submit_limiter
        _api_limiter._requests.clear()
        _submit_limiter._requests.clear()

    def tearDown(self):
        from server import _shutdown_event, _oracle_executor, _pending_oracles, _pending_lock
        _shutdown_event.set()
        _oracle_executor.shutdown(wait=False)
        from services.webhook_service import shutdown_webhook_pool
        shutdown_webhook_pool(wait=False)
        import time
        time.sleep(0.05)
        db.session.remove()
        db.drop_all()
        _oracle_executor.ensure_pool()
        with _pending_lock:
            _pending_oracles.clear()
        _shutdown_event.clear()
        self.ctx.pop()

    def test_refund_cooldown_blocks_second_refund(self):
        import datetime as dt
        c = self.client

        buyer_id, buyer_key = _register_agent(c, 'h-buyer', 'Buyer H',
                                               wallet='0x' + 'aa' * 20)

        # Create 2 jobs from same buyer
        resp1 = c.post('/jobs', json={
            'title': 'Cooldown Job 1',
            'description': 'First job',
            'price': 1.0,
        }, headers=_auth_headers(buyer_key))
        task_id_1 = resp1.get_json()['task_id']

        resp2 = c.post('/jobs', json={
            'title': 'Cooldown Job 2',
            'description': 'Second job',
            'price': 1.0,
        }, headers=_auth_headers(buyer_key))
        task_id_2 = resp2.get_json()['task_id']

        # Fund both with different tx hashes
        c.post(f'/jobs/{task_id_1}/fund',
               json={'tx_hash': '0xh-fund-1'},
               headers=_auth_headers(buyer_key))
        c.post(f'/jobs/{task_id_2}/fund',
               json={'tx_hash': '0xh-fund-2'},
               headers=_auth_headers(buyer_key))

        # Set depositor_address on both (no chain connected in test)
        depositor = '0x' + 'aa' * 20
        job1 = db.session.get(Job, task_id_1)
        job1.depositor_address = depositor
        job2 = db.session.get(Job, task_id_2)
        job2.depositor_address = depositor
        db.session.commit()

        # Expire both
        job1.status = 'expired'
        job2.status = 'expired'
        db.session.commit()

        # Refund job 1: expect 200
        resp = c.post(f'/jobs/{task_id_1}/refund',
                       headers=_auth_headers(buyer_key))
        self.assertEqual(resp.status_code, 200)

        # Refund job 2 immediately (same depositor): expect 429
        resp = c.post(f'/jobs/{task_id_2}/refund',
                       headers=_auth_headers(buyer_key))
        self.assertEqual(resp.status_code, 429)
        data = resp.get_json()
        self.assertIn('cooldown', data['error'].lower())
        self.assertIn('retry_after_seconds', data)

    def test_refund_cooldown_different_depositor_ok(self):
        c = self.client

        buyer1_id, buyer1_key = _register_agent(c, 'h2-buyer1', 'Buyer H2a',
                                                  wallet='0x' + 'aa' * 20)
        buyer2_id, buyer2_key = _register_agent(c, 'h2-buyer2', 'Buyer H2b',
                                                  wallet='0x' + 'cc' * 20)

        # Job 1 from buyer 1
        resp = c.post('/jobs', json={
            'title': 'Cooldown Diff 1',
            'description': 'First',
            'price': 1.0,
        }, headers=_auth_headers(buyer1_key))
        task_id_1 = resp.get_json()['task_id']

        # Job 2 from buyer 2
        resp = c.post('/jobs', json={
            'title': 'Cooldown Diff 2',
            'description': 'Second',
            'price': 1.0,
        }, headers=_auth_headers(buyer2_key))
        task_id_2 = resp.get_json()['task_id']

        # Fund
        c.post(f'/jobs/{task_id_1}/fund',
               json={'tx_hash': '0xh2-fund-1'},
               headers=_auth_headers(buyer1_key))
        c.post(f'/jobs/{task_id_2}/fund',
               json={'tx_hash': '0xh2-fund-2'},
               headers=_auth_headers(buyer2_key))

        # Set different depositor addresses
        job1 = db.session.get(Job, task_id_1)
        job1.depositor_address = '0x' + 'aa' * 20
        job2 = db.session.get(Job, task_id_2)
        job2.depositor_address = '0x' + 'cc' * 20
        db.session.commit()

        # Expire both
        job1.status = 'expired'
        job2.status = 'expired'
        db.session.commit()

        # Refund job 1: expect 200
        resp = c.post(f'/jobs/{task_id_1}/refund',
                       headers=_auth_headers(buyer1_key))
        self.assertEqual(resp.status_code, 200)

        # Refund job 2 (different depositor): expect 200
        resp = c.post(f'/jobs/{task_id_2}/refund',
                       headers=_auth_headers(buyer2_key))
        self.assertEqual(resp.status_code, 200)

    def test_refund_cooldown_expires(self):
        import datetime as dt
        from models import _utcnow
        c = self.client

        buyer_id, buyer_key = _register_agent(c, 'h3-buyer', 'Buyer H3',
                                               wallet='0x' + 'aa' * 20)

        # Create 2 jobs
        resp = c.post('/jobs', json={
            'title': 'Cooldown Expire 1',
            'description': 'First',
            'price': 1.0,
        }, headers=_auth_headers(buyer_key))
        task_id_1 = resp.get_json()['task_id']

        resp = c.post('/jobs', json={
            'title': 'Cooldown Expire 2',
            'description': 'Second',
            'price': 1.0,
        }, headers=_auth_headers(buyer_key))
        task_id_2 = resp.get_json()['task_id']

        # Fund both
        depositor = '0x' + 'aa' * 20
        c.post(f'/jobs/{task_id_1}/fund',
               json={'tx_hash': '0xh3-fund-1'},
               headers=_auth_headers(buyer_key))
        c.post(f'/jobs/{task_id_2}/fund',
               json={'tx_hash': '0xh3-fund-2'},
               headers=_auth_headers(buyer_key))

        job1 = db.session.get(Job, task_id_1)
        job1.depositor_address = depositor
        job2 = db.session.get(Job, task_id_2)
        job2.depositor_address = depositor
        db.session.commit()

        # Expire both
        job1.status = 'expired'
        job2.status = 'expired'
        db.session.commit()

        # Refund job 1
        resp = c.post(f'/jobs/{task_id_1}/refund',
                       headers=_auth_headers(buyer_key))
        self.assertEqual(resp.status_code, 200)

        # Backdate job1.updated_at by 2 hours so cooldown expires
        job1 = db.session.get(Job, task_id_1)
        job1.updated_at = _utcnow() - dt.timedelta(hours=2)
        db.session.commit()

        # Refund job 2: should succeed (cooldown expired)
        resp = c.post(f'/jobs/{task_id_2}/refund',
                       headers=_auth_headers(buyer_key))
        self.assertEqual(resp.status_code, 200)


# ===================================================================
# Scenario I: Deposit Wrong Target
# ===================================================================

class TestScenarioI_DepositWrongTarget(unittest.TestCase):
    """
    Scenario I: Buyer funds job but deposit verification fails
    (e.g., USDC sent to wrong address) -> job stays open.
    """

    def setUp(self):
        app.config['TESTING'] = True
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
        # Provide mock wallet service so fund/payout work without a real chain
        import services.wallet_service as ws_mod
        from unittest.mock import MagicMock
        mock_wallet = MagicMock()
        mock_wallet.is_connected.return_value = True
        mock_wallet.get_ops_address.return_value = '0x' + '00' * 20
        mock_wallet.verify_deposit.return_value = {
            'valid': True, 'depositor': '', 'amount': None,
        }
        mock_wallet.payout.return_value = {'payout_tx': '0xpayout_mock', 'fee_tx': '0xfee_mock'}
        mock_wallet.refund.return_value = '0xrefund_mock'
        mock_wallet.estimate_gas.return_value = {"error": "mock"}
        ws_mod._wallet_service = mock_wallet
        self.ctx = app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = app.test_client()
        from services.rate_limiter import _api_limiter, _submit_limiter
        _api_limiter._requests.clear()
        _submit_limiter._requests.clear()

    def tearDown(self):
        from server import _shutdown_event, _oracle_executor, _pending_oracles, _pending_lock
        _shutdown_event.set()
        _oracle_executor.shutdown(wait=False)
        from services.webhook_service import shutdown_webhook_pool
        shutdown_webhook_pool(wait=False)
        import time
        time.sleep(0.05)
        db.session.remove()
        db.drop_all()
        _oracle_executor.ensure_pool()
        with _pending_lock:
            _pending_oracles.clear()
        _shutdown_event.clear()
        self.ctx.pop()

    @patch('services.wallet_service.get_wallet_service')
    def test_fund_deposit_to_wrong_address(self, mock_get_wallet):
        from unittest.mock import MagicMock
        c = self.client

        # Mock wallet: connected but verify_deposit fails
        mock_wallet = MagicMock()
        mock_wallet.is_connected.return_value = True
        mock_wallet.verify_deposit.return_value = {
            'valid': False,
            'error': 'No USDC transfer to operations wallet found',
        }
        mock_get_wallet.return_value = mock_wallet

        buyer_id, buyer_key = _register_agent(c, 'i-buyer', 'Buyer I',
                                               wallet='0x' + 'aa' * 20)

        resp = c.post('/jobs', json={
            'title': 'Wrong Deposit Task',
            'description': 'Will send to wrong address',
            'price': 2.0,
        }, headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']

        # Try to fund with bad deposit
        resp = c.post(f'/jobs/{task_id}/fund',
                       json={'tx_hash': '0xwrong-target'},
                       headers=_auth_headers(buyer_key))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('verification failed', resp.get_json()['error'].lower())

        # Verify job is still open
        resp = c.get(f'/jobs/{task_id}')
        self.assertEqual(resp.get_json()['status'], 'open')


# ===================================================================
# Scenario J: Replay Attack
# ===================================================================

class TestScenarioJ_ReplayAttack(unittest.TestCase):
    """
    Scenario J: Buyer uses same tx_hash to fund two different jobs ->
    second fund should fail with 409 (unique constraint on deposit_tx_hash).
    """

    def setUp(self):
        app.config['TESTING'] = True
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
        # Provide mock wallet service so fund/payout work without a real chain
        import services.wallet_service as ws_mod
        from unittest.mock import MagicMock
        mock_wallet = MagicMock()
        mock_wallet.is_connected.return_value = True
        mock_wallet.get_ops_address.return_value = '0x' + '00' * 20
        mock_wallet.verify_deposit.return_value = {
            'valid': True, 'depositor': '', 'amount': None,
        }
        mock_wallet.payout.return_value = {'payout_tx': '0xpayout_mock', 'fee_tx': '0xfee_mock'}
        mock_wallet.refund.return_value = '0xrefund_mock'
        mock_wallet.estimate_gas.return_value = {"error": "mock"}
        ws_mod._wallet_service = mock_wallet
        self.ctx = app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = app.test_client()
        from services.rate_limiter import _api_limiter, _submit_limiter
        _api_limiter._requests.clear()
        _submit_limiter._requests.clear()

    def tearDown(self):
        from server import _shutdown_event, _oracle_executor, _pending_oracles, _pending_lock
        _shutdown_event.set()
        _oracle_executor.shutdown(wait=False)
        from services.webhook_service import shutdown_webhook_pool
        shutdown_webhook_pool(wait=False)
        import time
        time.sleep(0.05)
        db.session.remove()
        db.drop_all()
        _oracle_executor.ensure_pool()
        with _pending_lock:
            _pending_oracles.clear()
        _shutdown_event.clear()
        self.ctx.pop()

    def test_replay_attack_same_tx_hash(self):
        c = self.client

        buyer_id, buyer_key = _register_agent(c, 'j-buyer', 'Buyer J',
                                               wallet='0x' + 'aa' * 20)

        # Create job A
        resp = c.post('/jobs', json={
            'title': 'Replay Job A',
            'description': 'First job',
            'price': 1.0,
        }, headers=_auth_headers(buyer_key))
        task_id_a = resp.get_json()['task_id']

        # Create job B
        resp = c.post('/jobs', json={
            'title': 'Replay Job B',
            'description': 'Second job',
            'price': 1.0,
        }, headers=_auth_headers(buyer_key))
        task_id_b = resp.get_json()['task_id']

        # Fund job A with tx_hash
        resp = c.post(f'/jobs/{task_id_a}/fund',
                       json={'tx_hash': '0xreplay123'},
                       headers=_auth_headers(buyer_key))
        self.assertEqual(resp.status_code, 200)

        # Fund job B with SAME tx_hash → expect 409
        resp = c.post(f'/jobs/{task_id_b}/fund',
                       json={'tx_hash': '0xreplay123'},
                       headers=_auth_headers(buyer_key))
        self.assertEqual(resp.status_code, 409)
        self.assertIn('already been used', resp.get_json()['error'].lower())

        # Verify job B is still open
        resp = c.get(f'/jobs/{task_id_b}')
        self.assertEqual(resp.get_json()['status'], 'open')


# ===================================================================
# Scenario K: Concurrent Payout
# ===================================================================

class TestScenarioK_ConcurrentPayout(unittest.TestCase):
    """
    Scenario K: Two workers submit, both oracle-pass, but only the first
    resolution wins. Second worker's submission is failed because job
    was no longer in funded state.
    """

    def setUp(self):
        app.config['TESTING'] = True
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
        # Provide mock wallet service so fund/payout work without a real chain
        import services.wallet_service as ws_mod
        from unittest.mock import MagicMock
        mock_wallet = MagicMock()
        mock_wallet.is_connected.return_value = True
        mock_wallet.get_ops_address.return_value = '0x' + '00' * 20
        mock_wallet.verify_deposit.return_value = {
            'valid': True, 'depositor': '', 'amount': None,
        }
        mock_wallet.payout.return_value = {'payout_tx': '0xpayout_mock', 'fee_tx': '0xfee_mock'}
        mock_wallet.refund.return_value = '0xrefund_mock'
        mock_wallet.estimate_gas.return_value = {"error": "mock"}
        ws_mod._wallet_service = mock_wallet
        self.ctx = app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = app.test_client()
        from services.rate_limiter import _api_limiter, _submit_limiter
        _api_limiter._requests.clear()
        _submit_limiter._requests.clear()

    def tearDown(self):
        from server import _shutdown_event, _oracle_executor, _pending_oracles, _pending_lock
        _shutdown_event.set()
        _oracle_executor.shutdown(wait=False)
        from services.webhook_service import shutdown_webhook_pool
        shutdown_webhook_pool(wait=False)
        import time
        time.sleep(0.05)
        db.session.remove()
        db.drop_all()
        _oracle_executor.ensure_pool()
        with _pending_lock:
            _pending_oracles.clear()
        _shutdown_event.clear()
        self.ctx.pop()

    @patch('server._launch_oracle_with_timeout')
    def test_concurrent_resolution_only_first_wins(self, mock_oracle):
        c = self.client

        buyer_id, buyer_key = _register_agent(c, 'k-buyer', 'Buyer K',
                                               wallet='0x' + 'aa' * 20)
        worker_a_id, worker_a_key = _register_agent(c, 'k-worker-a', 'Worker KA',
                                                      wallet='0x' + 'bb' * 20)
        worker_b_id, worker_b_key = _register_agent(c, 'k-worker-b', 'Worker KB',
                                                      wallet='0x' + 'cc' * 20)

        # Create + fund job
        resp = c.post('/jobs', json={
            'title': 'Concurrent Payout Task',
            'description': 'Two workers race',
            'price': 5.0,
        }, headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']

        c.post(f'/jobs/{task_id}/fund',
               json={'tx_hash': '0xk-fund'},
               headers=_auth_headers(buyer_key))

        # Both workers claim
        c.post(f'/jobs/{task_id}/claim', json={},
               headers=_auth_headers(worker_a_key))
        c.post(f'/jobs/{task_id}/claim', json={},
               headers=_auth_headers(worker_b_key))

        # Both submit
        resp = c.post(f'/jobs/{task_id}/submit',
                       json={'content': 'Worker A solution'},
                       headers=_auth_headers(worker_a_key))
        sub_a_id = resp.get_json()['submission_id']

        resp = c.post(f'/jobs/{task_id}/submit',
                       json={'content': 'Worker B solution'},
                       headers=_auth_headers(worker_b_key))
        sub_b_id = resp.get_json()['submission_id']

        # Simulate: Worker A's oracle passes first → job resolved
        sub_a = db.session.get(Submission, sub_a_id)
        sub_a.status = 'passed'
        sub_a.oracle_score = 88

        job = db.session.get(Job, task_id)
        job.status = 'resolved'
        job.winner_id = worker_a_id
        job.payout_status = 'skipped'

        # Worker B's oracle also passes, but job already resolved
        sub_b = db.session.get(Submission, sub_b_id)
        sub_b.status = 'failed'
        sub_b.oracle_reason = 'Job was no longer in funded state'
        db.session.commit()

        # Verify via API
        resp = c.get(f'/jobs/{task_id}')
        job_data = resp.get_json()
        self.assertEqual(job_data['status'], 'resolved')
        self.assertEqual(job_data['winner_id'], worker_a_id)

        resp = c.get(f'/submissions/{sub_a_id}',
                      headers=_auth_headers(worker_a_key))
        self.assertEqual(resp.get_json()['status'], 'passed')

        resp = c.get(f'/submissions/{sub_b_id}',
                      headers=_auth_headers(worker_b_key))
        sub_b_data = resp.get_json()
        self.assertEqual(sub_b_data['status'], 'failed')
        self.assertIn('no longer in funded state', sub_b_data['oracle_reason'])


# ===================================================================
# Scenario L: Oracle Timeout
# ===================================================================

class TestScenarioL_OracleTimeout(unittest.TestCase):
    """
    Scenario L: Oracle evaluation times out -> submission marked failed
    with timeout reason -> worker can resubmit.
    """

    def setUp(self):
        app.config['TESTING'] = True
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
        # Provide mock wallet service so fund/payout work without a real chain
        import services.wallet_service as ws_mod
        from unittest.mock import MagicMock
        mock_wallet = MagicMock()
        mock_wallet.is_connected.return_value = True
        mock_wallet.get_ops_address.return_value = '0x' + '00' * 20
        mock_wallet.verify_deposit.return_value = {
            'valid': True, 'depositor': '', 'amount': None,
        }
        mock_wallet.payout.return_value = {'payout_tx': '0xpayout_mock', 'fee_tx': '0xfee_mock'}
        mock_wallet.refund.return_value = '0xrefund_mock'
        mock_wallet.estimate_gas.return_value = {"error": "mock"}
        ws_mod._wallet_service = mock_wallet
        self.ctx = app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = app.test_client()
        from services.rate_limiter import _api_limiter, _submit_limiter
        _api_limiter._requests.clear()
        _submit_limiter._requests.clear()

    def tearDown(self):
        from server import _shutdown_event, _oracle_executor, _pending_oracles, _pending_lock
        _shutdown_event.set()
        _oracle_executor.shutdown(wait=False)
        from services.webhook_service import shutdown_webhook_pool
        shutdown_webhook_pool(wait=False)
        import time
        time.sleep(0.05)
        db.session.remove()
        db.drop_all()
        _oracle_executor.ensure_pool()
        with _pending_lock:
            _pending_oracles.clear()
        _shutdown_event.clear()
        self.ctx.pop()

    @patch('server._launch_oracle_with_timeout')
    def test_oracle_timeout_marks_failed_and_allows_retry(self, mock_oracle):
        c = self.client

        buyer_id, buyer_key = _register_agent(c, 'l-buyer', 'Buyer L',
                                               wallet='0x' + 'aa' * 20)
        worker_id, worker_key = _register_agent(c, 'l-worker', 'Worker L',
                                                 wallet='0x' + 'bb' * 20)

        # Create + fund job
        resp = c.post('/jobs', json={
            'title': 'Timeout Task',
            'description': 'Oracle will time out',
            'price': 3.0,
            'max_retries': 3,
        }, headers=_auth_headers(buyer_key))
        task_id = resp.get_json()['task_id']

        c.post(f'/jobs/{task_id}/fund',
               json={'tx_hash': '0xl-fund'},
               headers=_auth_headers(buyer_key))

        # Worker claims + submits
        c.post(f'/jobs/{task_id}/claim', json={},
               headers=_auth_headers(worker_key))

        resp = c.post(f'/jobs/{task_id}/submit',
                       json={'content': 'solution that times out'},
                       headers=_auth_headers(worker_key))
        self.assertEqual(resp.status_code, 202)
        sub1_id = resp.get_json()['submission_id']

        # Simulate timeout
        sub1 = db.session.get(Submission, sub1_id)
        sub1.status = 'failed'
        sub1.oracle_reason = 'Evaluation timed out after 120s'
        sub1.oracle_steps = [{"step": 0, "name": "timeout", "output": {"error": "timeout"}}]
        job = db.session.get(Job, task_id)
        job.failure_count = (job.failure_count or 0) + 1
        db.session.commit()

        # Verify submission is failed with timeout reason
        resp = c.get(f'/submissions/{sub1_id}',
                      headers=_auth_headers(worker_key))
        sub_data = resp.get_json()
        self.assertEqual(sub_data['status'], 'failed')
        self.assertIn('timed out', sub_data['oracle_reason'])

        # Worker resubmits
        resp = c.post(f'/jobs/{task_id}/submit',
                       json={'content': 'retry after timeout'},
                       headers=_auth_headers(worker_key))
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.get_json()['attempt'], 2)

        # Job is still funded (not resolved, not failed)
        resp = c.get(f'/jobs/{task_id}')
        self.assertEqual(resp.get_json()['status'], 'funded')


if __name__ == '__main__':
    unittest.main()
