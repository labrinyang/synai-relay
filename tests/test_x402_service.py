"""Tests for x402 integration: models, access control, and route-level handling."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from decimal import Decimal
from unittest.mock import patch, MagicMock
from server import app
from models import db, Agent, Job, Submission, SubmissionAccess
from services.auth_service import generate_api_key


@pytest.fixture
def client():
    app.config['TESTING'] = True
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
    # Default: disable x402 so legacy tests work; x402 tests re-enable explicitly
    app.config['X402_ENABLED'] = False
    # Reset x402 init flag so it re-initializes cleanly (prevents cross-test pollution)
    import server as _srv
    _srv._x402_initialized = False
    # Reset wallet service singleton (may be polluted by prior test files)
    import services.wallet_service as _ws
    _ws._wallet_service = None
    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()


class TestSubmissionAccessModel:
    def test_create_access_record(self, client):
        with app.app_context():
            agent = Agent(agent_id='viewer-1', name='Viewer')
            worker = Agent(agent_id='worker-1', name='Worker')
            db.session.add_all([agent, worker])
            db.session.flush()

            job = Job(title='Test', description='Desc', price=Decimal('50'),
                      buyer_id='viewer-1', status='funded')
            db.session.add(job)
            db.session.flush()

            sub = Submission(task_id=job.task_id, worker_id='worker-1',
                             content={"answer": "test"}, status='pending')
            db.session.add(sub)
            db.session.flush()

            access = SubmissionAccess(
                submission_id=sub.id,
                viewer_agent_id='viewer-1',
                tx_hash='0x123abc',
                amount=Decimal('35.0'),
                chain_id=8453,
            )
            db.session.add(access)
            db.session.commit()

            found = SubmissionAccess.query.filter_by(
                submission_id=sub.id, viewer_agent_id='viewer-1').first()
            assert found is not None
            assert found.tx_hash == '0x123abc'
            assert found.chain_id == 8453

    def test_unique_constraint_prevents_double_access(self, client):
        """Same viewer + submission cannot have two access records."""
        from sqlalchemy.exc import IntegrityError
        with app.app_context():
            agent = Agent(agent_id='viewer-2', name='Viewer')
            worker = Agent(agent_id='worker-2', name='Worker')
            db.session.add_all([agent, worker])
            db.session.flush()

            job = Job(title='Test', description='Desc', price=Decimal('50'),
                      buyer_id='viewer-2', status='funded')
            db.session.add(job)
            db.session.flush()

            sub = Submission(task_id=job.task_id, worker_id='worker-2',
                             content={"answer": "x"}, status='pending')
            db.session.add(sub)
            db.session.flush()

            access1 = SubmissionAccess(
                submission_id=sub.id, viewer_agent_id='viewer-2',
                tx_hash='0xfirst', amount=Decimal('35'), chain_id=8453)
            db.session.add(access1)
            db.session.commit()

            access2 = SubmissionAccess(
                submission_id=sub.id, viewer_agent_id='viewer-2',
                tx_hash='0xsecond', amount=Decimal('35'), chain_id=8453)
            db.session.add(access2)
            with pytest.raises(IntegrityError):
                db.session.commit()
            db.session.rollback()


class TestJobChainId:
    def test_job_chain_id_default_null(self, client):
        with app.app_context():
            agent = Agent(agent_id='buyer-1', name='Buyer')
            db.session.add(agent)
            db.session.flush()

            job = Job(title='Test', description='Desc', price=Decimal('10'),
                      buyer_id='buyer-1')
            db.session.add(job)
            db.session.commit()

            assert job.chain_id is None


from services.x402_service import parse_chain_id, build_requirements


class TestParseChainId:
    def test_base(self):
        assert parse_chain_id("eip155:8453") == 8453

    def test_xlayer(self):
        assert parse_chain_id("eip155:196") == 196

    def test_invalid(self):
        with pytest.raises(ValueError, match="Invalid CAIP-2"):
            parse_chain_id("not-a-network")

    def test_empty(self):
        with pytest.raises(ValueError):
            parse_chain_id("")


class TestBuildRequirements:
    def test_single_chain(self):
        from unittest.mock import MagicMock
        adapter = MagicMock()
        adapter.caip2.return_value = "eip155:8453"
        adapter.usdc_address.return_value = "0xUSDC"

        reqs = build_requirements(
            Decimal("50"), "0xPAYTO", [adapter])
        assert len(reqs) == 1
        assert reqs[0].scheme == "exact"
        assert reqs[0].network == "eip155:8453"
        assert reqs[0].amount == "50000000"
        assert reqs[0].pay_to == "0xPAYTO"
        assert reqs[0].asset == "0xUSDC"

    def test_multi_chain(self):
        from unittest.mock import MagicMock
        a1 = MagicMock()
        a1.caip2.return_value = "eip155:8453"
        a1.usdc_address.return_value = "0xBASE_USDC"
        a2 = MagicMock()
        a2.caip2.return_value = "eip155:196"
        a2.usdc_address.return_value = "0xXLAYER_USDC"

        reqs = build_requirements(Decimal("50"), "0xPAYTO", [a1, a2])
        assert len(reqs) == 2
        assert reqs[0].network == "eip155:8453"
        assert reqs[1].network == "eip155:196"

    def test_uses_decimal_precision(self):
        """Ensure amount conversion uses Decimal, not float."""
        from unittest.mock import MagicMock
        adapter = MagicMock()
        adapter.caip2.return_value = "eip155:8453"
        adapter.usdc_address.return_value = "0xUSDC"

        reqs = build_requirements(Decimal("0.1"), "0xPAYTO", [adapter])
        assert reqs[0].amount == "100000"  # 0.1 * 10^6


# ---------------------------------------------------------------------------
# Task 10: _create_job refactor tests
# ---------------------------------------------------------------------------


class TestCreateJobRefactor:
    """Verify _create_job supports status and extra fields."""

    def _make_agent(self, agent_id='buyer-test'):
        raw_key, key_hash = generate_api_key()
        agent = Agent(agent_id=agent_id, name='Test Buyer',
                      api_key_hash=key_hash)
        db.session.add(agent)
        db.session.commit()
        return agent, raw_key

    def test_legacy_creates_open_job(self, client):
        with app.app_context():
            _, api_key = self._make_agent()
        resp = client.post('/jobs', json={
            'title': 'Test Task',
            'description': 'A test description',
            'price': 10.0,
        }, headers={'Authorization': f'Bearer {api_key}'})
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['status'] == 'open'

    def test_job_has_chain_id_field(self, client):
        with app.app_context():
            _, api_key = self._make_agent('buyer-chain')
        resp = client.post('/jobs', json={
            'title': 'Chain Test',
            'description': 'Testing chain_id',
            'price': 5.0,
        }, headers={'Authorization': f'Bearer {api_key}'})
        assert resp.status_code == 201
        task_id = resp.get_json()['task_id']
        with app.app_context():
            job = Job.query.get(task_id)
            assert job.chain_id is None


# ---------------------------------------------------------------------------
# Task 11: x402 POST /jobs integration tests
# ---------------------------------------------------------------------------


class TestX402CreateJob:
    def _make_agent(self, agent_id='x402-buyer', wallet='0xBUYER'):
        raw_key, key_hash = generate_api_key()
        agent = Agent(agent_id=agent_id, name='x402 Buyer',
                      wallet_address=wallet, api_key_hash=key_hash)
        db.session.add(agent)
        db.session.commit()
        return agent, raw_key

    def test_x402_disabled_creates_open_job(self, client):
        with app.app_context():
            _, api_key = self._make_agent('buyer-legacy')
            app.config['X402_ENABLED'] = False
        resp = client.post('/jobs', json={
            'title': 'Legacy Task',
            'description': 'No x402',
            'price': 10.0,
        }, headers={'Authorization': f'Bearer {api_key}'})
        assert resp.status_code == 201
        assert resp.get_json()['status'] == 'open'

    @patch('server._X402_SDK_AVAILABLE', True)
    @patch('server._get_x402_server')
    @patch('server.decode_payment_signature_header')
    def test_valid_x402_payment_creates_funded_job(self, mock_decode,
                                                    mock_get_server, client):
        with app.app_context():
            _, api_key = self._make_agent('buyer-funded')
            app.config['X402_ENABLED'] = True

        mock_server = MagicMock()
        mock_server.verify_payment.return_value = MagicMock(is_valid=True)
        mock_server.settle_payment.return_value = MagicMock(
            success=True, transaction="0xSETTLE_TX",
            network="eip155:8453", payer="0xBUYER")
        mock_get_server.return_value = mock_server

        mock_payload = MagicMock()
        mock_payload.accepted.network = "eip155:8453"
        mock_payload.accepted.amount = "50000000"
        mock_decode.return_value = mock_payload

        resp = client.post('/jobs', json={
            'title': 'Funded Task',
            'description': 'Paid via x402',
            'price': 50.0,
        }, headers={
            'Authorization': f'Bearer {api_key}',
            'X-PAYMENT': 'base64-encoded-payment',
        })

        assert resp.status_code == 201
        data = resp.get_json()
        assert data['status'] == 'funded'
        assert data['x402_settlement']['tx_hash'] == '0xSETTLE_TX'
        assert data['x402_settlement']['chain_id'] == 8453

    @patch('server._X402_SDK_AVAILABLE', True)
    @patch('server.encode_payment_required_header', return_value='encoded-header')
    @patch('server.PaymentRequired')
    @patch('server.build_requirements', return_value=[])
    def test_no_payment_header_returns_402(self, mock_br, mock_pr,
                                           mock_enc, client):
        with app.app_context():
            _, api_key = self._make_agent('buyer-402')
            app.config['X402_ENABLED'] = True
        resp = client.post('/jobs', json={
            'title': 'x402 Task',
            'description': 'Needs payment',
            'price': 50.0,
        }, headers={'Authorization': f'Bearer {api_key}'})
        assert resp.status_code == 402


# ---------------------------------------------------------------------------
# Task 12: GET /platform/chains tests
# ---------------------------------------------------------------------------


class TestPlatformChains:
    def test_chains_endpoint(self, client):
        resp = client.get('/platform/chains')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'chains' in data
        assert 'default_chain_id' in data


# ---------------------------------------------------------------------------
# Task 13: _check_submission_access tests
# ---------------------------------------------------------------------------


class TestSubmissionAccessControl:
    def _setup_task(self):
        buyer = Agent(agent_id='ac-buyer', name='Buyer')
        worker = Agent(agent_id='ac-worker', name='Worker',
                       wallet_address='0xWORKER')
        viewer = Agent(agent_id='ac-viewer', name='Viewer')
        db.session.add_all([buyer, worker, viewer])
        db.session.flush()
        job = Job(title='Test', description='Desc', price=Decimal('50'),
                  buyer_id='ac-buyer', status='funded',
                  solution_price=Decimal('35'))
        db.session.add(job)
        db.session.flush()
        sub = Submission(task_id=job.task_id, worker_id='ac-worker',
                         content={"answer": "solution"}, status='pending')
        db.session.add(sub)
        db.session.commit()
        return job, sub

    def test_author_sees_own_work(self, client):
        from server import _check_submission_access
        with app.app_context():
            job, sub = self._setup_task()
            assert _check_submission_access(sub, job, 'ac-worker') is True

    def test_buyer_requires_payment_during_active(self, client):
        from server import _check_submission_access
        with app.app_context():
            job, sub = self._setup_task()
            result = _check_submission_access(sub, job, 'ac-buyer')
            assert result is None

    def test_random_viewer_requires_payment(self, client):
        from server import _check_submission_access
        with app.app_context():
            job, sub = self._setup_task()
            result = _check_submission_access(sub, job, 'ac-viewer')
            assert result is None

    def test_resolved_task_shows_all(self, client):
        from server import _check_submission_access
        with app.app_context():
            job, sub = self._setup_task()
            job.status = 'resolved'
            job.winner_id = 'ac-worker'
            db.session.commit()
            assert _check_submission_access(sub, job, 'ac-buyer') is True
            assert _check_submission_access(sub, job, 'ac-viewer') is True

    def test_paid_viewer_gets_access(self, client):
        from server import _check_submission_access
        with app.app_context():
            job, sub = self._setup_task()
            access = SubmissionAccess(
                submission_id=sub.id, viewer_agent_id='ac-viewer',
                tx_hash='0xpaid', amount=Decimal('35'), chain_id=8453)
            db.session.add(access)
            db.session.commit()
            assert _check_submission_access(sub, job, 'ac-viewer') is True

    def test_unfunded_task_hides_content(self, client):
        from server import _check_submission_access
        with app.app_context():
            job, sub = self._setup_task()
            job.status = 'open'
            db.session.commit()
            assert _check_submission_access(sub, job, 'ac-viewer') is False


# ---------------------------------------------------------------------------
# Task 14: Submission paywall tests
# ---------------------------------------------------------------------------


class TestSubmissionPaywall:
    def _setup(self):
        raw_key_b, hash_b = generate_api_key()
        raw_key_w, hash_w = generate_api_key()
        buyer = Agent(agent_id='pw-buyer', name='Buyer',
                      api_key_hash=hash_b)
        worker = Agent(agent_id='pw-worker', name='Worker',
                       wallet_address='0xWORKER_WALLET',
                       api_key_hash=hash_w)
        db.session.add_all([buyer, worker])
        db.session.flush()
        job = Job(title='Paywall Test', description='Desc',
                  price=Decimal('50'), buyer_id='pw-buyer',
                  status='funded', solution_price=Decimal('35'))
        db.session.add(job)
        db.session.flush()
        sub = Submission(task_id=job.task_id, worker_id='pw-worker',
                         content={"code": "print('hello')"},
                         status='passed', oracle_score=80)
        db.session.add(sub)
        db.session.commit()
        return job, sub, raw_key_b, raw_key_w

    def test_author_sees_content(self, client):
        with app.app_context():
            _, sub, _, api_key_w = self._setup()
            sub_id = sub.id
        resp = client.get(f'/submissions/{sub_id}',
                          headers={'Authorization': f'Bearer {api_key_w}'})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['content'] == {"code": "print('hello')"}

    def test_resolved_task_returns_all_submissions_public(self, client):
        with app.app_context():
            job, sub, api_key_b, _ = self._setup()
            sub_id = sub.id
            job.status = 'resolved'
            job.winner_id = 'pw-worker'
            db.session.commit()
        resp = client.get(f'/submissions/{sub_id}',
                          headers={'Authorization': f'Bearer {api_key_b}'})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['content'] == {"code": "print('hello')"}

    @patch('server._X402_SDK_AVAILABLE', True)
    @patch('server.encode_payment_required_header', return_value='encoded-header')
    @patch('server.PaymentRequired')
    @patch('server.build_requirements', return_value=[])
    def test_buyer_gets_402_during_active_task(self, mock_br, mock_pr,
                                               mock_enc, client):
        with app.app_context():
            _, sub, api_key_b, _ = self._setup()
            sub_id = sub.id
            app.config['X402_ENABLED'] = True
        resp = client.get(f'/submissions/{sub_id}',
                          headers={'Authorization': f'Bearer {api_key_b}'})
        assert resp.status_code == 402

    def test_no_auth_redacted_content(self, client):
        """Without auth and x402 disabled, funded task shows redacted content."""
        with app.app_context():
            _, sub, _, _ = self._setup()
            sub_id = sub.id
        resp = client.get(f'/submissions/{sub_id}')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['content'] == '[redacted]'

    @patch('server._X402_SDK_AVAILABLE', True)
    @patch('server.encode_payment_required_header', return_value='encoded-header')
    @patch('server.PaymentRequired')
    @patch('server.build_requirements', return_value=[])
    def test_author_no_wallet_returns_409(self, mock_br, mock_pr,
                                          mock_enc, client):
        with app.app_context():
            raw_key_b, hash_b = generate_api_key()
            raw_key_w, hash_w = generate_api_key()
            buyer = Agent(agent_id='pw2-buyer', name='Buyer',
                          api_key_hash=hash_b)
            worker = Agent(agent_id='pw2-worker', name='Worker',
                           api_key_hash=hash_w)  # No wallet_address
            db.session.add_all([buyer, worker])
            db.session.flush()
            job = Job(title='No Wallet', description='Desc',
                      price=Decimal('50'), buyer_id='pw2-buyer',
                      status='funded', solution_price=Decimal('35'))
            db.session.add(job)
            db.session.flush()
            sub = Submission(task_id=job.task_id, worker_id='pw2-worker',
                             content={"code": "test"}, status='passed')
            db.session.add(sub)
            db.session.commit()
            sub_id = sub.id
            app.config['X402_ENABLED'] = True
        resp = client.get(f'/submissions/{sub_id}',
                          headers={'Authorization': f'Bearer {raw_key_b}'})
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Task 18: Full Lifecycle Integration Test
# ---------------------------------------------------------------------------

from unittest.mock import patch, MagicMock


class TestX402Lifecycle:
    """Full lifecycle: create funded job via x402 -> submit -> paywall -> resolve -> public."""

    def _make_agents(self):
        raw_b, hash_b = generate_api_key()
        raw_w, hash_w = generate_api_key()
        raw_v, hash_v = generate_api_key()
        buyer = Agent(agent_id='life-buyer', name='Buyer',
                      wallet_address='0xBUYER', api_key_hash=hash_b)
        worker = Agent(agent_id='life-worker', name='Worker',
                       wallet_address='0xWORKER', api_key_hash=hash_w)
        viewer = Agent(agent_id='life-viewer', name='Viewer',
                       api_key_hash=hash_v)
        db.session.add_all([buyer, worker, viewer])
        db.session.commit()
        return raw_b, raw_w, raw_v

    @patch('server._X402_SDK_AVAILABLE', True)
    @patch('server._get_x402_server')
    @patch('server.decode_payment_signature_header')
    @patch('server.encode_payment_required_header', return_value='encoded')
    @patch('server.PaymentRequired')
    def test_full_lifecycle(self, mock_pr, mock_enc, mock_decode, mock_get_server, client):
        with app.app_context():
            key_b, key_w, key_v = self._make_agents()
            app.config['X402_ENABLED'] = True

        # 1. Create funded job via x402
        mock_server = MagicMock()
        mock_server.verify_payment.return_value = MagicMock(is_valid=True)
        mock_server.settle_payment.return_value = MagicMock(
            success=True, transaction="0xDEPOSIT",
            network="eip155:8453", payer="0xBUYER")
        mock_get_server.return_value = mock_server

        mock_payload = MagicMock()
        mock_payload.accepted.network = "eip155:8453"
        mock_payload.accepted.amount = "50000000"
        mock_decode.return_value = mock_payload

        resp = client.post('/jobs', json={
            'title': 'Lifecycle Test', 'description': 'Full test',
            'price': 50.0,
        }, headers={
            'Authorization': f'Bearer {key_b}',
            'X-PAYMENT': 'encoded-payment',
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['status'] == 'funded'
        task_id = data['task_id']

        # 2. Worker claims
        resp = client.post(f'/jobs/{task_id}/claim',
                           headers={'Authorization': f'Bearer {key_w}'})
        assert resp.status_code == 200

        # 3. Worker submits
        resp = client.post(f'/jobs/{task_id}/submit', json={
            'content': {'solution': 'my answer'},
        }, headers={'Authorization': f'Bearer {key_w}'})
        assert resp.status_code in (200, 201, 202)

        # 4. Get submission ID
        resp = client.get(f'/jobs/{task_id}/submissions',
                          headers={'Authorization': f'Bearer {key_w}'})
        subs = resp.get_json().get('submissions', [])
        assert len(subs) >= 1
        sub_id = subs[0]['submission_id']

        # 5. Worker sees own content (free)
        resp = client.get(f'/submissions/{sub_id}',
                          headers={'Authorization': f'Bearer {key_w}'})
        assert resp.status_code == 200
        assert resp.get_json()['content'] != '[redacted]'

        # 6. Viewer gets 402 (must pay via x402)
        resp = client.get(f'/submissions/{sub_id}',
                          headers={'Authorization': f'Bearer {key_v}'})
        assert resp.status_code == 402

        # 7. Buyer also gets 402 (no free access during active task)
        resp = client.get(f'/submissions/{sub_id}',
                          headers={'Authorization': f'Bearer {key_b}'})
        assert resp.status_code == 402

        # 8. Simulate resolution — all submissions become public
        with app.app_context():
            job = db.session.get(Job, task_id)
            job.status = 'resolved'
            job.winner_id = 'life-worker'
            db.session.commit()

        # 9. After resolution, viewer sees content for free
        resp = client.get(f'/submissions/{sub_id}',
                          headers={'Authorization': f'Bearer {key_v}'})
        assert resp.status_code == 200
        assert resp.get_json()['content'] == {'solution': 'my answer'}

        # 10. Buyer also sees content for free after resolution
        resp = client.get(f'/submissions/{sub_id}',
                          headers={'Authorization': f'Bearer {key_b}'})
        assert resp.status_code == 200
        assert resp.get_json()['content'] == {'solution': 'my answer'}
