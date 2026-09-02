from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol, TypeVar, cast
from urllib.parse import quote

from purplemux_client.client import WorkerFailure
from purplemux_client.git import _require_slug
from purplemux_client.operations import (
    AuthoritativeMutationRejection,
    MutationResolution,
    PossibleDispatchFailure,
    PreDispatchFailure,
    Reconciliation,
    execute_mutation,
)

PullRequestStatus = Literal["OPEN", "MERGED", "CLOSED"]
_CORRELATION_RE = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_HTTP_REJECTION_RE = re.compile(r"\bHTTP (4\d\d)\b", re.IGNORECASE)
_READY_MUTATION = """
mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id}){
pullRequest{id}}}
""".strip()
_DRAFT_MUTATION = """
mutation($id:ID!){convertPullRequestToDraft(input:{pullRequestId:$id}){
pullRequest{id}}}
""".strip()
_QUEUE_QUERY = """
query($id:ID!){node(id:$id){... on PullRequest{mergeQueueEntry{id state}}}}
""".strip()


class GitHubCommandRunner(Protocol):
    def __call__(
        self,
        args: Sequence[str],
        *,
        capture_output: bool,
        text: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]: ...


class PullRequestTopologyError(WorkerFailure):
    """The authoritative PR topology is unsafe or ambiguous."""


class IncompletePullRequestEnumeration(WorkerFailure):
    """A bounded authoritative query could not prove it was exhausted."""


@dataclass(frozen=True)
class PullRequestState:
    number: int
    url: str
    state: PullRequestStatus
    is_draft: bool
    head_repository: str
    head_branch: str
    head_sha: str
    base_repository: str
    base_branch: str
    base_sha: str
    merge_commit_sha: str | None
    auto_merge_enabled: bool
    merge_queue_entry: str | None
    node_id: str
    body: str = field(repr=False)


@dataclass(frozen=True)
class MergeResult:
    pr: PullRequestState
    merge_commit_sha: str
    reconciled: bool = False


T = TypeVar("T")


class GitHubRepository:
    """Validated, repository-pinned GitHub PR topology operations."""

    def __init__(
        self,
        *,
        slug: str,
        executable: str,
        command_timeout_seconds: float,
        read_timeout_retries: int,
        page_size: int,
        max_pages: int,
        runner: GitHubCommandRunner,
    ) -> None:
        self.slug = slug
        self.executable = executable
        self.command_timeout_seconds = command_timeout_seconds
        self.read_timeout_retries = read_timeout_retries
        self.page_size = page_size
        self.max_pages = max_pages
        self._runner = runner

    @classmethod
    def open(
        cls,
        slug: str,
        *,
        executable: str = "gh",
        command_timeout_seconds: float = 30.0,
        read_timeout_retries: int = 1,
        page_size: int = 100,
        max_pages: int = 10,
        runner: GitHubCommandRunner = subprocess.run,
    ) -> GitHubRepository:
        _require_slug(slug)
        if command_timeout_seconds <= 0:
            raise ValueError("command_timeout_seconds must be positive")
        if read_timeout_retries < 0:
            raise ValueError("read_timeout_retries must not be negative")
        if not 1 <= page_size <= 100 or max_pages < 1:
            raise ValueError("page_size must be 1..100 and max_pages must be positive")
        repository = cls(
            slug=slug,
            executable=executable,
            command_timeout_seconds=command_timeout_seconds,
            read_timeout_retries=read_timeout_retries,
            page_size=page_size,
            max_pages=max_pages,
            runner=runner,
        )
        repository._validate_identity()
        return repository

    def find_pr(
        self, *, head: str, base: str, state: PullRequestStatus
    ) -> PullRequestState | None:
        self._validate_identity()
        self._validate_lookup(head, base, state)
        candidates = self._list_same_head(head, state)
        if state == "OPEN":
            wrong = [pr for pr in candidates if pr.base_branch != base]
            if wrong:
                descriptions = ", ".join(
                    f"#{pr.number}->{pr.base_branch}" for pr in wrong
                )
                raise PullRequestTopologyError(
                    f"open PR(s) from {head!r} target the wrong base: {descriptions}; "
                    f"expected {base!r}"
                )
        exact = [pr for pr in candidates if pr.base_branch == base]
        if len(exact) > 1:
            numbers = ", ".join(f"#{pr.number}" for pr in exact)
            raise PullRequestTopologyError(
                f"ambiguous {state.lower()} PRs from {head!r} to {base!r}: {numbers}"
            )
        return exact[0] if exact else None

    def require_pr(
        self,
        *,
        head: str,
        base: str,
        number: int | None = None,
        state: PullRequestStatus = "OPEN",
        expected_head_sha: str | None = None,
        expected_base_sha: str | None = None,
        draft: bool | None = None,
    ) -> PullRequestState:
        self._validate_identity()
        self._validate_lookup(head, base, state)
        discovered = self.find_pr(head=head, base=base, state=state)
        if discovered is None:
            raise PullRequestTopologyError(
                f"no {state.lower()} PR from {head!r} to {base!r}"
            )
        if number is not None and discovered.number != number:
            raise PullRequestTopologyError(
                f"PR identity changed: expected #{number}, found #{discovered.number}"
            )
        pr = self._get_pr(discovered.number, include_queue=True)
        self._require_topology(
            pr,
            head=head,
            base=base,
            state=state,
            expected_head_sha=expected_head_sha,
            expected_base_sha=expected_base_sha,
            draft=draft,
        )
        return pr

    def create_draft_pr(
        self,
        *,
        head: str,
        base: str,
        expected_head_sha: str,
        expected_base_sha: str,
        title: str,
        body: str,
        correlation_id: str,
    ) -> PullRequestState:
        self._validate_identity()
        self._validate_lookup(head, base, "OPEN")
        if not title.strip():
            raise ValueError("PR title must not be empty")
        if "\0" in title or "\0" in body:
            raise ValueError("PR title and body must not contain null bytes")
        if not _CORRELATION_RE.fullmatch(correlation_id):
            raise ValueError(
                "correlation_id must be 1..64 non-secret identifier characters"
            )
        self._require_branch_sha(head, expected_head_sha)
        self._require_branch_sha(base, expected_base_sha)
        existing = self.find_pr(head=head, base=base, state="OPEN")
        if existing is not None:
            raise PullRequestTopologyError(
                f"open PR #{existing.number} already exists for {head!r}"
            )
        marker = f"<!-- agent-workflow-manager:create-pr:{correlation_id} -->"
        marked_body = f"{body.rstrip()}\n\n{marker}" if body.strip() else marker
        pre_state = {
            "head": expected_head_sha,
            "base": expected_base_sha,
            "openSameHead": (),
            "correlation": correlation_id,
        }

        def postcondition() -> PullRequestState:
            pr = self.find_pr(head=head, base=base, state="OPEN")
            if pr is None:
                raise _PostconditionAbsent("created PR is not visible")
            if marker not in pr.body:
                raise PullRequestTopologyError(
                    f"open PR #{pr.number} lacks creation correlation marker"
                )
            detailed = self.require_pr(
                number=pr.number,
                head=head,
                base=base,
                state="OPEN",
                expected_head_sha=expected_head_sha,
                expected_base_sha=expected_base_sha,
                draft=True,
            )
            self._require_no_deferred_merge(detailed)
            return detailed

        return self._mutate(
            operation="create Draft PR",
            target=f"{self.slug}:{head}->{base}",
            pre_state=pre_state,
            args=[
                "api",
                "--method",
                "POST",
                f"repos/{self.slug}/pulls",
                "-f",
                f"title={title}",
                "-f",
                f"head={head}",
                "-f",
                f"base={base}",
                "-f",
                f"body={marked_body}",
                "-F",
                "draft=true",
            ],
            pre_dispatch=lambda: self._require_creation_preconditions(
                head,
                base,
                expected_head_sha,
                expected_base_sha,
            ),
            unchanged=lambda: self._creation_state_is_unchanged(
                head, base, expected_head_sha, expected_base_sha
            ),
            postcondition=postcondition,
            plan={
                "kind": "create_draft_pr",
                "repository": self.slug,
                "head": head,
                "base": base,
                "headSha": expected_head_sha,
                "baseSha": expected_base_sha,
                "correlationId": correlation_id,
            },
        )

    def set_draft(
        self,
        pr: int,
        *,
        draft: bool,
        expected_head: str,
        expected_head_sha: str,
        expected_base: str,
        expected_base_sha: str,
    ) -> PullRequestState:
        current = self.require_pr(
            number=pr,
            head=expected_head,
            base=expected_base,
            state="OPEN",
            expected_head_sha=expected_head_sha,
            expected_base_sha=expected_base_sha,
        )
        self._require_no_deferred_merge(current)
        if current.is_draft is draft:
            return current
        mutation = _DRAFT_MUTATION if draft else _READY_MUTATION

        def postcondition() -> PullRequestState:
            updated = self.require_pr(
                number=pr,
                head=expected_head,
                base=expected_base,
                state="OPEN",
                expected_head_sha=expected_head_sha,
                expected_base_sha=expected_base_sha,
                draft=draft,
            )
            self._require_no_deferred_merge(updated)
            return updated

        return self._mutate(
            operation="set PR Draft state",
            target=f"{self.slug}#{pr}",
            pre_state=current,
            args=[
                "api",
                "graphql",
                "-f",
                f"query={mutation}",
                "-F",
                f"id={current.node_id}",
            ],
            pre_dispatch=lambda: self._require_draft_preconditions(
                pr,
                expected_head,
                expected_head_sha,
                expected_base,
                expected_base_sha,
            ),
            unchanged=lambda: self._get_pr(pr, include_queue=True) == current,
            postcondition=postcondition,
            plan={
                "kind": "set_pr_draft",
                "repository": self.slug,
                "number": pr,
                "draft": draft,
                "headSha": expected_head_sha,
                "baseSha": expected_base_sha,
            },
        )

    def merge_pr(
        self,
        pr: int,
        *,
        expected_head: str,
        expected_head_sha: str,
        expected_base: str,
        expected_base_sha: str,
        method: Literal["merge"] = "merge",
    ) -> MergeResult:
        if method != "merge":
            raise ValueError("only immediate merge-commit mode is supported")
        current = self.require_pr(
            number=pr,
            head=expected_head,
            base=expected_base,
            state="OPEN",
            expected_head_sha=expected_head_sha,
            expected_base_sha=expected_base_sha,
            draft=False,
        )
        self._require_no_deferred_merge(current)

        def postcondition() -> MergeResult:
            return self._verify_immediate_merge(
                pr,
                expected_head=expected_head,
                expected_head_sha=expected_head_sha,
                expected_base=expected_base,
                expected_base_sha=expected_base_sha,
            )

        return self._mutate(
            operation="merge PR immediately",
            target=f"{self.slug}#{pr}",
            pre_state=current,
            args=[
                "api",
                "--method",
                "PUT",
                f"repos/{self.slug}/pulls/{pr}/merge",
                "-f",
                f"sha={expected_head_sha}",
                "-f",
                "merge_method=merge",
            ],
            pre_dispatch=lambda: self._require_merge_preconditions(
                pr,
                expected_head,
                expected_head_sha,
                expected_base,
                expected_base_sha,
            ),
            unchanged=lambda: self._get_pr(pr, include_queue=True) == current,
            postcondition=postcondition,
            plan={
                "kind": "merge_pr_immediately",
                "repository": self.slug,
                "number": pr,
                "headSha": expected_head_sha,
                "baseSha": expected_base_sha,
            },
        )

    def _verify_immediate_merge(
        self,
        number: int,
        *,
        expected_head: str,
        expected_head_sha: str,
        expected_base: str,
        expected_base_sha: str,
    ) -> MergeResult:
        merged = self.find_pr(head=expected_head, base=expected_base, state="MERGED")
        if merged is None or merged.number != number:
            raise _PostconditionAbsent("PR is not synchronously merged")
        detailed = self._get_pr(number, include_queue=True)
        self._require_topology(
            detailed,
            head=expected_head,
            base=expected_base,
            state="MERGED",
            expected_head_sha=expected_head_sha,
            expected_base_sha=None,
            draft=False,
        )
        self._require_no_deferred_merge(detailed)
        merge_sha = detailed.merge_commit_sha
        if not merge_sha:
            raise PullRequestTopologyError("merged PR has no merge commit SHA")
        if self._branch_sha(expected_base) != merge_sha:
            raise PullRequestTopologyError(
                f"base {expected_base!r} does not point at merge commit {merge_sha}"
            )
        commit = self._read_object(
            ["api", f"repos/{self.slug}/git/commits/{merge_sha}"]
        )
        parents = commit.get("parents")
        if not isinstance(parents, list):
            raise WorkerFailure("merge commit response has no parents")
        parent_shas = [item.get("sha") for item in parents if isinstance(item, Mapping)]
        if len(parent_shas) < 2 or parent_shas[0] != expected_base_sha:
            raise PullRequestTopologyError(
                f"merge commit first parent is not reviewed base {expected_base_sha}"
            )
        if expected_head_sha not in parent_shas[1:]:
            raise PullRequestTopologyError(
                f"merge commit does not contain reviewed head {expected_head_sha}"
            )
        return MergeResult(detailed, merge_sha)

    def _require_creation_preconditions(
        self,
        head: str,
        base: str,
        expected_head_sha: str,
        expected_base_sha: str,
    ) -> None:
        self._validate_identity()
        self._require_branch_sha(head, expected_head_sha)
        self._require_branch_sha(base, expected_base_sha)
        if self.find_pr(head=head, base=base, state="OPEN") is not None:
            raise PullRequestTopologyError(
                f"an open same-head PR appeared before creation for {head!r}"
            )

    def _creation_state_is_unchanged(
        self,
        head: str,
        base: str,
        expected_head_sha: str,
        expected_base_sha: str,
    ) -> bool:
        return (
            self._branch_sha(head) == expected_head_sha
            and self._branch_sha(base) == expected_base_sha
            and self.find_pr(head=head, base=base, state="OPEN") is None
        )

    def _require_draft_preconditions(
        self,
        number: int,
        head: str,
        head_sha: str,
        base: str,
        base_sha: str,
    ) -> None:
        current = self.require_pr(
            number=number,
            head=head,
            base=base,
            state="OPEN",
            expected_head_sha=head_sha,
            expected_base_sha=base_sha,
        )
        self._require_no_deferred_merge(current)

    def _require_merge_preconditions(
        self,
        number: int,
        head: str,
        head_sha: str,
        base: str,
        base_sha: str,
    ) -> None:
        current = self.require_pr(
            number=number,
            head=head,
            base=base,
            state="OPEN",
            expected_head_sha=head_sha,
            expected_base_sha=base_sha,
            draft=False,
        )
        self._require_no_deferred_merge(current)

    def _validate_identity(self) -> None:
        self._read_text(["auth", "status", "--hostname", "github.com"])
        data = self._read_object(["api", f"repos/{self.slug}"])
        identity = data.get("full_name") or data.get("nameWithOwner")
        if not isinstance(identity, str) or identity.lower() != self.slug.lower():
            raise WorkerFailure(
                f"GitHub resolved repository {identity!r}, expected {self.slug!r}"
            )

    def _validate_lookup(self, head: str, base: str, state: PullRequestStatus) -> None:
        if not head or not base or head == base or "\0" in head or "\0" in base:
            raise ValueError("head and base must be distinct non-empty branch names")
        if state not in {"OPEN", "MERGED", "CLOSED"}:
            raise ValueError(f"unsupported PR state: {state!r}")

    def _list_same_head(
        self, head: str, state: PullRequestStatus
    ) -> tuple[PullRequestState, ...]:
        api_state = "open" if state == "OPEN" else "closed"
        owner = self.slug.split("/", 1)[0]
        found: list[PullRequestState] = []
        exhausted = False
        for page in range(1, self.max_pages + 1):
            endpoint = (
                f"repos/{self.slug}/pulls?state={api_state}"
                f"&head={quote(owner + ':' + head, safe='')}"
                f"&per_page={self.page_size}&page={page}"
            )
            data = self._read_json(["api", endpoint])
            if not isinstance(data, list):
                raise IncompletePullRequestEnumeration(
                    "GitHub PR enumeration returned a non-list page"
                )
            page_items = cast(list[object], data)
            for raw in page_items:
                candidate = self._parse_pr(raw)
                if (
                    candidate.head_repository.lower() != self.slug.lower()
                    or candidate.head_branch != head
                    or candidate.state != state
                ):
                    continue
                found.append(candidate)
            if len(page_items) < self.page_size:
                exhausted = True
                break
        if not exhausted:
            raise IncompletePullRequestEnumeration(
                f"PR enumeration exceeded the {self.max_pages}-page safety bound"
            )
        return tuple(found)

    def _get_pr(self, number: int, *, include_queue: bool) -> PullRequestState:
        data = self._read_object(["api", f"repos/{self.slug}/pulls/{number}"])
        pr = self._parse_pr(data)
        if include_queue:
            queue = self._read_object(
                [
                    "api",
                    "graphql",
                    "-f",
                    f"query={_QUEUE_QUERY}",
                    "-F",
                    f"id={pr.node_id}",
                ]
            )
            node = (
                queue.get("data", {}).get("node")
                if isinstance(queue.get("data"), Mapping)
                else None
            )
            entry = node.get("mergeQueueEntry") if isinstance(node, Mapping) else None
            if isinstance(entry, Mapping):
                identifier = entry.get("id") or entry.get("state") or "present"
                pr = replace(pr, merge_queue_entry=str(identifier))
            elif entry is not None:
                raise WorkerFailure("GitHub returned malformed merge queue state")
        return pr

    def _parse_pr(self, raw: object) -> PullRequestState:
        if not isinstance(raw, Mapping):
            raise WorkerFailure("GitHub returned a malformed PR object")
        try:
            number = raw["number"]
            url = raw.get("html_url") or raw.get("url")
            head = raw["head"]
            base = raw["base"]
            if not isinstance(head, Mapping) or not isinstance(base, Mapping):
                raise KeyError("head/base")
            head_repo = head["repo"]
            base_repo = base["repo"]
            if not isinstance(head_repo, Mapping) or not isinstance(base_repo, Mapping):
                raise KeyError("repository")
            merged = bool(raw.get("merged_at")) or raw.get("merged") is True
            raw_state = str(raw["state"]).lower()
            state: PullRequestStatus = (
                "MERGED" if merged else "OPEN" if raw_state == "open" else "CLOSED"
            )
            node_id = raw["node_id"]
            body = raw.get("body") or ""
            values = (
                number,
                url,
                head_repo["full_name"],
                head["ref"],
                head["sha"],
                base_repo["full_name"],
                base["ref"],
                base["sha"],
                node_id,
                body,
            )
            if (
                not isinstance(number, int)
                or not all(isinstance(value, str) and value for value in values[1:9])
                or not isinstance(body, str)
            ):
                raise KeyError("typed fields")
        except (KeyError, TypeError) as exc:
            raise WorkerFailure("GitHub returned an incomplete PR object") from exc
        return PullRequestState(
            number=number,
            url=cast(str, url),
            state=state,
            is_draft=bool(raw.get("draft") or raw.get("isDraft")),
            head_repository=cast(str, head_repo["full_name"]),
            head_branch=cast(str, head["ref"]),
            head_sha=cast(str, head["sha"]),
            base_repository=cast(str, base_repo["full_name"]),
            base_branch=cast(str, base["ref"]),
            base_sha=cast(str, base["sha"]),
            merge_commit_sha=cast(str | None, raw.get("merge_commit_sha")),
            auto_merge_enabled=raw.get("auto_merge") is not None,
            merge_queue_entry=None,
            node_id=cast(str, node_id),
            body=body,
        )

    def _require_topology(
        self,
        pr: PullRequestState,
        *,
        head: str,
        base: str,
        state: PullRequestStatus,
        expected_head_sha: str | None,
        expected_base_sha: str | None,
        draft: bool | None,
    ) -> None:
        expected = self.slug.lower()
        if (
            pr.head_repository.lower() != expected
            or pr.base_repository.lower() != expected
        ):
            raise PullRequestTopologyError("PR crosses an unexpected repository")
        if pr.head_branch != head or pr.base_branch != base or pr.state != state:
            raise PullRequestTopologyError("PR head/base/state topology changed")
        if expected_head_sha is not None and pr.head_sha != expected_head_sha:
            raise PullRequestTopologyError(
                f"PR head changed: reviewed {expected_head_sha}, current {pr.head_sha}"
            )
        if expected_base_sha is not None and pr.base_sha != expected_base_sha:
            raise PullRequestTopologyError(
                f"PR base changed: reviewed {expected_base_sha}, current {pr.base_sha}"
            )
        if draft is not None and pr.is_draft is not draft:
            raise PullRequestTopologyError(
                f"PR Draft state is {pr.is_draft}, expected {draft}"
            )

    def _require_no_deferred_merge(self, pr: PullRequestState) -> None:
        if pr.auto_merge_enabled:
            raise PullRequestTopologyError("PR has auto-merge enabled")
        if pr.merge_queue_entry is not None:
            raise PullRequestTopologyError("PR is in a merge queue")

    def _branch_sha(self, branch: str) -> str:
        encoded = quote(branch, safe="")
        data = self._read_object(["api", f"repos/{self.slug}/git/ref/heads/{encoded}"])
        obj = data.get("object")
        sha = obj.get("sha") if isinstance(obj, Mapping) else None
        if not isinstance(sha, str) or not sha:
            raise WorkerFailure(f"GitHub branch {branch!r} has no usable SHA")
        return sha

    def _require_branch_sha(self, branch: str, expected: str) -> None:
        actual = self._branch_sha(branch)
        if actual != expected:
            raise PullRequestTopologyError(
                f"GitHub branch {branch!r} changed: expected {expected}, found {actual}"
            )

    def _mutate(
        self,
        *,
        operation: str,
        target: str,
        pre_state: object,
        args: Sequence[str],
        pre_dispatch: Callable[[], None],
        unchanged: Callable[[], bool],
        postcondition: Callable[[], T],
        plan: Mapping[str, object],
    ) -> T:
        def dispatch() -> T:
            pre_dispatch()
            self._run_mutation_json(args, operation)
            try:
                return postcondition()
            except _PostconditionAbsent as exc:
                raise AuthoritativeMutationRejection(str(exc)) from exc
            except PullRequestTopologyError as exc:
                raise AuthoritativeMutationRejection(str(exc)) from exc
            except WorkerFailure as exc:
                raise PossibleDispatchFailure(str(exc)) from exc

        def reconcile(rejection_proved: bool) -> Reconciliation[T]:
            try:
                value = postcondition()
            except _PostconditionAbsent as exc:
                resolution = self._failed_postcondition_resolution(
                    rejection_proved, unchanged
                )
                return Reconciliation(resolution, detail=str(exc))
            except PullRequestTopologyError as exc:
                resolution = self._failed_postcondition_resolution(
                    rejection_proved, unchanged
                )
                return Reconciliation(resolution, detail=str(exc))
            except (IncompletePullRequestEnumeration, WorkerFailure) as exc:
                return Reconciliation(MutationResolution.UNKNOWN, detail=str(exc))
            return Reconciliation(
                MutationResolution.DESIRED, value, "postcondition holds"
            )

        return execute_mutation(
            operation=operation,
            target=target,
            pre_state=pre_state,
            dispatch=dispatch,
            reconcile=reconcile,
            plan=plan,
        )

    @staticmethod
    def _failed_postcondition_resolution(
        rejection_proved: bool, unchanged: Callable[[], bool]
    ) -> MutationResolution:
        if not rejection_proved:
            return MutationResolution.UNKNOWN
        return (
            MutationResolution.REJECTED if unchanged() else MutationResolution.CONFLICT
        )

    def _run_mutation_json(self, args: Sequence[str], operation: str) -> dict[str, Any]:
        try:
            completed = self._runner(
                [self.executable, *args],
                capture_output=True,
                text=True,
                timeout=self.command_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PossibleDispatchFailure(f"GitHub {operation} timed out") from exc
        except OSError as exc:
            raise PreDispatchFailure(
                f"could not dispatch GitHub {operation}: {exc}"
            ) from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
            match = _HTTP_REJECTION_RE.search(detail)
            if match and match.group(1) not in {"408", "425", "429"}:
                raise AuthoritativeMutationRejection(
                    f"GitHub synchronously rejected {operation}: {detail}"
                )
            raise PossibleDispatchFailure(
                f"GitHub {operation} returned ambiguous exit {completed.returncode}: {detail}"
            )
        try:
            data = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise PossibleDispatchFailure(
                f"GitHub {operation} returned malformed JSON"
            ) from exc
        if not isinstance(data, dict):
            raise PossibleDispatchFailure(
                f"GitHub {operation} returned non-object JSON"
            )
        return cast(dict[str, Any], data)

    def _read_json(self, args: Sequence[str]) -> dict[str, Any] | list[Any]:
        output = self._read_text(args)
        try:
            data = json.loads(output)
        except (json.JSONDecodeError, TypeError) as exc:
            raise WorkerFailure(
                f"GitHub {' '.join(args[:2])} returned malformed JSON"
            ) from exc
        if not isinstance(data, (dict, list)):
            raise WorkerFailure(f"GitHub {' '.join(args[:2])} returned invalid JSON")
        return cast(dict[str, Any] | list[Any], data)

    def _read_object(self, args: Sequence[str]) -> dict[str, Any]:
        data = self._read_json(args)
        if not isinstance(data, dict):
            raise WorkerFailure(f"GitHub {' '.join(args[:2])} returned a non-object")
        return data

    def _read_text(self, args: Sequence[str]) -> str:
        attempts = self.read_timeout_retries + 1
        for attempt in range(attempts):
            try:
                completed = self._runner(
                    [self.executable, *args],
                    capture_output=True,
                    text=True,
                    timeout=self.command_timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                if attempt + 1 < attempts:
                    continue
                raise WorkerFailure(
                    f"read-only GitHub {' '.join(args)} timed out"
                ) from exc
            except OSError as exc:
                raise WorkerFailure(f"could not execute GitHub read: {exc}") from exc
            if completed.returncode != 0:
                detail = (
                    completed.stderr.strip() or completed.stdout.strip() or "no output"
                )
                raise WorkerFailure(f"GitHub {' '.join(args)} failed: {detail}")
            return completed.stdout
        raise AssertionError("unreachable")


class _PostconditionAbsent(WorkerFailure):
    pass
