"""The runner's checkout step, against real git.

This exists because of a specific failure: the self-hosted runner created an
empty temporary directory per job and ran the agent in it. Nothing errored.
The agent had no code to read, produced no changes, and the job was recorded as
a clean success — the worst possible shape for a bug, since every observable
signal said the system worked.

So these tests run real git against a real repository rather than mocking it.
A mock would have agreed with the broken version.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from serving.agent_jobs.runner import (
    WorktreeError,
    _assert_no_credential_on_disk,
    _auth_env,
    align_existing_checkout,
    build_patch,
    existing_checkout_sha,
    prepare_worktree,
)

if TYPE_CHECKING:
    from pathlib import Path


def _git(*args: str, cwd: Path) -> str:
    """Run git in a test fixture repository."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    ).stdout


@pytest.fixture()
def remote(tmp_path: Path) -> tuple[str, str, str, str]:
    """A servable repository with two commits.

    Returns ``(remote_base, repo, first_sha, head_sha)``.
    """
    source = tmp_path / "source"
    source.mkdir()
    _git("init", "--quiet", "--initial-branch", "main", ".", cwd=source)
    (source / "hello.py").write_text("def hello():\n    return 1\n")
    _git("add", "-A", cwd=source)
    _git("commit", "--quiet", "-m", "first", cwd=source)
    first = _git("rev-parse", "HEAD", cwd=source).strip()

    (source / "second.txt").write_text("later\n")
    _git("add", "-A", cwd=source)
    _git("commit", "--quiet", "-m", "second", cwd=source)
    head = _git("rev-parse", "HEAD", cwd=source).strip()

    # Serve it as `owner/name.git` under a base directory, so the runner's
    # "{base}/{repo}.git" URL construction is exercised as written.
    bare = tmp_path / "remotes" / "owner" / "name.git"
    bare.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "--quiet", "--bare", str(source), str(bare)],
        check=True,
        capture_output=True,
    )
    return f"file://{tmp_path / 'remotes'}", "owner/name", first, head


def test_the_agent_gets_a_populated_worktree(tmp_path: Path, remote):
    """The whole point: a job directory that actually contains the repository."""
    base, repo, _first, head = remote
    workdir = tmp_path / "job"
    workdir.mkdir()

    resolved = prepare_worktree(workdir=str(workdir), repo=repo, base_sha=None, remote_base=base)

    assert (workdir / "hello.py").read_text() == "def hello():\n    return 1\n"
    assert resolved == head


def test_a_pinned_commit_is_the_one_checked_out(tmp_path: Path, remote):
    """An owner who names a commit gets that commit, not the branch tip.

    Checked by content, not just by sha: the second commit adds a file, so its
    absence is direct evidence the older tree was materialized.
    """
    base, repo, first, head = remote
    assert first != head, "the fixture must have two distinct commits to distinguish"
    workdir = tmp_path / "job"
    workdir.mkdir()

    resolved = prepare_worktree(workdir=str(workdir), repo=repo, base_sha=first, remote_base=base)

    assert resolved == first
    assert (workdir / "hello.py").exists()
    assert not (workdir / "second.txt").exists()


def test_a_populated_worktree_produces_a_real_patch(tmp_path: Path, remote):
    """Checkout → edit → patch. The chain the empty directory silently broke."""
    base, repo, _first, _head = remote
    workdir = tmp_path / "job"
    workdir.mkdir()
    prepare_worktree(workdir=str(workdir), repo=repo, base_sha=None, remote_base=base)

    (workdir / "hello.py").write_text('def hello():\n    """Say hello."""\n    return 1\n')
    (workdir / "brand_new.txt").write_text("added by the agent\n")

    patch = build_patch(str(workdir))

    assert "hello.py" in patch
    # `git add -A -N` is what makes a file the agent created show up at all.
    assert "brand_new.txt" in patch
    assert "Say hello." in patch


def test_an_unreachable_repository_fails_the_job_with_a_reason(tmp_path: Path):
    """A clone that cannot happen must say so, not run an agent on nothing."""
    workdir = tmp_path / "job"
    workdir.mkdir()
    with pytest.raises(WorktreeError, match="could not fetch"):
        prepare_worktree(
            workdir=str(workdir),
            repo="owner/missing",
            base_sha=None,
            remote_base=f"file://{tmp_path}",
        )


@pytest.mark.parametrize("repo", ["not-a-slug", "--upload-pack=sh", "a/b/c", ""])
def test_a_malformed_repo_never_reaches_git(tmp_path: Path, repo: str):
    """Values interpolated into a URL and git's argv are shape-checked first."""
    with pytest.raises(WorktreeError, match="owner/name"):
        prepare_worktree(workdir=str(tmp_path), repo=repo, base_sha=None)


def test_a_non_commit_base_sha_is_refused(tmp_path: Path):
    """git reads a leading `-` as an option even where an operand belongs."""
    with pytest.raises(WorktreeError, match="commit hash"):
        prepare_worktree(workdir=str(tmp_path), repo="o/n", base_sha="--upload-pack=/bin/sh")


def test_the_clone_credential_stays_out_of_git_argv_and_config():
    """The token travels in the environment, where `ps` cannot read it."""
    env = _auth_env("ghs_secret")
    assert "ghs_secret" not in " ".join(env.values())  # base64, not plaintext
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    # Config supplied this way applies to the one invocation and is never
    # written to .git/config, which the sandbox can read.
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert _auth_env(None) == {}


def test_a_credential_left_on_disk_stops_the_job(tmp_path: Path):
    """Better to fail than to mount a worktree carrying a GitHub token."""
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("url = https://x-access-token:ghs_leaked@github.com/o/n\n")

    with pytest.raises(WorktreeError, match="clone credential"):
        _assert_no_credential_on_disk(str(tmp_path), "ghs_leaked")

    # And a clean tree passes, so the check is not vacuously true.
    (git_dir / "config").write_text("url = https://github.com/o/n\n")
    _assert_no_credential_on_disk(str(tmp_path), "ghs_leaked")


def test_a_real_checkout_leaves_no_remote_to_push_to(tmp_path: Path, remote):
    """No remote recorded: the runner is not given a push path it must not use."""
    base, repo, _first, _head = remote
    workdir = tmp_path / "job"
    workdir.mkdir()
    prepare_worktree(workdir=str(workdir), repo=repo, base_sha=None, remote_base=base)

    config = (workdir / ".git" / "config").read_text()
    assert "[remote " not in config


def test_an_existing_checkout_is_reused_rather_than_cloned_over(tmp_path: Path, remote):
    """The Actions dogfood checks the repository out itself."""
    base, repo, _first, head = remote
    workdir = tmp_path / "job"
    workdir.mkdir()
    prepare_worktree(workdir=str(workdir), repo=repo, base_sha=None, remote_base=base)
    _git("remote", "add", "origin", "https://github.com/owner/name.git", cwd=workdir)

    assert existing_checkout_sha(str(workdir), repo) == head


def test_the_wrong_repository_is_refused_not_worked_on(tmp_path: Path, remote):
    """A patch against the wrong repo looks plausible and applies to nothing."""
    base, repo, _first, _head = remote
    workdir = tmp_path / "job"
    workdir.mkdir()
    prepare_worktree(workdir=str(workdir), repo=repo, base_sha=None, remote_base=base)
    _git("remote", "add", "origin", "https://github.com/someone/else.git", cwd=workdir)

    with pytest.raises(WorktreeError, match="wrong repository"):
        existing_checkout_sha(str(workdir), repo)


def test_an_empty_directory_is_not_mistaken_for_a_checkout(tmp_path: Path):
    """Returning a sha here would skip the clone and reinstate the bug."""
    assert existing_checkout_sha(str(tmp_path), "o/n") is None


def test_a_pinned_commit_wins_over_a_pre_populated_checkout(tmp_path: Path, remote):
    """The Actions runner checks out the trigger ref, not the job's commit.

    Running the agent on the wrong tree is not a visible failure: the store
    keeps the owner's pinned sha, so the publisher applies a patch generated
    against one commit onto a different one. `--3way` makes that *usually*
    succeed, which is worse than failing — the PR looks plausible and encodes
    changes nobody wrote.
    """
    base, repo, first, head = remote
    workdir = tmp_path / "job"
    workdir.mkdir()
    # Stand in for actions/checkout: the worktree is on the branch tip.
    prepare_worktree(workdir=str(workdir), repo=repo, base_sha=None, remote_base=base)
    _git("remote", "add", "origin", "https://github.com/owner/name.git", cwd=workdir)
    # ...with full history available, as `fetch-depth: 0` gives.
    _git("fetch", "--quiet", "--unshallow", base + "/" + repo + ".git", cwd=workdir)

    checked_out = existing_checkout_sha(str(workdir), repo)
    assert checked_out == head

    aligned = align_existing_checkout(str(workdir), checked_out=checked_out, base_sha=first)

    assert aligned == first
    assert not (workdir / "second.txt").exists(), "the worktree must be on the pinned commit"


def test_an_unchanged_checkout_is_left_alone(tmp_path: Path, remote):
    """No pinned commit, or already on it: do not touch the worktree."""
    base, repo, _first, head = remote
    workdir = tmp_path / "job"
    workdir.mkdir()
    prepare_worktree(workdir=str(workdir), repo=repo, base_sha=None, remote_base=base)

    assert align_existing_checkout(str(workdir), checked_out=head, base_sha=None) == head
    assert align_existing_checkout(str(workdir), checked_out=head, base_sha=head) == head
    # An abbreviated pin resolves to the same commit rather than a needless switch.
    assert align_existing_checkout(str(workdir), checked_out=head, base_sha=head[:8]) == head


def test_a_pinned_commit_the_worktree_lacks_stops_the_job(tmp_path: Path, remote):
    """Better to fail than to silently produce a patch against another tree."""
    base, repo, _first, head = remote
    workdir = tmp_path / "job"
    workdir.mkdir()
    prepare_worktree(workdir=str(workdir), repo=repo, base_sha=None, remote_base=base)

    with pytest.raises(WorktreeError, match="does not contain"):
        align_existing_checkout(str(workdir), checked_out=head, base_sha="0" * 40)


def test_building_the_patch_gives_up_rather_than_hanging_forever(monkeypatch):
    """A worktree whose git config never returns must not pin the lease open.

    The final `git diff` runs against a tree the agent owned all run, including
    its `.git/config`. A `diff.external` that blocks would otherwise leave the
    runner reading forever while the heartbeat keeps renewing the lease — so
    the attempt never expires, the reaper never takes it, and a --loop runner
    claims nothing again.
    """
    import threading

    from serving.agent_jobs import runner as runner_module

    release = threading.Event()

    class HangingProcess:
        def __init__(self) -> None:
            self.killed = False

        def lines(self):
            release.wait(timeout=30)
            return iter(())

        def kill(self) -> None:
            self.killed = True
            release.set()

        def wait(self) -> int:
            return 0

    process = HangingProcess()
    monkeypatch.setattr(runner_module, "_PATCH_TIMEOUT_S", 0.5)

    with pytest.raises(WorktreeError, match="exceeded"):
        runner_module._read_bounded(process, deadline_s=0.5, max_bytes=1024)

    assert process.killed, "the sandbox process must be killed, not merely abandoned"


def test_building_the_patch_refuses_unbounded_output():
    """A patch that grows without end must not be buffered into the runner."""
    from serving.agent_jobs import runner as runner_module

    class FloodProcess:
        def __init__(self) -> None:
            self.killed = False

        def lines(self):
            while not self.killed:
                yield "x" * 1024 + "\n"

        def kill(self) -> None:
            self.killed = True

        def wait(self) -> int:
            return 0

    process = FloodProcess()

    with pytest.raises(WorktreeError, match="bytes"):
        runner_module._read_bounded(process, deadline_s=30, max_bytes=4096)

    assert process.killed


# ── denied egress is the data P0 exists to produce ─────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("curl: (6) Could not resolve host: telemetry.example.com", "telemetry.example.com"),
        ("Error: getaddrinfo ENOTFOUND registry.npmjs.org", "registry.npmjs.org"),
        (
            "urllib.error.URLError: <urlopen error [Errno -3] Temporary failure in name "
            "resolution> while fetching https://pypi.org/simple/",
            "pypi.org",
        ),
        ("fatal: unable to access 'https://github.com/o/n': Connection refused", "github.com"),
    ],
)
def test_a_blocked_call_is_recognised_with_its_host(text: str, expected: str):
    """On a deny-all network the denial surfaces as the tool's own failure."""
    from serving.agent_jobs.runner import detect_blocked_egress

    assert detect_blocked_egress(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "SyntaxError: invalid syntax",
        "test failed: expected 3, got 4",
        "Could not resolve host",  # a denial with no host is not evidence
        "npm ERR! missing script: build",
    ],
)
def test_an_ordinary_failure_is_not_reported_as_egress(text: str):
    """A false "the sandbox tried to phone home" is worse than a missed one.

    The record exists to justify what an external default should allow, so it
    has to mean something. Every failing command is not a denial.
    """
    from serving.agent_jobs.runner import detect_blocked_egress

    assert detect_blocked_egress(text) is None


def test_a_repository_whose_name_merely_ends_the_same_is_refused(tmp_path: Path, remote):
    """`endswith` accepted `acme/foo` for a job targeting `me/foo`.

    The agent would then run against the wrong codebase and produce a patch
    that applies to nothing — and nothing downstream would have caught it,
    because the patch itself is well-formed.
    """
    base, repo, _first, _head = remote
    workdir = tmp_path / "job"
    workdir.mkdir()
    prepare_worktree(workdir=str(workdir), repo=repo, base_sha=None, remote_base=base)
    _git("remote", "add", "origin", "https://github.com/evilowner/name.git", cwd=workdir)

    with pytest.raises(WorktreeError, match="wrong repository"):
        existing_checkout_sha(str(workdir), "owner/name")


def test_the_exact_repository_is_still_accepted(tmp_path: Path, remote):
    """Tightening the comparison must not reject the legitimate case."""
    base, repo, _first, head = remote
    workdir = tmp_path / "job"
    workdir.mkdir()
    prepare_worktree(workdir=str(workdir), repo=repo, base_sha=None, remote_base=base)
    _git("remote", "add", "origin", "git@github.com:owner/name.git", cwd=workdir)

    assert existing_checkout_sha(str(workdir), repo) == head
