"""State-machine and safety tests for sequential follow-up publication."""

from __future__ import annotations

from pathlib import Path

import pytest

from ops.sequential_prs import (
    GitHub,
    Manifest,
    PullRequest,
    Thread,
    Unit,
    ValidationFailure,
    execute_plan,
    plan_thread,
    validate_changed_paths,
)


class FakeGitHub:
    def __init__(
        self, foundations: dict[int, PullRequest], heads: dict[str, list[PullRequest]] | None = None
    ):
        self.foundations = foundations
        self.heads = heads or {}

    def get_pr(self, number: int) -> PullRequest:
        return self.foundations[number]

    def list_head_prs(self, branch: str) -> list[PullRequest]:
        return list(self.heads.get(branch, []))

    def create_pr(self, unit: Unit, manifest):  # pragma: no cover - planner never creates
        raise AssertionError("planner must not create PRs")


class FailingGit:
    def __init__(self) -> None:
        self.pushed = False

    def reconstruct_and_validate(self, unit: Unit, manifest: Manifest):
        raise ValidationFailure("focused validation failed")

    def push_candidate(self, manifest: Manifest, branch: str, commit: str) -> None:
        self.pushed = True


class VerifyingGit:
    def __init__(self) -> None:
        self.refs: list[str] = []

    def verify_source_reference(self, unit: Unit, manifest: Manifest) -> str:
        self.refs.append(unit.branch)
        return f"origin/{unit.branch}"


def test_dry_run_verifies_next_source_for_open_foundation() -> None:
    thread = make_thread("1419", 1419, ("prep/1419-a",))
    github = FakeGitHub({1419: pr(1419)})
    plan = plan_thread(thread, github)
    git = VerifyingGit()
    manifest = Manifest(
        "HarvardMadSys/hybridInference",
        "B-A-M-N",
        "hybridInference",
        "dev",
        "upstream",
        "origin",
        (thread,),
    )

    assert execute_plan(plan, git=git, github=github, manifest=manifest, dry_run=True) is None
    assert git.refs == ["prep/1419-a"]


def pr(
    number: int,
    state: str = "OPEN",
    merged_at: str | None = None,
    branch: str | None = None,
) -> PullRequest:
    return PullRequest(
        number,
        state,
        merged_at,
        branch or f"foundation/{number}",
        "dev",
        f"https://example/{number}",
    )


def unit(thread_id: str, unit_id: str, branch: str) -> Unit:
    return Unit(thread_id, unit_id, branch, "base", "tip", unit_id, unit_id, ())


def make_thread(thread_id: str, foundation_pr: int, branches: tuple[str, ...]) -> Thread:
    return Thread(
        thread_id,
        foundation_pr,
        f"foundation/{foundation_pr}",
        tuple(unit(thread_id, f"unit-{index}", branch) for index, branch in enumerate(branches)),
    )


def test_case_1_only_merged_1419_is_eligible() -> None:
    threads = [
        make_thread("1419", 1419, ("prep/1419-a", "prep/1419-b")),
        make_thread("1427", 1427, ("prep/1427-a",)),
        make_thread("1428", 1428, ("prep/1428-a",)),
        make_thread("1430", 1430, ("prep/1430-a",)),
    ]
    github = FakeGitHub(
        {1419: pr(1419, "CLOSED", "merged"), 1427: pr(1427), 1428: pr(1428), 1430: pr(1430)}
    )

    plans = [plan_thread(thread, github) for thread in threads]

    assert [plan.action for plan in plans] == ["publish", "noop", "noop", "noop"]
    assert plans[0].next_unit is not None
    assert plans[0].next_unit.branch == "prep/1419-a"


def test_case_2_independent_merged_foundations_each_publish_one_next_unit() -> None:
    threads = [
        make_thread("1419", 1419, ("prep/1419-a",)),
        make_thread("1430", 1430, ("prep/1430-a",)),
    ]
    github = FakeGitHub({1419: pr(1419, "CLOSED", "merged"), 1430: pr(1430, "CLOSED", "merged")})

    assert [plan_thread(thread, github).action for thread in threads] == ["publish", "publish"]


def test_case_3_closed_without_merge_halts_only_that_thread() -> None:
    thread = make_thread("1427", 1427, ("prep/1427-a",))
    plan = plan_thread(thread, FakeGitHub({1427: pr(1427, "CLOSED")}))

    assert plan.action == "halt"
    assert "without a merge" in plan.reason


def test_case_4_open_successor_is_adopted_without_duplicate() -> None:
    thread = make_thread("1419", 1419, ("prep/1419-a",))
    existing = pr(2000, branch="prep/1419-a")
    github = FakeGitHub({1419: pr(1419, "CLOSED", "merged")}, {"prep/1419-a": [existing]})

    plan = plan_thread(thread, github)

    assert plan.action == "adopt"
    assert plan.existing == existing


def test_case_5_never_publishes_two_stages_in_one_plan() -> None:
    thread = make_thread("1419", 1419, ("prep/1419-a", "prep/1419-b", "prep/1419-c"))
    github = FakeGitHub(
        {1419: pr(1419, "CLOSED", "merged")},
        {"prep/1419-a": [pr(2001, "CLOSED", "merged", "prep/1419-a")]},
    )

    plan = plan_thread(thread, github)

    assert plan.action == "publish"
    assert plan.next_unit is not None
    assert plan.next_unit.branch == "prep/1419-b"
    # The planner returns exactly one next unit; it does not continue to c.


def test_case_6_scope_is_checked_against_the_isolated_unit_patch() -> None:
    validate_changed_paths(
        {"apps/backend/routing/routers.py"}, {"apps/backend/routing/routers.py"}, "unit"
    )
    with pytest.raises(Exception, match="changed-path drift"):
        validate_changed_paths({"unit.py"}, {"unit.py", "predecessor.py"}, "unit")


def test_case_7_validation_failure_blocks_push_and_pr_creation() -> None:
    thread = make_thread("1419", 1419, ("prep/1419-a",))
    github = FakeGitHub({1419: pr(1419, "CLOSED", "merged")})
    plan = plan_thread(thread, github)
    git = FailingGit()
    manifest = Manifest(
        "HarvardMadSys/hybridInference",
        "B-A-M-N",
        "hybridInference",
        "dev",
        "upstream",
        "origin",
        (thread,),
    )

    with pytest.raises(ValidationFailure, match="focused validation failed"):
        execute_plan(plan, git=git, github=github, manifest=manifest, dry_run=False)

    assert git.pushed is False


def test_case_8_workflow_serializes_publication() -> None:
    root = Path(__file__).parents[3]
    workflow = (root / ".github/workflows/sequential-follow-up-prs.yml").read_text()

    assert "group: hybridinference-sequential-prs" in workflow
    assert "cancel-in-progress: false" in workflow
    assert 'cron: "*/10 * * * *"' in workflow
    assert 'test "${base}" = dev' in workflow
    assert 'test "${state}" = open' not in workflow


def test_github_head_query_filters_fork_owner(monkeypatch, tmp_path: Path) -> None:
    thread = make_thread("1419", 1419, ("prep/1419-a",))
    manifest = Manifest(
        "HarvardMadSys/hybridInference",
        "B-A-M-N",
        "hybridInference",
        "dev",
        "upstream",
        "origin",
        (thread,),
    )
    github = GitHub(tmp_path, manifest)
    seen: list[list[str]] = []

    def fake_gh_json(argv):
        seen.append(list(argv))
        return [
            {
                "number": 2001,
                "state": "OPEN",
                "headRefName": "prep/1419-a",
                "baseRefName": "dev",
                "url": "https://example/fork",
                "title": "fork",
                "headRepositoryOwner": {"login": "B-A-M-N"},
            },
            {
                "number": 2002,
                "state": "OPEN",
                "headRefName": "prep/1419-a",
                "baseRefName": "dev",
                "url": "https://example/other",
                "title": "other",
                "headRepositoryOwner": {"login": "someone-else"},
            },
        ]

    monkeypatch.setattr(github, "_gh_json", fake_gh_json)

    matches = github.list_head_prs("prep/1419-a")

    assert [match.number for match in matches] == [2001]
    assert seen[0][seen[0].index("--head") + 1] == "prep/1419-a"
