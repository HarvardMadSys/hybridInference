"""Advance the predeclared HybridInference follow-up PR sequences."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml  # type: ignore[import-untyped]


class SequencerError(RuntimeError):
    """A deterministic sequencer check failed."""


class ValidationFailure(SequencerError):
    """A reconstructed unit is not safe to publish."""


@dataclass(frozen=True)
class PullRequest:
    """The GitHub fields needed for state-machine decisions."""

    number: int
    state: str
    merged_at: str | None
    head_ref_name: str
    base_ref_name: str
    url: str
    title: str = ""
    head_sha: str | None = None

    @property
    def is_merged(self) -> bool:
        return self.merged_at is not None

    @property
    def status(self) -> str:
        if self.is_merged:
            return "MERGED"
        return self.state.upper()


@dataclass(frozen=True)
class Unit:
    """One isolated, ordered follow-up unit."""

    thread_id: str
    unit_id: str
    branch: str
    source_base: str
    source_tip: str
    title: str
    body: str
    validation: tuple[tuple[str, ...], ...]
    source_ref: str = ""


@dataclass(frozen=True)
class Thread:
    """A foundation PR followed by deterministic successor units."""

    thread_id: str
    foundation_pr: int
    foundation_branch: str
    units: tuple[Unit, ...]


@dataclass(frozen=True)
class Manifest:
    """Validated sequencer configuration."""

    upstream_repository: str
    fork_owner: str
    fork_repository: str
    base_ref: str
    upstream_remote: str
    fork_remote: str
    threads: tuple[Thread, ...]


@dataclass(frozen=True)
class Plan:
    """The one state-machine action permitted for a thread this run."""

    thread: Thread
    action: str
    current: PullRequest | None
    next_unit: Unit | None
    existing: PullRequest | None
    reason: str


class GitHubClient(Protocol):
    """The API surface used by planning and publication."""

    def get_pr(self, number: int) -> PullRequest: ...

    def list_head_prs(self, branch: str) -> list[PullRequest]: ...

    def create_pr(self, unit: Unit, manifest: Manifest) -> str: ...


def _required(mapping: Mapping[str, Any], key: str, context: str) -> Any:
    if key not in mapping:
        raise SequencerError(f"{context} is missing required field {key!r}")
    return mapping[key]


def _no_placeholders(value: str, context: str) -> str:
    if "{{" in value or "}}" in value or "<" in value or ">" in value:
        raise SequencerError(f"{context} contains a placeholder: {value!r}")
    return value


def load_manifest(path: Path) -> Manifest:
    """Load and validate the complete, non-LLM sequence declaration."""

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise SequencerError(f"manifest {path} must contain a mapping")
    upstream = _required(raw, "upstream", "manifest")
    if not isinstance(upstream, Mapping):
        raise SequencerError("manifest upstream must be a mapping")

    threads_raw = _required(raw, "threads", "manifest")
    if not isinstance(threads_raw, list):
        raise SequencerError("manifest threads must be a list")

    threads: list[Thread] = []
    seen_threads: set[str] = set()
    seen_branches: set[str] = set()
    for thread_index, thread_raw in enumerate(threads_raw):
        context = f"thread[{thread_index}]"
        if not isinstance(thread_raw, Mapping):
            raise SequencerError(f"{context} must be a mapping")
        thread_id = _no_placeholders(str(_required(thread_raw, "id", context)), context)
        if thread_id in seen_threads:
            raise SequencerError(f"duplicate thread id {thread_id!r}")
        seen_threads.add(thread_id)

        foundation = _required(thread_raw, "foundation", context)
        if not isinstance(foundation, Mapping):
            raise SequencerError(f"{context}.foundation must be a mapping")
        foundation_pr = int(_required(foundation, "pr", f"{context}.foundation"))
        foundation_branch = _no_placeholders(
            str(_required(foundation, "branch", f"{context}.foundation")),
            f"{context}.foundation.branch",
        )

        units_raw = _required(thread_raw, "units", context)
        if not isinstance(units_raw, list) or not units_raw:
            raise SequencerError(f"{context}.units must be a non-empty list")
        units: list[Unit] = []
        for unit_index, unit_raw in enumerate(units_raw):
            unit_context = f"{context}.units[{unit_index}]"
            if not isinstance(unit_raw, Mapping):
                raise SequencerError(f"{unit_context} must be a mapping")
            unit_id = _no_placeholders(str(_required(unit_raw, "id", unit_context)), unit_context)
            branch = _no_placeholders(
                str(_required(unit_raw, "branch", unit_context)),
                f"{unit_context}.branch",
            )
            if branch in seen_branches:
                raise SequencerError(f"duplicate unit branch {branch!r}")
            seen_branches.add(branch)
            source_base = _no_placeholders(
                str(_required(unit_raw, "source_base", unit_context)),
                f"{unit_context}.source_base",
            )
            source_tip = _no_placeholders(
                str(_required(unit_raw, "source_tip", unit_context)),
                f"{unit_context}.source_tip",
            )
            source_ref_raw = unit_raw.get("source_ref")
            source_ref = (
                ""
                if source_ref_raw is None
                else _no_placeholders(str(source_ref_raw), f"{unit_context}.source_ref")
            )
            validation_raw = _required(unit_raw, "validation", unit_context)
            if not isinstance(validation_raw, list) or not all(
                isinstance(command, list)
                and command
                and all(isinstance(arg, str) for arg in command)
                for command in validation_raw
            ):
                raise SequencerError(f"{unit_context}.validation must be a list of argv lists")
            units.append(
                Unit(
                    thread_id=thread_id,
                    unit_id=unit_id,
                    branch=branch,
                    source_base=source_base,
                    source_tip=source_tip,
                    title=str(_required(unit_raw, "title", unit_context)),
                    body=str(_required(unit_raw, "body", unit_context)),
                    validation=tuple(tuple(command) for command in validation_raw),
                    source_ref=source_ref,
                )
            )
        threads.append(
            Thread(
                thread_id=thread_id,
                foundation_pr=foundation_pr,
                foundation_branch=foundation_branch,
                units=tuple(units),
            )
        )

    return Manifest(
        upstream_repository=_no_placeholders(
            str(_required(upstream, "repository", "manifest.upstream")),
            "manifest.upstream.repository",
        ),
        fork_owner=_no_placeholders(
            str(_required(upstream, "fork_owner", "manifest.upstream")),
            "manifest.upstream.fork_owner",
        ),
        fork_repository=_no_placeholders(
            str(_required(upstream, "fork_repository", "manifest.upstream")),
            "manifest.upstream.fork_repository",
        ),
        base_ref=_no_placeholders(
            str(_required(upstream, "base_ref", "manifest.upstream")),
            "manifest.upstream.base_ref",
        ),
        upstream_remote=_no_placeholders(
            str(_required(upstream, "upstream_remote", "manifest.upstream")),
            "manifest.upstream.upstream_remote",
        ),
        fork_remote=_no_placeholders(
            str(_required(upstream, "fork_remote", "manifest.upstream")),
            "manifest.upstream.fork_remote",
        ),
        threads=tuple(threads),
    )


def pull_request_from_api(raw: Mapping[str, Any]) -> PullRequest:
    """Convert REST or `gh pr list` naming to one PR record."""

    head = raw.get("head")
    base = raw.get("base")
    head_map = head if isinstance(head, Mapping) else {}
    base_map = base if isinstance(base, Mapping) else {}
    return PullRequest(
        number=int(raw["number"]),
        state=str(raw.get("state", "UNKNOWN")),
        merged_at=raw.get("merged_at", raw.get("mergedAt")),
        head_ref_name=str(raw.get("head_ref", raw.get("headRefName", head_map.get("ref", "")))),
        base_ref_name=str(raw.get("base_ref", raw.get("baseRefName", base_map.get("ref", "")))),
        url=str(raw.get("html_url", raw.get("url", ""))),
        title=str(raw.get("title", "")),
        head_sha=raw.get("head_sha", raw.get("headSha", head_map.get("sha"))),
    )


def plan_thread(thread: Thread, github: GitHubClient) -> Plan:
    """Return at most one publication action for a thread."""

    current = github.get_pr(thread.foundation_pr)
    if current.head_ref_name != thread.foundation_branch or current.base_ref_name != "dev":
        return Plan(
            thread,
            "halt",
            current,
            None,
            None,
            "foundation PR head/base does not match the manifest",
        )
    if current.status == "OPEN":
        return Plan(thread, "noop", current, thread.units[0], None, "foundation PR is still open")
    if not current.is_merged:
        return Plan(
            thread,
            "halt",
            current,
            None,
            None,
            "foundation PR is closed without a merge",
        )

    for unit in thread.units:
        matches = github.list_head_prs(unit.branch)
        if len(matches) > 1:
            return Plan(
                thread,
                "halt",
                current,
                unit,
                None,
                f"multiple upstream PRs use {unit.branch}; manual reconciliation required",
            )
        if not matches:
            return Plan(
                thread,
                "publish",
                current,
                unit,
                None,
                "next unit is eligible and has no upstream PR",
            )

        existing = matches[0]
        if existing.head_ref_name != unit.branch or existing.base_ref_name != "dev":
            return Plan(
                thread,
                "halt",
                current,
                unit,
                existing,
                "successor PR head/base does not match the manifest",
            )
        if existing.status == "OPEN":
            return Plan(thread, "adopt", current, unit, existing, "successor PR already exists")
        if not existing.is_merged:
            return Plan(
                thread,
                "halt",
                current,
                unit,
                existing,
                "successor PR is closed without a merge",
            )
        current = existing

    return Plan(thread, "complete", current, None, None, "all declared units are merged")


class CommandRunner:
    """Small subprocess wrapper that never invokes a shell."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        input_data: str | bytes | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
        text_mode = not isinstance(input_data, bytes)
        result = subprocess.run(
            list(argv),
            cwd=cwd,
            input=input_data,
            capture_output=True,
            text=text_mode,
            check=False,
        )
        if check and result.returncode:
            stderr = (
                result.stderr.decode(errors="replace")
                if isinstance(result.stderr, bytes)
                else result.stderr
            )
            raise SequencerError(
                f"command failed ({result.returncode}): {' '.join(argv)}\n{stderr.strip()}"
            )
        return result


class GitHub(CommandRunner):
    """`gh`-backed upstream PR client."""

    def __init__(self, root: Path, manifest: Manifest):
        self.root = root
        self.manifest = manifest

    def _gh_json(self, argv: Sequence[str]) -> Any:
        result = self.run(["gh", *argv], cwd=self.root)
        return json.loads(str(result.stdout))

    def get_pr(self, number: int) -> PullRequest:
        raw = self._gh_json(["api", f"repos/{self.manifest.upstream_repository}/pulls/{number}"])
        if not isinstance(raw, Mapping):
            raise SequencerError(f"upstream PR #{number} response was not an object")
        return pull_request_from_api(raw)

    def list_head_prs(self, branch: str) -> list[PullRequest]:
        raw = self._gh_json(
            [
                "pr",
                "list",
                "--repo",
                self.manifest.upstream_repository,
                "--head",
                f"{self.manifest.fork_owner}:{branch}",
                "--state",
                "all",
                "--json",
                "number,state,mergedAt,headRefName,baseRefName,url,title",
            ]
        )
        if not isinstance(raw, list):
            raise SequencerError(f"head query for {branch} did not return a list")
        return [pull_request_from_api(item) for item in raw if isinstance(item, Mapping)]

    def create_pr(self, unit: Unit, manifest: Manifest) -> str:
        result = self.run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                manifest.upstream_repository,
                "--head",
                f"{manifest.fork_owner}:{unit.branch}",
                "--base",
                manifest.base_ref,
                "--title",
                unit.title,
                "--body-file",
                "-",
            ],
            cwd=self.root,
            input_data=unit.body,
        )
        url = str(result.stdout).strip()
        if not url:
            raise SequencerError(f"gh pr create returned no URL for {unit.branch}")
        return url


class Git(CommandRunner):
    """Git operations used for safe reconstruction and publication."""

    def __init__(self, root: Path):
        self.root = root

    def text(self, argv: Sequence[str], *, cwd: Path | None = None) -> str:
        result = self.run(["git", *argv], cwd=cwd or self.root)
        return str(result.stdout).strip()

    def binary_output(self, argv: Sequence[str], *, cwd: Path | None = None) -> bytes:
        result = self.run(["git", *argv], cwd=cwd or self.root, input_data=b"")
        output = result.stdout
        if not isinstance(output, bytes):
            raise SequencerError("expected byte output from git")
        return output

    def ensure_clean(self) -> None:
        if self.text(["status", "--porcelain"]):
            raise SequencerError("sequencer checkout must be clean before it runs")

    def resolve(self, ref: str) -> str:
        return self.text(["rev-parse", "--verify", ref])

    def expected_paths(self, source_base: str, source_tip: str) -> set[str]:
        output = self.text(["diff", "--name-only", source_base, source_tip])
        return {line for line in output.splitlines() if line}

    def source_patch(self, source_base: str, source_tip: str) -> bytes:
        return self.binary_output(["diff", "--binary", source_base, source_tip])

    def staged_paths(self, cwd: Path) -> set[str]:
        output = self.text(["diff", "--cached", "--name-only"], cwd=cwd)
        return {line for line in output.splitlines() if line}

    def verify_source_reference(self, unit: Unit, manifest: Manifest) -> str:
        """Resolve a durable source ref and enforce its immutable manifest tip."""

        source_ref = unit.source_ref or f"{manifest.fork_remote}/{unit.branch}"
        source_tip = self.resolve(source_ref)
        if source_tip != unit.source_tip:
            raise ValidationFailure(
                f"{unit.branch} tip changed: manifest {unit.source_tip}, fetched {source_tip}"
            )
        return source_ref

    @contextmanager
    def candidate_worktree(self, base_ref: str):
        with tempfile.TemporaryDirectory(prefix="hybridinference-sequencer-") as directory:
            candidate = Path(directory) / "tree"
            self.run(
                ["git", "worktree", "add", "--detach", str(candidate), base_ref], cwd=self.root
            )
            try:
                yield candidate
            finally:
                self.run(
                    ["git", "worktree", "remove", "--force", str(candidate)],
                    cwd=self.root,
                    check=False,
                )

    def reconstruct_and_validate(self, unit: Unit, manifest: Manifest) -> tuple[str, set[str], str]:
        self.verify_source_reference(unit, manifest)
        source_tip = unit.source_tip
        expected_paths = self.expected_paths(unit.source_base, source_tip)
        if not expected_paths:
            raise ValidationFailure(f"{unit.branch} source range is empty")
        patch = self.source_patch(unit.source_base, source_tip)

        with self.candidate_worktree(
            f"{manifest.upstream_remote}/{manifest.base_ref}"
        ) as candidate:
            self.run(["git", "apply", "--3way", "--index"], cwd=candidate, input_data=patch)
            if self.text(["ls-files", "-u"], cwd=candidate):
                raise ValidationFailure(f"{unit.branch} reconstruction left merge conflicts")
            actual_paths = self.staged_paths(candidate)
            validate_changed_paths(expected_paths, actual_paths, unit.branch)
            self.run(["git", "diff", "--cached", "--check"], cwd=candidate)
            self.run(["git", "diff", "HEAD", "--check"], cwd=candidate)
            # The candidate is reconstructed on upstream/dev, while the
            # sequencer itself is fork-only automation. Validate the
            # sequencer on its own checkout; do not require it in the
            # upstream-based candidate worktree.
            if (candidate / "ops/sequential_prs.py").exists():
                self.run(["uv", "run", "mypy", "ops/sequential_prs.py"], cwd=candidate)
            for command in unit.validation:
                self.run(command, cwd=candidate)
            self.run(
                [
                    "git",
                    "-c",
                    "user.name=HybridInference Sequential PR Bot",
                    "-c",
                    "user.email=actions@users.noreply.github.com",
                    "commit",
                    "--no-verify",
                    "-m",
                    unit.title,
                ],
                cwd=candidate,
            )
            commit = self.text(["rev-parse", "HEAD"], cwd=candidate)
            stat = self.text(
                ["diff", "--stat", f"{manifest.upstream_remote}/{manifest.base_ref}...HEAD"],
                cwd=candidate,
            )
            return commit, actual_paths, stat

    def remote_branch_sha(self, remote: str, branch: str) -> str | None:
        output = self.text(["ls-remote", "--refs", remote, f"refs/heads/{branch}"])
        if not output:
            return None
        return output.split()[0]

    def push_candidate(self, manifest: Manifest, branch: str, commit: str) -> None:
        old_sha = self.remote_branch_sha(manifest.fork_remote, branch)
        if old_sha:
            self.run(
                [
                    "git",
                    "push",
                    f"--force-with-lease=refs/heads/{branch}:{old_sha}",
                    manifest.fork_remote,
                    f"{commit}:refs/heads/{branch}",
                ],
                cwd=self.root,
            )
        else:
            self.run(
                ["git", "push", manifest.fork_remote, f"{commit}:refs/heads/{branch}"],
                cwd=self.root,
            )


def validate_changed_paths(expected: set[str], actual: set[str], branch: str) -> None:
    """Reject predecessor/later/unrelated files introduced during rebase."""

    if expected != actual:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValidationFailure(f"{branch} changed-path drift: missing={missing!r} extra={extra!r}")


def render_plan(plan: Plan, *, dry_run: bool) -> str:
    """Render the required per-thread dry-run/publication summary."""

    current = f"#{plan.current.number} {plan.current.status}" if plan.current else "none"
    next_unit = plan.next_unit.unit_id if plan.next_unit else "none"
    normalize = "yes" if plan.action == "publish" else "no"
    push = "yes" if plan.action == "publish" and not dry_run else "no"
    create = "yes" if plan.action == "publish" and not dry_run else "no"
    if dry_run and plan.action == "publish":
        push = create = "would"
    return (
        f"[{plan.thread.thread_id}] current={current} next={next_unit} "
        f"would_normalize={normalize} would_push={push} would_create_pr={create} "
        f"reason={plan.reason}"
    )


def execute_plan(
    plan: Plan,
    *,
    git: Git,
    github: GitHubClient,
    manifest: Manifest,
    dry_run: bool,
) -> str | None:
    """Validate and optionally publish one planned unit."""

    if plan.next_unit is None:
        return None
    unit = plan.next_unit

    if dry_run:
        source_ref = git.verify_source_reference(unit, manifest)
        print(f"[{plan.thread.thread_id}] source={source_ref}@{unit.source_tip} reachable")

    if plan.action != "publish":
        return None

    # A second duplicate check closes the validation-time race before any push.
    existing = github.list_head_prs(unit.branch)
    if existing:
        if len(existing) > 1:
            raise SequencerError(f"{unit.branch} gained multiple upstream PRs during validation")
        print(f"[{plan.thread.thread_id}] adopted PR #{existing[0].number} during validation")
        return existing[0].url

    commit, actual_paths, stat = git.reconstruct_and_validate(unit, manifest)
    print(f"[{plan.thread.thread_id}] candidate={commit} paths={sorted(actual_paths)!r}")
    print(f"[{plan.thread.thread_id}] diff={stat or '(empty)'}")
    if dry_run:
        return None

    # Publication is still guarded by a duplicate check after reconstruction.
    existing = github.list_head_prs(unit.branch)
    if existing:
        if len(existing) > 1:
            raise SequencerError(f"{unit.branch} gained multiple upstream PRs before push")
        print(f"[{plan.thread.thread_id}] adopted PR #{existing[0].number} before push")
        return existing[0].url
    git.push_candidate(manifest, unit.branch, commit)
    existing = github.list_head_prs(unit.branch)
    if existing:
        print(f"[{plan.thread.thread_id}] adopted PR #{existing[0].number} after push")
        return existing[0].url
    url = github.create_pr(unit, manifest)
    print(f"[{plan.thread.thread_id}] created {url}")
    return url


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path.cwd()
    try:
        manifest = load_manifest(args.manifest)
        git = Git(root)
        git.ensure_clean()
        github = GitHub(root, manifest)
    except (OSError, SequencerError, yaml.YAMLError) as exc:
        print(f"sequencer setup failed: {exc}", file=sys.stderr)
        return 1

    failures = 0
    for thread in manifest.threads:
        try:
            plan = plan_thread(thread, github)
            print(render_plan(plan, dry_run=args.dry_run))
            execute_plan(
                plan,
                git=git,
                github=github,
                manifest=manifest,
                dry_run=args.dry_run,
            )
        except (OSError, SequencerError, subprocess.SubprocessError) as exc:
            failures += 1
            print(f"[{thread.thread_id}] HALT: {exc}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
