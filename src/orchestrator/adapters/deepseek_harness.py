from __future__ import annotations

from orchestrator.adapters.base import BaseHarnessAdapter


class DeepSeekHarnessAdapter(BaseHarnessAdapter):
    """DeepSeek Harness (`dsh`, npm `@deepseek-ai/dsh`, developer preview).

    Experimental / UNVERIFIED flags: DeepSeek documents a headless mode but the exact
    flags were not confirmed. The default template is the product spec's example and
    will likely need editing. Beware: an unrelated community Python package also installs
    a `dsh` binary — check `dsh --version` resolves to the official one.
    """

    kind = "deepseek_harness"
    experimental = True
    default_command = "dsh"
    default_args = ["run", "--cwd", "{cwd}", "--prompt-file", "{prompt_file}"]
    default_env_vars = ["DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"]
    description = "Official DeepSeek agent harness; preferred for `deepseek`-tagged tasks."
    verify_hint = "dsh --version && dsh --help"
