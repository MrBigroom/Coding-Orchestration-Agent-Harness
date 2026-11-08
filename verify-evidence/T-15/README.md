# T-15 evidence index

Regenerate this directory with one command, from the worktree at the HEAD you want to certify:

```bash
orchestrator task evidence T-15            # default fast pass: lint + focused tests, ~3 s
orchestrator task evidence T-15 --full     # adds the full suite (~2.5 min); records mode=full
orchestrator task evidence T-15 --check    # read-only: prints the state, exits 1 when stale
```

`capture.sh` is the committed generator. The command refuses a worktree with uncommitted tracked
changes **including an edited or uncommitted `capture.sh`**, canonicalises only the files the pass
rewrote, and writes `manifest.json` with the HEAD, timestamp, command line, mode, tool versions and
produced files. There is no stamp-only mode: a file the generator did not rewrite keeps its old
stamp and is reported stale. The default pass is the cheap refresh; the expensive full-suite capture
is opt-in, and every stamp plus the manifest says which mode produced it. Measured in this bundle (see
`timing.txt` and the command's own printed elapsed time and rewrite count): lint 41 ms plus focused
tests 3.2 s for the fast steps; two full passes took 148.2 s (9 files rewritten) and 143.3 s
(2 files rewritten).

## What each file proves

| file | what it proves |
|---|---|
| `full_suite.txt` | whole suite at the stamped HEAD (written by the `--full` pass only) |
| `lint.txt` | `./scripts/orch_verify.sh lint` at the stamped HEAD |
| `freshness_tests.txt` | the `-v` run naming every T-15 test plus the two DoD-4 memory nodes |
| `assertion_shape.txt` | `scripts/t15_assertion_shape.py`: AST counts, every remaining conjoined assert has a site comment, zero reason-substring assertions |
| `dod_map.txt` | DoD 5(a)-(f) mapped to the node ids whose `PASSED` lines are in `freshness_tests.txt` |
| `source_anchors.txt` | `grep -n` anchors for the check, parser, generator, trailer classifier and wiring |
| `timing.txt` | per-step milliseconds of the full pass (DoD 3 evidence, not a gate) |
| `freshness_selfcheck.txt` | the generator's own worktree-basis check of this directory |
| `freshness_at_tip.txt` | committed-view `--check` at the bundle commit; the commits that add it and the final evidence commit touch only `verify-evidence/`, so the gate reports `pass` at the delivered tip with that explanation |
| `manifest.json` | generated manifest: HEAD, generated_at, mode, command, tool versions, file hashes |

## Freshness at the delivered tip

A committed file cannot contain the SHA of the commit that adds it. `freshness_selfcheck.txt` is
therefore a worktree-basis check of the bundle written by the one pass, and `freshness_at_tip.txt`
is the committed-view `orchestrator task evidence T-15 --check` run at the commit that first
contained the bundle. The commit adding that report is evidence-only (it touches nothing outside
`verify-evidence/` and not `capture.sh`), so the same check at the final tip reports `pass` with the
`evidence-only commit` explanation instead of a bare mismatch. The Supervisor gate itself runs the
row; this bundle is not asking the reviewer to infer it from an old copy.

The live Supervisor process imports the **main** checkout's code; T-15 is not merged there yet, so
the row appears in `freshness_tests.txt` (integration test running branch code), not in this round's
`verification.md`. That is a deployment fact, not a claim that the check is unwired.

## DoD 4 assertion inventory (and why the counts differ from the brief)

Measured at the branch base (`fe7d53e`), `tests/unit/test_memory_scopes.py` had **26** and
`tests/integration/test_memory_scope_boundary.py` **15** conjoined `assert a and b` statements
(AST). The brief's "28 / 16" does not reproduce on any commit in either file's history (the maximum
is 27 / 18); other tasks have touched these files since the brief was measured on 2026-11-04. This
phase split **22 unit + 13 integration** sites that could fail in two unrelated ways. What remains
is **4 + 2**, each one outcome over two fields, with a `# one outcome:` comment at the site:
`not session.dirty and not session.new` (nothing pending), a dry run wrote nothing
(`not repair.removed_files and orphan.exists()`), `gc=False` deleted nothing, and the two ghost
files survive/are both removed. `scripts/t15_assertion_shape.py` is the committed proof; it pins no
counts, so T-17 can touch these files without breaking the freshness suite.

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
`test_fix_scopes_reports_a_canonical_scope_that_is_still_unreachable`).

## HEAD moves for reasons the Worker does not control

An **evidence-only** commit (nothing outside `verify-evidence/`, and not `capture.sh`) cannot
change measured content, so a stamp that differs only by such commits is fresh with that
explanation. A commit carrying an `Orchestrator-Recovery:` or `Operator-Hand-Fix:` **trailer** is
*explained, not certified*: the reason names the commit and the trailer and the bundle still reads
stale, because a Worker can write any trailer into its own code commit. Detection is the git
trailer (`%(trailers)`), never a substring of the body; editing the generator is a code commit.

## Deleted evidence

If a file existed earlier on the branch and is gone at the commit under review, the check reports
`stale` and names the path and the deleting commit. It is never `not_applicable`: deleting evidence
to fit a review budget must not be rewarded with a silent pass. A task that never committed
evidence at all *is* `not_applicable`.

## Honest limitations

The check reports staleness; it does not force a re-run. Staleness is `skipped` with `stale:` in
the row's evidence string rather than a new status value, because the verification status vocabulary
is frozen (`docs/contract-change-request-T15-01-evidence-freshness-status.md`); a first-class
`stale` is proposed, not taken. The default fast pass deliberately leaves the expensive full-suite
file at its older stamp, so after a code change run `--full` before handing off; a reader can see
the mode in the stamp. A bundle with no committed `capture.sh` cannot be refreshed by the generic
command.