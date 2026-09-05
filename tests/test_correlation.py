from __future__ import annotations

import re

import pytest

from purplemux_client import run_correlation
from purplemux_client.correlation import RUN_IDENTITY_ENV


def test_same_run_and_logical_name_is_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(RUN_IDENTITY_ENV, "runner-run-a")

    first = run_correlation("issue-89 implementer")

    assert run_correlation("issue-89 implementer") == first
    assert run_correlation("issue-89 reviewer") != first
    assert 1 <= len(first) <= 64
    assert re.fullmatch(r"[A-Za-z0-9_-]+", first)


def test_same_logical_name_differs_across_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(RUN_IDENTITY_ENV, "runner-run-a")
    first = run_correlation("workspace")
    monkeypatch.setenv(RUN_IDENTITY_ENV, "runner-run-b")

    assert run_correlation("workspace") != first


def test_direct_execution_uses_process_stable_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(RUN_IDENTITY_ENV, raising=False)

    assert run_correlation("workspace") == run_correlation("workspace")


@pytest.mark.parametrize("logical_name", ["", "   ", "bad\0name"])
def test_logical_name_validation(logical_name: str) -> None:
    with pytest.raises(ValueError, match="logical resource name"):
        run_correlation(logical_name)


def test_long_unicode_name_still_produces_valid_bounded_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(RUN_IDENTITY_ENV, "runner-run-a")

    correlation = run_correlation("レビュー担当" * 100)

    assert len(correlation) <= 64
    assert re.fullmatch(r"[A-Za-z0-9_-]+", correlation)
