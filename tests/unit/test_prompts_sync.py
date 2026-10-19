from pathlib import Path

from orchestrator.prompts import architect, supervisor, worker

ROLES_MD = Path(__file__).resolve().parents[2] / "docs" / "roles.md"


def test_role_prompts_match_docs_verbatim():
    doc = ROLES_MD.read_text()
    for module in (architect, worker, supervisor):
        assert module.SYSTEM_PROMPT.strip() in doc, f"{module.__name__} drifted from docs/roles.md"


def test_architect_prompt_forbids_execution_claims():
    assert "You have NO execution access" in architect.SYSTEM_PROMPT
    assert "Never claim you ran anything" in architect.SYSTEM_PROMPT
