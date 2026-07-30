"""Real agent-runtime drivers (issue #1041, P-1: runtime -> gateway -> fake).

These spawn actual agent CLIs pointed at a target base URL and evaluate the
deterministic ``runtime_smoke`` script end to end. Missing binaries produce a
clean ``skip`` (never a silent pass), so the suite stays honest on machines
without a given runtime installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from freeinference_harness.agent_scripts import get_script, marker

if TYPE_CHECKING:
    from freeinference_harness.models import ScenarioConfig, TargetConfig

_SMOKE_SCRIPT_ID = "runtime_smoke"


def _result(
    status: str,
    failure_type: str | None,
    detail: str,
    observed: dict[str, Any],
    http_status: int | None = None,
) -> dict[str, Any]:
    """Builds a runner-compatible result dictionary."""
    return {
        "status": status,
        "failure_type": failure_type,
        "http_status": http_status,
        "detail": detail,
        "observed": observed,
    }


def _skip_missing(binary: str) -> dict[str, Any]:
    """Builds the skip result for a runtime that is not installed."""
    return _result(
        "skip",
        "runtime_missing",
        f"Runtime binary '{binary}' is not installed on this machine.",
        {"binary": binary},
    )


def _base_env() -> dict[str, str]:
    """Returns a minimal, hermetic subprocess environment."""
    env: dict[str, str] = {}
    for key in ("PATH", "HOME", "LANG", "LC_ALL", "TERM"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def _smoke_prompt(scenario: ScenarioConfig) -> tuple[str, str]:
    """Returns (script marker prompt, expected output token) for the smoke."""
    script = get_script(scenario.agent_script or _SMOKE_SCRIPT_ID)
    prompt = (
        f"{marker(script.script_id)} Repeat the assistant reply you receive "
        "verbatim as your final answer."
    )
    return prompt, script.expected.final_text_contains


def run_runtime_claude_smoke(
    target: TargetConfig,
    scenario: ScenarioConfig,
) -> dict[str, Any]:
    """Runs `claude -p` (headless Claude Code) against the target base URL.

    Exercises the full ``runtime -> gateway -> provider`` chain: Claude Code
    speaks the Anthropic Messages surface to the target and the deterministic
    ``runtime_smoke`` reply must survive translation back into the CLI result.
    """
    binary = shutil.which("claude")
    if binary is None:
        return _skip_missing("claude")

    prompt, expected_token = _smoke_prompt(scenario)
    env = _base_env()
    env.update(
        {
            "ANTHROPIC_BASE_URL": target.base_url.rstrip("/").removesuffix("/v1"),
            "ANTHROPIC_API_KEY": target.api_key or "harness-local",
            "ANTHROPIC_MODEL": target.model,
            "ANTHROPIC_SMALL_FAST_MODEL": target.model,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
        }
    )
    command = [
        binary,
        "-p",
        prompt,
        "--model",
        target.model,
        "--output-format",
        "json",
        "--max-turns",
        "1",
    ]
    return _run_and_check(
        command,
        env=env,
        timeout_seconds=target.timeout_seconds,
        expected_token=expected_token,
        runtime_name="claude-code",
        result_extractor=_extract_claude_result,
    )


def _extract_claude_result(stdout: str) -> str:
    """Extracts the final result text from `claude -p --output-format json`."""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout
    if isinstance(payload, dict):
        result = payload.get("result")
        if isinstance(result, str):
            return result
    return stdout


def run_runtime_codex_smoke(
    target: TargetConfig,
    scenario: ScenarioConfig,
) -> dict[str, Any]:
    """Runs `codex exec` against the target via a scratch CODEX_HOME config.

    The provider is registered with ``wire_api = "chat"`` so Codex speaks the
    OpenAI chat-completions surface to the target.
    """
    binary = shutil.which("codex")
    if binary is None:
        return _skip_missing("codex")

    prompt, expected_token = _smoke_prompt(scenario)
    with tempfile.TemporaryDirectory(prefix="agent-loop-codex-") as tmp:
        codex_home = Path(tmp)
        (codex_home / "config.toml").write_text(
            "\n".join(
                [
                    f'model = "{target.model}"',
                    'model_provider = "harness"',
                    "",
                    "[model_providers.harness]",
                    'name = "harness"',
                    f'base_url = "{target.base_url.rstrip("/").removesuffix("/v1")}/v1"',
                    'env_key = "HARNESS_API_KEY"',
                    'wire_api = "chat"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        env = _base_env()
        env.update(
            {
                "CODEX_HOME": str(codex_home),
                "HARNESS_API_KEY": target.api_key or "harness-local",
            }
        )
        command = [binary, "exec", "--skip-git-repo-check", prompt]
        return _run_and_check(
            command,
            env=env,
            timeout_seconds=target.timeout_seconds,
            expected_token=expected_token,
            runtime_name="codex",
            result_extractor=lambda stdout: stdout,
        )


def run_runtime_pi_smoke(
    target: TargetConfig,
    scenario: ScenarioConfig,
) -> dict[str, Any]:
    """Runs `pi -p` against the target via a scratch-HOME provider config.

    pi ignores ``OPENAI_BASE_URL``; a custom provider in
    ``~/.pi/agent/models.json`` is its supported route to a gateway — the same
    mechanism the sandbox's ``pi-freeinference`` wrapper uses, registered here
    as ``openai-completions`` so pi speaks the OpenAI chat surface.
    """
    binary = shutil.which("pi")
    if binary is None:
        return _skip_missing("pi")

    prompt, expected_token = _smoke_prompt(scenario)
    with tempfile.TemporaryDirectory(prefix="agent-loop-pi-") as tmp:
        home = Path(tmp)
        config_dir = home / ".pi" / "agent"
        config_dir.mkdir(parents=True)
        (config_dir / "models.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "harness": {
                            "baseUrl": f"{target.base_url.rstrip('/').removesuffix('/v1')}/v1",
                            "api": "openai-completions",
                            "apiKey": target.api_key or "harness-local",
                            "models": [{"id": target.model}],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        env = _base_env()
        # HOME is the config mechanism here, so the override is the point.
        env.update({"HOME": str(home), "PI_OFFLINE": "1"})
        command = [
            binary,
            "--provider",
            "harness",
            "--model",
            target.model,
            "--mode",
            "json",
            "--no-session",
            "-p",
            prompt,
        ]
        return _run_and_check(
            command,
            env=env,
            timeout_seconds=target.timeout_seconds,
            expected_token=expected_token,
            runtime_name="pi",
            result_extractor=_extract_pi_result,
        )


def run_runtime_opencode_smoke(
    target: TargetConfig,
    scenario: ScenarioConfig,
) -> dict[str, Any]:
    """Runs `opencode run` against the target via a scratch config file.

    Mirrors the sandbox's ``opencode-freeinference`` wrapper: a provider over
    the SDK package bundled in the binary (chat-completions dialect), with the
    models.dev catalog fetch disabled — OpenCode hard-fails without it when
    offline — and the model declared explicitly, which the disabled catalog
    makes mandatory.
    """
    binary = shutil.which("opencode")
    if binary is None:
        return _skip_missing("opencode")

    prompt, expected_token = _smoke_prompt(scenario)
    with tempfile.TemporaryDirectory(prefix="agent-loop-opencode-") as tmp:
        config_path = Path(tmp) / "opencode.json"
        config_path.write_text(
            json.dumps(
                {
                    "provider": {
                        "harness": {
                            "npm": "@ai-sdk/openai-compatible",
                            "name": "harness",
                            "options": {
                                "baseURL": f"{target.base_url.rstrip('/').removesuffix('/v1')}/v1",
                                "apiKey": target.api_key or "harness-local",
                            },
                            "models": {target.model: {"name": target.model}},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        env = _base_env()
        env.update(
            {
                "OPENCODE_CONFIG": str(config_path),
                "OPENCODE_DISABLE_MODELS_FETCH": "1",
                "OPENCODE_DISABLE_AUTOUPDATE": "1",
                "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
            }
        )
        command = [
            binary,
            "run",
            "--format",
            "json",
            "--auto",
            "-m",
            f"harness/{target.model}",
            prompt,
        ]
        return _run_and_check(
            command,
            env=env,
            timeout_seconds=target.timeout_seconds,
            expected_token=expected_token,
            runtime_name="opencode",
            result_extractor=_extract_opencode_result,
        )


def _extract_opencode_result(stdout: str) -> str:
    """Collects assistant text from OpenCode's ``--format json`` event lines."""
    texts: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "text":
            continue
        part = event.get("part") or {}
        text = part.get("text")
        if isinstance(text, str) and text:
            texts.append(text)
    return "\n".join(texts) if texts else stdout


def _extract_pi_result(stdout: str) -> str:
    """Collects assistant text from pi's ``--mode json`` event lines."""
    texts: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "message_end":
            continue
        message = event.get("message") or {}
        if message.get("role") != "assistant":
            continue
        for part in message.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "text":
                texts.append(part.get("text") or "")
    return "\n".join(texts) if texts else stdout


def _run_and_check(
    command: list[str],
    *,
    env: dict[str, str],
    timeout_seconds: float,
    expected_token: str,
    runtime_name: str,
    result_extractor: Any,
) -> dict[str, Any]:
    """Spawns the runtime, applies the shared pass criteria, and classifies."""
    observed: dict[str, Any] = {"runtime": runtime_name, "command": command[:1] + command[1:3]}
    try:
        completed = subprocess.run(
            command,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _result(
            "fail",
            "runtime_timeout",
            f"{runtime_name} did not finish within {timeout_seconds:.0f}s.",
            observed,
        )

    observed["exit_code"] = completed.returncode
    observed["stdout_preview"] = completed.stdout[-500:]
    observed["stderr_preview"] = completed.stderr[-500:]

    if completed.returncode != 0:
        return _result(
            "fail",
            "runtime_nonzero_exit",
            f"{runtime_name} exited with code {completed.returncode}.",
            observed,
        )

    final_text = result_extractor(completed.stdout)
    observed["final_text_preview"] = (final_text or "")[:200]
    if expected_token and expected_token not in (final_text or ""):
        return _result(
            "fail",
            "runtime_wrong_output",
            f"{runtime_name} completed but the final output missed {expected_token!r}.",
            observed,
        )
    return _result(
        "pass",
        None,
        f"{runtime_name} completed the deterministic smoke through the target.",
        observed,
        http_status=200,
    )
