"""Tests for x402 integration: models, access control, and route-level handling."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from decimal import Decimal
from server import app
from models import db, Agent, Job, Submission, SubmissionAccess


@pytest.fixture
def client():
    app.config['TESTING'] = True
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
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
