from orchestrator.utils.truncate import parse_test_counts, summarize_test_output, truncate_middle


def test_pytest_and_generic_counts():
    out = "....F\nFAILED tests/test_a.py::test_x - assert 1 == 2\n===== 1 failed, 4 passed, 2 skipped in 0.3s ====="
    assert parse_test_counts(out) == {"passed": 4, "failed": 1, "skipped": 2}
    assert parse_test_counts("3 passed, 0 failed") == {"passed": 3, "failed": 0, "skipped": 0}


def test_summary_contains_counts_and_failures_not_raw_log():
    raw = "noise\n" * 5000 + "FAILED t.py::a - boom\n===== 1 failed, 2 passed in 1s ====="
    s = summarize_test_output(raw, 300, 1)
    assert "failed: 1" in s and "FAILED t.py::a" in s and len(s) < 2000


def test_truncate_middle_keeps_ends():
    text = "A" * 1000 + "Z" * 1000
    out = truncate_middle(text, 100)
    assert out.startswith("A") and out.endswith("Z") and "truncated" in out
