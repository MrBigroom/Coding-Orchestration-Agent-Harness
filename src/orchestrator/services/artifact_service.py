"""Per-run artifact directory management (frozen layout):

runs/<run_id>/
  prompt.md            worker prompt sent to the harness
  stdout.log stderr.log
  patch.diff diffstat.txt
  test.log             verification command output
  handoff.md handoff.json
  review.md review.json  (+ review.raw.txt when output was ambiguous)
  verification.md verification.json
  run.json             run metadata snapshot
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orchestrator.core.config import get_settings
from orchestrator.core.schemas import Handoff, ReviewResult, VerificationResult
from orchestrator.utils.paths import safe_join
from orchestrator.utils.redaction import redact


def artifact_root() -> Path:
    root = get_settings().artifact_root
    assert root
    root.mkdir(parents=True, exist_ok=True)
    return root


def run_dir(run_id: int) -> Path:
    d = safe_join(artifact_root(), str(int(run_id)))
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_text(run_id: int, name: str, content: str, redact_content: bool = True) -> Path:
    p = safe_join(run_dir(run_id), name)
    p.write_text(redact(content) if redact_content else content, encoding="utf-8")
    return p


def write_json(run_id: int, name: str, data: Any) -> Path:
    p = safe_join(run_dir(run_id), name)
    p.write_text(redact(json.dumps(data, indent=2, default=str)), encoding="utf-8")
    return p


def read_text(run_id: int, name: str) -> str | None:
    p = safe_join(run_dir(run_id), name)
    return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None


def list_artifacts(run_id: int) -> list[dict[str, Any]]:
    d = run_dir(run_id)
    return [
        {"name": p.name, "size": p.stat().st_size}
        for p in sorted(d.iterdir())
        if p.is_file() and p.name not in {"CANCEL", "pid"}
    ]


# ---------------------------------------------------------------- renderers


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {i}" for i in items) if items else "- none"


def render_handoff_md(h: Handoff) -> str:
    dod = "\n".join(
        f"- [{'x' if d.met else ' '}] {d.item} — {d.evidence or 'no evidence'}"
        for d in h.dod_self_check
    ) or "- none supplied"
    deps = "\n".join(f"- {d.name} {d.version}: {d.reason}" for d in h.new_dependencies) or "- none"
    t = h.tests
    return f"""# Handoff — {h.phase}

**Status:** {h.status}
**Branch:** {h.branch}

## Summary
{h.summary}

## Files changed
{_bullets(h.files_changed)}

## Tests
`{t.command or 'not run'}` -> passed {t.passed}, failed {t.failed}, skipped {t.skipped}
Log: {t.log_path or 'n/a'}

## DoD self-check
{dod}

## New dependencies
{deps}

## Assumptions
{_bullets(h.assumptions)}

## Questions
{_bullets(h.questions)}

## Blockers
{_bullets(h.blockers)}
"""


def render_review_md(r: ReviewResult, reviewer: str) -> str:
    findings = "\n".join(
        f"{i}. **{f.file_or_area}** — {f.issue} (violates: {f.violates or 'n/a'})"
        for i, f in enumerate(r.findings, 1)
    ) or "none"
    return f"""# Review ({reviewer})

VERDICT: {r.verdict.value}

FINDINGS:
{findings}

## Missing artifacts
{_bullets(r.missing_artifacts)}

## Notes
{_bullets(r.notes)}
"""


def render_verification_md(v: VerificationResult) -> str:
    rows = "\n".join(
        f"| {c.name} | {c.status} | `{c.command or '-'}` | {c.evidence or '-'} | {c.notes or ''} |"
        for c in v.checks
    )
    return f"""# Supervisor verification — {v.phase}

**Final result:** {v.final_result}
**Contradicts worker:** {v.contradicts_worker}

| Check | Status | Command | Evidence | Notes |
|---|---|---|---|---|
{rows}
"""
