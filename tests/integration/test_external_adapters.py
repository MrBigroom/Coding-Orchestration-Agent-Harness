"""Optional checks against real external CLIs. Skipped unless the binary is installed AND
ORCHESTRATOR_TEST_EXTERNAL=1 (they may need credentials and network)."""

import os
import shutil

import pytest

pytestmark = pytest.mark.external


@pytest.mark.parametrize("binary", ["opencode", "dsh", "zcode", "qwen", "aider", "claude"])
def test_cli_reports_version(binary):
    argv = [binary, "--version"]
    if binary == "zcode" and not shutil.which(binary):
        from orchestrator.adapters.zcode import ZCodeAdapter

        adapter = ZCodeAdapter(ZCodeAdapter.default_spec())
        if adapter.detect():
            argv = [*adapter._prefix(), "--version"]
    if os.environ.get("ORCHESTRATOR_TEST_EXTERNAL") != "1" or not shutil.which(argv[0]):
        pytest.skip(f"{binary} not installed or ORCHESTRATOR_TEST_EXTERNAL!=1")
    import subprocess

    proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0
