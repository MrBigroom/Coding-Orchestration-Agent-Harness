"""Deterministic offline provider used by the demo and tests.

Behaviour is driven by `metadata["purpose"]` and task tags:
  - review:  APPROVED; `mock-reject` -> CHANGES_REQUESTED; `mock-ambiguous` -> unparseable text;
             `mock-needs-human` -> NEEDS_HUMAN; `mock-refute` -> a missing-file claim the worktree
             refutes; `mock-visibility` -> a claim that a changed file was truncated in the
             supplied diff
  - plan:    a two-phase plan
  - supervise: a recommendation-only JSON note
  - agent / worker: a READY_FOR_REVIEW handoff (or BLOCKED with `mock-blocked`)
"""

from __future__ import annotations

import json

from orchestrator.providers.base import (
    BaseProvider,
    CompletionRequest,
    CompletionResult,
    ProviderError,
)
from orchestrator.utils.truncate import estimate_tokens


class MockProvider(BaseProvider):
    kind = "mock"

    def validate_config(self) -> list[str]:
        return []

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        meta = req.metadata
        tags = set(meta.get("tags") or [])
        purpose = meta.get("purpose", "agent")
        if "mock-provider-error" in tags:
            raise ProviderError("mock provider simulated failure")

        if purpose == "review":
            text = self._review(tags, meta)
        elif purpose == "plan":
            text = json.dumps(
                {
                    "phases": [
                        {
                            "phase_number": 1,
                            "phase_slug": "scaffold",
                            "title": "Scaffold",
                            "definition_of_done": ["Project skeleton exists", "Tests pass"],
                            "acceptance_criteria": ["`make test` is green"],
                            "risks": [],
                            "risk": "low",
                        },
                        {
                            "phase_number": 2,
                            "phase_slug": "core",
                            "title": "Core logic",
                            "definition_of_done": ["Core module implemented with tests"],
                            "acceptance_criteria": [],
                            "risks": ["scope creep"],
                            "risk": "medium",
                        },
                    ]
                }
            )
        elif purpose == "supervise":
            text = json.dumps({"recommendation": "proceed", "notes": ["mock supervisor note"]})
        else:
            status = "BLOCKED" if "mock-blocked" in tags else "READY_FOR_REVIEW"
            text = json.dumps(
                {
                    "phase": meta.get("phase", "agent"),
                    "branch": meta.get("branch", ""),
                    "status": status,
                    "summary": "Mock agent completed the task."
                    if status != "BLOCKED"
                    else "Blocked: mock blocker.",
                    "files_changed": [],
                    "tests": {
                        "command": "",
                        "passed": 0,
                        "failed": 0,
                        "skipped": 0,
                        "log_path": "",
                    },
                    "dod_self_check": [],
                    "blockers": ["mock blocker"] if status == "BLOCKED" else [],
                }
            )
        usage = {
            "input_tokens": estimate_tokens(req.prompt.static_text() + req.prompt.dynamic_text()),
            "output_tokens": estimate_tokens(text),
            "cache_read_input_tokens": 0,
            "mock": True,
        }
        return CompletionResult(text=text, model=req.model, usage=usage, stop_reason="end_turn")

    @staticmethod
    def _review(tags: set[str], meta: dict) -> str:
        if "mock-ambiguous" in tags:
            return "I think it mostly looks fine, probably."
        if "mock-needs-human" in tags:
            verdict = "NEEDS_HUMAN"
        elif "mock-reject" in tags:
            verdict = "CHANGES_REQUESTED"
        else:
            verdict = "APPROVED"
        findings = []
        if verdict == "CHANGES_REQUESTED":
            findings = [
                {
                    "file_or_area": "MOCK_CHANGE.md",
                    "issue": "mock reviewer requested changes",
                    "violates": "DoD item 1",
                }
            ]
        if "mock-refute" in tags:
            # T-11: a claim about the REPOSITORY. README.md is present in the task worktree, so the
            # harness must mark this finding refuted instead of requeueing. It must not accept the
            # task on a refuted claim either — the round routes to NEEDS_HUMAN.
            findings = [
                {
                    "file_or_area": "README.md",
                    "issue": "README.md is missing from the branch",
                    "violates": "DoD item 1",
                }
            ]
            verdict = "CHANGES_REQUESTED"
        if "mock-visibility" in tags:
            # T-11: a claim about the SUPPLIED artifacts. Whether MOCK_CHANGE.md exists on disk is
            # irrelevant; only the truncation manifest and the supplied patch can decide it.
            findings = [
                {
                    "file_or_area": "MOCK_CHANGE.md",
                    "issue": "MOCK_CHANGE.md is not visible in the supplied diff (truncated)",
                    "violates": "DoD item 1",
                }
            ]
            verdict = "CHANGES_REQUESTED"
        missing = list(meta.get("missing_artifacts") or [])
        if "mock-assert-missing-artifact" in tags:
            missing.append("docs/not-in-repo.md")
        return json.dumps(
            {
                "verdict": verdict,
                "findings": findings,
                "missing_artifacts": missing,
                "notes": ["mock review based only on supplied artifacts"],
            }
        )
