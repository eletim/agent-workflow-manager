from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from purplemux_client.codex_trust import ensure_codex_project_trust
from purplemux_client.errors import WorkerFailure


def fake_app_server(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "requests.jsonl"
    server = tmp_path / "codex"
    server.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "projects = {}\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    with open(os.environ['AWM_TEST_CODEX_LOG'], 'a') as stream:\n"
        "        stream.write(json.dumps(request) + '\\n')\n"
        "    request_id = request.get('id')\n"
        "    if request_id == 1:\n"
        "        result = {'codexHome': '/codex', 'userAgent': 'fake', "
        "'platformFamily': 'unix', 'platformOs': 'linux'}\n"
        "    elif request_id == 2:\n"
        "        key = request['params']['edits'][0]['keyPath']\n"
        "        path = json.loads(key[len('projects.'):-len('.trust_level')])\n"
        "        projects[path] = {'trust_level': "
        "os.environ.get('AWM_TEST_TRUST_LEVEL', 'trusted')}\n"
        "        result = {'status': 'ok', 'version': '1', "
        "'filePath': '/codex/config.toml', 'overriddenMetadata': None}\n"
        "    elif request_id == 3:\n"
        "        path = request['params']['cwd']\n"
        "        result = {'config': {'projects': {path: projects[path]}}, "
        "'origins': {}}\n"
        "    else:\n"
        "        continue\n"
        "    print(json.dumps({'id': request_id, 'result': result}), flush=True)\n",
        encoding="utf-8",
    )
    server.chmod(0o755)
    return server, log


def racing_app_server(tmp_path: Path) -> tuple[Path, Path]:
    store = tmp_path / "projects.json"
    store.write_text("{}", encoding="utf-8")
    server = tmp_path / "racing-codex"
    server.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "store = Path(os.environ['AWM_TEST_CODEX_STORE'])\n"
        "projects = json.loads(store.read_text())\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    request_id = request.get('id')\n"
        "    if request_id == 1:\n"
        "        result = {'codexHome': os.environ['CODEX_HOME'], "
        "'userAgent': 'fake', 'platformFamily': 'unix', 'platformOs': 'linux'}\n"
        "    elif request_id == 2:\n"
        "        key = request['params']['edits'][0]['keyPath']\n"
        "        path = json.loads(key[len('projects.'):-len('.trust_level')])\n"
        "        time.sleep(0.2)\n"
        "        projects[path] = {'trust_level': 'trusted'}\n"
        "        temporary = store.with_name(store.name + '.' + str(os.getpid()))\n"
        "        temporary.write_text(json.dumps(projects))\n"
        "        os.replace(temporary, store)\n"
        "        result = {'status': 'ok', 'version': '1', "
        "'filePath': str(store), 'overriddenMetadata': None}\n"
        "    elif request_id == 3:\n"
        "        result = {'config': {'projects': projects}, 'origins': {}}\n"
        "    else:\n"
        "        continue\n"
        "    print(json.dumps({'id': request_id, 'result': result}), flush=True)\n",
        encoding="utf-8",
    )
    server.chmod(0o755)
    return server, store


def test_trusts_only_the_exact_canonical_project_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, log = fake_app_server(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(project, target_is_directory=True)
    monkeypatch.setenv("AWM_TEST_CODEX_LOG", str(log))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    assert ensure_codex_project_trust(str(alias), executable=str(server)) == str(
        project
    )
    assert ensure_codex_project_trust(str(project), executable=str(server)) == str(
        project
    )

    requests = [json.loads(line) for line in log.read_text().splitlines()]
    writes = [item for item in requests if item.get("method") == "config/batchWrite"]
    assert len(writes) == 2
    assert {item["params"]["edits"][0]["keyPath"] for item in writes} == {
        f"projects.{json.dumps(str(project))}.trust_level"
    }
    assert all(
        item["params"]["edits"]
        == [
            {
                "keyPath": f"projects.{json.dumps(str(project))}.trust_level",
                "value": "trusted",
                "mergeStrategy": "upsert",
            }
        ]
        for item in writes
    )


def test_missing_project_fails_before_starting_codex(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(WorkerFailure, match="could not be resolved"):
        ensure_codex_project_trust(str(missing), executable="must-not-run")


def test_unavailable_app_server_fails_with_startup_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    with pytest.raises(WorkerFailure, match="could not start Codex project trust"):
        ensure_codex_project_trust(
            str(tmp_path), executable=str(tmp_path / "missing-codex")
        )


def test_effective_untrusted_value_fails_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, log = fake_app_server(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("AWM_TEST_CODEX_LOG", str(log))
    monkeypatch.setenv("AWM_TEST_TRUST_LEVEL", "untrusted")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    with pytest.raises(WorkerFailure, match="did not confirm project trust"):
        ensure_codex_project_trust(str(project), executable=str(server))


def test_concurrent_processes_preserve_both_project_trust_entries(
    tmp_path: Path,
) -> None:
    server, store = racing_app_server(tmp_path)
    projects = [tmp_path / "project-one", tmp_path / "project-two"]
    for project in projects:
        project.mkdir()
    environment = dict(os.environ)
    environment["CODEX_HOME"] = str(tmp_path / "codex-home")
    environment["AWM_TEST_CODEX_STORE"] = str(store)
    program = (
        "from purplemux_client.codex_trust import ensure_codex_project_trust; "
        "import sys; ensure_codex_project_trust(sys.argv[1], executable=sys.argv[2])"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", program, str(project), str(server)],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for project in projects
    ]
    results = [process.communicate(timeout=10) for process in processes]

    assert [
        (process.returncode, stderr) for process, (_, stderr) in zip(processes, results)
    ] == [
        (0, ""),
        (0, ""),
    ]
    assert json.loads(store.read_text()) == {
        str(project.resolve()): {"trust_level": "trusted"} for project in projects
    }
