from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class IssueDrivenFinding:
    path: str
    message: str

    def as_json(self) -> dict[str, str]:
        return {"path": self.path, "message": self.message}


class IssueDrivenValidationError(ValueError):
    def __init__(self, findings: list[IssueDrivenFinding]) -> None:
        super().__init__("issue-driven JSON validation failed")
        self.findings = tuple(findings)


@dataclass(frozen=True)
class IssueDrivenConfig:
    repository: str
    integration_branch: str
    final_branch: str
    issues: tuple[int, ...]
    max_reviews: int
    merge_to_integration: bool
    final_review: bool
    merge_final: bool

    def as_json(self) -> dict[str, object]:
        return {
            "mode": "issue-driven",
            "repository": self.repository,
            "integration_branch": self.integration_branch,
            "final_branch": self.final_branch,
            "issues": list(self.issues),
            "max_reviews": self.max_reviews,
            "merge_to_integration": self.merge_to_integration,
            "final_review": self.final_review,
            "merge_final": self.merge_final,
        }


_REQUIRED_FIELDS = {
    "repository",
    "integration_branch",
    "final_branch",
    "issues",
    "max_reviews",
    "merge_to_integration",
    "final_review",
    "merge_final",
}
_ALLOWED_FIELDS = _REQUIRED_FIELDS | {"mode"}
# Kept in lockstep with preflight.MAX_OUTLINE_ITEMS by boundary tests.
_MAX_WORKFLOW_OUTLINE_ITEMS = 100


def _valid_branch_name(value: str) -> bool:
    forbidden = set(" ~^:?*[\\")
    components = value.split("/")
    return not (
        value == "@"
        or value.startswith(("-", ".", "/"))
        or value.endswith((".", "/", ".lock"))
        or ".." in value
        or "@{" in value
        or "//" in value
        or any(
            character in forbidden or ord(character) < 32 or ord(character) == 127
            for character in value
        )
        or any(
            component.startswith(".") or component.endswith(".lock")
            for component in components
        )
    )


def parse_issue_driven_json(source: str) -> IssueDrivenConfig:
    """Parse the intentionally small Issue Driven configuration."""
    if not isinstance(source, str):
        raise TypeError("source must be a string")
    duplicate_keys: list[str] = []

    def object_from_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                duplicate_keys.append(key)
            result[key] = value
        return result

    try:
        value = json.loads(source, object_pairs_hook=object_from_pairs)
    except json.JSONDecodeError as exc:
        raise IssueDrivenValidationError(
            [IssueDrivenFinding("$", f"invalid JSON at line {exc.lineno}: {exc.msg}")]
        ) from exc
    if not isinstance(value, dict):
        raise IssueDrivenValidationError(
            [IssueDrivenFinding("$", "top-level value must be an object")]
        )
    findings: list[IssueDrivenFinding] = []
    for key in sorted(set(duplicate_keys)):
        findings.append(IssueDrivenFinding(f"$.{key}", "field is duplicated"))
    for key in sorted(_REQUIRED_FIELDS - set(value)):
        findings.append(IssueDrivenFinding(f"$.{key}", "required field is missing"))
    for key in sorted(set(value) - _ALLOWED_FIELDS):
        findings.append(IssueDrivenFinding(f"$.{key}", "unknown field is not allowed"))
    if "mode" in value and value["mode"] != "issue-driven":
        findings.append(IssueDrivenFinding("$.mode", "must be exactly 'issue-driven'"))
    for key in ("repository", "integration_branch", "final_branch"):
        item = value.get(key)
        if (
            not isinstance(item, str)
            or not item
            or item != item.strip()
            or "\0" in item
        ):
            findings.append(
                IssueDrivenFinding(f"$.{key}", "must be a non-empty trimmed string")
            )
    integration = value.get("integration_branch")
    final = value.get("final_branch")
    for key, branch in (("integration_branch", integration), ("final_branch", final)):
        if isinstance(branch, str) and branch and not _valid_branch_name(branch):
            findings.append(
                IssueDrivenFinding(f"$.{key}", "must be a valid Git branch name")
            )
    if isinstance(integration, str) and integration == final:
        findings.append(
            IssueDrivenFinding("$.final_branch", "must differ from integration_branch")
        )
    issues = value.get("issues")
    if not isinstance(issues, list) or not issues:
        findings.append(IssueDrivenFinding("$.issues", "must be a non-empty array"))
    else:
        reserved_outline_items = 2 if value.get("final_review") is True else 1
        max_issues = _MAX_WORKFLOW_OUTLINE_ITEMS - reserved_outline_items
        if len(issues) > max_issues:
            findings.append(
                IssueDrivenFinding(
                    "$.issues",
                    f"must contain at most {max_issues} items when "
                    f"final_review is {value.get('final_review')!r}",
                )
            )
        seen: set[int] = set()
        for index, issue in enumerate(issues):
            if isinstance(issue, bool) or not isinstance(issue, int) or issue < 1:
                findings.append(
                    IssueDrivenFinding(
                        f"$.issues[{index}]", "must be a positive integer"
                    )
                )
            elif issue in seen:
                findings.append(
                    IssueDrivenFinding(f"$.issues[{index}]", "must be unique")
                )
            else:
                seen.add(issue)
        generated_branches = {f"feature/issue-{issue}" for issue in seen}
        for key, branch in (
            ("integration_branch", integration),
            ("final_branch", final),
        ):
            if isinstance(branch, str) and branch in generated_branches:
                findings.append(
                    IssueDrivenFinding(
                        f"$.{key}", "must differ from every generated Issue branch"
                    )
                )
    max_reviews = value.get("max_reviews")
    if (
        isinstance(max_reviews, bool)
        or not isinstance(max_reviews, int)
        or not 1 <= max_reviews <= 100
    ):
        findings.append(
            IssueDrivenFinding("$.max_reviews", "must be an integer from 1 to 100")
        )
    for key in ("merge_to_integration", "final_review", "merge_final"):
        if not isinstance(value.get(key), bool):
            findings.append(IssueDrivenFinding(f"$.{key}", "must be a boolean"))
    if findings:
        raise IssueDrivenValidationError(findings)
    return IssueDrivenConfig(
        repository=value["repository"],
        integration_branch=value["integration_branch"],
        final_branch=value["final_branch"],
        issues=tuple(value["issues"]),
        max_reviews=value["max_reviews"],
        merge_to_integration=value["merge_to_integration"],
        final_review=value["final_review"],
        merge_final=value["merge_final"],
    )


def _canonical_source() -> str:
    development_copy = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "sequential-version-development.py"
    )
    if development_copy.is_file():
        return development_copy.read_text(encoding="utf-8")
    packaged = resources.files("purplemux_client").joinpath(
        "_issue_driven_sequential_template.py"
    )
    return packaged.read_text(encoding="utf-8")


def _fixed_config_function(config: IssueDrivenConfig) -> str:
    issues = ",\n        ".join(
        f"Issue({number}, 'feature/issue-{number}')" for number in config.issues
    )
    return f"""def parse_args() -> Config:
    context = prepare_run_repository(
        repo={config.repository!r},
        base_branch={config.integration_branch!r},
    )
    repository = GitRepository.open(
        context.execution_root,
        command_timeout_seconds=COMMAND_TIMEOUT,
    )
    return Config(
        context.execution_root,
        repository.expected_github_slug,
        {config.integration_branch!r},
        {config.final_branch!r},
        (
        {issues},
        ),
        "git diff --check",
    )


"""


def _workflow_outline(config: IssueDrivenConfig) -> str:
    labels = [f"Issue #{number}" for number in config.issues]
    if config.final_review:
        labels.append("Whole-version review")
    labels.append("Final integration PR")
    entries = "\n".join(f"    {label!r}," for label in labels)
    return f"WORKFLOW_OUTLINE = [\n{entries}\n]"


def generate_issue_driven_workflow(config: IssueDrivenConfig) -> str:
    """Generate a deterministic plain-Python workflow using new-run recovery."""
    source = _canonical_source()
    source = source.replace(
        "    PurpleMuxRuntime,\n",
        "    PurpleMuxRuntime,\n    prepare_run_repository,\n",
        1,
    )
    outline_start = source.index("WORKFLOW_OUTLINE = [\n")
    outline_end = source.index("\n]", outline_start) + len("\n]")
    source = source[:outline_start] + _workflow_outline(config) + source[outline_end:]
    source = source.replace("MAX_REVIEWS = 5", f"MAX_REVIEWS = {config.max_reviews}", 1)
    source = source.replace(
        "MERGE_TO_INTEGRATION = True",
        f"MERGE_TO_INTEGRATION = {config.merge_to_integration}",
        1,
    )
    source = source.replace(
        "FINAL_REVIEW = True", f"FINAL_REVIEW = {config.final_review}", 1
    )
    source = source.replace(
        "MERGE_FINAL = False", f"MERGE_FINAL = {config.merge_final}", 1
    )
    start = source.index("def parse_args() -> Config:\n")
    end = source.index("def short_error(", start)
    return source[:start] + _fixed_config_function(config) + source[end:]
