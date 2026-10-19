"""Headers for memory sections in the cached prompt prefix."""

GLOBAL_HEADER = "Shared memory — global (environment, preferences, patterns). Approved entries only."
ROLE_HEADER = "Shared memory — role-specific. Approved entries only."
PROJECT_HEADER = "Shared memory — project (conventions/decisions/gotchas). Approved entries only."

SUGGESTION_GUIDANCE = """\
If you learn something durable (an environment fact, a convention, a gotcha), add it to
`memory_suggestions` in your handoff/review JSON. Suggestions are reviewed by the
Supervisor before they are stored; never put secrets in memory.
"""
