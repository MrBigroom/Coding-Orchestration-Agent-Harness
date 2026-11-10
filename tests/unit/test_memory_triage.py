"""Memory budget enforcement + rot/staleness triage. All checks are deterministic."""

import os

import pytest

from orchestrator.core import config as config_mod
from orchestrator.core.models import Project
from orchestrator.core.schemas import MemoryCreate, MemorySuggestion
from orchestrator.services import memory_service
from orchestrator.services import memory_triage as mt
from orchestrator.utils.memory_text import is_rot_prone, referenced_paths, rot_flags
from orchestrator.utils.truncate import estimate_tokens
from tests.conftest import make_repo, requires_git

DURABLE = "Never key a merge decision on the inclusive dirty predicate: untracked output is dirt."
TASK_SHAPED = (
    "T-8 decisions: (1) recovery commits use a git trailer, (2) run_completed is terminal."
)


@pytest.fixture
def budgets(monkeypatch):
    """Set per-section memory caps and restore the settings cache afterwards."""

    def _set(**values: int | bool):
        for key, val in values.items():
            value = str(val).lower() if isinstance(val, bool) else str(val)
            monkeypatch.setenv(f"ORCHESTRATOR_{key.upper()}", value)
        config_mod.reset_settings_cache()

    _set()
    yield _set
    config_mod.reset_settings_cache()


@pytest.fixture
def repo_project(session, tmp_path):
    """A registered project backed by a real git checkout with a few files."""
    root = make_repo(
        tmp_path / "repo",
        {
            "scripts/orch_verify.sh": "#!/bin/sh\necho ok\n",
            "src/orchestrator/cli.py": "x = 1\n",
            "src/orchestrator/services/memory_triage.py": "y = 2\n",
            "docs/build-loop.md": "# loop\n",
        },
    )
    p = Project(
        name="Demo Repo",
        slug="demo",
        path=str(root),
        base_branch="main",
        test_command="./scripts/orch_verify.sh test",
    )
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def add(session, content, *, category="project", scope="gotchas", slug="demo", approved=True):
    # A fixture that deliberately builds an OVER-CAP approved set (the budget tests) is a
    # scenario, not an approval decision: `override=True` keeps the pre-T-17 fixture shape
    # after T-17 made every approval-shaped write refuse an eviction by default.
    return memory_service.create(
        session,
        MemoryCreate(
            category=category, scope=scope, content=content, project_slug=slug, approved=approved
        ),
        override=approved,
    )


# ---------------------------------------------------------------------- rot flags


def test_rot_flags_are_content_derived_and_stable():
    assert "task_shape" in rot_flags(TASK_SHAPED)
    assert "forward_looking" in rot_flags("routes through the wrapper (task-6, pending merge)")
    assert "absolute_date" in rot_flags("Measured 2026-11-03: the API listens on 127.0.0.1:8321.")
    assert "bare_sha" in rot_flags("measured at ba77a431c, 16 files changed")
    assert rot_flags(DURABLE) == []
    # Same text, same answer: this decides what is injected, so no clock and no I/O.
    assert rot_flags(TASK_SHAPED) == rot_flags(TASK_SHAPED)
    assert is_rot_prone(TASK_SHAPED) and not is_rot_prone(DURABLE)


def test_referenced_paths_ignores_placeholders_and_globs():
    paths = referenced_paths("Read `src/orchestrator/cli.py` and runs/<run_id>/handoff.json.")
    assert "src/orchestrator/cli.py" in paths
    assert not any("*" in p or "<" in p for p in paths)


# --------------------------------------------------------------------------- budget


def test_budget_demotes_rot_prone_then_oldest_and_reports_it(session, budgets):
    budgets(max_memory_tokens_project=70)
    long_durable = add(session, " ".join(["alpha"] * 60) + " " + DURABLE)
    add(session, "Zeta rule: prefer argv lists over shell strings everywhere.")
    stale = add(session, TASK_SHAPED)

    sel = memory_service.select_for_prompt(session, "worker", "demo")
    assert sel.budgets["project"] == 70
    assert "Zeta rule" in sel.sections["project"]  # durable and newest survives
    excluded = {x["id"]: x for x in sel.excluded}
    assert stale.id in excluded, "task-shaped entry must be demoted first"
    assert long_durable.id in excluded
    assert all(x["reason"] for x in sel.excluded)  # never silent
    assert "budget" in excluded[stale.id]["reason"] and "rot-prone" in excluded[stale.id]["reason"]


def test_budget_selection_is_byte_identical_across_calls(session, budgets):
    budgets(max_memory_tokens_project=80)
    for i in range(6):
        add(session, f"Rule {i}: stamp the tested HEAD into each evidence file of kind {i}.")
    sel = memory_service.select_for_prompt(session, "worker", "demo")
    again = memory_service.select_for_prompt(session, "worker", "demo")
    assert sel.sections["project"] == again.sections["project"]
    assert [x["id"] for x in sel.excluded] == [x["id"] for x in again.excluded]


def test_for_prompt_uses_the_budget_and_keeps_its_shape(session, budgets):
    budgets(max_memory_tokens_project=40)
    add(session, TASK_SHAPED)
    kept = add(session, "Always resolve the interpreter inside the worktree, never the caller's.")
    text = memory_service.for_prompt(session, "worker", "demo")["project"]
    assert kept.content[:20] in text
    assert TASK_SHAPED[:20] not in text


def test_budget_can_be_disabled(session, budgets):
    budgets(memory_budget_enabled=False, max_memory_tokens_project=10)
    e = add(session, " ".join(["beta"] * 80) + " durable tail.")
    sel = memory_service.select_for_prompt(session, "worker", "demo")
    assert sel.excluded == []
    assert estimate_tokens(sel.sections["project"]) > 10
    assert e.content[:20] in sel.sections["project"]


def test_exclusions_appear_as_prompt_preview_warnings(session, budgets, repo_project):
    from orchestrator.services import prompt_builder

    budgets(max_memory_tokens_project=60)
    add(session, TASK_SHAPED)
    long_one = add(session, " ".join(["gamma"] * 70) + " durable tail.")
    built = prompt_builder.build_prompt(session, "worker", None, repo_project)
    assert built.memory_exclusions
    report = prompt_builder.preview(built.sections, built)
    assert any(f"memory #{long_one.id}" in w for w in report["warnings"])
    # Nothing is silently missing: the demoted entry stays approved in the index.
    assert long_one.approved and not long_one.rejected


# -------------------------------------------------------------------------- triage


def test_triage_groups_the_same_fact_reworded(session, repo_project):
    a = add(
        session,
        "Redaction must use a negative lookbehind anchor: an unanchored sk- pattern matches "
        "inside words and corrupts branch names in operator-facing text.",
        approved=False,
    )
    b = add(
        session,
        "Redaction has to use a negative lookbehind anchor, because an unanchored sk- pattern "
        "matches inside words and corrupts branch names in operator-facing text.",
        approved=False,
    )
    assert mt.duplicate_groups([a, b]) == [[a.id, b.id]]
    report = mt.triage(session, status="pending", project_slug="demo")
    row = next(r for r in report["rows"] if r["id"] == b.id)
    assert row["suggested_action"] == "merge"
    assert a.id in row["duplicates_of"]


def test_triage_does_not_merge_distinct_facts(session, repo_project):
    x = add(
        session,
        "Two dirty predicates exist deliberately: tracked_dirty gates merges, is_dirty guards "
        "worktree removal, because untracked output must never be discarded.",
        approved=False,
    )
    y = add(
        session,
        "The reviewer prompt and payload are archived to review_payload.md so findings can be "
        "audited after the round and truncated diffs are visible.",
        approved=False,
    )
    assert mt.duplicate_groups([x, y]) == []
    report = mt.triage(session, status="pending", project_slug="demo")
    actions = {r["id"]: r["suggested_action"] for r in report["rows"]}
    assert actions[x.id] != "merge" and actions[y.id] != "merge"


def test_triage_prefers_amending_an_approved_row(session, repo_project):
    approved = add(
        session,
        "Evidence lives under verify-evidence/<task>/ with scripts/capture.sh writing each "
        "output, so a reviewer can reach it from src/orchestrator/cli.py.",
    )
    inc = add(
        session,
        "scripts/capture.sh must stamp the tested tree before writing verify-evidence output, "
        "reading src/orchestrator/cli.py only for the path constants.",
        approved=False,
    )
    assert approved.id in [a["id"] for a in mt.related_approved(session, inc)]
    row = next(r for r in mt.triage(session, status="pending")["rows"] if r["id"] == inc.id)
    assert row["suggested_action"] == "amend"


def test_triage_flags_task_shape_and_live_staleness(session, repo_project):
    stale = add(
        session,
        "branch task-6, pending merge: scripts/orch_verify.sh wraps make test and "
        "src/orchestrator/deleted_module.py guards the wrapper.",
        approved=False,
    )
    healthy = add(
        session,
        "make test routes through scripts/orch_verify.sh, which resolves "
        "src/orchestrator/cli.py inside the worktree and refuses an inherited virtualenv.",
        approved=False,
    )
    codes = {f["code"] for f in mt.audit_entry(session, stale)}
    assert {"forward_looking", "stale-branch-claim", "stale-merge-claim", "missing-path"} <= codes
    ok_codes = {f["code"] for f in mt.audit_entry(session, healthy)}
    assert "missing-path" not in ok_codes and "stale-branch-claim" not in ok_codes
    row = next(r for r in mt.triage(session, status="pending")["rows"] if r["id"] == stale.id)
    assert row["suggested_action"] == "fix-stale"
    assert (
        next(r for r in mt.triage(session, status="pending")["rows"] if r["id"] == healthy.id)[
            "suggested_action"
        ]
        != "fix-stale"
    )


def test_missing_path_ignores_pruned_and_runtime_paths(session, repo_project):
    e = add(
        session,
        "Type-check needs web/node_modules/typescript linked in, and the renderer writes "
        "roles/worker.md from the memory tree before the next run.",
        approved=False,
    )
    codes = [f["code"] for f in mt.audit_entry(session, e)]
    assert "missing-path" not in codes


@requires_git
def test_missing_path_accepts_directory_claims(session, tmp_path):
    from tests.conftest import make_repo

    root = make_repo(
        tmp_path / "unity",
        {
            "Assets/Plugins/uLinkCompat/NetSync.cs": "class NetSync {}\n",
            "Builds/Linux/DeadZone.sh": "#!/bin/sh\n",
        },
    )
    p = Project(name="Unity Repo", slug="unity", path=str(root))
    session.add(p)
    session.commit()
    e = add(
        session,
        "The port replaces the cloud API in Assets/Plugins/uLinkCompat and runs through "
        "Builds/Linux/DeadZone.sh, which forces XWayland.",
        slug="unity",
        approved=False,
    )
    assert "missing-path" not in [f["code"] for f in mt.audit_entry(session, e)]


@requires_git
def test_missing_path_accepts_files_committed_on_a_branch_only(session, tmp_path):
    """A gated run commits its evidence on the phase branch; that is not a false claim."""
    import subprocess

    from tests.conftest import make_repo

    root = make_repo(tmp_path / "mockrepo", {"README.md": "# x\n"})
    subprocess.run(
        ["git", "checkout", "-q", "-b", "phase-1-demo"], cwd=root, check=True, env=os.environ
    )
    (root / "MOCK_CHANGE.md").write_text("run 1\n")
    subprocess.run(["git", "add", "MOCK_CHANGE.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "mock change"], cwd=root, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=root, check=True)
    p = Project(name="Mock Repo", slug="mockrepo", path=str(root))
    session.add(p)
    session.commit()
    e = add(
        session,
        "Mock changes are appended to MOCK_CHANGE.md, one line per run.",
        slug="mockrepo",
        approved=False,
    )
    assert "missing-path" not in [f["code"] for f in mt.audit_entry(session, e)]


def test_missing_path_still_flags_a_path_with_no_trace(session, repo_project):
    e = add(
        session,
        "The wrapper reads src/orchestrator/deleted_module.py before anything else.",
        approved=False,
    )
    flags = [f for f in mt.audit_entry(session, e) if f["code"] == "missing-path"]
    assert flags and "neither" in flags[0]["detail"]


def test_brace_expansion_is_not_a_path_claim(session, repo_project):
    e = add(
        session,
        "Symlink the worktree's web/{src,index.html,package.json,tsconfig.json,"
        "vite.config.ts,tailwind.config.js,postcss.config.js} into the scratch dir.",
        approved=False,
    )
    assert "missing-path" not in [f["code"] for f in mt.audit_entry(session, e)]


def test_triage_flags_an_unregistered_project(session):
    e = add(session, DURABLE, slug="ghost", approved=False)
    codes = {f["code"] for f in mt.audit_entry(session, e)}
    assert "unknown-project" in codes


def test_suggestions_are_reported_not_applied(session, repo_project, budgets):
    budgets(max_memory_tokens_project=200)
    injected = add(session, "Old durable rule that will be pushed out of the section.")
    late = add(session, DURABLE, approved=False)
    report = mt.triage(session, status="pending")
    row = next(r for r in report["rows"] if r["id"] == late.id)
    assert row["suggested_action"] in ("merge", "approve")
    # Reporting never changes approval state, and the cap only demotes from the render.
    assert late.approved is False and injected.approved is True


def test_triage_flags_a_claim_of_no_test_command(session, repo_project):
    e = add(
        session,
        "The test command is not configured for this project, so a green claim rests on the "
        "worker's word alone.",
        approved=False,
    )
    codes = {f["code"] for f in mt.audit_entry(session, e)}
    assert "contradicts-live-config" in codes


def test_budget_projection_prices_the_whole_queue(session, repo_project, budgets):
    budgets(max_memory_tokens_project=200)
    suggested = memory_service.suggest_from_handoff(
        session,
        [MemorySuggestion(category="project", scope="gotchas", content=DURABLE)],
        source="handoff:R-1",
        project_slug="demo",
    )
    add(session, TASK_SHAPED, approved=False)
    proj = mt.budget_projection(session, "demo")
    assert set(proj) == {"architect", "worker", "supervisor"}
    assert proj["worker"]["total_after"] >= proj["worker"]["total_now"]
    report = mt.triage(session, status="pending")
    assert suggested[0].id in [r["id"] for r in report["rows"]]
    assert report["projection"]["worker"]["total_after"] >= report["current_usage"]["project"]


def test_budget_report_shape(session, repo_project):
    rep = mt.budget_report(session, "demo")
    assert set(rep) == {
        "budgets",
        "budget_enabled",
        "project_slug",
        "roles",
        "pending_count",
        "pending_tokens",
    }
    assert rep["pending_count"] == 0


def test_triage_never_writes(session, repo_project):
    e = add(session, TASK_SHAPED, approved=False)
    before = (e.approved, e.rejected, e.content, e.scope)
    mt.triage(session, status="pending")
    mt.budget_report(session, "demo")
    session.expire_all()
    fresh = memory_service.list_entries(session, status="pending")
    assert [(x.approved, x.rejected, x.content, x.scope) for x in fresh if x.id == e.id] == [before]


def test_cap_pressure_is_not_reported_as_a_merge_suggestion(session, repo_project, budgets):
    """Approved rows are already injected or already demoted: that is a warning about the
    section, not a per-row merge suggestion. Pending rows still get priced against it."""
    budgets(max_memory_tokens_project=320)
    for i in range(4):
        add(
            session,
            f"Durable rule {i}: " + " ".join(["gamma"] * 60) + " always resolve the "
            f"interpreter inside the worktree, variant {i}.",
        )
    big = add(session, " ".join(["delta"] * 90) + " tail of a long durable rule.", approved=False)

    approved = mt.triage(session, status="approved", project_slug="demo")
    assert all(r["suggested_action"] != "merge" for r in approved["rows"]), [
        (r["id"], r["reason"]) for r in approved["rows"] if r["suggested_action"] == "merge"
    ]
    # The pressure is still reported, as section state rather than row advice.
    assert approved["excluded_now"] or approved["warnings"]

    pend = mt.triage(session, status="pending", project_slug="demo")
    row = next(r for r in pend["rows"] if r["id"] == big.id)
    assert row["suggested_action"] == "merge" and "budget" in row["reason"]


def test_runtime_setting_override_actually_binds_the_cap(session, repo_project, budgets):
    """The caps are advertised as runtime-editable, so a settings-table row must change
    injection — not just be accepted by PATCH /api/settings and then ignored."""
    from orchestrator.services import settings_service

    budgets(max_memory_tokens_project=5000)  # env default: everything fits
    for i in range(3):
        add(
            session,
            f"Rule {i}: "
            + " ".join(["epsilon"] * 50)
            + f" resolve the interpreter in the worktree {i}.",
        )
    wide = memory_service.select_for_prompt(session, "worker", "demo")
    assert wide.excluded == [], "env cap of 5000 should admit everything here"

    settings_service.update(session, {"max_memory_tokens_project": 200})
    tight = memory_service.select_for_prompt(session, "worker", "demo")
    assert tight.budgets["project"] == 200, "the settings row must win over env"
    assert tight.excluded, "a runtime override that cannot demote is not a cap"
    assert wide.used["project"] > tight.used["project"]

    settings_service.update(session, {"max_memory_tokens_project": 0})
    assert memory_service.select_for_prompt(session, "worker", "demo").excluded == []


def test_budget_enabled_and_warn_fraction_are_runtime_editable(session, repo_project, budgets):
    from orchestrator.services import settings_service

    budgets(max_memory_tokens_project=120)
    for i in range(3):
        add(session, f"Rule {i}: " + " ".join(["zeta"] * 40) + f" durable tail {i}.")
    assert memory_service.select_for_prompt(session, "worker", "demo").excluded

    settings_service.update(session, {"memory_budget_enabled": False})
    assert memory_service.memory_budgets_enabled(session) is False
    assert memory_service.select_for_prompt(session, "worker", "demo").excluded == []

    settings_service.update(
        session, {"memory_budget_enabled": True, "memory_budget_warn_fraction": 0.5}
    )
    assert memory_service.memory_warn_fraction(session) == 0.5
    sel = memory_service.select_for_prompt(session, "worker", "demo")
    assert sel.warnings, "a lower warn fraction must fire earlier"
    with pytest.raises(ValueError):
        settings_service.update(session, {"memory_budget_warn_fraction": 1.5})
