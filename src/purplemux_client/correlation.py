from __future__ import annotations

import hashlib
import os
import re
import secrets

RUN_IDENTITY_ENV = "AGENT_WORKFLOW_MANAGER_RUN_IDENTITY"
_PROCESS_NAMESPACE = secrets.token_hex(16)
_SAFE_LOGICAL = re.compile(r"[^A-Za-z0-9_-]+")


def run_correlation(logical_name: str) -> str:
    """Derive one valid, stable correlation identity for a logical run resource."""
    if not isinstance(logical_name, str):
        raise TypeError("logical resource name must be a string")
    if not logical_name.strip() or "\0" in logical_name:
        raise ValueError("logical resource name must be non-empty and contain no nulls")
    namespace = os.environ.get(RUN_IDENTITY_ENV) or _PROCESS_NAMESPACE
    digest = hashlib.sha256(f"{namespace}\0{logical_name}".encode()).hexdigest()[:20]
    readable = _SAFE_LOGICAL.sub("-", logical_name).strip("-_")[:40]
    if not readable:
        readable = "resource"
    return f"{readable}-{digest}"
