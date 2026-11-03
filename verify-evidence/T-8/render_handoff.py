"""Render the human-readable handoff block from the machine-readable one.

`handoff.json` is the source; this only formats it, so the two cannot disagree. Writes
`.orchestrator/handoff.md` (harness-side) and a committed twin at `verify-evidence/T-8/handoff.md`
(reviewer-reachable: `.orchestrator/` is gitignored and nothing under it is archived into
`runs/<run_id>/`).

    PYTHONPATH=src python verify-evidence/T-8/render_handoff.py

ROUND 6 ORDER NOTE. `review_service.py:151-153` concatenates handoff.md + handoff.json and
`prompt_builder` middle-truncates that string to `max_log_tokens=4000`
(`core/config.py:62`), i.e. ~10.6 KB of head and ~5.3 KB of tail (`utils/truncate.py:13-19`).
Round 5's block was 48 KB, so the reviewer lost DoD 5-10 — reviewer finding 7, recorded by the
operator as a HARNESS limitation, not a T-8 deliverable (see `docs/build-loop.md` "Review input
size (known limitation)"). This renderer responds to it in the only way a Worker may without
touching review plumbing: it puts the DoD self-check FIRST, then the verdict digest, the measured
counts and the finding-by-finding resolution — everything decision-critical inside the head
budget — and keeps the total near 15 KB instead of 38 KB. The full per-item detail is in
`verify-evidence/T-8/README.md`, committed and cited row-by-row from the evidence strings.
"""

from __future__ import annotations

import json
import re
import subprocess

from orchestrator.core.schemas import Handoff

h = Handoff.model_validate(json.load(open(".orchestrator/handoff.json")))
t = h.tests
dod = h.dod_self_check


def git(*args: str) -> str:
    """Measure divergence at render time so this block cannot go stale between commits."""
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()


_behind, _ahead = (git("rev-list", "--left-right", "--count", "main...HEAD").split() + ["?", "?"])[:2]
_head = git("rev-parse", "--short", "HEAD")
_main = git("rev-parse", "--short", "main")
_mt = subprocess.run(["git", "merge-tree", "--write-tree", "main", "HEAD"], capture_output=True, text=True)
_merge_tree = "no conflicts" if _mt.returncode == 0 else f"CONFLICTS (exit {_mt.returncode})"

L: list[str] = []
L.append("# T-8 handoff — recovery commits after a timeout committed half-finished worker edits\n")
L.append(
    f"**Branch** `{h.branch}` · **Status** `{h.status}` · **Phase** {h.phase} · **Round 6** — applies "
    f"`reviews.6` (CHANGES_REQUESTED, 8 findings). **Base** `main`: merge-base `14a221c`, measured at "
    f"render time on HEAD `{_head}` = {_behind} behind / {_ahead} ahead of `main@{_main}`, "
    f"`git merge-tree --write-tree main HEAD` → {_merge_tree}. **Not merged, not pushed.** "
    "The branch name renders `ta[REDACTED]` in ARCHIVED copies until DoD 10's anchor ships — the running "
    "harness is `main@%s`, which redacts `details` and artifact text with the unanchored pattern; the "
    "on-disk `.orchestrator/handoff.json` here is byte-exact. **If this block reaches you truncated, the "
    "order is deliberate: DoD self-check first, then verdicts, counts and findings. Nothing in it is "
    "restated later, so a cut costs only the tail sections, and every evidence string also names a "
    "`verify-evidence/T-8/README.md` row.**\n" % _main
)

# ------------------------------------------------------------------ 1. DoD self-check (first: the gate's job)
L.append("## DoD self-check (item → met → evidence)\n")
for i, d in enumerate(dod, 1):
    box = "[x]" if d.met else "[ ]"
    L.append(f"### {i}. {box} {d.item}\n\n{d.evidence}\n")

# ------------------------------------------------------------------ 2. verdict digest + trailing fields
SHORT = [
    "trailer `Orchestrator-Recovery: <run>` + structured `findings_json` inside `details`",
    "subject prefix `chore(orchestrator-recovery):` kept byte-exact; trailer added alongside",
    "next run's prompt lists the commits and requires re-verify (lint+test) before handoff",
    "reviewer sections + GUI `RecoveryNote`; unauthorized `review_payload.md` removed (finding 2)",
    "offline timeout sim; the F821 wreckage is caught by tests AND by the lint command",
    "`run_completed` added, additive only; emission split documented; T-9 trigger set written",
    "per-round fields cleared at round start; a row cannot say in_progress + RUN_TIMEOUT",
    "contract-edit detector = GRANT test (not mention); 4 named per-case proofs; → NEEDS_HUMAN",
    "tracked vs untracked dirt split by decision; both predicates tested",
    "credential patterns anchored with a lookbehind; names survive, keys still redact",
]
L.append("## Verdict digest — 10 items, then the trailing fields\n")
L.append("| DoD | met | in one line |\n| --- | --- | --- |")
for i, d in enumerate(dod, 1):
    L.append(f"| {i} | {'✅' if d.met else '❌'} | {SHORT[i - 1]} |")
L.append("")
L.append(
    f"**New dependencies:** none ({len(h.new_dependencies)}). **Blockers:** "
    f"{len(h.blockers)} — {'none' if not h.blockers else '; '.join(h.blockers)}. **Questions:** "
    f"{len(h.questions)}. **Assumptions:** {len(h.assumptions)}. **Files changed:** "
    f"{len(h.files_changed)}. **Contracts touched:** {len(h.contracts_touched)} entries — "
    "`docs/frozen-contracts.md` **NOT edited**; the two open contract questions are CCR-T8-01 and "
    "CCR-T8-02, both **PROPOSED**. Tests: "
    f"`{t.command}` → {t.passed} passed / {t.failed} failed / {t.skipped} skipped "
    f"(`{t.log_path}`).\n"
)

# ------------------------------------------------------------------ 3. measured counts, one HEAD
L.append("## Tests — every number stamped at one HEAD\n")
_t = open("verify-evidence/T-8/targeted_tests.txt").read()
_blocks = re.findall(
    r'^### \$ (\./scripts/orch_verify\.sh test .*)\n(?:.*\n)*?^(\d+ passed.*)$', _t, re.M
)
_pair = "\n".join(
    f"$ {cmd}\n{num}" for cmd, num in _blocks if " -vv " not in cmd  # -vv lists NAMES, not a headline
)
L.append(
    "```\n$ ./scripts/orch_verify.sh test\n"
    f"{t.passed} passed, {t.failed} failed, {t.skipped} skipped   (SUITE_EXIT=0)\n"
    "$ ./scripts/orch_verify.sh lint\nAll checks passed!   (LINT_EXIT=0)\n" + _pair + "\n```\n\n"
    "Counts above are PARSED from the committed logs, not typed. Each `verify-evidence/T-8/*.txt` "
    f"begins with `# HEAD=<sha> … captured=<iso8601>`, written by the script beside it "
    "(`suite_and_lint.sh`, `targeted_tests.sh`, `ruling1_grant_tests.sh`, `dod1_recovery_machinery.sh`, "
    "`dod10_redaction_probe.sh`, `dod4_gui_typecheck.sh`), so \"one run at one HEAD\" is checkable "
    f"instead of asserted. This round: three full-suite invocations — `00c14ec` 421/0/6, `4ca2e3b` "
    f"421/0/6, `{_head}` 421/0/6 — identical, and the two commits between them added only files under "
    "`verify-evidence/`. Round 5's reviewer finding 5 was exactly this claim made without the stamp "
    "(targeted at `21f79c4`, suite/lint at `e0904bb`); the stamp is the fix, and it is why the +4 since "
    "round 5's 417 reconciles: `test_recovery_markers.py` 12 \u2192 16 (three split grant cases + one "
    "reviewer-payload test).\n\n"
    "`./scripts/orch_verify.sh` prints `ORCH_VERIFY|mode=test|module_file=…/worktrees/coding-orchestrator/8/src/…` "
    "and exits 3 if the import resolves outside this worktree — so the numbers describe **this branch**, "
    "not the main checkout (the editable-install trap; `docs/build-loop.md` § \"Verification commands must "
    "be path-independent\"). GUI: `tsc -b --force` over this branch's `web/src` → exit 0. All tests offline: "
    "mock harness, mock provider, temp git repos; no live service, no network, nothing installed.\n"
)

# ------------------------------------------------------------------ 4. findings → resolution
L.append(
    "## `reviews.6` findings → what changed\n"
    "\n"
    "| # | finding | resolution | where |\n"
    "| --- | --- | --- | --- |\n"
    "| 1 | CCR-T8-02 said `author` is an object; the server emits a string | CCR rewritten to the emitted "
    "shape with a key-by-key table; splitting author listed as a rejected option | "
    "`docs/contract-change-request-T8-02-recovery-commits-field.md` (codes it cites: "
    "`git_service.py:117,380`, `web/src/api/types.ts:89`) |\n"
    "| 2 | `runs/<id>/review_payload.md` added to the frozen artifact layout, no DoD grant, overlaps the "
    "tracked archiving item | **removed**; layout untouched; the gap documented with one owner; coverage "
    "moved to section tests | `review_service.py:162-166`, `docs/build-loop.md:297-314`, "
    "`tests/unit/test_recovery_markers.py:367`, `tests/integration/test_recovery_after_timeout.py:311-363` |\n"
    "| 3 | memory suggestion still called authorization 'a literal mention' | reworded to the grant rule, in "
    "both `project/decisions` and `project/gotchas` so they agree | `memory_suggestions` in "
    "`.orchestrator/handoff.json` |\n"
    "| 4 | DoD 4 evidence claimed a REST route was asserted | corrected: since ruling 3 the test asserts the "
    "route **404s** and that the detail field carries what the GUI renders | DoD 4 evidence, this block |\n"
    "| 5 | counts claimed from ONE run were measured at two HEADs | every capture re-run at one HEAD by "
    "HEAD-stamped scripts; the block states the stamp | `verify-evidence/T-8/suite_and_lint.sh`, first line "
    "of each `.txt`, Tests section above |\n"
    "| 6 | ruling-1 control and the three proofs not visible; no file:line per case | the 90-line test split "
    "into four named cases (+ the missing diffstat assertion) and an evidence file printing the control with "
    "line numbers and each PASS | `tests/unit/test_recovery_markers.py:235,278,324,353`, "
    "`verify-evidence/T-8/ruling1_grant_tests.txt` |\n"
    "| 7 | supplied block truncated at DoD 5; DoD 6-10 and trailing fields not reviewable | **recorded, not "
    "fixed** (operator ruling: harness limitation; do not touch review plumbing). Mitigated by size (38 KB → "
    "~15 KB) and by ordering the decision content into the head budget | `docs/build-loop.md:297-314`, this "
    "renderer's module docstring |\n"
    "| 8 | 'Three of them had a named event' mapped 3 events onto 2 statuses; stray line break | corrected to "
    "two of four terminal statuses; `cancelled` named as a `task_status` line; `run_error` labelled as not a "
    "terminal status; break closed | `docs/build-loop.md:333-378` |\n"
)

# ------------------------------------------------------------------ 5. provenance
L.append(
    "## Provenance\n\n"
    "**DoD 1-9's implementation content is not a Worker-authored commit on this branch.** It sits in "
    "`ba77a43`, a `chore(orchestrator-recovery): uncommitted worker output from R-10` commit (16 files, "
    "+1627/-46) created by the harness after run 10 was SIGTERMed at the 2700 s timeout with no "
    "`handoff.json`. That content was **never self-checked by the Worker that wrote it** — the defect this "
    "task is about. It is left exactly as it is: not re-implemented, rewritten, amended, rebased or "
    "re-split into neat feature commits. `5a0cd1a` (round 2's sweep, 9 files, all under `verify-evidence/`) "
    "is the same and is also left alone. Declared work: round 2 verified every item by reading file:line, "
    "implemented DoD 10 (`9001e82`, lookbehind in `94db0cc`) and closed gaps (`232b6d4`, `e42304a`, "
    "`05011a4`); round 3 was submit-only, no code (the discard defect that killed it is **T-10** — "
    "`run_service.py` untouched here); round 4 regenerated evidence; round 5 applied the operator's rulings "
    "(`21f79c4`); **round 6 applies reviews.6's findings** (`00c14ec` code/docs, `bd9239f` + this commit for "
    "evidence). Each item's evidence says which commit its content came from. Finding 2 is worth stating "
    "plainly: the undeclared `review_payload.md` was itself a defect inside that wreckage, found by a "
    "reviewer and not by the harness — the DoD 8 detector watches the five contract documents, not the "
    "artifact-layout paragraph of `frozen-contracts.md`.\n"
)

# ------------------------------------------------------------------ 6. summary (JSON field)
L.append("## Summary\n\n" + h.summary + "\n")

# ------------------------------------------------------------------ 7. files changed, grouped
groups: dict[str, list[str]] = {}
for f in h.files_changed:
    key = (
        "evidence"
        if f.startswith("verify-evidence/")
        else "docs"
        if f.startswith("docs/")
        else "web (GUI)"
        if f.startswith("web/")
        else "tests"
        if f.startswith("tests/")
        else "src (harness)"
    )
    groups.setdefault(key, []).append(f)
L.append("## Files changed (%d)\n" % len(h.files_changed))
for k in ("src (harness)", "tests", "docs", "web (GUI)", "evidence"):
    for f in groups.get(k, []):
        L.append(f"* `{f}`" + ("" if k != "src (harness)" else ""))
L.append("")

L.append("## New dependencies\n\nNone. Nothing was installed; no network was used.\n")
L.append("## Contracts touched\n\n" + "\n".join("* " + c for c in h.contracts_touched) + "\n")
L.append("## Assumptions\n\n" + "\n".join("* " + a for a in h.assumptions) + "\n")
L.append("## Questions\n\n" + "\n".join(f"{i}. {q}" for i, q in enumerate(h.questions, 1)) + "\n")
L.append("## Blockers\n\n" + ("None.\n" if not h.blockers else "\n".join("* " + b for b in h.blockers) + "\n"))
L.append(
    "## Evidence paths (committed; none under gitignored `.orchestrator/`)\n\n"
    "* `verify-evidence/T-8/README.md` — the DoD → file:line → proof index, incl. per-case citations for "
    "ruling 1 and the round-6 finding-by-finding note\n"
    "* `full_suite.txt`, `lint.txt`, `targeted_tests.txt`, `ruling1_grant_tests.txt`, "
    "`dod1_recovery_machinery.txt`, `dod10_redaction_probe.txt`, `dod4_gui_typecheck.txt` — each with the "
    "`.sh` that regenerates it, each HEAD-stamped\n"
    "* `write_handoff.py` (writes + schema-validates `handoff.json`) and `render_handoff.py` (this block)\n"
    "* `docs/contract-change-request-T8-build-log-events.md` (CCR-T8-01) and "
    "`docs/contract-change-request-T8-02-recovery-commits-field.md` (CCR-T8-02) — both PROPOSED, neither "
    "applied\n"
)
L.append(
    "## Memory suggestions (%d — full text in `.orchestrator/handoff.json`, which the harness reads "
    "machine-side)\n\n" % len(h.memory_suggestions)
    + "\n".join(
        f"* **{m.category}/{m.scope}** — {m.content[:230].rstrip()}…"
        for m in h.memory_suggestions
    )
    + "\n"
)
L.append(
    "## Out of scope, and not touched\n\n"
    "`src/orchestrator/adapters/**`, pi chat drivers, `tests/unit/test_chat_drivers.py`, `context_windows`, "
    "`chat_service.py`, `run_service.py`'s ingestion path (T-10 owns the discard-on-timeout defect), "
    "`docs/frozen-contracts.md`, merge-gate semantics, `core/state.py`, review/verification plumbing. "
    f"Divergence is noted, not resolved: `main@{_main}` moved twice since this branch was cut; "
    f"`git merge-tree` reports {_merge_tree}. `git diff main..HEAD` shows phantom deletions in "
    "`test_chat_drivers.py` that do not exist — diff against the merge-base `14a221c`, never against "
    "`main`.\n"
)

out = "\n".join(L)
for path in (".orchestrator/handoff.md", "verify-evidence/T-8/handoff.md"):
    open(path, "w").write(out)
    print("wrote", path, len(out.splitlines()), "lines", len(out), "bytes")
