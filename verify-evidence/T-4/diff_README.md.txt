# HEAD=5c678f942c9a3ff009ad1bff301660d7ab833cbd (5c678f9) branch=task-4-memory-suggestions-accept-unvalidated-scopes-cre captured=2026-11-04T13:22:57+08:00 tree=0 dirty-entries (excluding verify-evidence/T-4)
# git diff db0011212a775e0ee766ebbee40a4f1cdf26a6e7 HEAD -- README.md  (COMPLETE: 20 diff lines)
diff --git a/README.md b/README.md
index 145dcbb..9c022ac 100644
--- a/README.md
+++ b/README.md
@@ -178,7 +178,14 @@ yourself, run `scripts/harness-bench/bench.sh`.
 
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
+check existed. Suggestions start **pending**,
 and only **approved** entries are injected into prompts. Each role gets its own slice:
 the Architect gets decisions + conventions, the Worker conventions + gotchas, the
 Supervisor decisions + gotchas, and every role gets global memory. See
DIFF_COMPLETE=README.md
