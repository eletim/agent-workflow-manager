from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
README = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")


def test_personal_setup_is_first_and_uses_custom_purplemux_cli() -> None:
    assert README.startswith("## Personal self-hosted setup\n")
    assert "git clone https://github.com/eletim/purplemux.git" in README
    assert 'exec node "$HOME/DevEnv/purplemux/bin/cli.js" "$@"' in README
    assert "Do **not** substitute" in README
    assert "npm install -g purplemux" in README


def test_personal_setup_starts_agent_workflow_manager_without_secrets() -> None:
    personal_setup, _ = README.split("# Agent Workflow Manager", maxsplit=1)

    assert "bash start.sh" in personal_setup
    assert "PMUX_TOKEN=" not in personal_setup
    assert "cli-token" not in personal_setup
