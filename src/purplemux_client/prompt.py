from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

PromptAgent = Literal["codex", "claude-code"]
PROMPT_AGENTS = frozenset({"codex", "claude-code"})


@dataclass(frozen=True)
class PromptExecution:
    """User-authored inputs retained with a generated one-step run."""

    agent: PromptAgent
    cwd: str
    prompt: str

    def as_json(self) -> dict[str, str]:
        return {"agent": self.agent, "cwd": self.cwd, "prompt": self.prompt}


def prepare_prompt_execution(*, agent: str, cwd: str, prompt: str) -> PromptExecution:
    """Validate Prompt-mode inputs and resolve its authoritative directory."""
    if agent not in PROMPT_AGENTS:
        raise ValueError("agent must be codex or claude-code")
    if not cwd or "\0" in cwd:
        raise ValueError("cwd must be a non-empty directory path")
    if not prompt or not prompt.strip():
        raise ValueError("prompt must not be empty")
    if "\0" in prompt:
        raise ValueError("prompt must not contain null bytes")
    try:
        resolved = Path(cwd).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"cwd could not be resolved: {exc}") from exc
    if not resolved.is_dir():
        raise ValueError(f"cwd is not a directory: {resolved}")
    return PromptExecution(cast(PromptAgent, agent), str(resolved), prompt)


def build_prompt_workflow(execution: PromptExecution) -> str:
    """Generate the plain Python run used by the shared runner in Prompt mode."""
    correlation = f"prompt-{secrets.token_hex(8)}"
    values = {
        "agent": execution.agent,
        "cwd": execution.cwd,
        "prompt": execution.prompt,
        "correlation": correlation,
    }
    literal = {key: json.dumps(value) for key, value in values.items()}
    return f"""\
import signal

from purplemux_client import (
    CreateSessionRequest,
    CreateWorkspaceRequest,
    PurpleMuxRuntime,
    emit_step,
)

WORKFLOW_OUTLINE = ["Prompt"]

agent = {literal["agent"]}
cwd = {literal["cwd"]}
prompt = {literal["prompt"]}
correlation = {literal["correlation"]}
client = None
tab = None


def stop_prompt(signum, _frame):
    if client is not None and tab is not None:
        try:
            client.interrupt(tab)
        except BaseException:
            pass
    raise SystemExit(128 + signum)


signal.signal(signal.SIGTERM, stop_prompt)

emit_step("Prompt", "started")
try:
    runtime = PurpleMuxRuntime()
    workspace = runtime.create_workspace(
        CreateWorkspaceRequest(
            cwd=cwd,
            name="AWM Prompt",
            correlation_id=correlation,
        )
    )
    client = runtime.workspace(workspace.id)
    tab = client.create_session(
        CreateSessionRequest(
            worker=agent,
            cwd=cwd,
            command=agent,
            correlation_id=correlation,
        )
    )
    client.wait_until_ready(tab, 60)
    client.send_input(tab, prompt)
    client.wait_for_turn_completion(tab, 3600)
    result = client.read_result(tab)
except BaseException as exc:
    emit_step("Prompt", "failed", error=str(exc))
    raise
else:
    print(result)
    emit_step("Prompt", "completed", workspace=workspace.id, tab=tab)
"""
