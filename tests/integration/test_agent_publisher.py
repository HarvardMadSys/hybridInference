"""Publisher tests against real local git repositories.

These use throwaway on-disk repos (no network, no GitHub) so the actual git
behavior is exercised: the branch really gets created, the patch really
applies, and — the part that matters — a rejected patch really leaves the
remote untouched.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from serving.agent_jobs.publisher import PublishError, publish_patch

pytestmark = pytest.mark.integration

_GIT_ENV = {
    "PATH": "/usr/bin:/bin:/usr/local/bin",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(args: list[str], cwd: Path) -> str:
    """Run git in a test repo."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


@pytest.fixture()
def origin(tmp_path: Path) -> tuple[str, str]:
    """Create a bare origin with one commit; return (clone_url, base_sha)."""
    source = tmp_path / "source"
    source.mkdir()
    _git(["init", "--quiet", "--initial-branch=main", "."], cwd=source)
    (source / "app.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    (source / "README.md").write_text("# demo\n", encoding="utf-8")
    _git(["add", "--all"], cwd=source)
    _git(["commit", "--quiet", "-m", "initial"], cwd=source)
    base_sha = _git(["rev-parse", "HEAD"], cwd=source).strip()

    bare = tmp_path / "origin.git"
    _git(["clone", "--quiet", "--bare", str(source), str(bare)], cwd=tmp_path)
    # Allow fetching an arbitrary SHA, as a real forge does.
    _git(["config", "uploadpack.allowAnySHA1InWant", "true"], cwd=bare)
    return str(bare), base_sha


def _branches(bare_url: str) -> list[str]:
    """List branches present in the bare origin."""
    out = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
        cwd=bare_url,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def test_publishes_branch_with_applied_patch(origin):
    """A clean patch lands on exactly one agent/<job-id> branch."""
    clone_url, base_sha = origin
    patch = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def main():\n"
        '+    print("patched")\n'
        "     return 0\n"
    )

    result = publish_patch(
        job_id="ajob_test01",
        patch=patch,
        clone_url=clone_url,
        base_sha=base_sha,
        commit_message="agent: patch app.py",
    )

    assert result.branch == "agent/ajob_test01"
    assert result.changed_files == ["app.py"]
    assert sorted(_branches(clone_url)) == ["agent/ajob_test01", "main"]

    # main is untouched and the branch really contains the change.
    content = subprocess.run(
        ["git", "show", f"{result.branch}:app.py"],
        cwd=clone_url,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert 'print("patched")' in content
    main_content = subprocess.run(
        ["git", "show", "main:app.py"],
        cwd=clone_url,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "patched" not in main_content


def test_workflow_patch_is_refused_and_remote_untouched(origin):
    """A .github/ patch never reaches the remote without human release."""
    clone_url, base_sha = origin
    patch = (
        "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"
        "--- a/.github/workflows/ci.yml\n"
        "+++ b/.github/workflows/ci.yml\n"
        "@@ -0,0 +1 @@\n"
        "+on: push\n"
    )

    with pytest.raises(PublishError) as excinfo:
        publish_patch(
            job_id="ajob_evil",
            patch=patch,
            clone_url=clone_url,
            base_sha=base_sha,
            commit_message="agent: sneak a workflow in",
        )
    assert "human release" in str(excinfo.value)
    assert _branches(clone_url) == ["main"]


def test_secret_patch_is_refused_even_with_human_release(origin):
    """The workflow override does not waive the secret scan."""
    clone_url, base_sha = origin
    patch = (
        "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"
        "--- a/.github/workflows/ci.yml\n"
        "+++ b/.github/workflows/ci.yml\n"
        "@@ -0,0 +1 @@\n"
        f'+  token: "ghp_{"a" * 36}"\n'
    )

    with pytest.raises(PublishError) as excinfo:
        publish_patch(
            job_id="ajob_secret",
            patch=patch,
            clone_url=clone_url,
            base_sha=base_sha,
            commit_message="agent: with secret",
            allow_workflow_changes=True,
        )
    assert "github_token" in str(excinfo.value)
    assert _branches(clone_url) == ["main"]


def test_human_released_workflow_patch_publishes(origin):
    """With explicit release, a clean workflow patch is allowed through."""
    clone_url, base_sha = origin
    patch = (
        "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/.github/workflows/ci.yml\n"
        "@@ -0,0 +1 @@\n"
        "+on: push\n"
    )

    result = publish_patch(
        job_id="ajob_released",
        patch=patch,
        clone_url=clone_url,
        base_sha=base_sha,
        commit_message="agent: approved workflow change",
        allow_workflow_changes=True,
    )
    assert result.branch == "agent/ajob_released"
    assert "agent/ajob_released" in _branches(clone_url)


def test_unapplyable_patch_fails_without_publishing(origin):
    """A patch that does not apply is an error, not a partial push."""
    clone_url, base_sha = origin
    patch = (
        "diff --git a/missing.py b/missing.py\n"
        "--- a/missing.py\n"
        "+++ b/missing.py\n"
        "@@ -1,3 +1,3 @@\n"
        " context that does not exist\n"
        "-old\n"
        "+new\n"
    )

    with pytest.raises(PublishError):
        publish_patch(
            job_id="ajob_bad",
            patch=patch,
            clone_url=clone_url,
            base_sha=base_sha,
            commit_message="agent: broken patch",
        )
    assert _branches(clone_url) == ["main"]


def test_noop_patch_is_reported_rather_than_pushed(origin):
    """A patch that applies to nothing does not create an empty branch."""
    clone_url, base_sha = origin
    patch = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def main():\n"
        "     return 0\n"
    )

    with pytest.raises(PublishError):
        publish_patch(
            job_id="ajob_noop",
            patch=patch,
            clone_url=clone_url,
            base_sha=base_sha,
            commit_message="agent: no-op",
        )
    assert _branches(clone_url) == ["main"]


def test_new_file_patch_publishes(origin):
    """Adding a new file works end to end (the common agent case)."""
    clone_url, base_sha = origin
    patch = (
        "diff --git a/newmod.py b/newmod.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/newmod.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def helper():\n"
        "+    return 42\n"
    )

    result = publish_patch(
        job_id="ajob_newfile",
        patch=patch,
        clone_url=clone_url,
        base_sha=base_sha,
        commit_message="agent: add helper",
    )
    listed = subprocess.run(
        ["git", "ls-tree", "--name-only", result.branch],
        cwd=clone_url,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "newmod.py" in listed


def test_option_shaped_base_sha_is_refused_before_git_sees_it():
    """base_sha must never reach git as an option.

    Regression: git parses a leading `--` argument as an option even after the
    remote name, so `--upload-pack=/bin/sh -c ...` would have executed a
    command inside the trusted publisher — the process that holds the
    repository credential.
    """
    for hostile in (
        "--upload-pack=/bin/sh -c 'touch /tmp/pwned'",
        "--exec=evil",
        "-x",
        "not-a-sha",
        "",
    ):
        with pytest.raises(PublishError) as excinfo:
            publish_patch(
                job_id="ajob_x",
                patch="diff --git a/x b/x\n",
                clone_url="/nonexistent",
                base_sha=hostile,
                commit_message="m",
            )
        assert "commit hash" in str(excinfo.value)
