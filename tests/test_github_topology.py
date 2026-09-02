from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence

import pytest

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
    def __init__(self, prs: list[dict[str, object]] | None = None) -> None:
        self.prs = prs or []
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
        command = list(args)
        self.calls.append(command)
        assert capture_output and text and not check
        if command[1:4] == ["auth", "status", "--hostname"]:
            return self._done({"ok": True})
        if command[1:3] == ["api", "repos/acme/project"]:
            return self._done({"full_name": "acme/project"})
        if len(command) >= 3 and command[1] == "api" and "pulls?" in command[2]:
            endpoint = command[2]
            requested_open = "state=open" in endpoint
            page = int(re.search(r"[?&]page=(\d+)", endpoint).group(1))  # type: ignore[union-attr]
            per_page = int(re.search(r"[?&]per_page=(\d+)", endpoint).group(1))  # type: ignore[union-attr]
            matching = [
                item for item in self.prs if (item["state"] == "open") is requested_open
            ]
            start = (page - 1) * per_page
            return self._done(matching[start : start + per_page])
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
        raise AssertionError(f"unexpected command: {command}")

    def _mutation_result(self, data: object) -> subprocess.CompletedProcess[str]:
        outcome = self.mutation_outcome
        self.mutation_outcome = "success"
        if outcome == "timeout_after_apply":
            raise subprocess.TimeoutExpired(["gh"], 30)
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


def test_open_discovery_rejects_wrong_base_and_ambiguity() -> None:
    wrong = repository(FakeGitHubRunner([pr_data(1, base="main")]))
    with pytest.raises(PullRequestTopologyError, match="wrong base"):
        wrong.find_pr(head="feature/65", base="dev/v0.1.4", state="OPEN")

    duplicate = repository(FakeGitHubRunner([pr_data(1), pr_data(2)]))
    with pytest.raises(PullRequestTopologyError, match="ambiguous"):
        duplicate.find_pr(head="feature/65", base="dev/v0.1.4", state="OPEN")


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
    "outcome", ["timeout_after_apply", "malformed_after_apply", "nonzero_after_apply"]
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
