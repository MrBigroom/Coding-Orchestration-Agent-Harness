from __future__ import annotations

from orchestrator.adapters.base import BaseHarnessAdapter


class ZCodeAdapter(BaseHarnessAdapter):
    """ZCode (Z.ai, GLM models). ZCode is primarily a desktop app; reports mention an
    Agent CLI in its source repo, but the binary name and flags were NOT confirmed.

    Experimental / UNVERIFIED: command and args are the product spec's placeholders.
    """

    kind = "zcode"
    experimental = True
    default_command = "zcode-cli"
    default_args = ["run", "--cwd", "{cwd}", "--prompt-file", "{prompt_file}"]
    default_env_vars = ["GLM_API_KEY", "GLM_OPENAI_BASE_URL"]
    description = "Z.ai harness for GLM models; preferred for `glm`-tagged tasks."
    verify_hint = "zcode-cli --help (binary name may differ)"
