from __future__ import annotations

import ast
import importlib.machinery
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from purplemux_client import __all__ as PURPLEMUX_CLIENT_API

PREFLIGHT_NAME = "WORKFLOW_PREFLIGHT"
PREFLIGHT_KEYS = frozenset({"commands", "imports", "environment", "paths"})


@dataclass(frozen=True)
class ValidationIssue:
    kind: str
    message: str
    line: int | None = None
    column: int | None = None

    def as_json(self) -> dict[str, object]:
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass(frozen=True)
class ValidationResult:
    issues: tuple[ValidationIssue, ...]

    @property
    def valid(self) -> bool:
        return not self.issues

    def as_json(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "issues": [issue.as_json() for issue in self.issues],
        }


class WorkflowValidator:
    """Best-effort static validation that never executes workflow code."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        module_search_path: Sequence[str] | None = None,
    ) -> None:
        self._environment = os.environ if environment is None else environment
        self._cwd = Path.cwd() if cwd is None else cwd
        self._module_search_path = (
            tuple(sys.path) if module_search_path is None else tuple(module_search_path)
        )

    def validate(self, code: str) -> ValidationResult:
        try:
            tree = ast.parse(code, filename="<workflow>")
            compile(tree, "<workflow>", "exec")
        except (SyntaxError, ValueError, TypeError) as exc:
            return ValidationResult(
                (
                    ValidationIssue(
                        "syntax",
                        getattr(exc, "msg", str(exc)),
                        getattr(exc, "lineno", None),
                        getattr(exc, "offset", None),
                    ),
                )
            )

        issues: list[ValidationIssue] = []
        self._validate_imports(tree, issues)
        self._validate_required_environment(tree, issues)
        self._validate_metadata(tree, issues)
        return ValidationResult(tuple(issues))

    def _validate_imports(
        self, tree: ast.Module, issues: list[ValidationIssue]
    ) -> None:
        seen_modules: set[str] = set()
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
                if node.module == "purplemux_client":
                    for alias in node.names:
                        if alias.name != "*" and alias.name not in PURPLEMUX_CLIENT_API:
                            issues.append(
                                ValidationIssue(
                                    "api",
                                    f"purplemux_client has no public API named {alias.name!r}",
                                    node.lineno,
                                    node.col_offset + 1,
                                )
                            )
            for name in names:
                root = name.partition(".")[0]
                if root in seen_modules:
                    continue
                seen_modules.add(root)
                if not self._module_exists(root):
                    issues.append(
                        ValidationIssue(
                            "import",
                            f"Python dependency {root!r} is not available to the Runner interpreter",
                            getattr(node, "lineno", None),
                            self._column(node),
                        )
                    )

    def _validate_required_environment(
        self, tree: ast.Module, issues: list[ValidationIssue]
    ) -> None:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Subscript) or not self._is_os_environ(
                node.value
            ):
                continue
            if isinstance(node.slice, ast.Constant) and isinstance(
                node.slice.value, str
            ):
                name = node.slice.value
                if name not in self._environment:
                    issues.append(
                        ValidationIssue(
                            "environment",
                            f"required environment variable {name!r} is not set",
                            node.lineno,
                            node.col_offset + 1,
                        )
                    )

    @staticmethod
    def _is_os_environ(node: ast.expr) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "environ"
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
        )

    def _validate_metadata(
        self, tree: ast.Module, issues: list[ValidationIssue]
    ) -> None:
        declarations = [
            node
            for node in tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            and self._assignment_name(node) == PREFLIGHT_NAME
        ]
        if not declarations:
            return
        declaration = declarations[-1]
        value_node = declaration.value
        if value_node is None:
            self._metadata_issue(issues, declaration, "must have a literal value")
            return
        try:
            value = ast.literal_eval(value_node)
        except (ValueError, TypeError):
            self._metadata_issue(issues, declaration, "must be a literal dictionary")
            return
        if not isinstance(value, dict):
            self._metadata_issue(issues, declaration, "must be a dictionary")
            return
        unknown = sorted((key for key in value if key not in PREFLIGHT_KEYS), key=repr)
        if unknown:
            self._metadata_issue(
                issues,
                declaration,
                "contains unsupported keys: " + ", ".join(map(repr, unknown)),
            )
        for key in PREFLIGHT_KEYS:
            if key not in value:
                continue
            entries = value[key]
            if not isinstance(entries, (list, tuple)) or any(
                not isinstance(entry, str) or not entry for entry in entries
            ):
                self._metadata_issue(
                    issues, declaration, f"{key!r} must be a list of non-empty strings"
                )
                continue
            for entry in entries:
                self._validate_requirement(key, entry, declaration, issues)

    def _validate_requirement(
        self,
        kind: str,
        value: str,
        node: ast.stmt,
        issues: list[ValidationIssue],
    ) -> None:
        message: str | None = None
        if (
            kind == "commands"
            and shutil.which(value, path=self._environment.get("PATH")) is None
        ):
            message = f"required command {value!r} was not found on PATH"
        elif kind == "imports" and not self._module_exists(value.partition(".")[0]):
            message = f"required Python dependency {value!r} is not available"
        elif kind == "environment" and value not in self._environment:
            message = f"required environment variable {value!r} is not set"
        elif kind == "paths":
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = self._cwd / path
            if not path.exists():
                message = f"required path {value!r} does not exist"
        if message:
            issues.append(
                ValidationIssue(
                    kind.rstrip("s"), message, node.lineno, node.col_offset + 1
                )
            )

    def _module_exists(self, name: str) -> bool:
        if name in sys.builtin_module_names or name in sys.stdlib_module_names:
            return True
        try:
            return (
                importlib.machinery.PathFinder.find_spec(
                    name, list(self._module_search_path)
                )
                is not None
            )
        except (ImportError, AttributeError, ValueError):
            return False

    @staticmethod
    def _column(node: ast.AST) -> int | None:
        column = getattr(node, "col_offset", None)
        return column + 1 if isinstance(column, int) else None

    @staticmethod
    def _assignment_name(node: ast.Assign | ast.AnnAssign) -> str | None:
        target = (
            node.targets[0]
            if isinstance(node, ast.Assign) and len(node.targets) == 1
            else getattr(node, "target", None)
        )
        return target.id if isinstance(target, ast.Name) else None

    @staticmethod
    def _metadata_issue(
        issues: list[ValidationIssue], node: ast.stmt, detail: str
    ) -> None:
        issues.append(
            ValidationIssue(
                "metadata",
                f"{PREFLIGHT_NAME} {detail}",
                node.lineno,
                node.col_offset + 1,
            )
        )
