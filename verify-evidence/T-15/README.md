# T-15 evidence index

Regenerate this directory with one command, from the worktree at the HEAD you want to certify:

```bash
orchestrator task evidence T-15            # default fast pass: lint + focused tests, seconds
orchestrator task evidence T-15 --full     # adds the full suite (~2.5 min); records mode=full
orchestrator task evidence T-15 --only lint.txt   # re-run ONE step the manifest declares
orchestrator task evidence T-15 --check    # read-only: prints the state, exits 1 when stale
orchestrator task evidence T-15 --at-tip   # writes freshness_at_tip.txt; refuses unless fresh
```

`capture.sh` is the committed generator and it is the **only** thing the command runs. There is no
arbitrary-command mode (a command the branch does not contain cannot be reproduced from it and can
re-stamp content it never measured) and no stamp-only mode (a stamp is a claim that the content was
measured at that commit). It refuses a worktree with **any** uncommitted input — tracked changes or
untracked source/test files, including an edited or uncommitted `capture.sh` — because
`capture.sh` measures the tree on disk while the stamp names a commit. It canonicalises only the
files the pass rewrote, and writes `manifest.json` (HEAD, timestamp, command line, mode, `--only`
selection, tool versions, produced files).

Measured timings, from `timing.txt` (the only place a number may be quoted from):

| pass | wall | files the generator saw | files it rewrote |
|---|---|---|---|
| `fast` (default) | 4.25 s | 11 | 5 |
| `full` (`--full`) | 145.03 s | 11 | 6 |

`timing.txt` is written only by a `--full` pass, so the default pass stays byte-identical on a
re-run (DoD 5(d)); the next `--full` pass refreshes both rows. A mode switch rewrites the stamp line
of the files that pass produced (one line per file, which is why the stamp carries the mode at all).
`manifest.json`, the self-check and this hand-maintained README are written outside the capture and
are not counted in those two columns; the README is also not hashed into the manifest, so editing
this prose never puts a false sha256 into the bundle it describes.

## What each file proves

| file | what it proves |
|---|---|
| `full_suite.txt` | whole suite at the stamped HEAD (written by the `--full` pass only) |
| `lint.txt` | `./scripts/orch_verify.sh lint` at the stamped HEAD |
| `freshness_tests.txt` | the `-v` run naming every T-15 test plus the two DoD-4 memory nodes |
| `assertion_shape.txt` | `scripts/t15_assertion_shape.py`: every remaining conjoined assert with its line and one-line justification, and zero reason-substring assertions |
| `dod_map.txt` | DoD 5(a)-(f) and the extra guards mapped to node ids whose `PASSED` lines are in `freshness_tests.txt` |
| `source_anchors.txt` | `grep -nF` anchors for the check, parser, walk-back target, generator/input classifier, generator, trailer classifier, scope predicate and both call sites |
| `status_consumers.txt` | raw `grep -rn` for the four frozen check values plus `stale` and `verification_status` over the Literal, the Supervisor row, the renderer and the GUI tone map (CCR-T15-01 consumer list) |
| `timing.txt` | both pass modes, each row carrying the HEAD its own pass measured: wall time, file count, rewrite count, per-step ms (DoD 3 evidence) |
| `freshness_selfcheck.txt` | the worktree-basis check written LAST by the pass that produced this bundle; it names the commit under verification and the comparison target it compared |
| `freshness_at_tip.txt` | the committed-view `--at-tip` check, written LAST after the commit that delivered this bundle; it prints the delivered tip and the comparison target, and is not a stamp |
| `manifest.json` | generated manifest: HEAD, generated_at, mode, `--only`, command, generator/inputs, tool versions, produced files (outputs only) |

## Freshness at the delivered tip

The comparison target is the nearest ancestor of the commit under verification that is **not
evidence-only and not empty** (`docs/build-loop.md`, "Evidence freshness"). An evidence-only commit
changes no code, so a bundle it delivers is honestly stamped at the code HEAD that was measured.

A committed file cannot contain the SHA of the commit that adds it, so the proof has two parts, in
this order:

1. the `--full` pass stamps every file it rewrote at the code HEAD it certifies — the comparison
   target of the worktree HEAD, which is why a refresh on top of a bundle commit still names the
   code HEAD — and writes `freshness_selfcheck.txt` **last** (`basis=worktree`,
   `commit_under_verification=<worktree HEAD>`, `compared_commit=<code HEAD>`);
2. `orchestrator task evidence T-15 --at-tip`, run **LAST** after the commit that delivers this
   bundle, writes `freshness_at_tip.txt` with `basis=committed`, `delivered_tip=<the tip it ran
   at>`, `compared_commit=<the code HEAD it resolved>` and `status=pass`. The commit that adds the
   report is also evidence-only, so a read-only `--check` at the final tip reports `pass` naming
   that code HEAD.

The Supervisor gate runs the row itself. The live Supervisor process imports the **main** checkout's
code, so the `stale` row first appears in `verification.md` after this branch merges and the server
restarts; that is a deployment fact, not a claim that the check is unwired. `freshness_tests.txt`
contains the in-process integration test that renders the row.

## DoD 4 assertion inventory (and why the counts differ from the brief)

The brief was measured 2026-11-04 at a different tip. Measured at this bundle's HEAD with
`python3 scripts/t15_assertion_shape.py` (output in `assertion_shape.txt`, same file the `-v` run
cites): `tests/unit/test_memory_scopes.py` has **4** conjoined `assert a and b` statements and
`tests/integration/test_memory_scope_boundary.py` has **2**. The brief's "28 / 16" does not
reproduce on any commit in either file's history (the maximum is 27 / 18); other tasks touched both
files after 2026-11-04 and this phase split 22 unit + 13 boundary sites that could fail in two
unrelated ways. Each of the six that remain is one outcome over two fields, with a
`# one outcome: ...` comment at the site, listed by line in `assertion_shape.txt`.

The forbidden reason-substring shape from the brief (then `test_memory_scopes.py:612`) was:

```python
assert "rejected" in repair.skipped[0]["why"]
```

and is now an exact value, with a mutation test that changes only the reason text and proves the
assertion goes red:

```python
assert repair.skipped[0]["why"] == "rejected rows are left alone"
```

Two further `fix_scopes` reasons moved from substring to exact values in the same pass
(`test_fix_scopes_refuses_to_guess_a_role_and_to_delete_a_row`,
`test_fix_scopes_reports_a_canonical_scope_that_is_still_unreachable`). The committed check is a
**shape** rule, not a count, so T-17 can touch these files without breaking it; the counts live in
`assertion_shape.txt` instead of in an assertion.

## HEAD moves for reasons the Worker does not control

An **evidence-only** commit (nothing outside `verify-evidence/`, and no path `manifest.json`
declares as `generator`/`inputs`) changes no code, so it is walked past: a stamp naming its parent
code HEAD is truthful, and that is why this bundle can pass on the commit that ships it. Every
commit that changes code after the bundle is the comparison target and makes it `stale` —
including a harness recovery commit that swept uncommitted source and an operator hand-fix. A
commit carrying an `Orchestrator-Recovery:` or `Operator-Hand-Fix:` **trailer** is *explained,
never certified*: git's `%(trailers)` parser identifies it (never a substring search of the body),
the reason names the commit, its trailer and its author, and the bundle still reads `stale`,
because a Worker can write any trailer into its own code commit. Forging a trailer changes the
prose of the reason and nothing else. An empty commit is walked past like an evidence-only one.

## Deleted evidence

If a file existed earlier on the branch and is gone at the commit under review, the check reports
`stale` and names the path and the deleting commit. It is never `not_applicable`: deleting evidence
to fit a review budget must not be rewarded with a silent pass. A task that never committed evidence
at all *is* `not_applicable`.

## Honest limitations

The check reports staleness; it does not force a re-run. `stale` is a first-class check status
(approved as CCR-T15-01, `docs/contract-change-request-T15-01-evidence-freshness-status.md`), and it
still never sets `contradicts_worker` and never fails the gate unless the operator sets
`ORCHESTRATOR_EVIDENCE_FRESHNESS_STRICT=true`. The default fast pass deliberately leaves the
expensive full-suite file at its older stamp, so after a code change run `--full` before handing
off; a reader can see the mode in the stamp and in `timing.txt`. A bundle with no committed
`capture.sh` cannot be refreshed by the command, and `--only` can only select outputs the committed
manifest declares. The scope is code-writing task kinds (`coding`, `maintenance`); an `agent` or
`review` task gets neither the verification row nor the reviewer finding.