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


@dataclass
class TruncationRecord:
    """One piece of content the prompt builder did not send in full.

    Recorded for the archived reviewer payload (T-11 DoD 1): a truncation that is not named is
    indistinguishable from evidence the reviewer never received.
    """

    section: str
    item: str
    bytes_available: int
    bytes_included: int
    budget: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "item": self.item,
            "bytes_available": self.bytes_available,
            "bytes_included": self.bytes_included,
            "budget": self.budget,
            "reason": self.reason,
        }


class TruncationRecorder:
    """Collects truncations without changing the text any provider receives."""

    def __init__(self) -> None:
        self.records: list[TruncationRecord] = []

    def record(
        self,
        section: str,
        item: str,
        original: str,
        rendered: str,
        budget: str,
        reason: str,
    ) -> None:
        if rendered == original:
            return
        self.records.append(
            TruncationRecord(
                section=section,
                item=item,
                bytes_available=len(original.encode("utf-8")),
                bytes_included=len(rendered.encode("utf-8")),
                budget=budget,
                reason=reason,
            )
        )


def _limited(
    recorder: TruncationRecorder | None,
    section: str,
    item: str,
    text: str,
    max_tokens: int,
    mode: str = "middle",
    budget_name: str = "max_log_tokens",
) -> str:
    """Apply the existing truncation helper and record exactly what it dropped."""
    rendered = (
        truncate_tail(text, max_tokens) if mode == "tail" else truncate_middle(text, max_tokens)
    )
    if recorder is not None:
        recorder.record(
            section,
            item,
            text,
            rendered,
            f"{budget_name}={max_tokens} tokens",
            f"{mode} truncation by prompt_builder",
        )
    return rendered


def split_diff_files(patch: str) -> list[tuple[str, str]]:
    """Split a unified diff into (path, chunk) pairs, preserving every byte per file."""
    if not patch:
        return []
    starts = [m.start() for m in re.finditer(r"(?m)^diff --git ", patch)]
    if not starts:
        return [("<unnamed diff>", patch)]
    starts.append(len(patch))
    out: list[tuple[str, str]] = []
    for i in range(len(starts) - 1):
        chunk = patch[starts[i] : starts[i + 1]]
        path = "<unknown>"
        for line in chunk.splitlines():
            if line.startswith("+++ b/"):
                path = line[6:]
                break
            if line.startswith("diff --git "):
                # Fallback for a binary/mode-only chunk with no +++ line: last token of the
                # header is the b-side path.
                parts = line.rsplit(" b/", 1)
                if len(parts) == 2:
                    path = parts[1]
        out.append((path, chunk))
    return out


# Source first, evidence last: when the diff has to be cut, the code a reviewer must judge wins
# the budget over the logs that describe it. Stable within a priority band (patch order).
_DIFF_PRIORITY: tuple[tuple[str, int], ...] = (
    ("src/", 0),
    ("web/src/", 0),
    ("tests/", 1),
    ("scripts/", 2),
    ("pyproject", 2),
    ("Makefile", 2),
    ("justfile", 2),
    ("docs/", 3),
    ("verify-evidence/", 5),
)


def diff_priority(path: str) -> int:
    """Ordering key for the diff budget: lower is more important to include in full."""
    for prefix, rank in _DIFF_PRIORITY:
        if path.startswith(prefix):
            return rank
    return 4


def _chunk_head(chunk: str, max_tokens: int, path: str = "") -> tuple[str, int, int, bool]:
    """Header plus as many complete hunks as fit; the first hunk is cut only as a last resort.

    Returns `(rendered, hunks_shown, hunks_total)`. The rendered text always ends with a visible
    marker when hunks were dropped, so a partially supplied file cannot look complete to the
    reviewer: only the archived manifest used to know (T-11 round-2 reviewer finding 4).
    """
    lines = chunk.splitlines(keepends=True)
    hunk_starts = [i for i, line in enumerate(lines) if line.startswith("@@")]
    if not hunk_starts:
        rendered = truncate_middle(chunk, max_tokens)
        return rendered, 1, 1, True
    header = "".join(lines[: hunk_starts[0]])
    hunks: list[str] = []
    for n, start in enumerate(hunk_starts):
        end = hunk_starts[n + 1] if n + 1 < len(hunk_starts) else len(lines)
        hunks.append("".join(lines[start:end]))
    shown: list[str] = []
    rendered = header
    for hunk in hunks:
        # Reserve room for the marker itself so the note about the cut is not the thing cut.
        candidate = rendered + hunk
        if estimate_tokens(candidate) + 40 > max_tokens:
            break
        rendered = candidate
        shown.append(hunk)
    sliced = False
    if not shown and hunks:
        # One hunk is itself larger than the file budget: keep a middle slice of it rather than
        # dropping the file silently.
        rendered = header + truncate_middle(
            hunks[0], max(50, max_tokens - estimate_tokens(header) - 40)
        )
        shown = [hunks[0]]
        sliced = True
    if sliced or len(shown) < len(hunks):
        what = "the body of the shown hunk was cut" if sliced else f"{len(hunks) - len(shown)} hunk(s) omitted"
        omitted_headers = [
            hunk.splitlines()[0] for hunk in hunks[len(shown) :] if hunk.splitlines()
        ]
        where = (
            " (" + ", ".join(omitted_headers) + ")" if omitted_headers else ""
        )
        rendered += (
            f"\n... [review diff: {len(shown)} of {len(hunks)} hunks shown for "
            f"{path or 'this file'}; {what} by the per-file diff budget{where} — the "
            "archive's truncation manifest has the byte counts] ...\n"
        )
    return rendered, len(shown), len(hunks), sliced


def render_diff_budget(
    patch: str, max_tokens: int, recorder: TruncationRecorder | None = None
) -> str:
    """Render a diff under an explicit per-file budget and name every file that did not fit.

    Replaces silently cutting the middle of one big diff. Files are ordered source-first, and a
    file is included **whole** whenever it fits in what is left, because a reviewer verifying
    wiring needs a complete file far more than it needs the first 10% of five files (round-3
    reviewer finding 5: the wiring was unverifiable because one budget cut landed mid-file). A
    file that cannot fit whole gets as many complete hunks as the remaining budget allows and an
    inline marker saying so; the files after it are named as omitted. One file may consume at most
    half the budget, so a single generated artifact cannot starve the rest. The output starts with
    three explicit lists — included in full, partially included (with hunk counts), omitted — so
    the reviewer can tell *in the prompt itself* what was held back. The same information lands in
    the truncation manifest. `max_diff_tokens` is not widened.
    """
    chunks = split_diff_files(patch)
    if not chunks or estimate_tokens(patch) <= max_tokens:
        return patch
    ordered = sorted(chunks, key=lambda pair: diff_priority(pair[0]))
    header_reserve = max(80, min(300, max_tokens // 8))
    per_file = max(120, (max_tokens - header_reserve) // max(1, len(ordered)))
    cap = max(per_file, (max_tokens - header_reserve) // 2)
    full: list[str] = []
    partial: list[str] = []
    omitted: list[str] = []
    rendered_parts: list[str] = []
    remaining = max_tokens - header_reserve
    minimum = 60  # ~ a file header plus one short hunk
    for path, chunk in ordered:
        size = estimate_tokens(chunk)
        budget = min(size, cap, max(remaining, 0))
        if budget < minimum:
            omitted.append(path)
            if recorder is not None:
                recorder.record(
                    "dynamic_state",
                    f"diff:{path}",
                    chunk,
                    "",
                    f"max_diff_tokens={max_tokens} tokens; per-file cap {per_file}/{cap} tokens",
                    "omitted because the diff budget was exhausted by higher-priority files",
                )
            continue
        if size <= budget:
            rendered = chunk
            hunks_shown, hunks_total, sliced = 1, 1, False
            full.append(path)
            reason = "included in full"
        else:
            rendered, hunks_shown, hunks_total, sliced = _chunk_head(chunk, budget, path)
            detail = f"{hunks_shown} of {hunks_total} hunks"
            if sliced:
                detail += ", body cut inside the shown hunk"
            partial.append(f"{path} ({detail})")
            reason = (
                f"per-file diff budget: {detail} shown; omitted hunk headers are named inline in "
                "the reviewer-visible text"
            )
        rendered_parts.append(rendered)
        remaining -= estimate_tokens(rendered)
        if recorder is not None:
            recorder.record(
                "dynamic_state",
                f"diff:{path}",
                chunk,
                rendered,
                f"max_diff_tokens={max_tokens} tokens; per-file={per_file} tokens; cap={cap}",
                reason,
            )
    preamble = (
        f"[review diff budget: max_diff_tokens={max_tokens}; {len(ordered)} file(s); "
        f"per-file budget {per_file} tokens (one file may use up to {cap}); files are ordered "
        "source-first so code is funded before evidence.\n"
        f"included in full ({len(full)}): {', '.join(full) or 'none'}\n"
        f"partially included ({len(partial)}): {', '.join(partial) or 'none'}\n"
        f"omitted ({len(omitted)}): {', '.join(omitted) or 'none'}]"
    )
    return preamble + "\n" + "".join(rendered_parts)


def render_truncation_manifest(records: list[dict[str, Any]]) -> str:
    """The markdown manifest archived beside the exact reviewer input (T-11 DoD 1)."""
    if not records:
        return (
            "No truncation: every prompt section and every changed file was included in full "
            "in the archived input below."
        )
    rows = [
        "| section | file / item | bytes available | bytes included | budget | reason |",
        "|---|---|---:|---:|---|---|",
    ]
    for r in records:
        item = str(r.get("item", "")).replace("|", "\\|")
        budget = str(r.get("budget", "")).replace("|", "\\|")
        reason = str(r.get("reason", "")).replace("|", "\\|")
        rows.append(
            f"| {r.get('section', '')} | {item} | {r.get('bytes_available', 0)} | "
            f"{r.get('bytes_included', 0)} | {budget} | {reason} |"
        )
    return "\n".join(rows)

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


def _contracts(
    task: Task | None, project: Project | None, recorder: TruncationRecorder | None = None
) -> str:
    parts: list[str] = []
    if project:
        doc = Path(project.path) / "docs" / "frozen-contracts.md"
        if doc.is_file():
            original = doc.read_text(errors="replace")
            parts.append(
                "Project frozen contracts (docs/frozen-contracts.md):\n"
                + _limited(
                    recorder,
                    "contracts",
                    "docs/frozen-contracts.md",
                    original,
                    4000,
                    budget_name="hard-coded contracts budget",
                )
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
    # T-12: what happened to the PREVIOUS round's submission, so a retry cannot look like a
    # fresh first attempt.
    submission: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


def _dynamic(state: DynamicState, recorder: TruncationRecorder | None = None) -> str:
    s = get_settings()
    parts: list[str] = []
    if state.recovery:
        parts.append(
            "### Recovery commits on this branch (created by the harness, NEVER self-checked "
            "by the Worker)\n"
            + _limited(
                recorder, "dynamic_state", "recovery commits", state.recovery, s.max_log_tokens
            )
        )
    if state.harness_findings:
        parts.append(
            "### Harness-detected findings (computed by the orchestrator, not asserted by a "
            "model)\n"
            + _limited(
                recorder,
                "dynamic_state",
                "harness findings",
                state.harness_findings,
                s.max_log_tokens,
            )
        )
    if state.submission:
        parts.append(
            "### Previous round submission status\n"
            + _limited(
                recorder, "dynamic_state", "previous submission", state.submission, s.max_log_tokens
            )
        )
    if state.delivery_evidence:
        parts.append(
            "### Handoff provenance and delivery evidence (computed from git by the harness, not "
            "asserted by the Worker)\n"
            + _limited(
                recorder,
                "dynamic_state",
                "delivery evidence",
                state.delivery_evidence,
                s.max_log_tokens,
            )
        )
    if state.handoff:
        parts.append(
            "### Latest handoff\n"
            + _limited(recorder, "dynamic_state", "handoff block", state.handoff, s.max_log_tokens)
        )
    if state.test_summary:
        parts.append(
            "### Latest test summary\n"
            + _limited(
                recorder,
                "dynamic_state",
                "test summary",
                state.test_summary,
                s.max_log_tokens,
                mode="tail",
            )
        )
    if state.verification:
        parts.append(
            "### Supervisor verification narrative\n"
            + _limited(
                recorder,
                "dynamic_state",
                "verification narrative",
                state.verification,
                s.max_log_tokens,
            )
        )
    if state.diff:
        # A withheld sensitive file is a truncation like any other: it is recorded here, named in
        # the archived manifest, and shown to the reviewer as an explicit omission marker instead
        # of vanishing between the patch and the prompt (T-11 round-2 reviewer finding 3).
        dropped: list[str] = []
        diff = strip_sensitive_files_from_diff(state.diff, dropped=dropped)
        if dropped and recorder is not None:
            originals = dict(split_diff_files(state.diff))
            for path in dropped:
                recorder.record(
                    "dynamic_state",
                    f"diff:{path}",
                    originals.get(path, ""),
                    "",
                    f"max_diff_tokens={s.max_diff_tokens} tokens",
                    "sensitive file: content withheld before the diff budget "
                    "(utils/redaction.strip_sensitive_files_from_diff); only the path is shown",
                )
        parts.append(
            "### Latest diff\n```diff\n"
            + render_diff_budget(diff, s.max_diff_tokens, recorder)
            + "\n```"
        )
    if state.feedback:
        parts.append(
            "### Latest reviewer feedback\n"
            + _limited(recorder, "dynamic_state", "reviewer feedback", state.feedback, 2000)
        )
    for k, v in sorted(state.extra.items()):
        parts.append(
            f"### {k}\n" + _limited(recorder, "dynamic_state", k, v, s.max_log_tokens)
        )
    return "\n\n".join(parts)


def build_prompt(
    session: Session,
    role: str,
    task: Task | None = None,
    project: Project | None = None,
    state: DynamicState | None = None,
) -> BuiltPrompt:
    state = state or DynamicState()
    recorder = TruncationRecorder()
    mem = memory_service.for_prompt(session, role, project.slug if project else None)

    def sec(name: str, content: str) -> PromptSection:
        return PromptSection(
            name=name,
            content=redact(content),
            static=name in STATIC_SECTIONS,
            cache_breakpoint=name in BREAKPOINTS,
        )

    history = state.history or ""
    sections = [
        sec("system", system_prompt_for(role)),
        sec("global_memory", f"{memory.GLOBAL_HEADER}\n{mem['global']}" if mem["global"] else ""),
        sec("role_memory", f"{memory.ROLE_HEADER}\n{mem['role']}" if mem["role"] else ""),
        sec(
            "project_memory", f"{memory.PROJECT_HEADER}\n{mem['project']}" if mem["project"] else ""
        ),
        sec("contracts", _contracts(task, project, recorder)),
        sec("phase", _phase(task, project)),
        sec(
            "history",
            _limited(recorder, "history", "history", history, 2000),
        ),
        sec("dynamic_state", _dynamic(state, recorder)),
    ]
    prompt = BuiltPrompt(role=role, sections=sections)
    prompt = enforce_budget(prompt, recorder)
    prompt.truncations = [r.as_dict() for r in recorder.records]
    return prompt


def enforce_budget(prompt: BuiltPrompt, recorder: TruncationRecorder | None = None) -> BuiltPrompt:
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
            original = s.content
            s.content = (
                "[structured summary: content exceeded the context budget and was "
                "truncated]\n" + truncate_middle(s.content, per)
            )
            if recorder is not None:
                recorder.record(
                    "context_budget",
                    s.name,
                    original,
                    s.content,
                    f"max_prompt_context_tokens={limit} tokens; per-section={per} tokens",
                    "aggregate prompt context budget (prompt_builder.enforce_budget)",
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
