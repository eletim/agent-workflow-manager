from __future__ import annotations

import ast
import importlib.machinery
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from purplemux_client import __all__ as PURPLEMUX_CLIENT_API

PREFLIGHT_NAME = "WORKFLOW_PREFLIGHT"
PREFLIGHT_KEYS = frozenset({"commands", "imports", "environment", "paths"})
OUTLINE_NAME = "WORKFLOW_OUTLINE"
DRY_RUN_NAME = "WORKFLOW_DRY_RUN"
DRY_RUN_VERSION = 1
MAX_OUTLINE_ITEMS = 100
MAX_OUTLINE_LABEL_CHARS = 200
DEFAULT_CHECK_TIMEOUT = 2.0
STDLIB_MODULE_ALIASES = frozenset({"os.path"})


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
    outline: tuple[str, ...] = ()
    dry_run_issues: tuple[ValidationIssue, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.issues

    def as_json(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "issues": [issue.as_json() for issue in self.issues],
            "outline": list(self.outline),
            "dryRunEligible": not self.dry_run_issues,
            "dryRunIssues": [issue.as_json() for issue in self.dry_run_issues],
        }


class WorkflowValidator:
    """Best-effort static validation that never executes workflow code."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        module_search_path: Sequence[str] | None = None,
        check_timeout: float = DEFAULT_CHECK_TIMEOUT,
    ) -> None:
        if check_timeout <= 0:
            raise ValueError("check_timeout must be positive")
        self._environment = os.environ if environment is None else environment
        self._cwd = Path.cwd() if cwd is None else cwd
        self._module_search_path = (
            self._workflow_module_search_path()
            if module_search_path is None
            else tuple(module_search_path)
        )
        self._check_timeout = check_timeout
        self._worker_lock = threading.Lock()
        self._workers: set[subprocess.Popen[str]] = set()
        self._closed = False

    def validate(
        self,
        code: str,
        *,
        environment: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> ValidationResult:
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

        return self._run_worker(code, environment=environment, cwd=cwd)

    def close(self) -> None:
        """Permanently prevent new helpers, then kill and reap active ones."""
        with self._worker_lock:
            self._closed = True
            workers = tuple(self._workers)
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
        for worker in workers:
            worker.wait()
        with self._worker_lock:
            self._workers.difference_update(workers)

    def _run_worker(
        self,
        code: str,
        *,
        environment: Mapping[str, str] | None,
        cwd: Path | None,
    ) -> ValidationResult:
        payload = json.dumps(
            {
                "code": code,
                "environment": dict(
                    self._environment if environment is None else environment
                ),
                "cwd": str(self._cwd if cwd is None else cwd),
                "moduleSearchPath": self._module_search_path,
            }
        )
        try:
            with self._worker_lock:
                if self._closed:
                    return ValidationResult(
                        (
                            ValidationIssue(
                                "validation",
                                "workflow validator is closed",
                            ),
                        )
                    )
                worker = subprocess.Popen(
                    self._worker_command(),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    shell=False,
                )
                self._workers.add(worker)
        except Exception as exc:
            return ValidationResult(
                (
                    ValidationIssue(
                        "validation",
                        f"could not start workflow validation checks: {type(exc).__name__}: {exc}",
                    ),
                )
            )
        try:
            try:
                stdout, stderr = worker.communicate(
                    payload, timeout=self._check_timeout
                )
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.communicate()
                return ValidationResult(
                    (
                        ValidationIssue(
                            "timeout",
                            f"workflow validation checks exceeded {self._check_timeout:g}s",
                        ),
                    )
                )
        finally:
            with self._worker_lock:
                self._workers.discard(worker)
        if worker.returncode != 0:
            detail = stderr.strip() or f"helper exited with status {worker.returncode}"
            return ValidationResult(
                (
                    ValidationIssue(
                        "validation",
                        f"workflow validation check failed: {detail}",
                    ),
                )
            )
        try:
            value = json.loads(stdout)
            issues = tuple(ValidationIssue(**issue) for issue in value["issues"])
            outline = tuple(value["outline"])
            dry_run_issues = tuple(
                ValidationIssue(**issue) for issue in value["dryRunIssues"]
            )
            if any(not isinstance(label, str) for label in outline):
                raise TypeError("outline labels must be strings")
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            return ValidationResult(
                (
                    ValidationIssue(
                        "validation",
                        f"workflow validation returned an invalid response: {type(exc).__name__}: {exc}",
                    ),
                )
            )
        return ValidationResult(issues, outline, dry_run_issues)

    @staticmethod
    def _worker_command() -> list[str]:
        return [sys.executable, "-m", "purplemux_client.preflight", "--check-worker"]

    def _validate_read_checks(
        self, tree: ast.Module
    ) -> tuple[
        tuple[ValidationIssue, ...],
        tuple[str, ...],
        tuple[ValidationIssue, ...],
    ]:
        issues: list[ValidationIssue] = []
        self._validate_imports(tree, issues)
        self._validate_required_environment(tree, issues)
        self._validate_metadata(tree, issues)
        outline = self._validate_outline(tree, issues)
        return tuple(issues), outline, self._validate_dry_run(tree)

    def _validate_dry_run(self, tree: ast.Module) -> tuple[ValidationIssue, ...]:
        issues: list[ValidationIssue] = []
        aliases: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    aliases[alias.asname or alias.name.split(".", 1)[0]] = alias.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        declarations = [
            node
            for node in tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            and self._assignment_name(node) == DRY_RUN_NAME
        ]
        if not declarations:
            return (
                ValidationIssue(
                    "dry_run",
                    f"declare {DRY_RUN_NAME} = {DRY_RUN_VERSION} to enable Dry Run",
                ),
            )
        declaration = declarations[-1]
        value_node = declaration.value
        try:
            value = ast.literal_eval(value_node) if value_node is not None else None
        except (ValueError, TypeError):
            value = None
        if value != DRY_RUN_VERSION:
            issues.append(
                ValidationIssue(
                    "dry_run",
                    f"{DRY_RUN_NAME} must be the literal integer {DRY_RUN_VERSION}",
                    declaration.lineno,
                    declaration.col_offset + 1,
                )
            )
        mutation_calls = {
            "subprocess.run",
            "subprocess.Popen",
            "subprocess.call",
            "subprocess.check_call",
            "subprocess.check_output",
            "os.system",
            "os.remove",
            "os.unlink",
            "os.rename",
            "os.replace",
            "os.mkdir",
            "os.makedirs",
            "os.chmod",
            "os.chown",
            "os.link",
            "os.symlink",
            "os.truncate",
            "shutil.copy",
            "shutil.copy2",
            "shutil.copyfile",
            "shutil.copymode",
            "shutil.copystat",
            "shutil.copytree",
            "shutil.move",
            "shutil.rmtree",
            "shutil.chown",
            "requests.post",
            "requests.put",
            "requests.patch",
            "requests.delete",
            "urllib.request.urlopen",
        }
        mutation_method_suffixes = {
            ".write_text",
            ".write_bytes",
            ".unlink",
            ".mkdir",
            ".rename",
            ".replace",
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = self._qualified_name(node.func)
            if name:
                root, separator, remainder = name.partition(".")
                name = aliases.get(root, root) + (
                    separator + remainder if separator else ""
                )
            raw_open = name in {"open", "builtins.open"} and self._open_call_can_write(
                node
            )
            raw_os_open = name == "os.open" and self._os_open_can_write(node)
            raw_method_open = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "open"
                and not (name or "").startswith("purplemux_client.")
                and self._method_open_can_write(node)
            )
            if raw_open:
                name = "open"
            raw_mutation_method = False
            if isinstance(node.func, ast.Attribute) and isinstance(
                node.func.value, ast.Call
            ):
                receiver = self._qualified_name(node.func.value.func)
                if receiver:
                    receiver = aliases.get(receiver, receiver)
                raw_mutation_method = receiver in {"pathlib.Path", "Path"} and any(
                    node.func.attr == suffix.removeprefix(".")
                    for suffix in mutation_method_suffixes
                )
            display_name = (
                name
                if name is not None
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else "unknown"
            )
            if (
                raw_open
                or raw_os_open
                or raw_method_open
                or name in mutation_calls
                or raw_mutation_method
                or (
                    name is not None
                    and any(
                        name.endswith(suffix) for suffix in mutation_method_suffixes
                    )
                )
            ):
                issues.append(
                    ValidationIssue(
                        "dry_run",
                        f"raw mutation-capable call {display_name} makes Dry Run ineligible",
                        node.lineno,
                        node.col_offset + 1,
                    )
                )
        return tuple(issues)

    @staticmethod
    def _open_call_can_write(node: ast.Call) -> bool:
        mode_node: ast.expr | None = node.args[1] if len(node.args) > 1 else None
        for keyword in node.keywords:
            if keyword.arg == "mode":
                mode_node = keyword.value
        if mode_node is None:
            return False
        try:
            mode = ast.literal_eval(mode_node)
        except (ValueError, TypeError):
            return True
        return not isinstance(mode, str) or (
            bool(mode)
            and set(mode) <= set("rwaxbt+")
            and any(flag in mode for flag in "wax+")
        )

    @staticmethod
    def _method_open_can_write(node: ast.Call) -> bool:
        mode_node: ast.expr | None = node.args[0] if node.args else None
        for keyword in node.keywords:
            if keyword.arg == "mode":
                mode_node = keyword.value
        if mode_node is None:
            return False
        try:
            mode = ast.literal_eval(mode_node)
        except (ValueError, TypeError):
            # Unknown receivers include safe factory methods such as
            # GitRepository.open(path). Literal write modes remain detectable
            # for both Path(...).open("w") and path.open("w").
            return False
        return not isinstance(mode, str) or (
            bool(mode)
            and set(mode) <= set("rwaxbt+")
            and any(flag in mode for flag in "wax+")
        )

    @staticmethod
    def _os_open_can_write(node: ast.Call) -> bool:
        flags_node: ast.expr | None = node.args[1] if len(node.args) > 1 else None
        for keyword in node.keywords:
            if keyword.arg == "flags":
                flags_node = keyword.value
        if flags_node is None:
            return True
        write_flags = {
            "O_WRONLY",
            "O_RDWR",
            "O_APPEND",
            "O_CREAT",
            "O_EXCL",
            "O_TRUNC",
            "O_TMPFILE",
        }
        for child in ast.walk(flags_node):
            if isinstance(child, ast.Name) and child.id in write_flags:
                return True
            if isinstance(child, ast.Attribute) and child.attr in write_flags:
                return True
        try:
            flags = ast.literal_eval(flags_node)
        except (ValueError, TypeError):
            return True
        return not isinstance(flags, int) or isinstance(flags, bool) or flags != 0

    @staticmethod
    def _qualified_name(node: ast.expr) -> str | None:
        parts: list[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            return None
        parts.append(node.id)
        return ".".join(reversed(parts))

    def _validate_imports(
        self, tree: ast.Module, issues: list[ValidationIssue]
    ) -> None:
        seen_modules: set[str] = set()
        for node in tree.body:
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
                if name in seen_modules:
                    continue
                seen_modules.add(name)
                try:
                    exists = self._module_exists(name)
                except Exception as exc:
                    issues.append(
                        ValidationIssue(
                            "import",
                            f"could not check Python dependency {name!r}: {type(exc).__name__}: {exc}",
                            getattr(node, "lineno", None),
                            self._column(node),
                        )
                    )
                    continue
                if not exists:
                    issues.append(
                        ValidationIssue(
                            "import",
                            f"Python dependency {name!r} is not available to the workflow process",
                            getattr(node, "lineno", None),
                            self._column(node),
                        )
                    )

    def _validate_required_environment(
        self, tree: ast.Module, issues: list[ValidationIssue]
    ) -> None:
        for statement in tree.body:
            expression = self._direct_expression(statement)
            if expression is None:
                continue
            for node in self._immediate_nodes(expression):
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

    @staticmethod
    def _direct_expression(statement: ast.stmt) -> ast.expr | None:
        if isinstance(statement, ast.Expr):
            return statement.value
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            return statement.value
        if isinstance(statement, (ast.If, ast.While)):
            return statement.test
        if isinstance(statement, (ast.For, ast.AsyncFor)):
            return statement.iter
        return None

    @staticmethod
    def _immediate_nodes(expression: ast.expr) -> tuple[ast.AST, ...]:
        nodes: list[ast.AST] = []

        class ImmediateVisitor(ast.NodeVisitor):
            def visit_Lambda(self, node: ast.Lambda) -> None:
                return

            def visit_IfExp(self, node: ast.IfExp) -> None:
                self.visit(node.test)

            def visit_BoolOp(self, node: ast.BoolOp) -> None:
                self.visit(node.values[0])

            def visit_ListComp(self, node: ast.ListComp) -> None:
                return

            def visit_SetComp(self, node: ast.SetComp) -> None:
                return

            def visit_DictComp(self, node: ast.DictComp) -> None:
                return

            def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
                return

            def generic_visit(self, node: ast.AST) -> None:
                nodes.append(node)
                super().generic_visit(node)

        ImmediateVisitor().visit(expression)
        return tuple(nodes)

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
        try:
            if (
                kind == "commands"
                and shutil.which(value, path=self._environment.get("PATH")) is None
            ):
                message = f"required command {value!r} was not found on PATH"
            elif kind == "imports" and not self._module_exists(value):
                message = f"required Python dependency {value!r} is not available"
            elif kind == "environment" and value not in self._environment:
                message = f"required environment variable {value!r} is not set"
            elif kind == "paths":
                path = Path(value).expanduser()
                if not path.is_absolute():
                    path = self._cwd / path
                if not path.exists():
                    message = f"required path {value!r} does not exist"
        except Exception as exc:
            message = (
                f"could not check required {kind.rstrip('s')} {value!r}: "
                f"{type(exc).__name__}: {exc}"
            )
        if message:
            issues.append(
                ValidationIssue(
                    kind.rstrip("s"), message, node.lineno, node.col_offset + 1
                )
            )

    def _validate_outline(
        self, tree: ast.Module, issues: list[ValidationIssue]
    ) -> tuple[str, ...]:
        declarations = [
            node
            for node in tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            and self._assignment_name(node) == OUTLINE_NAME
        ]
        if not declarations:
            return ()
        declaration = declarations[-1]
        value_node = declaration.value
        if value_node is None:
            self._outline_issue(issues, declaration, "must have a literal value")
            return ()
        try:
            value = ast.literal_eval(value_node)
        except (ValueError, TypeError):
            self._outline_issue(
                issues, declaration, "must be a literal list of strings"
            )
            return ()
        if not isinstance(value, list):
            self._outline_issue(issues, declaration, "must be a list of strings")
            return ()
        if len(value) > MAX_OUTLINE_ITEMS:
            self._outline_issue(
                issues,
                declaration,
                f"must contain at most {MAX_OUTLINE_ITEMS} items",
            )
            return ()
        for label in value:
            if not isinstance(label, str) or not label.strip():
                self._outline_issue(
                    issues, declaration, "items must be non-empty strings"
                )
                return ()
            if len(label) > MAX_OUTLINE_LABEL_CHARS:
                self._outline_issue(
                    issues,
                    declaration,
                    f"items must be at most {MAX_OUTLINE_LABEL_CHARS} characters",
                )
                return ()
            if not label.isprintable():
                self._outline_issue(
                    issues, declaration, "items must be human-readable text"
                )
                return ()
        return tuple(value)

    def _module_exists(self, name: str) -> bool:
        if name in STDLIB_MODULE_ALIASES:
            return True
        parts = name.split(".")
        if any(not part for part in parts):
            return False
        root = parts[0]
        if root in sys.builtin_module_names:
            return len(parts) == 1
        spec = importlib.machinery.PathFinder.find_spec(
            root, list(self._module_search_path)
        )
        if spec is None:
            return len(parts) == 1 and root in sys.stdlib_module_names
        qualified = root
        for part in parts[1:]:
            locations = spec.submodule_search_locations
            if locations is None:
                return False
            qualified = f"{qualified}.{part}"
            spec = importlib.machinery.PathFinder.find_spec(qualified, list(locations))
            if spec is None:
                return False
        return True

    @staticmethod
    def _workflow_module_search_path() -> tuple[str, ...]:
        return (tempfile.gettempdir(), *sys.path[1:])

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

    @staticmethod
    def _outline_issue(
        issues: list[ValidationIssue], node: ast.stmt, detail: str
    ) -> None:
        issues.append(
            ValidationIssue(
                "outline",
                f"{OUTLINE_NAME} {detail}",
                node.lineno,
                node.col_offset + 1,
            )
        )


def _run_check_worker() -> None:
    payload = json.load(sys.stdin)
    validator = WorkflowValidator(
        environment=payload["environment"],
        cwd=Path(payload["cwd"]),
        module_search_path=payload["moduleSearchPath"],
    )
    tree = ast.parse(payload["code"], filename="<workflow>")
    issues, outline, dry_run_issues = validator._validate_read_checks(tree)
    json.dump(
        {
            "issues": [issue.as_json() for issue in issues],
            "outline": list(outline),
            "dryRunIssues": [issue.as_json() for issue in dry_run_issues],
        },
        sys.stdout,
    )


if __name__ == "__main__" and sys.argv[1:] == ["--check-worker"]:
    _run_check_worker()
