from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from purplemux_client.review_fix import parse_review_fix_json

ROOT = Path(__file__).parents[1]
GUIDE = ROOT / "src/purplemux_client/web_static/review-fix-guide.md"


def test_documented_review_fix_example_matches_the_parser(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "remote",
            "add",
            "origin",
            "https://github.com/acme/repository.git",
        ],
        check=True,
    )
    text = GUIDE.read_text(encoding="utf-8")
    match = re.search(r"```json\n(.*?)\n```", text, re.DOTALL)
    assert match is not None
    declaration = json.loads(match.group(1))
    declaration["repository"] = str(repository)

    config = parse_review_fix_json(json.dumps(declaration))

    assert config.max_iterations == 5
    assert config.start.command == "./start-dev.sh"
    assert config.start.ready_check == "http://127.0.0.1:3000/api/health"
    assert config.review_agent == "codex"
    assert config.implementation_agent == "codex"


def test_guide_keeps_control_flow_in_generated_python() -> None:
    guide = " ".join(GUIDE.read_text(encoding="utf-8").split())

    assert "generated Python owns service startup" in guide
    assert "only generates, submits, and observes" in guide
    assert "Only a `FAIL` result" in guide
    assert "`BLOCKED` ends without an implementation attempt" in guide
