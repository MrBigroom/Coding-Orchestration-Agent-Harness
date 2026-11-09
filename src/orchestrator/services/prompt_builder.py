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
import uuid
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
from orchestrator.utils.redaction import (
    RedactionHit,
    redact,
    redact_with_report,
    strip_sensitive_files_from_diff,
)
from orchestrator.utils.truncate import estimate_tokens, truncate_middle, truncate_tail


@dataclass
class TruncationRecord:
    """One piece of content the prompt builder did not send in full.

    Recorded for the archived reviewer payload (T-11 DoD 1): a truncation that is not named is
    indistinguishable from evidence the reviewer never received.

    T-16 adds a `kind`, so the manifest can separate the three things a reader used to have to
    guess between: `budget` (content cut to fit a token budget), `secret` (content withheld
    because it is a sensitive file) and `redaction` (content rewritten in place by the redactor).
    A redaction row also names the pattern class and the substitution count, never the match.
    """

    section: str
    item: str
    bytes_available: int
    bytes_included: int
    budget: str
    reason: str
    kind: str = "budget"
    pattern_class: str = ""
    substitutions: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "item": self.item,
            "bytes_available": self.bytes_available,
            "bytes_included": self.bytes_included,
            "budget": self.budget,
            "reason": self.reason,
            "kind": self.kind,
            "pattern_class": self.pattern_class,
            "substitutions": self.substitutions,
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
        *,
        kind: str = "budget",
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
                kind=kind,
            )
        )

    def record_redactions(
        self,
        section: str,
        item: str,
        original: str,
        rendered: str,
        hits: tuple[RedactionHit, ...],
    ) -> None:
        """One manifest row per pattern class that rewrote reviewer-visible text (T-16 DoD 1).

        Deliberately records only the class and the count: a manifest that reprinted the match
        would leak the secret it is describing, and one that reprinted the replacement would
        describe `[REDACTED]` as if it were content.
        """
        if rendered == original or not hits:
            return
        for hit in hits:
            self.records.append(
                TruncationRecord(
                    section=section,
                    item=item,
                    bytes_available=len(original.encode("utf-8")),
                    bytes_included=len(rendered.encode("utf-8")),
                    budget="redaction (not a token budget)",
                    reason=(
                        f"pattern class '{hit.pattern_class}' rewrote {hit.count} occurrence(s) "
                        "in place; the matched text and the substituted value are not recorded"
                    ),
                    kind="redaction",
                    pattern_class=hit.pattern_class,
                    substitutions=hit.count,
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

    Returns `(rendered, hunks_shown, hunks_total, sliced)`, where `sliced` is True when the body of
    the shown hunk was itself cut. The rendered text always ends with a visible
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
        what = (
            "the body of the shown hunk was cut"
            if sliced
            else f"{len(hunks) - len(shown)} hunk(s) omitted"
        )
        omitted_headers = [
            hunk.splitlines()[0] for hunk in hunks[len(shown) :] if hunk.splitlines()
        ]
        where = " (" + ", ".join(omitted_headers) + ")" if omitted_headers else ""
        rendered += (
            f"\n... [review diff: {len(shown)} of {len(hunks)} hunks shown for "
            f"{path or 'this file'}; {what} by the per-file diff budget{where} — the "
            "archive's truncation manifest has the byte counts] ...\n"
        )
    return rendered, len(shown), len(hunks), sliced


# T-16 DoD 7/8: evidence and compensation artifacts get their own share of the diff budget. The
# number is a fraction of the post-header budget, not a wider `max_diff_tokens`; source files can
# only spend the rest, so a large source tree cannot consume the evidence that makes its claims
# checkable. The reserve is handed back to source when there are no compensation artifacts. When
# the compensation bundle still cannot fit, the manifest says so AND the review raises a
# harness-detected finding rather than leaving a reviewer to conclude the evidence is missing.
COMPENSATION_BUDGET_FRACTION = 1 / 3

# T-36: the allocator must satisfy the invariant the auditor enforces. Every diffstat file gets
# a first-hunk floor before any file receives more, and `_floor_head` is HARD-capped at
# `DIFF_FILE_FLOOR_TOKENS` by `estimate_tokens` (longest fitting variant of header/marker/body),
# so a long path or marker cannot make one floor cost more than another. Arithmetic for the DoD 1
# guarantee at the default `max_diff_tokens=12000`:
#   post-header  12000 - 300 = 11700   (header_reserve, capped at 300)
#   source share 11700 * 2/3 = 7800    (T-16 compensation reserve in force)
#   40 files     40 * 160    = 6400  <= 7800, leaving 1400;  48 files = 7680 <= 7800
#   73 files = 11700 // 160 when there is no compensation reserve.
# Proved by `test_forty_files_keep_their_floor_at_the_default_budget_even_with_long_paths`, whose
# fixture carries `verify-evidence/` files so the 7800 share (not the full 11700) is exercised.
# The floor stays at 160 rather than buying a bigger first hunk: the remainder pass still gives a
# big source file the lion's share, while this keeps README/docs/tests off zero bytes (R-68: 8 of
# 30 files at 0).
DIFF_FILE_FLOOR_TOKENS = 160
DIFF_GROUP_MINIMUM_TOKENS = 60  # ~ a file header plus one short hunk


def is_compensation_artifact(path: str) -> bool:
    """A path whose purpose is making a Worker claim checkable, not implementing the task."""
    p = path.replace("\\", "/")
    name = p.rsplit("/", 1)[-1]
    if p.startswith("verify-evidence/"):
        return True
    if name.startswith("contract-change-request-") and name.endswith(".md"):
        return True
    return name in {"handoff.md", "handoff.json"} and not p.startswith("runs/")


def _record_omitted(
    recorder: TruncationRecorder | None,
    path: str,
    chunk: str,
    budget_text: str,
    reason: str,
) -> None:
    if recorder is not None:
        recorder.record("dynamic_state", f"diff:{path}", chunk, "", budget_text, reason)


def _floor_marker(path: str) -> str:
    """The reviewer-visible note that a file was stopped at its floor."""
    return (
        f"\n... [review diff floor: {path or 'this file'} stopped at its first hunk; "
        "the remainder did not fit after every file's floor — see the truncation manifest] ...\n"
    )


# Used when the full marker does not fit inside the floor next to the diff header. The path and
# the byte counts are in the preamble and the truncation manifest either way.
FLOOR_MARKER_SHORT = "\n... [review diff floor: first hunk only; see the truncation manifest] ...\n"


def _fit_floor(header: str, marker: str, hunk: str, max_tokens: int) -> str | None:
    """Header + `@@` line + as much of the first hunk as fits + marker, or None if it cannot fit.

    The `@@` line is never dropped: the auditor asks for one hunk, and a render without it is
    indistinguishable from an omitted file. `None` means this (header, marker) combination cannot
    carry a hunk inside the budget, so `_floor_head` tries a shorter variant.
    """
    lines = hunk.splitlines(keepends=True)
    if not lines:
        return None
    at_line = lines[0]
    rest = "".join(lines[1:])
    room = max_tokens - estimate_tokens(header) - estimate_tokens(at_line) - estimate_tokens(marker)
    if room < 0:
        return None
    body = rest if estimate_tokens(rest) <= room else truncate_middle(rest, room)
    candidate = header + at_line + body + marker
    # `estimate_tokens` rounds up, so verify rather than trust the arithmetic above.
    return candidate if estimate_tokens(candidate) <= max_tokens else None


def _floor_head(chunk: str, max_tokens: int, path: str) -> tuple[str, int, int, bool]:
    """Render only the first hunk, keeping the header and a visible floor marker.

    The allocator uses this for a file that gets its floor but no remainder. The result is
    **hard-capped** at `max_tokens` by `estimate_tokens`: variants are tried in preference order
    (full diff header before the compact one, full marker before the short one, full body before
    a middle slice) and the first that fits wins. That cap is what makes
    `DIFF_FILE_FLOOR_TOKENS * file_count` a real upper bound, so a 120-character path plus a
    160-token floor cannot silently cost 200 tokens and break the arithmetic the docs state.

    Returning `""` means no variant fitted at all (a path too long for even one header line);
    the caller then names the file as omitted instead of pretending it received a floor.
    """
    lines = chunk.splitlines(keepends=True)
    hunk_starts = [i for i, line in enumerate(lines) if line.startswith("@@")]
    if estimate_tokens(chunk) <= max_tokens:
        return chunk, 1, 1, False
    if not hunk_starts:
        return truncate_middle(chunk, max_tokens), 1, 1, True
    header = "".join(lines[: hunk_starts[0]])
    hunks: list[str] = []
    for n, start in enumerate(hunk_starts):
        end = hunk_starts[n + 1] if n + 1 < len(hunk_starts) else len(lines)
        hunks.append("".join(lines[start:end]))
    headers = [header]
    if path:
        # The splitter needs only the `diff --git a/<path> b/<path>` line to keep the path, so the
        # redundant ---/+++ copies are droppable when a pathologically long path crowds the floor.
        compact = f"diff --git a/{path} b/{path}\n"
        if compact != header:
            headers.append(compact)
    for candidate_header in headers:
        for marker in (_floor_marker(path), FLOOR_MARKER_SHORT, ""):
            rendered = _fit_floor(candidate_header, marker, hunks[0], max_tokens)
            if rendered is not None:
                return rendered, 1, len(hunks), True
    return "", 1, len(hunks), True


def _allocate_diff_group(
    group: list[tuple[str, str]],
    budget: int,
    *,
    max_tokens: int,
    recorder: TruncationRecorder | None,
    full: list[str],
    partial: list[str],
    omitted: list[str],
    rendered_parts: list[str],
    evidence: bool,
) -> int:
    """Spend one group's budget; returns the tokens actually used.

    T-36 floor pass: every file in the group first receives its first-hunk floor
    (`DIFF_FILE_FLOOR_TOKENS`, or its whole content when smaller). Only after that does any file
    receive more, so a large first file cannot leave later README/docs/test files at 0 bytes. A
    file whose floor does not fit is named in the manifest as omitted entirely; an evidence file
    that cannot even get its floor keeps the old sub-minimum behaviour so a tiny budget still
    shows something and says it is below the floor. The floor render itself is capped at
    `DIFF_FILE_FLOOR_TOKENS` by `estimate_tokens` (`_floor_head`), so the floor pass can never
    spend more than `DIFF_FILE_FLOOR_TOKENS * len(group)`, which is what the DoD 1 arithmetic
    relies on. A file that is floored but gets no remainder is recorded with
    `_floor_only_reason(...)`, which distinguishes an exhausted group budget from a share under
    the floor and from a next hunk that did not fit (hunk granularity).

    After the floor, the source group keeps its historical cap (one file may use at most half its
    group budget) and is topped up in priority order, so the big source file still gets the
    remainder. The evidence group is allocated **fairly**, not greedily: every file is brought up
    to `budget // len(group)` before any leftover is handed back to files that still need more. A
    large first evidence log therefore cannot consume the budget that a later
    `zz_review_index.txt` needs; the earlier greedy rule
    (`max(per_file, remaining - minimum * left)`) let it do exactly that (T-16 review round 1,
    finding 1).

    Allocation is by **delivered** tokens, not by the token target: `_chunk_head` stops on whole
    hunk boundaries, so a 3900-token target can emit a 2175-token render. The difference is
    returned to the group and offered to later files (and to the same file on a later pass), which
    is what stops README/docs sitting at their floor while tokens no one could spend are reported
    as exhausted (T-36 review round 6, finding 1/2).
    """
    if not group:
        return 0
    sizes = [estimate_tokens(chunk) for _path, chunk in group]
    minimum = DIFF_GROUP_MINIMUM_TOKENS
    if budget <= 0:
        for path, chunk in group:
            omitted.append(path)
            _record_omitted(
                recorder,
                path,
                chunk,
                f"max_diff_tokens={max_tokens} tokens",
                "omitted entirely: omitted because the diff budget was exhausted by "
                "higher-priority files",
            )
        return 0

    share = max(0, budget // max(1, len(group)))
    count = len(group)
    # Render-aware allocation state. `targets` is the token budget handed to `_chunk_head`,
    # `renders` is the text it produced and `costs` is what that text actually costs. The three
    # differ because `_chunk_head` stops on whole hunk boundaries: asking a 160-token floor file
    # for 700 tokens can produce a 500-token render, and the 200 undelivered tokens belong to the
    # group, not to that file (T-36 review round 6, finding 1). The old code allocated token
    # targets and rendered afterwards, so that 200 was silently lost while later files stayed at
    # their floor.
    targets = [0] * count
    renders: list[str] = [""] * count
    costs = [0] * count
    floored = [False] * count
    # True when `_floor_head` could not render *any* variant inside DIFF_FILE_FLOOR_TOKENS. Such a
    # file has no affordable floor, so it must be reported as omitted entirely rather than as a
    # funded floor of 0 tokens.
    floor_unrenderable = [False] * count
    floor_renders: list[str | None] = [None] * count
    floor_costs: list[int | None] = [None] * count
    remaining = budget

    def _render_target(idx: int, target: int) -> tuple[str, int]:
        """Render file `idx` with a token target and return `(text, delivered tokens)`."""
        path, chunk = group[idx]
        size = sizes[idx]
        if target >= size:
            return chunk, size
        if target <= 0:
            return "", 0
        rendered, _shown, _total, _sliced = _chunk_head(chunk, target, path)
        cost = estimate_tokens(rendered)
        if cost > target:
            # `_chunk_head` keeps a minimum body slice for very small targets, which can exceed
            # the target itself. `_floor_head` is hard-capped by `estimate_tokens`, so use it when
            # a sub-minimum target has to stay a real bound (the tiny-budget evidence branch).
            capped, _shown, _total, _sliced = _floor_head(chunk, target, path)
            if capped:
                return capped, estimate_tokens(capped)
        return rendered, cost

    def _raise(idx: int, limit: int) -> int:
        """Give file `idx` as much of `limit` as the group can still afford.

        Returns the tokens actually gained. A hunk is all-or-nothing, so asking for exactly
        `current + remaining` can stall below the next boundary; the largest affordable target is
        found instead. A target that buys no bytes is still remembered, so the caller does not
        retry the same request forever while another file could use the tokens.
        """
        nonlocal remaining
        limit = min(limit, sizes[idx])
        if remaining <= 0 or targets[idx] >= limit:
            return 0
        affordable = costs[idx] + remaining
        rendered, cost = _render_target(idx, limit)
        chosen: tuple[int, str, int] | None = None
        if cost <= affordable:
            chosen = (limit, rendered, cost)
        else:
            low, high = targets[idx], limit
            while low <= high:
                mid = (low + high) // 2
                mid_rendered, mid_cost = _render_target(idx, mid)
                if mid_cost <= affordable:
                    chosen = (mid, mid_rendered, mid_cost)
                    low = mid + 1
                else:
                    high = mid - 1
        if chosen is None:
            return 0
        chosen_target, chosen_rendered, chosen_cost = chosen
        targets[idx] = chosen_target
        gained = chosen_cost - costs[idx]
        if gained <= 0:
            return 0
        renders[idx] = chosen_rendered
        costs[idx] = chosen_cost
        remaining -= gained
        return gained

    if evidence and share < minimum:
        # The budget cannot fund every file: keep the T-16 behaviour of a minimum slice for as
        # many files as fit, in priority order, rather than confirming every file at a useless
        # sub-minimum share. This branch is not the normal floor pass; the manifest below says
        # each file is below the floor.
        for idx, size in enumerate(sizes):
            if remaining < minimum:
                break
            _raise(idx, min(size, minimum))
        # Only now may an included file take what the others did not need, again by delivered
        # cost so an under-delivered hunk does not strand the rest of the group's budget.
        while remaining > 0:
            gained = 0
            for idx, size in enumerate(sizes):
                if remaining <= 0:
                    break
                if not costs[idx] or costs[idx] >= size:
                    continue
                gained += _raise(idx, size)
            if gained <= 0:
                break
        per_file, cap = share, 0
    else:
        # Floor pass. Render the floor once so the budget accounts for the header and marker it
        # will actually cost; a file smaller than the floor is simply included whole.
        for idx, ((path, chunk), size) in enumerate(zip(group, sizes, strict=True)):
            if size <= DIFF_FILE_FLOOR_TOKENS:
                floor_renders[idx] = chunk
                floor_costs[idx] = size
            else:
                rendered, _shown, _total, _sliced = _floor_head(
                    chunk, DIFF_FILE_FLOOR_TOKENS, path
                )
                floor_renders[idx] = rendered or None
                floor_costs[idx] = estimate_tokens(rendered) if rendered else None
        for idx, need in enumerate(floor_costs):
            if need is None:
                floor_unrenderable[idx] = True
                continue
            need = min(need, sizes[idx])
            if remaining >= need:
                targets[idx] = need
                renders[idx] = floor_renders[idx] or ""
                costs[idx] = need
                floored[idx] = True
                remaining -= need

        # A budget too small for even the floor of an evidence file still shows a minimum slice
        # for as many as fit, so a tiny budget is not silently blank.
        if evidence and remaining >= minimum:
            for idx, size in enumerate(sizes):
                if floored[idx] or costs[idx]:
                    continue
                _raise(idx, min(size, minimum))

        if evidence:
            # Pass 1: bring every included file up to its fair share, by delivered cost.
            for idx, size in enumerate(sizes):
                if not costs[idx]:
                    continue
                _raise(idx, min(size, share))
            # Pass 2: only now may a file take leftover the others did not need. The loop repeats
            # because a file can stop at a hunk boundary: the tokens it did not take become
            # available to the files after it (or to it again on the next pass).
            while remaining > 0:
                gained = 0
                for idx, size in enumerate(sizes):
                    if remaining <= 0:
                        break
                    if not costs[idx] or costs[idx] >= size:
                        continue
                    gained += _raise(idx, size)
                if gained <= 0:
                    break
            per_file, cap = share, 0
        else:
            per_file = max(120, budget // max(1, len(group)))
            cap = max(per_file, budget // 2)
            # Top up in priority order, capped. Files that could not get a floor are not
            # resurrected here: their floor was unaffordable, and the manifest names them omitted.
            while remaining > 0:
                gained = 0
                for idx, size in enumerate(sizes):
                    if remaining <= 0:
                        break
                    if not costs[idx] or costs[idx] >= size:
                        continue
                    gained += _raise(idx, min(size, cap))
                if gained <= 0:
                    break

    # What the allocation passes could not spend, counted on **delivered** tokens rather than on
    # token targets (T-36 review finding 2): a floor-only row must not claim the remainder was
    # exhausted while the group still had tokens no hunk could absorb.
    leftover = remaining
    used = 0
    for idx, ((path, chunk), size) in enumerate(zip(group, sizes, strict=True)):
        cost = costs[idx]
        budget_text = (
            f"max_diff_tokens={max_tokens} tokens; evidence fair share={per_file} "
            "tokens; compensation reserve in force"
            if evidence
            else f"max_diff_tokens={max_tokens} tokens; per-file={per_file} tokens; cap={cap}"
        )
        if cost <= 0:
            omitted.append(path)
            if floor_unrenderable[idx]:
                _record_omitted(
                    recorder,
                    path,
                    chunk,
                    budget_text,
                    "omitted entirely: no floor render fits inside the "
                    f"{DIFF_FILE_FLOOR_TOKENS}-token floor for this path (its diff header alone "
                    "is larger than the floor), so it received no bytes",
                )
                continue
            _record_omitted(
                recorder,
                path,
                chunk,
                budget_text,
                "omitted entirely because the group budget could not fund this file's "
                f"{DIFF_FILE_FLOOR_TOKENS}-token floor after higher-priority files",
            )
            continue
        if cost >= size:
            rendered = chunk
            hunks_shown, hunks_total, sliced = 1, 1, False
            full.append(path)
            reason = "included in full"
        elif floored[idx] and cost == floor_costs[idx]:
            rendered = renders[idx]
            partial.append(f"{path} (first hunk only, floor allocation)")
            reason = _floor_only_reason(leftover, per_file, cap)
        else:
            rendered = renders[idx]
            _same, hunks_shown, hunks_total, sliced = _chunk_head(chunk, targets[idx], path)
            detail = f"{hunks_shown} of {hunks_total} hunks"
            if sliced:
                detail += ", body cut inside the shown hunk"
            if floored[idx] or cost >= DIFF_FILE_FLOOR_TOKENS:
                partial.append(f"{path} ({detail})")
                reason = (
                    f"per-file diff budget: {detail} shown; omitted hunk headers are named inline "
                    "in the reviewer-visible text"
                )
            else:
                detail += ", below floor"
                partial.append(f"{path} ({detail})")
                reason = (
                    "below floor: the group budget could not fund this file's "
                    f"{DIFF_FILE_FLOOR_TOKENS}-token floor; a {minimum}-token minimum was shown "
                    "instead"
                )
        rendered_parts.append(rendered)
        used += cost
        if recorder is not None:
            recorder.record(
                "dynamic_state",
                f"diff:{path}",
                chunk,
                rendered,
                budget_text,
                reason,
            )
    return used


def _floor_only_reason(leftover: int, per_file: int, cap: int) -> str:
    """Why a file that received its floor received nothing more (T-36 DoD 3).

    Three outcomes, and the manifest must not blur them:

    * `leftover <= 0` -- the group budget really was fully committed by delivered bytes.
    * `leftover > 0` and the file's **effective ceiling** is at or below the floor -- the group
      still had tokens, but no pass could hand this file more. For the source group the ceiling
      is `max(per_file, cap)`, because the top-up pass may spend up to `cap` even when the fair
      share is smaller (default budget, 49 files: `per_file=159`, `cap=3900`). The evidence group
      is uncapped after its fair-share pass, represented here by `cap == 0`: it can use the whole
      remaining group budget, so it may never receive the "share under the floor" claim.
    * otherwise -- the ceiling *was* larger than the floor, so the cause is hunk granularity: the
      next hunk did not fit inside what remained of the file's allowance and no hunk could absorb
      the group's leftover tokens.

    The pre-fix string asserted unconditionally that the share did not exceed the floor whenever
    `leftover > 0`, which is false for the measured shapes (this branch at `per_file=1114`,
    `leftover=1769`; R-68 at `per_file=260`; the default budget with 49 files at `per_file=159`,
    `cap=3900`). It also claimed the unallocated tokens "went to other files", contradicting
    itself: `leftover > 0` means they were not spent. The sentence is now derived from a comparison
    that is actually performed, and no branch says the leftover was allocated elsewhere (T-36
    review rounds 7 and 8, blocking findings).
    """
    if leftover <= 0:
        return (
            "floor only, remainder exhausted: the first hunk fits inside the "
            f"{DIFF_FILE_FLOOR_TOKENS}-token per-file floor, but the group budget was fully "
            "spent by the floor pass and the remainder pass, so this file received no more "
            "than its floor"
        )
    ceiling = max(per_file, cap)
    if cap > 0 and ceiling <= DIFF_FILE_FLOOR_TOKENS:
        return (
            "floor only, share under the floor: the first hunk fits inside the "
            f"{DIFF_FILE_FLOOR_TOKENS}-token per-file floor and this file's share "
            f"({per_file} tokens) and effective ceiling ({ceiling} tokens, cap {cap}) did "
            f"not exceed that floor, so the {leftover} tokens the allocation passes left over "
            "stayed unspent instead of being allocated to this file"
        )
    ceiling_text = f"{ceiling} tokens" if cap > 0 else "uncapped"
    return (
        "floor only, next hunk did not fit: the first hunk fits inside the "
        f"{DIFF_FILE_FLOOR_TOKENS}-token per-file floor, but rendering is hunk-granular, so "
        "this file's next hunk did not fit inside what remained of its own allowance "
        f"({per_file} tokens, cap {cap or 'none'}, effective ceiling {ceiling_text}); the "
        f"{leftover} tokens the group had left stayed unallocated because no hunk could "
        "absorb them"
    )


def render_diff_budget(
    patch: str, max_tokens: int, recorder: TruncationRecorder | None = None
) -> str:
    """Render a diff under an explicit per-file budget and name every file that did not fit.

    Replaces silently cutting the middle of one big diff. Files are ordered source-first, and a
    file is included **whole** whenever it fits in what is left, because a reviewer verifying
    wiring needs a complete file far more than it needs the first 10% of five files (round-3
    reviewer finding 5: the wiring was unverifiable because one budget cut landed mid-file). A
    file that cannot fit whole gets as many complete hunks as the remaining budget allows and an
    inline marker saying so; the files after it are named as omitted. The source group keeps the
    historical one-file-at-most-half rule, so a single generated source artifact cannot starve the
    rest. The output starts with explicit lists — included in full, partially included (with hunk
    counts), omitted — so the reviewer can tell *in the prompt itself* what was held back. The
    same information lands in the truncation manifest. `max_diff_tokens` is not widened.

    T-16 DoD 7: compensation artifacts (`verify-evidence/**`, contract-change requests, committed
    handoff copies) are a separate group with a reserved slice (`COMPENSATION_BUDGET_FRACTION`)
    of the post-header budget. Source files may not spend it, and any leftovers from source are
    added to it. Files in that group get a fair share so one huge evidence log cannot tail-drop
    the rest. If even the reserved slice cannot carry them, the manifest records the shortfall
    and `review_service` raises a harness-detected finding.

    T-36: before any of that, every file gets its first-hunk floor, so the auditor's invariant
    (every diffstat file has a hunk in the supplied patch) holds for at least 40 files at the
    default budget. Raising `max_diff_tokens` is not the fix; the floor is.
    """
    chunks = split_diff_files(patch)
    if not chunks or estimate_tokens(patch) <= max_tokens:
        return patch
    ordered = sorted(chunks, key=lambda pair: diff_priority(pair[0]))
    compensation = [(path, chunk) for path, chunk in ordered if is_compensation_artifact(path)]
    source = [(path, chunk) for path, chunk in ordered if not is_compensation_artifact(path)]
    header_reserve = max(80, min(300, max_tokens // 8))
    available = max_tokens - header_reserve
    reserve = int(available * COMPENSATION_BUDGET_FRACTION) if compensation else 0
    source_budget = available - reserve
    full: list[str] = []
    partial: list[str] = []
    omitted: list[str] = []
    rendered_parts: list[str] = []
    source_used = _allocate_diff_group(
        source,
        source_budget,
        max_tokens=max_tokens,
        recorder=recorder,
        full=full,
        partial=partial,
        omitted=omitted,
        rendered_parts=rendered_parts,
        evidence=False,
    )
    evidence_budget = max(0, available - source_used)
    _allocate_diff_group(
        compensation,
        evidence_budget,
        max_tokens=max_tokens,
        recorder=recorder,
        full=full,
        partial=partial,
        omitted=omitted,
        rendered_parts=rendered_parts,
        evidence=True,
    )
    # The rendered text follows allocation order (source first, then compensation evidence).
    source_per_file = max(120, source_budget // max(1, len(source))) if source else 0
    source_cap = max(source_per_file, source_budget // 2) if source else 0
    evidence_per_file = (
        evidence_budget // max(1, len(compensation)) if compensation else 0
    )
    if compensation:
        budget_line = (
            f"[review diff budget: max_diff_tokens={max_tokens}; {len(ordered)} file(s); "
            f"{len(source)} source file(s) share {source_budget} tokens (per-file "
            f"{source_per_file}, cap {source_cap}); {len(compensation)} compensation artifact(s) "
            f"have a reserved slice of {evidence_budget} tokens (per-file {evidence_per_file}); "
            f"every file first gets a {DIFF_FILE_FLOOR_TOKENS}-token first-hunk floor before any "
            "file gets more; files are ordered source-first, but the evidence that makes the "
            "source checkable is never left to the tail."
        )
    else:
        budget_line = (
            f"[review diff budget: max_diff_tokens={max_tokens}; {len(ordered)} file(s); "
            f"per-file budget {source_per_file} tokens (one file may use up to {source_cap}); "
            f"every file first gets a {DIFF_FILE_FLOOR_TOKENS}-token first-hunk floor before any "
            "file gets more; files are ordered source-first so code is funded before evidence."
        )
    preamble = (
        budget_line
        + f"\nincluded in full ({len(full)}): {', '.join(full) or 'none'}\n"
        f"partially included ({len(partial)}): {', '.join(partial) or 'none'}\n"
        f"omitted ({len(omitted)}): {', '.join(omitted) or 'none'}]"
    )
    return preamble + "\n" + "".join(rendered_parts)


def undelivered_compensation(
    records: list[dict[str, Any]],
    rendered: str | None = None,
    patch: str | None = None,
) -> list[str]:
    """Compensation files the rendered diff did not carry in full (T-16 DoD 7).

    When `rendered` and `patch` are supplied, the answer is taken from the **actual text sent**
    (which includes any later `enforce_budget` shrink), not only from the per-file rows recorded
    before that step. The record-only form is kept for callers that have just rendered the diff
    and for the unit tests that build a recorder by hand.
    """
    if rendered is not None and patch is not None:
        originals = dict(split_diff_files(patch))
        shown = dict(split_diff_files(rendered))

        def delivered(path: str, chunk: str) -> bool:
            """True when this file's text reached the reviewer, even if rewritten in place.

            The prior equality check treated an in-place redaction as an undelivered file,
            because the as-sent chunk no longer equalled the raw `patch.diff` chunk. Redaction is
            reported separately (a `kind="redaction"` manifest row), never as a budget shortfall
            (T-16 review round 2, finding 1). Comparing against `redact(chunk)` keeps the budget
            answer honest: a context-budget cut still differs from the redacted whole chunk.
            """
            shown_chunk = shown.get(path)
            if shown_chunk is None:
                return False
            return shown_chunk == chunk or shown_chunk == redact(chunk)

        return sorted(
            path
            for path, chunk in originals.items()
            if is_compensation_artifact(path) and not delivered(path, chunk)
        )
    out: list[str] = []
    for record in records:
        if str(record.get("kind") or "budget") != "budget":
            continue
        item = str(record.get("item", ""))
        if not item.startswith("diff:"):
            continue
        path = item[5:]
        if not is_compensation_artifact(path):
            continue
        if int(record.get("bytes_included") or 0) < int(record.get("bytes_available") or 0):
            out.append(path)
    return sorted(set(out))


_BUDGET_MANIFEST_HEADER = (
    "| section | file / item | bytes available | bytes included | budget | reason |",
    "|---|---|---:|---:|---|---|",
)
_REDACTION_MANIFEST_HEADER = (
    "| section | file / item | pattern class | substitutions | bytes before | bytes after | note |",
    "|---|---|---|---:|---:|---:|---|",
)


def _manifest_table(records: list[dict[str, Any]], redaction: bool = False) -> str:
    rows = list(_REDACTION_MANIFEST_HEADER if redaction else _BUDGET_MANIFEST_HEADER)
    for r in records:
        item = str(r.get("item", "")).replace("|", "\\|")
        if redaction:
            note = str(r.get("reason", "")).replace("|", "\\|")
            rows.append(
                f"| {r.get('section', '')} | {item} | "
                f"{str(r.get('pattern_class', '')).replace('|', '\\|')} | "
                f"{int(r.get('substitutions') or 0)} | {r.get('bytes_available', 0)} | "
                f"{r.get('bytes_included', 0)} | {note} |"
            )
            continue
        budget = str(r.get("budget", "")).replace("|", "\\|")
        reason = str(r.get("reason", "")).replace("|", "\\|")
        rows.append(
            f"| {r.get('section', '')} | {item} | {r.get('bytes_available', 0)} | "
            f"{r.get('bytes_included', 0)} | {budget} | {reason} |"
        )
    return "\n".join(rows)


def render_truncation_manifest(
    records: list[dict[str, Any]],
    *,
    rendered: str | None = None,
    patch: str | None = None,
) -> str:
    """The markdown manifest archived beside the exact reviewer input (T-11 DoD 1).

    T-16: the table is split by `kind` so a reader can tell withheld-as-a-secret from a budget cut
    from a rewrite in place. A redaction row names the pattern class and the substitution count and
    nothing else; its `note` column says whether the aggregate context budget later shrank the
    section it describes (T-16 review round 3, finding 5), never what was matched. When the
    evidence could not be delivered, the manifest states that shortfall in the same section, so it
    is not left for the reviewer to infer from an absent file.
    """
    if not records:
        undelivered_only = undelivered_compensation([], rendered=rendered, patch=patch)
        if not undelivered_only:
            return (
                "No truncation: every prompt section and every changed file was included in full "
                "in the archived input below."
            )
        return (
            "**Compensation-artifact shortfall:** the reviewer diff budget could not carry "
            f"{len(undelivered_only)} evidence/compensation file(s) in full: "
            + ", ".join(undelivered_only)
            + ". This is a harness-detected delivery failure, not missing Worker evidence; the "
            "review records it as a finding."
        )
    by_kind: dict[str, list[dict[str, Any]]] = {"budget": [], "secret": [], "redaction": []}
    for record in records:
        by_kind.setdefault(str(record.get("kind") or "budget"), []).append(record)
    if not any(by_kind[k] for k in ("secret", "redaction")):
        table = _manifest_table(by_kind.get("budget", []))
    else:
        sections: list[str] = []
        if by_kind.get("secret"):
            sections.append(
                "### Withheld as a secret (content removed, path shown)\n\n"
                + _manifest_table(by_kind["secret"])
            )
        if by_kind.get("budget"):
            sections.append("### Cut by a token budget\n\n" + _manifest_table(by_kind["budget"]))
        if by_kind.get("redaction"):
            sections.append(
                "### Rewritten in place by the redactor (not a budget cut, not withheld content)"
                "\n\n" + _manifest_table(by_kind["redaction"], redaction=True)
            )
        table = "\n\n".join(sections)
    undelivered = undelivered_compensation(records, rendered=rendered, patch=patch)
    if undelivered:
        table += (
            "\n\n**Compensation-artifact shortfall:** the reviewer diff budget could not carry "
            f"{len(undelivered)} evidence/compensation file(s) in full: {', '.join(undelivered)}. "
            "This is a harness-detected delivery failure, not missing Worker evidence; the review "
            "records it as a finding."
        )
    return table

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
    # T-16 DoD 3: the budgeted diff text the reviewer was actually sent, rendered once by
    # `render_diff_for_prompt` so the completeness check audits THIS text rather than the archived
    # full patch. `None` means "render it inside _dynamic" (unit-test callers).
    rendered_diff: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


def render_diff_for_prompt(
    patch: str, max_tokens: int, recorder: TruncationRecorder | None = None
) -> str:
    """Withhold sensitive files and apply the diff budget in one place.

    `review_service` calls this once, then uses the returned text both for the prompt and for the
    diff-completeness check (T-16 DoD 3). Recording lives here so a withheld file is a manifest row
    with `kind="secret"` (distinct from a budget cut) and the reviewer-visible marker is explicit.
    """
    dropped: list[str] = []
    diff = strip_sensitive_files_from_diff(patch, dropped=dropped)
    if dropped and recorder is not None:
        originals = dict(split_diff_files(patch))
        for path in dropped:
            recorder.record(
                "dynamic_state",
                f"diff:{path}",
                originals.get(path, ""),
                "",
                f"max_diff_tokens={max_tokens} tokens",
                "sensitive file: content withheld before the diff budget "
                "(utils/redaction.strip_sensitive_files_from_diff); only the path is shown",
                kind="secret",
            )
    return render_diff_budget(diff, max_tokens, recorder)


def _dynamic(
    state: DynamicState,
    recorder: TruncationRecorder | None = None,
    rendered_diff_out: list[str] | None = None,
    diff_nonce: str = "",
) -> str:
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
        rendered_diff = (
            state.rendered_diff
            if state.rendered_diff is not None
            else render_diff_for_prompt(state.diff, s.max_diff_tokens, recorder)
        )
        if rendered_diff_out is not None:
            rendered_diff_out.append(rendered_diff)
        parts.append(_diff_block_open(diff_nonce) + rendered_diff + "\n```")
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


_DIFF_BLOCK_HEADER = "### Latest diff"
_DIFF_BLOCK_CLOSE = "\n```"


def _diff_block_open(diff_nonce: str) -> str:
    """The sentinel that opens the diff block, carrying a per-build harness nonce.

    The block is located by TEXT SEARCH inside `dynamic_state`, a section that also carries
    Worker-written handoff text. With a fixed header, a submission could quote the header and a
    fabricated hunk list and become the audit-of-record for `diff_completeness`,
    `undelivered_compensation` and `supplied_evidence` -- the machinery whose whole purpose is to
    check the reviewer's input would then be checking text the Worker supplied (T-16 review round
    3, finding 2). The nonce is generated when the prompt is built, i.e. after every Worker string
    was already written, so no submission can contain it.

    A prompt built with an empty nonce is NOT injection-resistant; the review path always sets one
    (`build_prompt`). The empty form exists only so a hand-built `DynamicState` in a test still
    round-trips through `extract_rendered_diff`.
    """
    return f"{_DIFF_BLOCK_HEADER} {diff_nonce}\n```diff\n"


def extract_rendered_diff(prompt: BuiltPrompt) -> str:
    """The diff text left in the final `dynamic_state` section after every budget was applied.

    `review_service` audits exactly this string (T-16 DoD 3). An empty result means the aggregate
    context budget removed the diff block entirely, so the audit correctly reports that the
    reviewer received no hunks; it never silently falls back to the pre-budget render.

    The block is found by the nonce-bearing sentinel, so Worker-supplied text that quotes the
    header cannot substitute a fabricated diff for the real one (see `_diff_block_open`).
    """
    sentinel = _diff_block_open(prompt.diff_nonce)
    for section in prompt.sections:
        if section.name != "dynamic_state":
            continue
        start = section.content.find(sentinel)
        if start == -1:
            return ""
        start += len(sentinel)
        end = section.content.find(_DIFF_BLOCK_CLOSE, start)
        return section.content[start:end] if end != -1 else section.content[start:]
    return ""


def build_prompt(
    session: Session,
    role: str,
    task: Task | None = None,
    project: Project | None = None,
    state: DynamicState | None = None,
    recorder: TruncationRecorder | None = None,
) -> BuiltPrompt:
    state = state or DynamicState()
    # T-16: a caller that rendered the diff itself (review_service) passes the SAME recorder, so
    # the diff rows and the redaction rows land in one manifest and nothing is rendered twice.
    recorder = recorder or TruncationRecorder()
    # Budgeted selection: memory is a static section, so the cap is enforced here rather
    # than by enforce_budget, which can only shrink dynamic content.
    selection = memory_service.select_for_prompt(session, role, project.slug if project else None)
    mem = selection.sections

    def sec(name: str, content: str) -> PromptSection:
        report = redact_with_report(content)
        recorder.record_redactions(name, f"section:{name}", content, report.text, report.hits)
        return PromptSection(
            name=name,
            content=report.text,
            static=name in STATIC_SECTIONS,
            cache_breakpoint=name in BREAKPOINTS,
        )

    history = state.history or ""
    rendered_diff_out: list[str] = []
    # Per-build, so the diff sentinel cannot have been quoted by any Worker text already in `state`.
    diff_nonce = uuid.uuid4().hex
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
        sec("dynamic_state", _dynamic(state, recorder, rendered_diff_out, diff_nonce)),
    ]
    prompt = BuiltPrompt(
        role=role,
        sections=sections,
        memory_exclusions=selection.excluded,
        memory_warnings=selection.warnings,
        diff_nonce=diff_nonce,
    )
    prompt = enforce_budget(prompt, recorder)
    # T-16 DoD 3: report the diff text the reviewer is ACTUALLY sent. `enforce_budget` can trim
    # dynamic_state after `render_diff_for_prompt` ran, so re-extracting from the final section is
    # what makes the completeness audit and the sent prompt the same object (review round 1,
    # finding 5).
    prompt.rendered_diff = (
        extract_rendered_diff(prompt)
        if state.diff
        else (state.rendered_diff or "")
    )
    prompt.truncations = [r.as_dict() for r in recorder.records]
    return prompt


def _mark_redactions_later_cut(
    recorder: TruncationRecorder | None, section: str, limit: int
) -> None:
    """Truthfully flag redaction rows whose enclosing section the context budget then shrank.

    `record_redactions` runs while a section is being built, i.e. BEFORE `enforce_budget`, so a
    row can describe a rewrite in text the aggregate context budget later removed (T-16 review
    round 3, finding 5). The row cannot say precisely whether the redacted bytes survived the
    cut, so it says exactly that: the section was reduced afterwards and the described text may
    not have reached the reviewer. Claiming nothing is worse -- a reader would take the row as
    proof the reviewer saw a `[REDACTED]` where they may have seen nothing at all.
    """
    if recorder is None:
        return
    note = (
        "the enclosing section was afterwards reduced by the aggregate context budget "
        f"(max_prompt_context_tokens={limit}); the text described here may not have reached "
        "the reviewer"
    )
    for record in recorder.records:
        if record.kind == "redaction" and record.section == section and note not in record.reason:
            record.reason = f"{record.reason}; {note}"


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
            # T-16 review round 3, finding 5: a redaction row recorded before this cut must not
            # read as if the reviewer saw the rewritten text.
            _mark_redactions_later_cut(recorder, s.name, limit)
    return prompt


# ---------------------------------------------------------------- preview


def preview(sections: list[PromptSection], prompt: BuiltPrompt | None = None) -> dict[str, Any]:
    rows = []
    warnings: list[str] = []
    for warning in prompt.memory_warnings if prompt else []:
        warnings.append(warning)
    for exc in prompt.memory_exclusions if prompt else []:
        warnings.append(
            f"memory #{exc['id']} ({exc['bucket']}/{exc['scope']}, ~{exc['tokens']} tok) "
            f"is approved but NOT injected: {exc['reason']}"
        )
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
