"""Optional checks against real external CLIs. Skipped unless the binary is installed AND
ORCHESTRATOR_TEST_EXTERNAL=1 (they may need credentials and network)."""

import os
import shutil

import pytest

pytestmark = pytest.mark.external


@pytest.mark.parametrize("binary", ["opencode", "dsh", "zcode-cli", "qwen", "aider", "claude"])
def test_cli_reports_version(binary):
    if os.environ.get("ORCHESTRATOR_TEST_EXTERNAL") != "1" or not shutil.which(binary):
        pytest.skip(f"{binary} not installed or ORCHESTRATOR_TEST_EXTERNAL!=1")
    import subprocess

    proc = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0
