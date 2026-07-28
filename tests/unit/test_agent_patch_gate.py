"""Unit tests for the agent patch validation gate.

This gate is the security boundary of the patch-out model: everything an
untrusted agent can influence passes through it before becoming a git object.
"""

from __future__ import annotations

import pytest

from serving.agent_jobs.patch_gate import (
    MAX_CHANGED_FILES,
    branch_name_for,
    validate_patch,
)

_CLEAN_PATCH = """diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,4 @@
 def main():
+    print("fixed")
     return 0
"""


def _patch_for(path: str, added_line: str = "    pass", mode_line: str = "") -> str:
    """Build a minimal one-file patch touching ``path``."""
    mode = f"{mode_line}\n" if mode_line else ""
    return (
        f"diff --git a/{path} b/{path}\n"
        f"{mode}"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -0,0 +1 @@\n"
        f"+{added_line}\n"
    )


def test_clean_patch_passes():
    """A normal source-file patch is accepted."""
    result = validate_patch(_CLEAN_PATCH)
    assert result.ok is True
    assert result.changed_files == ["src/app.py"]
    assert result.requires_human_release is False
    assert result.reason == "ok"


def test_empty_patch_is_rejected():
    """An empty patch is a failure, not a silent no-op publish."""
    result = validate_patch("   \n  ")
    assert result.ok is False
    assert "empty" in result.reason


def test_unparseable_patch_is_rejected():
    """Content without diff headers never reaches git."""
    result = validate_patch("just some prose the agent emitted\n")
    assert result.ok is False
    assert "no recognizable file headers" in result.reason


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".github/workflows/deploy.yaml",
        ".github/CODEOWNERS",
        ".github/dependabot.yml",
    ],
)
def test_github_directory_requires_human_release(path):
    """Workflow/CI config changes are gated behind explicit human release.

    Pushing a branch with a modified workflow can execute it with repository
    secrets on the push event — before anyone reads the draft PR.
    """
    result = validate_patch(_patch_for(path))
    assert result.ok is False
    assert result.requires_human_release is True
    assert "human release" in result.reason


def test_ordinary_dotfiles_are_not_blocked():
    """The block is scoped to .github/, not every dotfile."""
    assert validate_patch(_patch_for(".gitignore")).ok is True
    assert validate_patch(_patch_for("docs/.vale.ini")).ok is True


@pytest.mark.parametrize(
    "path",
    ["../outside.txt", "/etc/passwd", "src/../../escape.py", "~/.ssh/authorized_keys"],
)
def test_path_escape_is_rejected(path):
    """Paths that leave the repository root are refused."""
    result = validate_patch(_patch_for(path))
    assert result.ok is False
    assert "outside the repository" in result.reason


def test_symlink_creation_is_rejected():
    """A new symlink (mode 120000) can point at anything; refuse it."""
    result = validate_patch(
        _patch_for("link", added_line="/etc/passwd", mode_line="new mode 120000")
    )
    assert result.ok is False
    assert "symlinks" in result.reason


@pytest.mark.parametrize(
    ("label", "secret"),
    [
        ("github_token", "ghp_" + "a" * 36),
        ("openai_key", "sk-" + "b" * 32),
        ("anthropic_key", "sk-ant-" + "c" * 32),
        ("gateway_key", "hyi-" + "d" * 32),
        ("agent_worker_token", "ajt.abcdefghijkl.mnopqrstuvwx"),
        ("aws_access_key", "AKIAIOSFODNN7EXAMPLE"),
        ("slack_token", "xoxb-123456789012-abcdefghijkl"),
        ("private_key_block", "-----BEGIN RSA PRIVATE KEY-----"),
    ],
)
def test_added_secrets_are_detected(label, secret):
    """Credential-shaped content in added lines blocks publication."""
    result = validate_patch(_patch_for("src/config.py", added_line=f'TOKEN = "{secret}"'))
    assert result.ok is False
    assert label in result.reason


def test_preexisting_secret_in_context_does_not_block():
    """A secret on a context/removed line is not this patch's doing."""
    patch = (
        "diff --git a/src/config.py b/src/config.py\n"
        "--- a/src/config.py\n"
        "+++ b/src/config.py\n"
        "@@ -1,2 +1,2 @@\n"
        f'-TOKEN = "ghp_{"a" * 36}"\n'
        '+TOKEN = os.environ["TOKEN"]\n'
    )
    result = validate_patch(patch)
    assert result.ok is True


def test_secret_lookalike_prose_does_not_trip_the_scanner():
    """Ordinary words near 'key' or 'token' must not false-positive."""
    patch = _patch_for("docs/readme.md", added_line="Set your API key via the SK-prefixed env var.")
    assert validate_patch(patch).ok is True


def test_size_limit_is_enforced():
    """An oversized patch is refused rather than pushed."""
    big = _CLEAN_PATCH + ("+" + "x" * 200 + "\n") * 100
    result = validate_patch(big, max_bytes=1000)
    assert result.ok is False
    assert "byte limit" in result.reason


def test_file_count_limit_is_enforced():
    """A patch touching too many files is refused."""
    patch = "".join(_patch_for(f"src/file_{index}.py") for index in range(5))
    result = validate_patch(patch, max_files=3)
    assert result.ok is False
    assert "over the 3 limit" in result.reason
    assert len(result.changed_files) == 5


def test_default_file_limit_is_generous_enough_for_real_work():
    """A realistic multi-file change is not blocked by the default cap."""
    patch = "".join(_patch_for(f"src/file_{index}.py") for index in range(50))
    assert MAX_CHANGED_FILES >= 50
    assert validate_patch(patch).ok is True


def test_renames_are_tracked():
    """Rename targets count as changed files (format-patch preserves them)."""
    patch = (
        "diff --git a/old.py b/new.py\n"
        "similarity index 100%\n"
        "rename from old.py\n"
        "rename to new.py\n"
    )
    result = validate_patch(patch)
    assert "new.py" in result.changed_files
    assert result.ok is True


def test_rename_into_github_is_blocked():
    """Renaming a file *into* .github/ is caught like a direct edit."""
    patch = (
        "diff --git a/script.yml b/script.yml\n"
        "similarity index 100%\n"
        "rename from script.yml\n"
        "rename to .github/workflows/evil.yml\n"
    )
    result = validate_patch(patch)
    assert result.ok is False
    assert result.requires_human_release is True


def test_multiple_violations_are_all_reported():
    """The gate reports every problem, not just the first."""
    patch = _patch_for(".github/workflows/ci.yml", added_line=f'token: "ghp_{"a" * 36}"')
    result = validate_patch(patch)
    assert result.ok is False
    assert "human release" in result.reason
    assert "github_token" in result.reason


def test_branch_name_is_namespaced_and_sanitized():
    """Publication targets exactly one predictable branch per job."""
    assert branch_name_for("ajob_abc123") == "agent/ajob_abc123"


@pytest.mark.parametrize(
    "job_id",
    ["ajob_abc123", "weird/../id", "trailing.", "..", "with spaces", "-leading", ""],
)
def test_branch_names_are_always_valid_git_refs(job_id):
    """No job id can produce a ref name git would reject.

    git refuses refs containing '..', ending in '.', or with empty components,
    so the sanitizer must never emit one — otherwise publication fails at the
    push with a confusing error instead of being caught here.
    """
    name = branch_name_for(job_id)
    assert name.startswith("agent/")
    suffix = name[len("agent/") :]
    assert suffix
    assert ".." not in name
    assert not name.endswith(".")
    assert " " not in name


def test_credential_in_a_filename_is_caught():
    """A secret can hide in a path, which is never an added-content line.

    Regression: the scan only looked at `+` lines, so a patch adding a file
    *named* after a token got the credential pushed as part of the tree.
    """
    patch = (
        "diff --git a/ghp_0123456789abcdefghij.txt b/ghp_0123456789abcdefghij.txt\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/ghp_0123456789abcdefghij.txt\n"
        "@@ -0,0 +1 @@\n"
        "+harmless content\n"
    )
    result = validate_patch(patch)
    assert not result.ok
    assert any("credential-shaped" in violation for violation in result.violations)
