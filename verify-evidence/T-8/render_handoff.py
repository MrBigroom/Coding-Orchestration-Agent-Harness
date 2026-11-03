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


def git(*args: str) -> str:
    """Measure divergence at render time so this block cannot go stale between commits."""
    import subprocess

    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout.strip()


# `--left-right --count main...HEAD` prints LEFT=on-main-not-here (behind), RIGHT=here-not-main (ahead).
_behind, _ahead = (git("rev-list", "--left-right", "--count", "main...HEAD").split() + ["?"])[:2]
_head = git("rev-parse", "--short", "HEAD")

L: list[str] = []
L.append("# T-8 handoff — recovery commits after a timeout committed half-finished worker edits\n")
L.append(
    f"**Branch:** `{h.branch}`  \n**Status:** `{h.status}`  \n**Phase:** {h.phase}  \n"
    "**Base:** `main` (merge-base `14a221c`); measured by this script at render time on "
    f"HEAD `{_head}`: `git rev-list --left-right --count main...HEAD` = `{_behind} {_ahead}` "
    f"({_behind} behind / {_ahead} ahead of `main@{git('rev-parse','--short','main')}`). The "
    "evidence-only commit that records this regeneration adds one to the ahead count. "
    "`git merge-tree --write-tree main HEAD` = no conflicts. Not merged, not pushed.\n"
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
    "Round 2 (run 11) wrote this handoff first, then verified every item by reading the shipped "
    "file and line; implemented the only item `ba77a43` did not contain — **DoD 10**, the redaction "
    "anchor (`9001e82`, sharpened to a negative lookbehind in `94db0cc`); and closed three gaps "
    "found while verifying, in separate `feat(T-8)` commits: `232b6d4` (the "
    "two `docs/build-loop.md` sections that were *referenced but never written*, and "
    "`run_completed` missing from the event vocabulary — so DoD 6/7's \"documented\" was not "
    "literally true), `e42304a` (DoD 5's **lint**-command half: run 5's real defect was an F821 "
    "and `ba77a43` proved only the runtime/test path), `05011a4` (DoD 7's GUI-header half: the "
    "assertion was on the ORM row, not on the payload the header renders) plus the committed "
    "evidence directory, and `e7953b5` (CCR-T8-01, PROPOSED). Round 2 was then SIGTERMed at the "
    "2700 s timeout one minute after writing its handoff, and its uncommitted evidence refresh was "
    "swept by the harness into `5a0cd1a` — a second `chore(orchestrator-recovery): uncommitted "
    "worker output from R-11` commit (9 files, +136/-65, all under `verify-evidence/`), also left "
    "exactly as it is.\n\n"
    "**Round 3 was SUBMIT-ONLY** (round 5 is the current round). Round 2's `.orchestrator/handoff.json` was valid at "
    "01:11 (status `READY_FOR_REVIEW`, 416/0/6, 10 DoD items, blockers `[]`) and the run was killed "
    "at 01:12:04; `run_service.execute` returns at l.395 on a non-clean exit, before "
    "`adapter.parse_result` at l.397, so a timed-out run cannot ingest a handoff that already "
    "exists. That ingestion defect is owned by **T-10**, not here — `run_service.py` was not "
    "touched in round 3. Round 3 re-submits round 2's handoff, preserved by the operator at "
    "`~/.local/share/coding-orchestrator/evidence/T-8-round2-discarded-handoff/`, and changes only: "
    "the provenance text; the DoD 10 item wording, which now names the negative lookbehind "
    "`(?<![A-Za-z0-9])` instead of a word boundary (adopting `94db0cc`'s reasoning — `\\bsk-` would "
    "miss `TOKEN_sk-proj-…`, i.e. `\\b` would have weakened redaction while fixing the names); the "
    "one memory suggestion that still carried the stale `\\b` claim; and a re-measurement of "
    "lint/tests/targeted at `5a0cd1a` (`7750c86`). **No production or test code changed in round 3.**\n\n"
    "**Round 5 (this round) applies the operator's rulings on reviews.6** in commit `21f79c4`: "
    "ruling 1 rewrites `project_service.authorized_contract_files` from a literal-mention match "
    "to a GRANT test — a prohibition naming a contract file, or a bare mention of it, counted as "
    "authorization before, including this task's own DoD 8, so the contract-edit control failed "
    "open; ruling 2 documents the `run_completed` / `run_failed` / `run_timeout` emission split "
    "without moving any emitter; ruling 3 deletes the dead `GET /api/tasks/{id}/recovery` route "
    "and corrects `docs/build-loop.md` to describe the `task.recovery_commits` field the GUI "
    "actually reads; ruling 4 files "
    "`docs/contract-change-request-T8-02-recovery-commits-field.md` (PROPOSED) for that one "
    "additive `/api` field; ruling 5 re-measures the targeted counts and corrects the `13 passed` "
    "claim in DoD 5's evidence, which was 14 on disk. `ba77a43` and `5a0cd1a` were still not "
    "amended, rebased or re-split."
)
L.append(
    "## Tests\n\n"
    "```\n$ ./scripts/orch_verify.sh test\n"
    f"{t.passed} passed, {t.failed} failed, {t.skipped} skipped   (exit 0)\n"
    "$ ./scripts/orch_verify.sh lint\nAll checks passed!   (exit 0)\n```\n\n"
    f"Log: `{t.log_path}` (committed). The wrapper prints "
    "`ORCH_VERIFY|mode=test|module_file=…/worktrees/coding-orchestrator/8/src/…` and exits 3 if "
    "the import resolves outside this worktree, so the numbers describe **this branch**, not the "
    "main checkout. Targeted: `verify-evidence/T-8/targeted_tests.txt`, now LABELED per command and "
    "re-runnable via `verify-evidence/T-8/targeted_tests.sh` — ONE run of the tree under review "
    "(HEAD `21f79c4`): 12 `test_recovery_markers.py`, 5 `test_recovery_after_timeout.py`, 17 of the "
    "two together, 14 `test_redaction.py`, 5 `test_git_worktree.py`, 36 all four. The +1 in the "
    "recovery counts is `test_contract_authorization_is_a_grant_not_a_mention`, added by ruling 1. "
    "GUI: `tsc -b --force` over this branch's `web/src` → exit 0 "
    "(`verify-evidence/T-8/dod4_gui_typecheck.txt`, captured at `5a0cd1a`; `web/src` has not changed since). All tests offline: mock harness, mock "
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
    "* `docs/contract-change-request-T8-build-log-events.md` — CCR-T8-01, PROPOSED, not applied\n"
    "* `docs/contract-change-request-T8-02-recovery-commits-field.md` — CCR-T8-02, PROPOSED, "
    "not applied (added in round 5 by ruling 4)\n"
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
