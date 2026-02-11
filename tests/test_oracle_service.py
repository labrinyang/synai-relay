import pytest
from unittest.mock import patch, MagicMock
from services.oracle_service import OracleService


def _mock_llm_response(content):
    """Helper to create a mock LLM response."""
    return {'choices': [{'message': {'content': content}}]}


@patch('services.oracle_service.requests.post')
def test_early_exit_on_clear_fail(mock_post):
    """Step 2 CLEAR_FAIL should skip to verdict without running steps 3-5."""
    mock_post.return_value = MagicMock(
        json=MagicMock(side_effect=[
            _mock_llm_response('{"addresses_task": false, "analysis": "Off topic", "verdict": "CLEAR_FAIL"}'),
            _mock_llm_response('{"verdict": "REJECTED", "score": 5, "reason": "Does not address the task"}'),
        ])
    )

    svc = OracleService()
    result = svc.evaluate("Write a sort function", "Sort numbers", None, "I like pizza")
    assert result['verdict'] == 'REJECTED'
    assert result['score'] < 80
    # Should have called LLM only twice (step 2 + step 6)
    assert mock_post.call_count == 2


@patch('services.oracle_service.requests.post')
def test_full_pipeline_resolved(mock_post):
    """Full 6-step pipeline ending in RESOLVED."""
    mock_post.return_value = MagicMock(
        json=MagicMock(side_effect=[
            _mock_llm_response('{"addresses_task": true, "analysis": "Good", "verdict": "CONTINUE"}'),
            _mock_llm_response('{"items_checked": ["sort"], "gaps": [], "completeness_score": 95}'),
            _mock_llm_response('{"score": 92, "strengths": ["clean"], "weaknesses": [], "verdict": "CONTINUE"}'),
            _mock_llm_response('{"arguments_against": [], "severity": "none", "summary": "No issues"}'),
            _mock_llm_response('{"verdict": "RESOLVED", "score": 92, "reason": "Excellent work"}'),
        ])
    )

    svc = OracleService()
    result = svc.evaluate("Sort function", "Implement quicksort", None, "def qsort(arr): ...")
    assert result['verdict'] == 'RESOLVED'
    assert result['score'] == 92
    assert len(result['steps']) == 5  # steps 2-6
