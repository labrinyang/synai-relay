import pytest
from unittest.mock import patch, MagicMock
from services.oracle_service import OracleService


def _mock_llm_response(content):
    """Helper to create a mock LLM response."""
    return {'choices': [{'message': {'content': content}}]}


@patch('services.oracle_service.requests.post')
def test_early_exit_on_clear_fail(mock_post):
    """Step 2 CLEAR_FAIL should skip to verdict without running steps 3-8."""
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
    # Should have called LLM only twice (step 2 + step 9)
    assert mock_post.call_count == 2


@patch('services.oracle_service.requests.post')
def test_full_pipeline_resolved(mock_post):
    """Full 8-step pipeline (steps 2-9) ending in RESOLVED."""
    mock_post.return_value = MagicMock(
        json=MagicMock(side_effect=[
            # Step 2: Guard
            _mock_llm_response('{"addresses_task": true, "analysis": "Good", "verdict": "CONTINUE"}'),
            # Step 3: Structural Integrity
            _mock_llm_response('{"format_ok": true, "structural_score": 90, "issues": []}'),
            # Step 4: Completeness & Coverage
            _mock_llm_response('{"items_checked": ["sort"], "gaps": [], "completeness_score": 95}'),
            # Step 5: Depth & Quality
            _mock_llm_response('{"quality_score": 92, "strengths": ["clean"], "weaknesses": []}'),
            # Step 6: Consistency Audit
            _mock_llm_response('{"consistency_score": 90, "contradictions": [], "summary": "Consistent"}'),
            # Step 7: Devil's Advocate
            _mock_llm_response('{"arguments_against": [], "severity": "none", "summary": "No issues"}'),
            # Step 8: Penalty Calculator
            _mock_llm_response('{"adjusted_score": 92, "penalties": [], "total_penalty": 0}'),
            # Step 9: Final Verdict
            _mock_llm_response('{"verdict": "RESOLVED", "score": 92, "reason": "Excellent work"}'),
        ])
    )

    svc = OracleService()
    result = svc.evaluate("Sort function", "Implement quicksort", None, "def qsort(arr): ...")
    assert result['verdict'] == 'RESOLVED'
    assert result['score'] == 92
    assert len(result['steps']) == 8  # steps 2-9
