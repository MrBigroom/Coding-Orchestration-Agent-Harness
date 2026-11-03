"""Cache-optimised prompt construction.

Section order is fixed (static first, dynamic last):
  1. system / role definition        static  [cache breakpoint]
  2. global memory                   static
  3. role memory                     static
  4. project memory                  static  [cache breakpoint]
  5. frozen contracts / spec context static
  6. phase instructions / DoD        static  [cache breakpoint]
  7. historical context              dynamic
  8. current dynamic state           dynamic (diff, test summary, handoff, feedback)

Dynamic content is truncated/summarised to configured token budgets, and everything is
redacted before it can reach a provider.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlmodel import Session

from orchestrator.core.config import get_settings
from orchestrator.core.models import Project, Role, Task
from orchestrator.core.schemas import Handoff
from orchestrator.prompts import architect, memory, supervisor, worker
from orchestrator.providers.base import BuiltPrompt, PromptSection
from orchestrator.services import memory_service
from orchestrator.utils.redaction import redact, strip_sensitive_files_from_diff
from orchestrator.utils.truncate import estimate_tokens, truncate_middle, truncate_tail

SECTION_ORDER = [
    "system",
    "global_memory",
    "role_memory",
    "project_memory",
    "contracts",
    "phase",
    "history",
    "dynamic_state",
]
STATIC_SECTIONS = set(SECTION_ORDER[:6])
BREAKPOINTS = {"system", "project_memory", "phase"}

# Things that should never appear in a static (cached) section: they change every call.
_VOLATILE = [
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?"), "timestamp"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "uuid"),
    (re.compile(r"(?i)\b(elapsed|duration)[:=]\s*\d+"), "elapsed time"),
]


def system_prompt_for(role: str) -> str:
    if role in (Role.architect, Role.reviewer):
        return architect.SYSTEM_PROMPT + "\n" + architect.OUTPUT_CONTRACT
    if role == Role.supervisor:
        return supervisor.SYSTEM_PROMPT + "\n" + supervisor.RECOMMENDATION_CONTRACT
    schema = json.dumps(Handoff.model_json_schema(), sort_keys=True)
    return (
        worker.SYSTEM_PROMPT
        + "\n"
        + worker.PROTOCOL.format(handoff_schema=schema)
        + "\n"
        + memory.SUGGESTION_GUIDANCE
    )


def _contracts(task: Task | None, project: Project | None) -> str:
    parts: list[str] = []
    if project:
        doc = Path(project.path) / "docs" / "frozen-contracts.md"
        if doc.is_file():
            parts.append(
                "Project frozen contracts (docs/frozen-contracts.md):\n"
                + truncate_middle(doc.read_text(errors="replace"), 4000)
            )
    if task and task.frozen_contracts:
        parts.append(
            "Frozen contracts for this task:\n" + "\n".join(f"- {c}" for c in task.frozen_contracts)
        )
    if task and task.out_of_scope:
        parts.append("Out of scope:\n" + "\n".join(f"- {c}" for c in task.out_of_scope))
    return "\n\n".join(parts)


def _phase(task: Task | None, project: Project | None) -> str:
    if not task:
        return ""
    lines = [
        f"Task {task.short_id}: {task.title}",
        f"Kind: {task.kind}   Risk: {task.risk}",
    ]
    if task.phase_number is not None:
        lines.append(f"Phase: {task.phase_number} ({task.phase_slug or ''})")
    if task.branch_name:
        lines.append(f"Branch: {task.branch_name}")
    if project:
        lines.append(f"Project: {project.name}  base branch: {project.base_branch}")
        if project.test_command:
            lines.append(f"Test command: {project.test_command}")
        if project.lint_command:
            lines.append(f"Lint command: {project.lint_command}")
    if task.description:
        lines += ["", "Steps / description:", task.description]
    if task.definition_of_done:
        lines += ["", "Definition of Done:"] + [
            f"{i}. {d}" for i, d in enumerate(task.definition_of_done, 1)
        ]
    if task.acceptance_criteria:
        lines += ["", "Acceptance criteria:"] + [f"- {a}" for a in task.acceptance_criteria]
    return "\n".join(lines)


@dataclass
class DynamicState:
    diff: str | None = None
    test_summary: str | None = None
    handoff: str | None = None
    feedback: str | None = None
    verification: str | None = None
    history: str | None = None
    # Harness-detected recovery-commit wreckage on this branch (never self-checked by a Worker).
    recovery: str | None = None
    # Findings the harness computed deterministically (contract-file hits, recovery counts).
    harness_findings: str | None = None
    # How the handoff arrived (clean exit / salvaged from a killed run / synthesized) and what git
    # says about the files it claims — provenance a Worker cannot edit (T-10 DoD 7).
    delivery_evidence: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


def _dynamic(state: DynamicState) -> str:
    s = get_settings()
    parts: list[str] = []
    if state.recovery:
        parts.append(
            "### Recovery commits on this branch (created by the harness, NEVER self-checked "
            "by the Worker)\n" + truncate_middle(state.recovery, s.max_log_tokens)
        )
    if state.harness_findings:
        parts.append(
            "### Harness-detected findings (computed by the orchestrator, not asserted by a "
            "model)\n" + truncate_middle(state.harness_findings, s.max_log_tokens)
        )
    if state.delivery_evidence:
        parts.append(
            "### Handoff provenance and delivery evidence (computed from git by the harness, not "
            "asserted by the Worker)\n" + truncate_middle(state.delivery_evidence, s.max_log_tokens)
        )
    if state.handoff:
        parts.append("### Latest handoff\n" + truncate_middle(state.handoff, s.max_log_tokens))
    if state.test_summary:
        parts.append(
            "### Latest test summary\n" + truncate_tail(state.test_summary, s.max_log_tokens)
        )
    if state.verification:
        parts.append(
            "### Supervisor verification narrative\n"
            + truncate_middle(state.verification, s.max_log_tokens)
        )
    if state.diff:
        diff = strip_sensitive_files_from_diff(state.diff)
        parts.append(
            "### Latest diff\n```diff\n" + truncate_middle(diff, s.max_diff_tokens) + "\n```"
        )
    if state.feedback:
        parts.append("### Latest reviewer feedback\n" + truncate_middle(state.feedback, 2000))
    for k, v in sorted(state.extra.items()):
        parts.append(f"### {k}\n{truncate_middle(v, s.max_log_tokens)}")
    return "\n\n".join(parts)


def build_prompt(
    session: Session,
    role: str,
    task: Task | None = None,
    project: Project | None = None,
    state: DynamicState | None = None,
) -> BuiltPrompt:
    state = state or DynamicState()
    mem = memory_service.for_prompt(session, role, project.slug if project else None)

    def sec(name: str, content: str) -> PromptSection:
        return PromptSection(
            name=name,
            content=redact(content),
            static=name in STATIC_SECTIONS,
            cache_breakpoint=name in BREAKPOINTS,
        )

    sections = [
        sec("system", system_prompt_for(role)),
        sec("global_memory", f"{memory.GLOBAL_HEADER}\n{mem['global']}" if mem["global"] else ""),
        sec("role_memory", f"{memory.ROLE_HEADER}\n{mem['role']}" if mem["role"] else ""),
        sec(
            "project_memory", f"{memory.PROJECT_HEADER}\n{mem['project']}" if mem["project"] else ""
        ),
        sec("contracts", _contracts(task, project)),
        sec("phase", _phase(task, project)),
        sec("history", truncate_middle(state.history or "", 2000)),
        sec("dynamic_state", _dynamic(state)),
    ]
    prompt = BuiltPrompt(role=role, sections=sections)
    return enforce_budget(prompt)


def enforce_budget(prompt: BuiltPrompt) -> BuiltPrompt:
    """If over the max context, shrink dynamic sections into a structured summary."""
    limit = get_settings().max_prompt_context_tokens
    total = sum(estimate_tokens(s.content) for s in prompt.sections)
    if total <= limit:
        return prompt
    static_tokens = sum(estimate_tokens(s.content) for s in prompt.sections if s.static)
    room = max(500, limit - static_tokens)
    dyn = [s for s in prompt.sections if not s.static and s.content]
    per = max(200, room // max(1, len(dyn)))
    for s in dyn:
        if estimate_tokens(s.content) > per:
            s.content = (
                "[structured summary: content exceeded the context budget and was "
                "truncated]\n" + truncate_middle(s.content, per)
            )
    return prompt


# ---------------------------------------------------------------- preview


def preview(sections: list[PromptSection]) -> dict[str, Any]:
    rows = []
    warnings: list[str] = []
    seen_dynamic: str | None = None
    for idx, s in enumerate(sections):
        tokens = estimate_tokens(s.content)
        rows.append(
            {
                "index": idx,
                "name": s.name,
                "static": s.static,
                "cache_breakpoint": s.cache_breakpoint,
                "tokens": tokens,
                "empty": not s.content.strip(),
                "excerpt": s.content[:300],
            }
        )
        if not s.content.strip():
            continue
        if not s.static and seen_dynamic is None:
            seen_dynamic = s.name
        elif s.static and seen_dynamic is not None:
            warnings.append(
                f"dynamic section '{seen_dynamic}' appears before static section '{s.name}' "
                "— this busts the prompt cache for everything after it"
            )
        if s.static:
            for pat, label in _VOLATILE:
                if pat.search(s.content):
                    warnings.append(
                        f"static section '{s.name}' contains a {label}; volatile "
                        "values in the cached prefix invalidate the cache"
                    )
                    break
    static_tokens = sum(r["tokens"] for r in rows if r["static"])
    dynamic_tokens = sum(r["tokens"] for r in rows if not r["static"])
    return {
        "sections": rows,
        "static_tokens": static_tokens,
        "dynamic_tokens": dynamic_tokens,
        "total_tokens": static_tokens + dynamic_tokens,
        "cacheable_fraction": round(static_tokens / max(1, static_tokens + dynamic_tokens), 3),
        "warnings": warnings,
        "ok": not warnings,
    }


def sections_from_dicts(items: list[dict[str, Any]]) -> list[PromptSection]:
    return [
        PromptSection(
            name=str(i.get("name", f"section{n}")),
            content=str(i.get("content", "")),
            static=bool(i.get("static", False)),
            cache_breakpoint=bool(i.get("cache_breakpoint", False)),
        )
        for n, i in enumerate(items)
    ]
