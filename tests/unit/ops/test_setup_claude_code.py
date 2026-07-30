"""Tests for the FreeInference Claude Code setup script."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SETUP_SCRIPT = REPO_ROOT / "ops/setup/setup_claude_code.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _run_setup(
    home: Path, monkeypatch_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    fake_bin = home / "bin"
    fake_bin.mkdir(exist_ok=True)
    _write_executable(fake_bin / "claude", "#!/bin/sh\necho '2.1.200'\n")
    _write_executable(fake_bin / "curl", "#!/bin/sh\necho '200'\n")

    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FREEINFERENCE_API_KEY": "hyi-test-key",
        **(monkeypatch_env or {}),
    }
    return subprocess.run(
        ["bash", str(SETUP_SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_merges_current_settings_and_removes_only_legacy_shell_block(tmp_path: Path) -> None:
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    settings_path = settings_dir / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "permissions": {"allow": ["Read"]},
                "env": {
                    "KEEP_ME": "yes",
                    "ANTHROPIC_MODEL": "old-main",
                    "ANTHROPIC_SMALL_FAST_MODEL": "old-small",
                    "API_TIMEOUT_MS": "600000",
                },
            }
        )
    )
    shell_profile = tmp_path / ".zshrc"
    shell_profile.write_text(
        "# keep before\n"
        "# >>> freeinference claude-code >>>\n"
        'export ANTHROPIC_BASE_URL="https://freeinference.org/anthropic"\n'
        "# <<< freeinference claude-code <<<\n"
        "# keep after\n"
    )

    result = _run_setup(
        tmp_path,
        {
            "FREEINFERENCE_MODEL": "deepseek-v4-flash",
            "FREEINFERENCE_HAIKU_MODEL": "qwen3.6-35b",
        },
    )

    assert result.returncode == 0, result.stderr
    settings = json.loads(settings_path.read_text())
    assert settings["model"] == "deepseek-v4-flash"
    assert settings["permissions"] == {"allow": ["Read"]}
    assert settings["env"] == {
        "KEEP_ME": "yes",
        "ANTHROPIC_BASE_URL": "https://freeinference.org/anthropic",
        "ANTHROPIC_AUTH_TOKEN": "hyi-test-key",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-v4-flash",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-v4-flash",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "qwen3.6-35b",
    }
    assert shell_profile.read_text() == "# keep before\n# keep after\n"


def test_refuses_to_replace_invalid_settings_json(tmp_path: Path) -> None:
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    settings_path = settings_dir / "settings.json"
    settings_path.write_text("{not valid json\n")

    result = _run_setup(tmp_path)

    assert result.returncode != 0
    assert "Refusing to overwrite invalid JSON" in result.stderr
    assert settings_path.read_text() == "{not valid json\n"
