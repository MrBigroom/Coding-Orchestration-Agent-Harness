"""Sample architect outputs for parser tests."""

TEXT_APPROVED = """VERDICT: APPROVED
FINDINGS:
- none
"""

TEXT_CHANGES = """VERDICT: CHANGES_REQUESTED
FINDINGS:
1. src/api.py: endpoint lacks input validation (violates DoD item 2)
2. tests: no test for the error path
"""

JSON_AND_TEXT = """VERDICT: APPROVED
FINDINGS:
- none

```json
{"verdict": "APPROVED", "findings": [], "missing_artifacts": [], "notes": ["ok"]}
```
"""

DISAGREE = """VERDICT: APPROVED
FINDINGS:
- none
{"verdict": "CHANGES_REQUESTED", "findings": [], "missing_artifacts": [], "notes": []}
"""

AMBIGUOUS = "Looks good to me overall, maybe tweak a few things."

EXEC_CLAIM = """VERDICT: APPROVED
FINDINGS:
- none
I ran the tests and they pass.
"""
