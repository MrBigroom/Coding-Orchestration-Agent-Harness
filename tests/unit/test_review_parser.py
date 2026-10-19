from orchestrator.core.models import ReviewVerdict
from orchestrator.services.review_service import _EXEC_CLAIM, missing_artifacts, parse_review
from tests.fixtures import review_outputs as R


def test_text_verdicts():
    res, amb = parse_review(R.TEXT_APPROVED)
    assert res.verdict == ReviewVerdict.APPROVED and not amb and res.findings == []
    res, amb = parse_review(R.TEXT_CHANGES)
    assert res.verdict == ReviewVerdict.CHANGES_REQUESTED and len(res.findings) == 2
    assert res.findings[0].file_or_area == "src/api.py"


def test_json_takes_precedence_when_consistent():
    res, amb = parse_review(R.JSON_AND_TEXT)
    assert res.verdict == ReviewVerdict.APPROVED and res.notes == ["ok"] and not amb


def test_ambiguous_or_conflicting_fails_closed():
    for text in (R.AMBIGUOUS, R.DISAGREE, ""):
        res, amb = parse_review(text)
        assert amb and res.verdict == ReviewVerdict.CHANGES_REQUESTED
        assert "ambiguous" in res.findings[0].issue


def test_execution_claims_detected():
    assert _EXEC_CLAIM.search(R.EXEC_CLAIM)
    assert not _EXEC_CLAIM.search(R.TEXT_APPROVED)


def test_missing_artifacts_are_findings():
    assert (
        missing_artifacts({"handoff.json", "patch.diff", "test.log", "verification.json"}, False)
        == []
    )
    missing = missing_artifacts({"patch.diff"}, False)
    assert len(missing) == 3
    assert any(
        "synthesized" in m
        for m in missing_artifacts(
            {"handoff.json", "patch.diff", "test.log", "verification.json"}, True
        )
    )
