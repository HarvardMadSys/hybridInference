"""Unit tests for the agent job runner.

Covers the parts where being wrong is expensive: losing the lease must stop
the run rather than race a replacement, cancellation must actually kill the
agent, and the runner must never acquire a git write path.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from serving.agent_jobs import runner as runner_mod
from serving.agent_jobs.runner import (
    ClaimedJob,
    LeaseLost,
    build_patch,
    conversation_prompt,
    run_agent,
)
from serving.agent_jobs.runtimes import ClaudeCodeRuntime, GenericRuntime
from serving.agent_jobs.sandbox import ProcessBackend

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_JOB = ClaimedJob(
    job_id="ajob_1",
    attempt_id=7,
    attempt_no=1,
    repo="o/n",
    base_sha="abc",
    task_prompt="do it",
    runtime="generic",
    model="glm-5.1",
    worker_token="ajt.a.b",
    sandbox_token="ajt.model.b",
)


def test_follow_up_prompt_replays_runtime_neutral_conversation():
    """A harness switch still receives the prior thread and current request."""
    job = ClaimedJob(
        **{
            **_JOB.__dict__,
            "task_prompt": "now add tests",
            "context_messages": [
                {"role": "user", "content": "fix the timeout"},
                {"role": "assistant", "content": "I updated the middleware"},
            ],
        }
    )

    prompt = conversation_prompt(job)

    assert "USER: fix the timeout" in prompt
    assert "ASSISTANT: I updated the middleware" in prompt
    assert prompt.endswith("NEW USER REQUEST:\nnow add tests")


@pytest.fixture(autouse=True)
def _allow_unisolated_sandbox(monkeypatch):
    """These tests exercise runner logic, not sandbox policy.

    The process backend refuses to start unisolated unless an operator opts
    in — that refusal is asserted in test_agent_sandbox.py; here we accept it
    so the runner's own behaviour is what is under test.
    """
    monkeypatch.setenv("AGENT_SANDBOX_ALLOW_UNISOLATED", "1")


class FakeControl:
    """Records what the runner reported."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.artifacts: dict[str, str] = {}
        self.finished: tuple[str, str | None] | None = None
        self.job_id = "ajob_1"

    def append_event(self, event) -> None:
        self.events.append((event.event_type, event.payload))

    def save_artifact(self, kind: str, content: str) -> None:
        self.artifacts[kind] = content

    def finish(self, state: str, detail: str | None = None) -> None:
        self.finished = (state, detail)

    def close(self) -> None:
        pass


class FakeHeart:
    """Stand-in heartbeater with directly settable flags."""

    def __init__(self, *, cancel: bool = False, lost: bool = False) -> None:
        self.cancel_requested = cancel
        self.lease_lost = lost

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


def _echo_runtime(lines: int = 3, *, sleep: float = 0.0) -> GenericRuntime:
    """A runtime whose 'agent' is a python one-liner emitting N lines.

    Deliberately a single line: the template is shell-*split*, never
    shell-*executed*, so an embedded newline would be mangled rather than
    interpreted — which is exactly the property that makes GenericRuntime safe.
    """
    script = (
        f"for i in range({lines}): "
        f"print('line %d' % i, flush=True); __import__('time').sleep({sleep})"
    )
    return GenericRuntime(f"{sys.executable} -c {script!r}")


def test_agent_output_streams_back_as_events(tmp_path):
    """Every line the agent prints reaches the control plane."""
    control, heart = FakeControl(), FakeHeart()
    code, _tail, _errors = run_agent(
        _echo_runtime(3),
        job=_JOB,
        workdir=str(tmp_path),
        gateway_base_url="http://gw",
        control=control,
        heart=heart,
        timeout_s=30,
        backend=ProcessBackend(acknowledged_unsafe=True),
    )
    assert code == 0
    assert [payload["text"] for _kind, payload in control.events] == [
        "line 0",
        "line 1",
        "line 2",
    ]


def test_losing_the_lease_aborts_instead_of_racing(tmp_path):
    """A superseded attempt stops rather than continuing beside its replacement."""
    control, heart = FakeControl(), FakeHeart(lost=True)
    with pytest.raises(LeaseLost):
        run_agent(
            _echo_runtime(50, sleep=0.02),
            job=_JOB,
            workdir=str(tmp_path),
            gateway_base_url="http://gw",
            control=control,
            heart=heart,
            timeout_s=30,
            backend=ProcessBackend(acknowledged_unsafe=True),
        )


def test_cancellation_kills_the_agent(tmp_path):
    """Owner cancellation, delivered via heartbeat, actually stops the process."""
    control, heart = FakeControl(), FakeHeart(cancel=True)
    code, _tail, _errors = run_agent(
        _echo_runtime(200, sleep=0.02),
        job=_JOB,
        workdir=str(tmp_path),
        gateway_base_url="http://gw",
        control=control,
        heart=heart,
        timeout_s=30,
        backend=ProcessBackend(acknowledged_unsafe=True),
    )
    assert code == 130
    assert ("lifecycle", {"phase": "cancelled_by_owner"}) in control.events


def test_agent_timeout_is_enforced(tmp_path):
    """A runaway agent is killed rather than holding the job forever."""
    control, heart = FakeControl(), FakeHeart()
    code, _tail, _errors = run_agent(
        _echo_runtime(500, sleep=0.05),
        job=_JOB,
        workdir=str(tmp_path),
        gateway_base_url="http://gw",
        control=control,
        heart=heart,
        timeout_s=0.15,
        backend=ProcessBackend(acknowledged_unsafe=True),
    )
    assert code == 124
    assert any(kind == "error" for kind, _ in control.events)


def test_agent_environment_is_hermetic(tmp_path, monkeypatch):
    """The agent inherits only what it needs — not the CI runner's secrets."""
    monkeypatch.setenv("A_CI_SECRET", "super-secret-value")
    control, heart = FakeControl(), FakeHeart()
    dumper = GenericRuntime(
        f"{sys.executable} -c " + repr("import os;print('A_CI_SECRET' in os.environ)")
    )
    run_agent(
        dumper,
        job=_JOB,
        workdir=str(tmp_path),
        gateway_base_url="http://gw",
        control=control,
        heart=heart,
        timeout_s=30,
        backend=ProcessBackend(acknowledged_unsafe=True),
    )
    assert control.events[0][1]["text"] == "False"


def test_build_patch_captures_new_and_modified_files(tmp_path):
    """The patch includes untracked files, which a bare git diff would miss."""
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *args: subprocess.run(args, cwd=repo, check=True, capture_output=True)  # noqa: E731
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    (repo / "existing.txt").write_text("before\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "init")

    (repo / "existing.txt").write_text("after\n")
    (repo / "brand_new.txt").write_text("hello\n")

    patch = build_patch(str(repo))
    assert "existing.txt" in patch
    assert "brand_new.txt" in patch, "untracked files must appear in the patch"
    assert "+after" in patch


def test_build_patch_is_empty_when_nothing_changed(tmp_path):
    """A job that changed nothing produces no patch rather than a bogus one."""
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *args: subprocess.run(args, cwd=repo, check=True, capture_output=True)  # noqa: E731
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    (repo / "f.txt").write_text("x\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "init")

    assert build_patch(str(repo)).strip() == ""


def test_missing_runtime_binary_fails_the_job_loudly(monkeypatch, tmp_path):
    """A job whose runtime is absent fails now, not after a lease timeout."""
    control = FakeControl()
    monkeypatch.setattr(
        runner_mod,
        "claim",
        lambda **_kwargs: ClaimedJob(
            job_id="ajob_1",
            attempt_id=7,
            attempt_no=1,
            repo="o/n",
            base_sha=None,
            task_prompt="p",
            runtime="claude-code",
            model="m",
            worker_token="ajt.a.b",
            sandbox_token="ajt.model.b",
        ),
    )
    monkeypatch.setattr(runner_mod, "ControlPlane", lambda *a, **k: control)
    monkeypatch.setattr(runner_mod.shutil, "which", lambda _name: None)

    code = runner_mod.run_once(
        base_url="http://gw",
        dispatcher_token="d",
        worker_id="w",
        workdir=str(tmp_path),
    )
    assert code == 2
    assert control.finished[0] == "failed"
    assert "not installed" in control.finished[1]


def test_empty_queue_is_a_clean_no_op(monkeypatch, tmp_path):
    """Nothing queued is success, not an error the CI would flag."""
    monkeypatch.setattr(runner_mod, "claim", lambda **_kwargs: None)
    assert (
        runner_mod.run_once(
            base_url="http://gw",
            dispatcher_token="d",
            worker_id="w",
            workdir=str(tmp_path),
        )
        == 0
    )


def test_runner_source_contains_no_push_path():
    """The runner must never gain a git write path — patch-out is the contract."""
    source = Path(runner_mod.__file__).read_text()
    assert '"push"' not in source
    assert "git push" not in source


def test_failed_tool_calls_are_reported_not_swallowed(tmp_path):
    """A denied tool call must reach the caller, not be trusted away.

    Found on the first real run: Claude Code's permission gate denied the
    Edit, the model then reported "I've added the docstring" anyway, and the
    job was recorded as a clean success that changed nothing. The runner now
    reports what the tools did rather than what the agent said about them.
    """
    lines = [
        '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1","name":"Edit","input":{}}]}}',
        '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1","is_error":true,"content":"permission denied"}]}}',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"Done!"}]}}',
    ]
    script = f"import json;[print(line, flush=True) for line in json.loads({json.dumps(lines)!r})]"

    class _ScriptedClaude(ClaudeCodeRuntime):
        """Claude Code's event parsing, driven by a scripted stdout."""

        def prepare(self, **_kwargs):
            return [sys.executable, "-c", script], {}

    control = FakeControl()
    code, _tail, errors = run_agent(
        _ScriptedClaude(),
        job=_JOB,
        workdir=str(tmp_path),
        gateway_base_url="http://gw",
        control=control,
        heart=FakeHeart(),
        timeout_s=30,
        backend=ProcessBackend(acknowledged_unsafe=True),
    )
    assert code == 0
    assert len(errors) == 1
    assert "permission denied" in errors[0]
    # The owner sees it in the stream, not only in the final detail string.
    assert any(kind == "error" for kind, _payload in control.events)


class _CountingBackend:
    """Records how often a supplied backend is preflighted."""

    name = "counting"

    def __init__(self) -> None:
        self.preflights = 0

    def preflight(self, workdir_root: str | None = None) -> None:
        self.preflights += 1

    def has_binary(self, _name: str) -> bool:
        return True

    def base_env(self) -> dict[str, str]:
        return {}


def test_a_supplied_backend_is_not_preflighted_per_claim(monkeypatch, tmp_path):
    """An idle standing runner must not re-probe Docker on every poll.

    Preflight spawns several `docker` subprocesses and logs the shared-kernel
    warning. Running it per claim meant an idle runner polling every 5s burned
    ~17k probes and wrote ~17k warning lines a day — visible only once a
    runner actually stood up and idled.
    """
    monkeypatch.setattr(runner_mod, "claim", lambda **_kwargs: None)
    backend = _CountingBackend()

    for _ in range(3):
        runner_mod.run_once(
            base_url="http://gw",
            dispatcher_token="d",
            worker_id="w",
            workdir=str(tmp_path),
            backend=backend,
        )

    assert backend.preflights == 0, "run_once preflighted a backend its caller already preflighted"


def test_a_self_built_backend_is_still_preflighted_before_claiming(monkeypatch, tmp_path):
    """The one-shot path keeps failing fast on a misconfigured host."""
    backend = _CountingBackend()
    monkeypatch.setattr(runner_mod, "build_backend_from_env", lambda: backend)
    claims: list[bool] = []

    def _claim(**_kwargs):
        claims.append(True)
        return None

    monkeypatch.setattr(runner_mod, "claim", _claim)

    runner_mod.run_once(
        base_url="http://gw",
        dispatcher_token="d",
        worker_id="w",
        workdir=str(tmp_path),
    )

    assert backend.preflights == 1
    assert claims, "preflight must not have replaced the claim"
