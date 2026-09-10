from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

import purplemux_client.issue_driven as issue_driven
import purplemux_client.operations as operations
from purplemux_client import (
    GitHubRepository,
    IncompletePullRequestEnumeration,
    MutationOutcomeUnknown,
    PullRequestTopologyError,
    WorkerFailure,
)

HEAD_SHA = "h" * 40
BASE_SHA = "b" * 40
MERGE_SHA = "m" * 40


def pr_data(
    number: int,
    *,
    head: str = "feature/65",
    base: str = "dev/v0.1.4",
    head_sha: str = HEAD_SHA,
    base_sha: str = BASE_SHA,
    state: str = "open",
    draft: bool = True,
    body: str = "",
    auto_merge: object = None,
    merge_sha: str | None = None,
) -> dict[str, object]:
    merged = state == "merged"
    return {
        "number": number,
        "html_url": f"https://github.com/acme/project/pull/{number}",
        "state": "closed" if merged else state,
        "draft": draft,
        "node_id": f"PR_{number}",
        "body": body,
        "merged": merged,
        "merged_at": "2026-01-01T00:00:00Z" if merged else None,
        "merge_commit_sha": merge_sha,
        "auto_merge": auto_merge,
        "head": {
            "ref": head,
            "sha": head_sha,
            "repo": {"full_name": "acme/project"},
        },
        "base": {
            "ref": base,
            "sha": base_sha,
            "repo": {"full_name": "acme/project"},
        },
    }


class FakeGitHubRunner:
    def __init__(
        self,
        prs: list[dict[str, object]] | None = None,
        *,
        delay_seconds: float = 0,
    ) -> None:
        self.prs = prs or []
        self.delay_seconds = delay_seconds
        self.refs = {"feature/65": HEAD_SHA, "dev/v0.1.4": BASE_SHA}
        self.queue_entry: object = None
        self.mutation_outcome = "success"
        self.concurrent_wrong_base = False
        self.calls: list[list[str]] = []

    def __call__(
        self,
        args: Sequence[str],
        *,
        capture_output: bool,
        text: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        command = list(args)
        self.calls.append(command)
        assert capture_output and text and not check
        if command[1:4] == ["auth", "status", "--hostname"]:
            return self._done({"ok": True})
        if command[1:3] == ["api", "repos/acme/project"]:
            return self._done({"full_name": "acme/project"})
        if len(command) >= 3 and command[1] == "api" and "pulls?" in command[2]:
            endpoint = command[2]
            page = int(re.search(r"[?&]page=(\d+)", endpoint).group(1))  # type: ignore[union-attr]
            per_page = int(re.search(r"[?&]per_page=(\d+)", endpoint).group(1))  # type: ignore[union-attr]
            if "state=all" in endpoint:
                matching = self.prs
            else:
                requested_open = "state=open" in endpoint
                matching = [
                    item
                    for item in self.prs
                    if (item["state"] == "open") is requested_open
                ]
            requested_head = parse_qs(urlsplit(endpoint).query).get("head")
            if requested_head:
                head_branch = requested_head[0].split(":", 1)[1]
                matching = [
                    item
                    for item in matching
                    if isinstance(item.get("head"), dict)
                    and item["head"].get("ref") == head_branch  # type: ignore[union-attr]
                ]
            start = (page - 1) * per_page
            return self._done(matching[start : start + per_page])
        if len(command) >= 3 and command[1] == "api" and "/compare/" in command[2]:
            return self._done({"status": "ahead"})
        if len(command) >= 3 and command[1] == "api" and "/pulls/" in command[2]:
            endpoint = command[2]
            if endpoint.endswith("/merge") and "--method" in command:
                number = int(endpoint.split("/")[-2])
                item = self._find(number)
                item["state"] = "closed"
                item["merged"] = True
                item["merged_at"] = "2026-01-01T00:00:00Z"
                item["draft"] = False
                item["merge_commit_sha"] = MERGE_SHA
                item["base"] = {
                    "ref": "dev/v0.1.4",
                    "sha": MERGE_SHA,
                    "repo": {"full_name": "acme/project"},
                }
                self.refs["dev/v0.1.4"] = MERGE_SHA
                return self._mutation_result({"merged": True, "sha": MERGE_SHA})
            return self._done(self._find(int(endpoint.rsplit("/", 1)[1])))
        if len(command) >= 3 and command[1:3] == ["api", "graphql"]:
            query = next(value[6:] for value in command if value.startswith("query="))
            if "mergeQueueEntry" in query:
                return self._done(
                    {"data": {"node": {"mergeQueueEntry": self.queue_entry}}}
                )
            number = int(
                next(
                    value.rsplit("_", 1)[1]
                    for value in command
                    if value.startswith("id=PR_")
                )
            )
            item = self._find(number)
            item["draft"] = "convertPullRequestToDraft" in query
            return self._mutation_result({"data": {"ok": True}})
        if (
            len(command) >= 3
            and command[1] == "api"
            and "/git/ref/heads/" in command[2]
        ):
            branch = command[2].split("/git/ref/heads/", 1)[1].replace("%2F", "/")
            return self._done({"object": {"sha": self.refs[branch]}})
        if len(command) >= 3 and command[1] == "api" and "/git/commits/" in command[2]:
            return self._done({"parents": [{"sha": BASE_SHA}, {"sha": HEAD_SHA}]})
        if (
            len(command) >= 5
            and command[1:4] == ["api", "--method", "PUT"]
            and command[4].endswith("/merge")
        ):
            number = int(command[4].split("/")[-2])
            item = self._find(number)
            item["state"] = "closed"
            item["merged"] = True
            item["merged_at"] = "2026-01-01T00:00:00Z"
            item["draft"] = False
            item["merge_commit_sha"] = MERGE_SHA
            item["base"] = {
                "ref": "dev/v0.1.4",
                "sha": MERGE_SHA,
                "repo": {"full_name": "acme/project"},
            }
            self.refs["dev/v0.1.4"] = MERGE_SHA
            return self._mutation_result({"merged": True, "sha": MERGE_SHA})
        if (
            len(command) >= 4
            and command[1:3] == ["api", "--method"]
            and command[3] == "POST"
        ):
            fields = {
                command[index + 1].split("=", 1)[0]: command[index + 1].split("=", 1)[1]
                for index, value in enumerate(command)
                if value in {"-f", "-F"}
            }
            created = pr_data(
                max((int(item["number"]) for item in self.prs), default=0) + 1,
                head=fields["head"],
                base=fields["base"],
                body=fields["body"],
            )
            self.prs.append(created)
            if self.concurrent_wrong_base:
                self.prs.append(pr_data(int(created["number"]) + 1, base="main"))
            return self._mutation_result(created)
        if len(command) >= 5 and command[1:4] == ["api", "--method", "PATCH"]:
            number = int(command[4].rsplit("/", 1)[1])
            item = self._find(number)
            item["body"] = next(
                command[index + 1].split("=", 1)[1]
                for index, value in enumerate(command)
                if value == "-f" and command[index + 1].startswith("body=")
            )
            return self._mutation_result(item)
        raise AssertionError(f"unexpected command: {command}")

    def _mutation_result(self, data: object) -> subprocess.CompletedProcess[str]:
        outcome = self.mutation_outcome
        self.mutation_outcome = "success"
        if outcome == "timeout_after_apply":
            raise subprocess.TimeoutExpired(["gh"], 30)
        if outcome == "interrupt_after_apply":
            raise KeyboardInterrupt
        if outcome == "malformed_after_apply":
            return subprocess.CompletedProcess([], 0, "not-json", "")
        if outcome == "nonzero_after_apply":
            return subprocess.CompletedProcess([], 1, "", "transport closed")
        if outcome == "reject":
            return subprocess.CompletedProcess([], 1, "", "gh: rejected (HTTP 422)")
        return self._done(data)

    def _find(self, number: int) -> dict[str, object]:
        return next(item for item in self.prs if item["number"] == number)

    @staticmethod
    def _done(data: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, json.dumps(data), "")


def repository(runner: FakeGitHubRunner, **kwargs: int) -> GitHubRepository:
    return GitHubRepository.open(
        "acme/project",
        runner=runner,
        page_size=kwargs.get("page_size", 10),
        max_pages=kwargs.get("max_pages", 3),
    )


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_transient_github_read_errors_retry_with_backoff_and_logging(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    runner = FakeGitHubRunner()
    original_runner = runner
    failures = 0
    sleeps: list[float] = []

    def transient_read(
        args: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal failures
        if len(args) >= 3 and "pulls?" in args[2] and failures < 2:
            failures += 1
            return subprocess.CompletedProcess(
                args, 1, "", f"gh: temporary failure (HTTP {status})"
            )
        return original_runner(args, **kwargs)  # type: ignore[arg-type]

    github = GitHubRepository.open(
        "acme/project",
        runner=transient_read,
        read_timeout_retries=2,
        read_retry_backoff_seconds=0.1,
        sleep=sleeps.append,
    )
    with caplog.at_level(logging.WARNING):
        assert (
            github.find_pr(head="feature/65", base="dev/v0.1.4", state="OPEN") is None
        )

    assert failures == 2
    assert sleeps == [0.1, 0.2]
    assert caplog.text.count(f"HTTP {status}") == 2
    assert "retrying attempt 2/3" in caplog.text
    assert "retrying attempt 3/3" in caplog.text


@pytest.mark.parametrize(
    "detail",
    [
        "gh: API rate limit exceeded for user ID 1. (HTTP 403)",
        "gh: You have exceeded a secondary rate limit. (HTTP 403)",
    ],
)
def test_rate_limit_specific_github_403_is_retried(detail: str) -> None:
    runner = FakeGitHubRunner()
    failures = 0
    sleeps: list[float] = []

    def rate_limited_read(
        args: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal failures
        if len(args) >= 3 and "pulls?" in args[2] and failures == 0:
            failures += 1
            return subprocess.CompletedProcess(args, 1, "", detail)
        return runner(args, **kwargs)  # type: ignore[arg-type]

    github = GitHubRepository.open(
        "acme/project",
        runner=rate_limited_read,
        read_retry_backoff_seconds=0.1,
        sleep=sleeps.append,
    )

    assert github.find_pr(head="feature/65", base="dev/v0.1.4", state="OPEN") is None
    assert failures == 1
    assert sleeps == [0.1]


def test_transient_github_read_exhaustion_preserves_last_error() -> None:
    runner = FakeGitHubRunner()
    failures = 0

    def unavailable_read(
        args: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal failures
        if len(args) >= 3 and "pulls?" in args[2]:
            failures += 1
            return subprocess.CompletedProcess(
                args, 1, "", f"gh: gateway failure {failures} (HTTP 504)"
            )
        return runner(args, **kwargs)  # type: ignore[arg-type]

    github = GitHubRepository.open(
        "acme/project",
        runner=unavailable_read,
        read_timeout_retries=2,
        read_retry_backoff_seconds=0,
    )

    with pytest.raises(WorkerFailure, match=r"gateway failure 3 \(HTTP 504\)"):
        github.find_pr(head="feature/65", base="dev/v0.1.4", state="OPEN")
    assert failures == 3


@pytest.mark.parametrize(
    "detail",
    [
        "gh: Not Found (HTTP 404)",
        "gh: Resource not accessible by personal access token (HTTP 403)",
    ],
)
def test_permanent_github_read_error_is_not_retried(detail: str) -> None:
    runner = FakeGitHubRunner()
    failures = 0

    def rejected_read(
        args: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal failures
        if len(args) >= 3 and "pulls?" in args[2]:
            failures += 1
            return subprocess.CompletedProcess(args, 1, "", detail)
        return runner(args, **kwargs)  # type: ignore[arg-type]

    github = GitHubRepository.open(
        "acme/project", runner=rejected_read, read_timeout_retries=2
    )

    with pytest.raises(WorkerFailure, match=re.escape(detail)):
        github.find_pr(head="feature/65", base="dev/v0.1.4", state="OPEN")
    assert failures == 1


def test_github_read_timeout_retry_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = FakeGitHubRunner()
    timed_out = False
    sleeps: list[float] = []

    def timeout_once(
        args: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal timed_out
        if len(args) >= 3 and "pulls?" in args[2] and not timed_out:
            timed_out = True
            raise subprocess.TimeoutExpired(args, 30)
        return runner(args, **kwargs)  # type: ignore[arg-type]

    github = GitHubRepository.open(
        "acme/project",
        runner=timeout_once,
        read_retry_backoff_seconds=0.1,
        sleep=sleeps.append,
    )

    with caplog.at_level(logging.WARNING):
        assert (
            github.find_pr(head="feature/65", base="dev/v0.1.4", state="OPEN") is None
        )
    assert sleeps == [0.1]
    assert "failure (timeout); retrying attempt 2/3" in caplog.text


def test_transient_github_mutation_error_is_not_retried() -> None:
    runner = FakeGitHubRunner([pr_data(1)])
    mutation_calls = 0

    def unavailable_mutation(
        args: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal mutation_calls
        if any("markPullRequestReadyForReview" in value for value in args):
            mutation_calls += 1
            return subprocess.CompletedProcess(
                args, 1, "", "gh: Service Unavailable (HTTP 503)"
            )
        return runner(args, **kwargs)  # type: ignore[arg-type]

    github = GitHubRepository.open(
        "acme/project", runner=unavailable_mutation, read_retry_backoff_seconds=0
    )

    with pytest.raises(MutationOutcomeUnknown):
        github.set_draft(
            1,
            draft=False,
            expected_head="feature/65",
            expected_head_sha=HEAD_SHA,
            expected_base="dev/v0.1.4",
            expected_base_sha=BASE_SHA,
        )
    assert mutation_calls == 1


def test_open_discovery_rejects_wrong_base_and_ambiguity() -> None:
    wrong = repository(FakeGitHubRunner([pr_data(1, base="main")]))
    for topology in (wrong, wrong.inspect_pr_snapshot(("feature/65",))):
        with pytest.raises(PullRequestTopologyError, match="wrong base"):
            topology.find_pr(head="feature/65", base="dev/v0.1.4", state="OPEN")

    duplicate = repository(FakeGitHubRunner([pr_data(1), pr_data(2)]))
    for topology in (duplicate, duplicate.inspect_pr_snapshot(("feature/65",))):
        with pytest.raises(PullRequestTopologyError, match="ambiguous"):
            topology.find_pr(head="feature/65", base="dev/v0.1.4", state="OPEN")


def test_bounded_all_pr_enumeration_and_commit_comparison_are_read_only() -> None:
    runner = FakeGitHubRunner(
        [pr_data(1), pr_data(2, state="merged", merge_sha=MERGE_SHA)]
    )
    repo = repository(runner)

    snapshot = repo.inspect_pr_snapshot(("feature/65",))
    comparison = repo.compare_commits(base_sha="a" * 40, head_sha="b" * 40)

    assert [pr.number for pr in snapshot.pull_requests] == [1, 2]
    assert (
        snapshot.require_pr(
            number=1,
            head="feature/65",
            base="dev/v0.1.4",
            state="OPEN",
            expected_head_sha=HEAD_SHA,
            expected_base_sha=BASE_SHA,
        ).number
        == 1
    )
    with pytest.raises(PullRequestTopologyError, match="PR head changed"):
        snapshot.require_pr(
            head="feature/65",
            base="dev/v0.1.4",
            expected_head_sha="a" * 40,
        )
    assert comparison == "ahead"
    assert all("--method" not in call for call in runner.calls)


def test_snapshot_scope_ignores_excess_unrelated_pr_history() -> None:
    unrelated = [pr_data(number, head=f"historical/{number}") for number in range(1001)]
    runner = FakeGitHubRunner([*unrelated, pr_data(2000)])

    snapshot = repository(runner, page_size=10, max_pages=3).inspect_pr_snapshot(
        ("feature/65",)
    )

    assert [pr.number for pr in snapshot.pull_requests] == [2000]
    pull_endpoints = [call[2] for call in runner.calls if "pulls?" in call[2]]
    assert len(pull_endpoints) == 1
    assert "head=acme%3Afeature%2F65" in pull_endpoints[0]


def test_snapshot_scope_ignores_unrelated_deleted_fork_pr() -> None:
    deleted_fork = pr_data(1, head="historical/deleted-fork")
    deleted_fork["head"] = {
        "ref": "historical/deleted-fork",
        "sha": HEAD_SHA,
        "repo": None,
    }
    runner = FakeGitHubRunner([deleted_fork, pr_data(2)])

    snapshot = repository(runner).inspect_pr_snapshot(("feature/65",))

    assert [pr.number for pr in snapshot.pull_requests] == [2]


def test_issue_validation_batches_multiple_branches_under_command_latency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heads = tuple(f"feature/{number}" for number in range(32))
    prs = [
        pr_data(number + 1, head=head, head_sha=f"{number + 1:040x}")
        for number, head in enumerate(heads)
    ]
    runner = FakeGitHubRunner(prs, delay_seconds=0.05)
    github = repository(runner)
    remote_shas = {
        "dev/v0.1.4": BASE_SHA,
        **{head: f"{number + 1:040x}" for number, head in enumerate(heads)},
    }
    git = SimpleNamespace(
        expected_github_slug="acme/project",
        inspect_remote_branches=lambda branches: {
            branch: remote_shas.get(branch) for branch in branches
        },
    )
    monkeypatch.setattr(
        issue_driven,
        "_inspect_repository_declaration",
        lambda **kwargs: SimpleNamespace(
            source_repository=Path("/repo"), base_sha=BASE_SHA
        ),
    )
    monkeypatch.setattr(issue_driven.GitRepository, "open", lambda *args, **kwargs: git)
    monkeypatch.setattr(
        issue_driven.GitHubRepository, "open", lambda *args, **kwargs: github
    )

    started = time.monotonic()
    states = issue_driven.inspect_issue_driven_topology(
        repo="acme/project",
        integration_branch="dev/v0.1.4",
        issues=tuple((number + 1, head) for number, head in enumerate(heads)),
    )
    elapsed = time.monotonic() - started

    assert len(states) == 32
    assert {state.classification for state in states} == {"recoverable"}
    assert elapsed < 1.5
    assert (
        sum(call[1:4] == ["auth", "status", "--hostname"] for call in runner.calls) == 1
    )


def test_find_none_requires_complete_bounded_enumeration() -> None:
    runner = FakeGitHubRunner([pr_data(1)])
    repo = repository(runner, page_size=1, max_pages=1)
    with pytest.raises(IncompletePullRequestEnumeration, match="safety bound"):
        repo.find_pr(head="feature/65", base="dev/v0.1.4", state="OPEN")


def test_require_pr_guards_both_reviewed_shas_and_deferred_merge() -> None:
    runner = FakeGitHubRunner([pr_data(1)])
    repo = repository(runner)
    with pytest.raises(PullRequestTopologyError, match="PR base changed"):
        repo.require_pr(
            number=1,
            head="feature/65",
            base="dev/v0.1.4",
            expected_head_sha=HEAD_SHA,
            expected_base_sha="x" * 40,
        )

    runner.queue_entry = {"id": "MQ_1", "state": "AWAITING_CHECKS"}
    queued = repo.require_pr(number=1, head="feature/65", base="dev/v0.1.4")
    assert queued.merge_queue_entry == "MQ_1"
    with pytest.raises(PullRequestTopologyError, match="merge queue"):
        repo.set_draft(
            1,
            draft=False,
            expected_head="feature/65",
            expected_head_sha=HEAD_SHA,
            expected_base="dev/v0.1.4",
            expected_base_sha=BASE_SHA,
        )


@pytest.mark.parametrize(
    "outcome",
    [
        "timeout_after_apply",
        "malformed_after_apply",
        "nonzero_after_apply",
        "interrupt_after_apply",
    ],
)
def test_ready_reconciles_response_loss_after_apply(outcome: str) -> None:
    runner = FakeGitHubRunner([pr_data(1)])
    repo = repository(runner)
    runner.mutation_outcome = outcome

    result = repo.set_draft(
        1,
        draft=False,
        expected_head="feature/65",
        expected_head_sha=HEAD_SHA,
        expected_base="dev/v0.1.4",
        expected_base_sha=BASE_SHA,
    )
    assert result.is_draft is False
    assert (
        sum("markPullRequestReadyForReview" in " ".join(call) for call in runner.calls)
        == 1
    )


def test_unchanged_after_possible_ready_dispatch_is_unknown() -> None:
    runner = FakeGitHubRunner([pr_data(1)])
    repo = repository(runner)

    def timeout_without_apply(
        args: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if any("markPullRequestReadyForReview" in value for value in args):
            raise subprocess.TimeoutExpired(["gh"], 30)
        return runner(args, **_kwargs)  # type: ignore[arg-type]

    repo._runner = timeout_without_apply  # type: ignore[assignment]
    with pytest.raises(MutationOutcomeUnknown, match="unknown"):
        repo.set_draft(
            1,
            draft=False,
            expected_head="feature/65",
            expected_head_sha=HEAD_SHA,
            expected_base="dev/v0.1.4",
            expected_base_sha=BASE_SHA,
        )


def test_interrupted_github_dispatch_with_unchanged_state_is_unknown() -> None:
    runner = FakeGitHubRunner([pr_data(1)])
    repo = repository(runner)
    interruption_calls = 0

    def interrupt_without_apply(
        args: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal interruption_calls
        if any("markPullRequestReadyForReview" in value for value in args):
            interruption_calls += 1
            raise KeyboardInterrupt
        return runner(args, **_kwargs)  # type: ignore[arg-type]

    repo._runner = interrupt_without_apply  # type: ignore[assignment]
    with pytest.raises(MutationOutcomeUnknown, match="unknown"):
        repo.set_draft(
            1,
            draft=False,
            expected_head="feature/65",
            expected_head_sha=HEAD_SHA,
            expected_base="dev/v0.1.4",
            expected_base_sha=BASE_SHA,
        )
    assert interruption_calls == 1


def test_authoritative_ready_rejection_confirms_exact_unchanged_state() -> None:
    runner = FakeGitHubRunner([pr_data(1)])
    repo = repository(runner)

    def reject_without_apply(
        args: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if any("markPullRequestReadyForReview" in value for value in args):
            return subprocess.CompletedProcess([], 1, "", "gh: rejected (HTTP 422)")
        return runner(args, **_kwargs)  # type: ignore[arg-type]

    repo._runner = reject_without_apply  # type: ignore[assignment]
    with pytest.raises(WorkerFailure, match="confirmed_rejected") as raised:
        repo.set_draft(
            1,
            draft=False,
            expected_head="feature/65",
            expected_head_sha=HEAD_SHA,
            expected_base="dev/v0.1.4",
            expected_base_sha=BASE_SHA,
        )
    assert not isinstance(raised.value, MutationOutcomeUnknown)


def test_correlated_creation_reconciles_and_concurrent_wrong_base_fails_closed() -> (
    None
):
    runner = FakeGitHubRunner()
    repo = repository(runner)
    runner.mutation_outcome = "timeout_after_apply"
    created = repo.create_draft_pr(
        head="feature/65",
        base="dev/v0.1.4",
        expected_head_sha=HEAD_SHA,
        expected_base_sha=BASE_SHA,
        title="Issue 65",
        body="Body",
        correlation_id="run-65",
    )
    assert "agent-workflow-manager:create-pr:run-65" in created.body

    concurrent_runner = FakeGitHubRunner()
    concurrent = repository(concurrent_runner)
    concurrent_runner.concurrent_wrong_base = True
    concurrent_runner.mutation_outcome = "timeout_after_apply"
    with pytest.raises(MutationOutcomeUnknown, match="wrong base"):
        concurrent.create_draft_pr(
            head="feature/65",
            base="dev/v0.1.4",
            expected_head_sha=HEAD_SHA,
            expected_base_sha=BASE_SHA,
            title="Issue 65",
            body="Body",
            correlation_id="run-concurrent",
        )


@pytest.mark.parametrize("draft", [True, False])
def test_update_pr_body_preserves_exact_review_topology(draft: bool) -> None:
    runner = FakeGitHubRunner([pr_data(1, body="Old", draft=draft)])
    repo = repository(runner)

    updated = repo.update_pr_body(
        1,
        body="New policy context",
        expected_head="feature/65",
        expected_head_sha=HEAD_SHA,
        expected_base="dev/v0.1.4",
        expected_base_sha=BASE_SHA,
    )

    assert updated.body == "New policy context"
    assert updated.is_draft is draft
    assert any("PATCH" in call for call in runner.calls)


@pytest.mark.parametrize(
    "outcome",
    ["timeout_after_apply", "malformed_after_apply", "nonzero_after_apply"],
)
def test_update_pr_body_reconciles_response_loss(outcome: str) -> None:
    runner = FakeGitHubRunner([pr_data(1, body="Old", draft=False)])
    repo = repository(runner)
    runner.mutation_outcome = outcome

    updated = repo.update_pr_body(
        1,
        body="New handoff",
        expected_head="feature/65",
        expected_head_sha=HEAD_SHA,
        expected_base="dev/v0.1.4",
        expected_base_sha=BASE_SHA,
    )

    assert updated.body == "New handoff"
    assert updated.is_draft is False
    assert sum("PATCH" in call for call in runner.calls) == 1


def test_unchanged_after_possible_body_update_is_unknown() -> None:
    runner = FakeGitHubRunner([pr_data(1, body="Old")])
    repo = repository(runner)

    def timeout_without_apply(
        args: Sequence[str],
        *,
        capture_output: bool,
        text: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        if "PATCH" in args:
            raise subprocess.TimeoutExpired(["gh"], 30)
        return runner(
            args,
            capture_output=capture_output,
            text=text,
            timeout=timeout,
            check=check,
        )

    repo._runner = timeout_without_apply  # type: ignore[assignment]
    with pytest.raises(MutationOutcomeUnknown, match="update PR body"):
        repo.update_pr_body(
            1,
            body="New handoff",
            expected_head="feature/65",
            expected_head_sha=HEAD_SHA,
            expected_base="dev/v0.1.4",
            expected_base_sha=BASE_SHA,
        )

    assert runner.prs[0]["body"] == "Old"


def test_authoritative_body_update_rejection_confirms_unchanged_state() -> None:
    runner = FakeGitHubRunner([pr_data(1, body="Old", draft=False)])
    repo = repository(runner)

    def reject_without_apply(
        args: Sequence[str],
        *,
        capture_output: bool,
        text: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        if "PATCH" in args:
            return subprocess.CompletedProcess([], 1, "", "gh: rejected (HTTP 422)")
        return runner(
            args,
            capture_output=capture_output,
            text=text,
            timeout=timeout,
            check=check,
        )

    repo._runner = reject_without_apply
    with pytest.raises(WorkerFailure, match="confirmed_rejected"):
        repo.update_pr_body(
            1,
            body="New handoff",
            expected_head="feature/65",
            expected_head_sha=HEAD_SHA,
            expected_base="dev/v0.1.4",
            expected_base_sha=BASE_SHA,
        )

    assert runner.prs[0]["body"] == "Old"
    assert runner.prs[0]["draft"] is False


def test_body_update_honors_dry_run_boundary_without_exposing_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = FakeGitHubRunner([pr_data(1, body="Old")])
    repo = repository(runner)
    observed: dict[str, object] = {}

    class BoundaryReached(RuntimeError):
        pass

    def boundary(operation: str, target: str, pre_state: object) -> None:
        observed.update(operation=operation, target=target, plan=pre_state)
        raise BoundaryReached

    monkeypatch.setattr(operations, "dry_run_boundary", boundary)
    with pytest.raises(BoundaryReached):
        repo.update_pr_body(
            1,
            body="secret-free generated handoff",
            expected_head="feature/65",
            expected_head_sha=HEAD_SHA,
            expected_base="dev/v0.1.4",
            expected_base_sha=BASE_SHA,
        )

    assert observed["operation"] == "update PR body"
    assert observed["plan"] == {
        "kind": "update_pr_body",
        "repository": "acme/project",
        "number": 1,
        "headSha": HEAD_SHA,
        "baseSha": BASE_SHA,
    }
    assert not any("PATCH" in call for call in runner.calls)


def test_merge_uses_immediate_endpoint_and_verifies_commit_topology() -> None:
    runner = FakeGitHubRunner([pr_data(1, draft=False)])
    repo = repository(runner)
    runner.mutation_outcome = "nonzero_after_apply"

    result = repo.merge_pr(
        1,
        expected_head="feature/65",
        expected_head_sha=HEAD_SHA,
        expected_base="dev/v0.1.4",
        expected_base_sha=BASE_SHA,
    )

    assert result.merge_commit_sha == MERGE_SHA
    mutation_calls = [call for call in runner.calls if "--method" in call]
    assert len(mutation_calls) == 1
    assert "PUT" in mutation_calls[0]
    assert mutation_calls[0][1] == "api"
    assert "pr" not in mutation_calls[0]
    assert not any("auto" in value or "queue" in value for value in mutation_calls[0])
