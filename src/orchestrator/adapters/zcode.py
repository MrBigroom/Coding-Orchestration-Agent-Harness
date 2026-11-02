from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from orchestrator.adapters.base import BaseHarnessAdapter, RunContext, render_args
from orchestrator.adapters.common import JsonOutputMixin, ManagedConfigMixin, provider_settings


class ZCodeAdapter(JsonOutputMixin, ManagedConfigMixin, BaseHarnessAdapter):
    """ZCode CLI, checked against the CLI bundled with ZCode 3.10.2 (CLI 0.16.5).

    The CLI help advertises options its parser does not accept, including --settings and
    --max-turns. Managed runs therefore use an isolated temporary HOME containing
    .zcode/cli/config.json. The provider id is derived from the API key env-var name so
    ZCode resolves the key itself; the secret is never written to the config.
    """

    kind = "zcode"
    experimental = True
    default_command = "zcode"
    default_args = ["--cwd", "{cwd}", "--mode", "yolo", "--json", "--prompt", "{prompt}"]
    default_env_vars = [
        "QWEN_API_KEY",
        "QWEN_OPENAI_BASE_URL",
        "GLM_API_KEY",
        "GLM_OPENAI_BASE_URL",
    ]
    description = "Z.ai harness for GLM models; second choice after Pi."
    verify_hint = "zcode --version && zcode --help"
    bundled_cli = Path("/opt/ZCode/resources/glm/zcode.cjs")

    def _node(self) -> str | None:
        # Prefer the system binary: a PATH `node` may be a shell wrapper that needs
        # environment variables deliberately excluded from the scrubbed child env.
        system_node = Path("/usr/bin/node")
        return str(system_node) if system_node.is_file() else shutil.which("node")

    def _uses_bundled_cli(self) -> bool:
        return (
            self.spec.command == self.default_command
            and shutil.which(self.spec.command) is None
            and self.bundled_cli.is_file()
            and self._node() is not None
        )

    def _prefix(self) -> list[str]:
        if self._uses_bundled_cli():
            return [self._node() or "node", str(self.bundled_cli)]
        return [self.spec.command]

    def resolved_binary(self) -> str | None:
        if self._uses_bundled_cli():
            return str(self.bundled_cli)
        return super().resolved_binary()

    def build_command(self, ctx: RunContext) -> list[str]:
        return [*self._prefix(), *render_args(self.spec.args_template, self.template_values(ctx))]

    def command_preview(self, ctx: RunContext) -> list[str]:
        values = self.template_values(ctx, include_prompt=False)
        values["prompt"] = f"<contents of {ctx.prompt_file.name}>"
        return [*self._prefix(), *render_args(self.spec.args_template, values)]

    def prepare(self, ctx: RunContext, tmp: Path) -> dict[str, str]:
        prov = provider_settings(self.capabilities)
        if prov is None:
            return {}
        key_env = prov["api_key_env"]
        if not key_env.endswith("_API_KEY"):
            raise ValueError("ZCode provider api_key_env must end in _API_KEY")
        provider_id = re.sub(r"[^a-z0-9]+", "_", key_env[: -len("_API_KEY")].lower())
        model = ctx.model or "glm-5.3"
        config = {
            "provider": {
                provider_id: {
                    "kind": "openai-compatible",
                    "options": {"baseURL": prov["base_url"]},
                    "models": {model: {"id": model}},
                }
            },
            "model": f"{provider_id}/{model}",
        }
        config_path = tmp / ".zcode" / "cli" / "config.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps(config, indent=2))
        return {"HOME": str(tmp), "ZCODE_STORAGE_DIR": str(tmp / ".zcode")}
