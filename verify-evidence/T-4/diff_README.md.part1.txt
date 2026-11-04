# HEAD=d5aa57b57878ce3d954fc3feb29b7b9a43a7689b (d5aa57b) branch=task-4-memory-suggestions-accept-unvalidated-scopes-cre captured=2026-11-04T13:56:33+08:00 tree=0 dirty-entries (excluding verify-evidence/T-4)
# git diff db0011212a775e0ee766ebbee40a4f1cdf26a6e7 HEAD -- README.md   PART 1/1
# The COMPLETE per-file diff, cut at 160-line boundaries so that a reader with a
# 2000-line/50KB view limit never sees a function chopped off mid-hunk: every diff
# line appears in exactly one part, in order. Raw diff = 22 lines.
diff --git a/README.md b/README.md
index 145dcbb..c6ded48 100644
--- a/README.md
+++ b/README.md
@@ -178,7 +178,16 @@ yourself, run `scripts/harness-bench/bench.sh`.
 
 Memory lives in SQLite (`memory_entries`) and is rendered to markdown under
 `$ORCHESTRATOR_HOME/memory/{global,projects/<slug>,roles}/`. Workers and Architects
-*suggest* entries via `memory_suggestions` in their JSON. Suggestions start **pending**,
+*suggest* entries via `memory_suggestions` in their JSON. A suggestion's scope is checked
+against the closed vocabulary (`environment|preferences|patterns`,
+`conventions|decisions|gotchas`, `architect|worker|supervisor`): a project/global scope
+outside it is coerced to the deterministic fallback (`gotchas`/`environment`, never
+inferred from the words) and the substitution is written to the build log, a role
+suggestion that resolves to no role file is dropped with a warning, and
+`orchestrator memory list` / `memory fix-scopes` mark and repair rows stored before that
+check existed. The create and edit boundaries (POST/PATCH, `memory add`) *reject* an
+out-of-vocabulary value for every category, and for `category=role` they check the `role`
+column too, because that is the column injection reads. Suggestions start **pending**,
 and only **approved** entries are injected into prompts. Each role gets its own slice:
 the Architect gets decisions + conventions, the Worker conventions + gotchas, the
 Supervisor decisions + gotchas, and every role gets global memory. See
DIFF_PART_COMPLETE=README.md part 1/1
