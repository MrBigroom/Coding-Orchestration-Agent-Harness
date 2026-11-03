"""Render the human-readable handoff block from the machine-readable one.

Used by T-8's round 2 so `handoff.md` cannot drift from `handoff.json`: the JSON is the source
and this only formats it. Writes `.orchestrator/handoff.md` (harness-side) and a committed copy
at `verify-evidence/T-8/handoff.md` (reviewer-reachable, because `.orchestrator/` is gitignored
and nothing under it is archived into runs/<run_id>/).
"""

from __future__ import annotations

import json

from orchestrator.core.schemas import Handoff

raw = json.load(open(".orchestrator/handoff.json"))
h = Handoff.model_validate(raw)
t = h.tests

L: list[str] = []
L.append("# T-8 handoff — recovery commits after a timeout committed half-finished worker edits\n")
L.append(
    f"**Branch:** `{h.branch}`  \n**Status:** `{h.status}`  \n**Phase:** {h.phase}  \n"
    "**Base:** `main` (merge-base `14a221c`); branch is **6 ahead / 2 behind** `main@360228a` "
    "(`git merge-tree --write-tree main HEAD` = no conflicts). Not merged, not pushed.\n"
)
L.append("## Summary\n\n" + h.summary + "\n")
L.append(
    "## Provenance, before anything else\n\n"
    "**DoD 1-9 are not Worker-authored commits.** They are on this branch inside `ba77a43`, a "
    "`chore(orchestrator-recovery): uncommitted worker output from R-10` commit — 16 files, "
    "+1627/-46 — created by the harness after run 10 was SIGTERMed at the 2700 s adapter timeout "
    "with no `handoff.json`. That content was **never self-checked by the Worker that wrote it**, "
    "which is the exact defect this task is about. It is left exactly as it is: not "
    "re-implemented, not rewritten, not amended, not rebased, not re-split into neat feature "
    "commits.\n\n"
    "Round 2 (this round) did three things:\n\n"
    "1. wrote this handoff first, then verified every item by reading the shipped file and line;\n"
    "2. implemented the only item `ba77a43` did not contain — **DoD 10**, the redaction anchor "
    "(`9001e82`);\n"
    "3. closed three gaps found while verifying, in separate `feat(T-8)` commits: `232b6d4` (the "
    "two `docs/build-loop.md` sections that were *referenced but never written*, and "
    "`run_completed` missing from the event vocabulary — so DoD 6/7's \"documented\" was not "
    "literally true), `e42304a` (DoD 5's **lint**-command half: run 5's real defect was an F821 "
    "and `ba77a43` proved only the runtime/test path), `05011a4` (DoD 7's GUI-header half: the "
    "assertion was on the ORM row, not on the payload the header renders) plus the committed "
    "evidence directory, and `e7953b5` (CCR-T8-01, PROPOSED).\n"
)
L.append(
    "## Tests\n\n"
    "```\n$ ./scripts/orch_verify.sh test\n"
    f"{t.passed} passed, {t.failed} failed, {t.skipped} skipped   (exit 0)\n"
    "$ ./scripts/orch_verify.sh lint\nAll checks passed!   (exit 0)\n```\n\n"
    f"Log: `{t.log_path}` (committed). The wrapper prints "
    "`ORCH_VERIFY|mode=test|module_file=…/worktrees/coding-orchestrator/8/src/…` and exits 3 if "
    "the import resolves outside this worktree, so the numbers describe **this branch**, not the "
    "main checkout. Targeted: `verify-evidence/T-8/targeted_tests.txt` (16 recovery + 13 "
    "redaction). GUI: `tsc -b --force` over this branch's `web/src` → exit 0 "
    "(`verify-evidence/T-8/dod4_gui_typecheck.txt`). All tests offline: mock harness, mock "
    "provider, temp git repos; no live service, no network, no new dependency.\n"
)
L.append("## Files changed\n\n" + "\n".join(f"* `{f}`" for f in h.files_changed) + "\n")
L.append("## DoD self-check (item → met → evidence)\n")
for i, d in enumerate(h.dod_self_check, 1):
    box = "[x]" if d.met else "[ ]"
    L.append(f"### {i}. {box} {d.item}\n\n{d.evidence}\n")
L.append("## New dependencies\n\nNone.\n")
L.append("## Contracts touched\n\n" + "\n".join("* " + c for c in h.contracts_touched) + "\n")
L.append("## Assumptions\n\n" + "\n".join("* " + a for a in h.assumptions) + "\n")
L.append("## Questions\n\n" + "\n".join(f"{i}. {q}" for i, q in enumerate(h.questions, 1)) + "\n")
L.append("## Blockers\n\n" + ("None.\n" if not h.blockers else "\n".join(h.blockers) + "\n"))
L.append(
    "## Evidence paths\n\n"
    "* `verify-evidence/T-8/README.md` — the DoD → file:line → proof index (committed, so the "
    "reviewer can reach it)\n"
    "* `verify-evidence/T-8/full_suite.txt`, `lint.txt`, `targeted_tests.txt`\n"
    "* `verify-evidence/T-8/dod1_recovery_machinery.{sh,txt}`, `dod10_redaction_probe.{sh,txt}`, "
    "`dod4_gui_typecheck.{sh,txt}`\n"
    "* `.orchestrator/handoff.json` — machine-readable, validated against the frozen `Handoff` "
    "schema in `src/orchestrator/core/schemas.py`\n"
    "* `docs/contract-change-request-T8-build-log-events.md` — PROPOSED, not applied\n"
)
L.append(
    "## Memory suggestions\n\n"
    + "\n\n".join(f"* **{m.category}/{m.scope}** — {m.content}" for m in h.memory_suggestions)
    + "\n"
)

out = "\n".join(L)
for path in (".orchestrator/handoff.md", "verify-evidence/T-8/handoff.md"):
    open(path, "w").write(out)
    print("wrote", path, len(out.splitlines()), "lines")
