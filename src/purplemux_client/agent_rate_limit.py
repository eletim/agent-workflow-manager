"""Recognize provider throttling without treating terminal text as a result."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any

from purplemux_client.errors import WorkerFailure

# Match provider error messages, not a bare number or a review discussing HTTP 429.
_RATE_LIMIT = re.compile(
    r"(?im)^\s*(?:[■✕×!❌]\s*)?(?:error:\s*)?(?:"
    r"exceeded retry limit,?\s*last status:\s*429\b[^\n]*|"
    r"(?:HTTP(?:/\d(?:\.\d)?)?\s+)?429\s+Too Many Requests\b[^\n]*|"
    r"(?:API Error:\s*)?429\s*[:{][^\n]*|"
    r"(?:you(?:'ve| have) (?:hit|reached) (?:your |the )?(?:usage|rate|request) limit|usage limit reached|rate limit exceeded)\b[^\n]*"
    r")\s*$"
)
_RETRY_AFTER = re.compile(r"(?im)\bretry[- ]after\s*[:=]\s*([^\r\n;]+)")


@dataclass(frozen=True)
class RateLimitEvidence:
    fingerprint: str
    retry_after_seconds: float | None = None


def detect_agent_rate_limit(text: object) -> RateLimitEvidence | None:
    if not isinstance(text, str):
        return None
    # Structured review/task results may quote errors; they are not provider errors.
    try:
        if isinstance(json.loads(text), (dict, list)):
            return None
    except (ValueError, TypeError):
        pass
    clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)[-8000:]
    matches = list(_RATE_LIMIT.finditer(clean))
    if not matches:
        return None
    match = matches[-1]
    # A newer prompt/output after the notice makes the old notice non-authoritative.
    tail = clean[match.end() :].strip()
    if tail and not all(
        line.lstrip()
        .lower()
        .startswith(("›", ">", "?", "tip:", "retry-after:", "too many requests"))
        for line in tail.splitlines()
    ):
        return None
    delay_match = _RETRY_AFTER.search(match.group() + "\n" + tail)
    delay = None
    if delay_match:
        try:
            delay = float(delay_match[1].strip())
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(delay_match[1].strip())
                if retry_at.tzinfo is not None:
                    delay = max(0.0, retry_at.timestamp() - time.time())
            except (ValueError, TypeError, OverflowError):
                pass
    if delay is not None and (not math.isfinite(delay) or delay < 0):
        delay = None
    return RateLimitEvidence(
        hashlib.sha256(match.group().strip().encode()).hexdigest(), delay
    )


class AgentRateLimited(WorkerFailure):
    """Sanitized provider failure with enough identity to re-observe the same turn."""

    def __init__(
        self, tab_id: str, status: dict[str, Any], evidence: RateLimitEvidence
    ) -> None:
        super().__init__(f"Agent provider rate limit (429/usage limit); tab={tab_id}")
        self.tab_id = tab_id
        self.provider = status.get("agentProviderId")
        self.agent_session_id = status.get("agentSessionId")
        self.event_seq = status.get("eventSeq")
        self.evidence = evidence
