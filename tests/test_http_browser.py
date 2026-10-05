from __future__ import annotations

import re
import shutil
import socket
import threading
import time
from collections.abc import Iterator

import pytest

from purplemux_client.prompt import PromptExecution
from purplemux_client.runner import PythonRunner
from purplemux_client.web import RunnerHTTPServer

selenium = pytest.importorskip("selenium")
from selenium import webdriver  # noqa: E402
from selenium.webdriver.chrome.options import Options  # noqa: E402
from selenium.webdriver.common.by import By  # noqa: E402
from selenium.webdriver.common.keys import Keys  # noqa: E402
from selenium.webdriver.support.ui import WebDriverWait  # noqa: E402


def _private_http_host() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 80))
        return str(probe.getsockname()[0])
    finally:
        probe.close()


@pytest.fixture
def insecure_browser_server() -> Iterator[tuple[str, PythonRunner]]:
    host = _private_http_host()
    if host.startswith("127."):
        pytest.skip("a non-loopback HTTP interface is required")
    runner = PythonRunner(managed_workflows=False, stop_timeout=0.5)
    runner.start(
        'import sys\nprint("HTTP_STDOUT")\nprint("HTTP_STDERR", file=sys.stderr)\n'
    )
    deadline = time.monotonic() + 5
    while runner.snapshot().state == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert runner.snapshot().state == "success"

    server = RunnerHTTPServer((host, 0), runner)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://{host}:{server.server_address[1]}/", runner
    server.shutdown()
    server.server_close()
    thread.join()


def test_runtime_selected_run_retains_mode_detail(tmp_path) -> None:
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if chrome is None:
        pytest.skip("Chrome or Chromium is required for the HTTP browser smoke test")

    runner = PythonRunner(managed_workflows=False, stop_timeout=0.5)
    server = None
    thread = None
    driver = None
    try:
        workflow_code = 'print("WORKFLOW_DETAIL")'
        issue_code = 'print("ISSUE_DETAIL")'
        issue_json = '{"mode":"issue-driven","repository":"acme/project"}'
        prompt_text = "Retain this exact selected-run prompt."
        run_ids = {
            "workflow": runner.start(workflow_code),
            "issue": runner.start(issue_code, issue_driven_json=issue_json),
            "prompt": runner.start(
                'print("PROMPT_DETAIL")',
                prompt=PromptExecution("codex", str(tmp_path), prompt_text),
            ),
        }
        deadline = time.monotonic() + 5
        while (
            any(
                runner.snapshot(run_id).state == "running"
                for run_id in run_ids.values()
            )
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert all(
            runner.snapshot(run_id).state == "success" for run_id in run_ids.values()
        )

        server = RunnerHTTPServer(("127.0.0.1", 0), runner)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        options = Options()
        options.binary_location = chrome
        for argument in (
            "--headless=new",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--no-proxy-server",
        ):
            options.add_argument(argument)
        driver = webdriver.Chrome(options=options)
        driver.get(f"http://127.0.0.1:{server.server_address[1]}/")
        wait = WebDriverWait(driver, 5)
        wait.until(
            lambda browser: browser.find_element(
                By.ID, "issue-driven-mode"
            ).is_displayed()
        )
        wait.until(
            lambda browser: browser.find_element(By.ID, "run-list").is_displayed()
        )
        assert driver.find_element(By.ID, "issue-driven-fields").is_displayed()
        assert driver.find_element(By.ID, "new-run").get_attribute("aria-pressed") == "true"

        for run_id, field_id, expected in (
            (run_ids["prompt"], "prompt-text", prompt_text),
            (run_ids["issue"], "issue-driven-json", issue_json),
            (run_ids["issue"], "issue-driven-python", issue_code),
            (run_ids["workflow"], "code", workflow_code),
        ):
            driver.find_element(By.CSS_SELECTOR, f'[data-run-id="{run_id}"]').click()
            wait.until(
                lambda browser: browser.find_element(By.ID, "runtime-panel").is_displayed()
            )
            field = driver.find_element(By.ID, field_id)
            assert field.get_attribute("value") == expected
            assert field.get_attribute("readonly") is not None
            assert (
                driver.find_element(By.ID, "new-run").get_attribute("aria-pressed")
                == "false"
            )
    finally:
        if driver is not None:
            driver.quit()
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join()
        runner.close()


def test_copy_actions_on_insecure_http_origin(
    insecure_browser_server: tuple[str, PythonRunner],
) -> None:
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if chrome is None:
        pytest.skip("Chrome or Chromium is required for the HTTP browser smoke test")

    options = Options()
    options.binary_location = chrome
    for argument in (
        "--headless=new",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--no-proxy-server",
        "--disable-features=HttpsUpgrades",
    ):
        options.add_argument(argument)

    driver = webdriver.Chrome(options=options)
    try:
        url, runner = insecure_browser_server
        driver.get(url)
        wait = WebDriverWait(driver, 5)
        wait.until(
            lambda browser: browser.find_element(
                By.ID, "issue-driven-mode"
            ).is_displayed()
        )
        wait.until(
            lambda browser: browser.find_element(
                By.CSS_SELECTOR, "#run-list .run-item"
            ).is_displayed()
        )
        driver.find_element(By.CSS_SELECTOR, "#run-list .run-item").click()
        wait.until(
            lambda browser: browser.find_element(By.ID, "stdout").text.endswith(
                "  HTTP_STDOUT"
            )
        )
        timestamped_stdout = driver.find_element(By.ID, "stdout").text
        assert re.fullmatch(
            r"(?:Today|Yesterday|\d{1,2}/\d{1,2}) \d{2}:\d{2}:\d{2}  HTTP_STDOUT",
            timestamped_stdout,
        )
        assert driver.execute_script("return window.isSecureContext") is False
        assert driver.execute_script("return typeof navigator.clipboard") == "undefined"

        run_id = runner.start("import time\ntime.sleep(30)\n")
        wait.until(
            lambda browser: (
                browser.find_element(By.ID, "favicon")
                .get_attribute("href")
                .startswith("data:image/svg+xml,")
            )
        )
        runner.stop(run_id)
        wait.until(
            lambda browser: (
                browser.find_element(By.ID, "favicon")
                .get_attribute("href")
                .endswith("/favicon.svg")
            )
        )

        driver.find_element(By.ID, "guide-open").click()
        wait.until(
            lambda browser: browser.find_element(By.ID, "guide-copy").is_enabled()
        )
        guide = driver.find_element(By.ID, "guide-content").get_attribute("textContent")
        driver.find_element(By.ID, "guide-copy").click()
        assert driver.find_element(By.ID, "guide-copy").text == "Copied"
        driver.find_element(By.ID, "guide-close").click()

        # The code editor is a read-only view of the selected run's own
        # snapshot; using it as a scratch paste target requires switching to
        # the New run draft first.
        driver.find_element(By.ID, "new-run").click()
        wait.until(
            lambda browser: (
                browser.find_element(By.ID, "code").get_attribute("readonly") is None
            )
        )

        editor = driver.find_element(By.ID, "code")
        editor.click()
        editor.send_keys(Keys.CONTROL, "a")
        editor.send_keys(Keys.CONTROL, "v")
        assert editor.get_attribute("value") == guide

        assert not driver.find_element(By.ID, "output-copy").is_enabled()
        assert editor.get_attribute("value") == guide

        driver.find_element(By.ID, "guide-open").click()
        driver.execute_script("document.execCommand = () => false")
        driver.find_element(By.ID, "guide-copy").click()
        manual = driver.find_element(By.ID, "manual-copy-content")
        assert driver.find_element(By.ID, "guide-copy").text == "Copy manually"
        assert manual.get_attribute("value") == guide
        assert driver.execute_script(
            "return [arguments[0].selectionStart, arguments[0].selectionEnd]", manual
        ) == [0, len(guide)]
    finally:
        driver.quit()


def test_runner_is_usable_at_mobile_and_desktop_viewports(
    insecure_browser_server: tuple[str, PythonRunner],
) -> None:
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if chrome is None:
        pytest.skip("Chrome or Chromium is required for the HTTP browser smoke test")

    options = Options()
    options.binary_location = chrome
    for argument in (
        "--headless=new",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--no-proxy-server",
        "--disable-features=HttpsUpgrades",
    ):
        options.add_argument(argument)

    driver = webdriver.Chrome(options=options)
    try:
        url, _ = insecure_browser_server
        driver.set_window_rect(width=390, height=844)
        driver.get(url)
        wait = WebDriverWait(driver, 5)
        wait.until(
            lambda browser: browser.find_element(
                By.ID, "issue-driven-mode"
            ).is_displayed()
        )

        def page_fits_viewport(browser: webdriver.Chrome) -> bool:
            return bool(
                browser.execute_script(
                    "return document.documentElement.scrollWidth <= "
                    "document.documentElement.clientWidth"
                )
            )

        assert page_fits_viewport(driver)
        assert driver.find_element(By.ID, "issue-driven-fields").is_displayed()
        assert not driver.find_element(By.ID, "workflow-fields").is_displayed()
        for control_id in (
            "new-run",
            "issue-driven-mode",
            "settings-open",
        ):
            assert driver.find_element(By.ID, control_id).is_displayed()
        assert driver.find_element(By.ID, "diagnostics-panel").is_displayed()

        runtime_panel = driver.find_element(By.ID, "runtime-panel")
        assert not runtime_panel.is_displayed()
        assert (
            driver.find_element(By.ID, "new-run").get_attribute("aria-pressed")
            == "true"
        )
        assert (
            driver.find_element(By.ID, "issue-driven-mode").get_attribute(
                "aria-pressed"
            )
            == "true"
        )
        developer_views = driver.find_element(By.ID, "developer-views")
        assert developer_views.get_attribute("open") is None

        driver.find_element(By.ID, "issue-driven-mode").click()
        wait.until(
            lambda browser: browser.find_element(
                By.ID, "issue-driven-fields"
            ).is_displayed()
        )
        for control_id in ("validate", "dry-run", "run"):
            assert driver.find_element(By.ID, control_id).is_displayed()
        issue_driven_json = driver.find_element(By.ID, "issue-driven-json")
        issue_driven_json.send_keys(" ")
        preserved_issue_driven_json = issue_driven_json.get_attribute("value")

        assert driver.find_element(By.ID, "run-list").is_displayed()
        assert not driver.find_elements(By.CSS_SELECTOR, "#run-list .run-item.selected")
        assert not runtime_panel.is_displayed()
        assert developer_views.get_attribute("open") is None
        assert driver.find_element(By.ID, "new-run").is_displayed()
        assert driver.find_element(By.ID, "new-run").get_attribute("aria-pressed") == "true"
        assert driver.find_element(By.CSS_SELECTOR, ".runs-title").text == "RUNS"
        assert issue_driven_json.get_attribute("value") == preserved_issue_driven_json

        driver.find_element(By.CSS_SELECTOR, "#run-list .run-item").click()
        wait.until(lambda browser: runtime_panel.is_displayed())
        assert driver.find_element(By.ID, "new-run").get_attribute("aria-pressed") == "false"
        progress_panel = driver.find_element(By.CSS_SELECTOR, ".progress-panel")
        assert progress_panel.is_displayed()
        assert progress_panel.find_element(By.TAG_NAME, "h2").text == "Progress"
        assert driver.find_element(By.ID, "progress-empty").is_displayed()
        assert driver.find_element(By.ID, "stdout").text.endswith("HTTP_STDOUT")
        assert driver.find_element(By.ID, "stderr").text.endswith("HTTP_STDERR")
        driver.find_element(By.ID, "new-run").click()

        mobile_workflow = (
            "from purplemux_client import emit_step\n"
            "import sys\n"
            'WORKFLOW_OUTLINE = ["mobile step"]\n'
            'emit_step("mobile step", "started")\n'
            'print("X" * 4000, flush=True)\n'
            'print("MOBILE_STDERR", file=sys.stderr, flush=True)\n'
            "import time\n"
            "time.sleep(30)\n"
        )
        editor = driver.find_element(By.ID, "code")
        driver.execute_script(
            "arguments[0].value = arguments[1]; "
            "arguments[0].dispatchEvent(new Event('input', {bubbles: true}));",
            editor,
            mobile_workflow,
        )
        driver.find_element(By.ID, "run").click()
        wait.until(lambda browser: browser.find_element(By.ID, "stop").is_enabled())
        wait.until(lambda browser: runtime_panel.is_displayed())
        wait.until(
            lambda browser: browser.find_element(By.ID, "outline-panel").is_displayed()
        )
        wait.until(
            lambda browser: len(browser.find_element(By.ID, "stdout").text) >= 4000
        )
        wait.until(
            lambda browser: browser.find_element(By.ID, "stderr").text.endswith(
                "MOBILE_STDERR"
            )
        )
        assert driver.find_element(By.ID, "progress").is_displayed()
        assert driver.find_element(By.ID, "stdout").is_displayed()
        assert driver.find_element(By.ID, "stderr").is_displayed()
        assert page_fits_viewport(driver)
        driver.find_element(By.ID, "stop").click()
        wait.until(lambda browser: not browser.find_element(By.ID, "stop").is_enabled())

        driver.find_element(By.ID, "settings-open").click()
        assert driver.find_element(By.ID, "settings-dialog").is_displayed()
        driver.find_element(By.ID, "settings-close").click()
        driver.find_element(By.CSS_SELECTOR, "#diagnostics-panel > summary").click()
        assert driver.find_element(By.ID, "refresh-readiness").is_displayed()

        driver.set_window_rect(width=1200, height=900)
        wait.until(page_fits_viewport)
        assert driver.find_element(By.ID, "run-list").is_displayed()
        assert driver.find_element(By.ID, "stdout").is_displayed()
    finally:
        driver.quit()


def test_issue_driven_story_survives_failure_recovery_and_browser_reload() -> None:
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if chrome is None:
        pytest.skip("Chrome or Chromium is required for the HTTP browser smoke test")

    exact_prompt = (
        "Review the authoritative head exactly.\n"
        "This sentinel comes only from the backend trace: 現実-🎯-actual."
    )
    workflow = f"""\
from purplemux_client import (emit_agent_turn, emit_issue_driven_context, emit_issue_navigation)
WORKFLOW_OUTLINE = ["Work items", "Final integration PR"]
emit_issue_driven_context("acme/project", "dev/v1", "main")
emit_issue_navigation("mini-task:ux", 40, "https://github.com/acme/project/pull/40", workspace_id="ws-story", implementation_tab_id="tab-implementation", scope_review_tab_id="tab-scope", correctness_review_tab_id="tab-correctness", label="Mini task ux")
emit_agent_turn(1, "Review the implementation", "reviewer", 1, "started", repository="acme/project", phase="correctness-review", work_item_id="mini-task:ux", work_item_label="Mini task ux", commit_sha={"a" * 40!r}, prompt={exact_prompt!r})
emit_agent_turn(1, "Review the implementation", "reviewer", 1, "completed", repository="acme/project", phase="correctness-review", work_item_id="mini-task:ux", work_item_label="Mini task ux", transition_outcome="changes_requested", commit_sha={"a" * 40!r}, result="CHANGES_REQUESTED")
emit_agent_turn(2, "Fix the requested changes", "implementer", 1, "started", repository="acme/project", phase="fix", work_item_id="mini-task:ux", work_item_label="Mini task ux", commit_sha={"a" * 40!r}, prompt="Apply only the requested fix.")
emit_agent_turn(2, "Fix the requested changes", "implementer", 1, "failed", repository="acme/project", phase="fix", work_item_id="mini-task:ux", work_item_label="Mini task ux", commit_sha={"a" * 40!r}, error="agent stopped before producing a result")
raise RuntimeError("workflow failed after the authoritative turn failure")
"""
    preview = {
        "status": "planned",
        "phases": ["Work items", "Final integration PR"],
        "agents": [
            {
                "role": "Reviewer",
                "agent": "codex",
                "purpose": "Reviews each implementation head.",
            }
        ],
    }
    issue_driven_json = '{"mode":"issue-driven","one_shot_issue":322}'
    runner = PythonRunner(managed_workflows=False, stop_timeout=0.5)
    server = None
    thread = None
    driver = None
    try:
        failed_run_id = runner.start(
            workflow,
            issue_driven_json=issue_driven_json,
            issue_driven_preview=preview,
        )
        deadline = time.monotonic() + 5
        while (
            runner.snapshot(failed_run_id).state == "running"
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert runner.snapshot(failed_run_id).state == "failed"

        recovered_run_id = runner.resume(failed_run_id)
        deadline = time.monotonic() + 5
        while (
            runner.snapshot(recovered_run_id).state == "running"
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert runner.snapshot(recovered_run_id).state == "failed"
        authoritative_traces = {
            run_id: runner.snapshot(run_id).as_json()["agentTurns"]
            for run_id in (failed_run_id, recovered_run_id)
        }

        server = RunnerHTTPServer(("127.0.0.1", 0), runner)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_address[1]}/"

        options = Options()
        options.binary_location = chrome
        for argument in (
            "--headless=new",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--no-proxy-server",
        ):
            options.add_argument(argument)
        driver = webdriver.Chrome(options=options)
        driver.get(url)
        wait = WebDriverWait(driver, 5)

        wait.until(
            lambda browser: browser.find_element(
                By.ID, "issue-driven-mode"
            ).is_displayed()
        )
        assert driver.find_element(By.ID, "issue-driven-fields").is_displayed()
        assert not driver.find_element(By.ID, "workflow-fields").is_displayed()
        assert not driver.find_element(By.ID, "runtime-panel").is_displayed()
        assert driver.find_element(By.ID, "new-run").get_attribute("aria-pressed") == "true"
        developer_views = driver.find_element(By.ID, "developer-views")
        assert developer_views.get_attribute("open") is None

        failed_item = wait.until(
            lambda browser: browser.find_element(
                By.CSS_SELECTOR, f'[data-run-id="{failed_run_id}"]'
            )
        )
        failed_item.click()
        wait.until(
            lambda browser: (
                "ACTUAL" in browser.find_element(By.ID, "workflow-story-state").text
            )
        )
        assert driver.find_element(By.ID, "outline-title").text == (
            "Planned run preview"
        )
        assert (
            "not actual execution"
            in driver.find_element(By.ID, "outline-description").text
        )
        turns = driver.find_elements(By.CSS_SELECTOR, "#agent-turns > .agent-turn")
        assert len(turns) == 2
        assert "Changes Requested" in turns[0].text
        assert "Correctness Review → Fix" in turns[0].text
        assert "agent stopped before producing a result" in turns[1].text

        prompt_details = turns[0].find_element(
            By.CSS_SELECTOR, "details.agent-turn-prompt"
        )
        assert prompt_details.get_attribute("open") is None
        assert prompt_details.find_element(By.TAG_NAME, "summary").text == (
            "Show exact actual prompt"
        )
        prompt_details.find_element(By.TAG_NAME, "summary").click()
        assert prompt_details.find_element(By.TAG_NAME, "pre").text == exact_prompt

        driver.find_element(
            By.CSS_SELECTOR, f'[data-run-id="{recovered_run_id}"]'
        ).click()
        wait.until(
            lambda browser: (
                f"Run #{failed_run_id} (FAILED) → Run #{recovered_run_id}"
                in browser.find_element(By.ID, "workflow-recovery-transition").text
            )
        )
        recovery = driver.find_element(By.ID, "workflow-recovery-transition")
        assert f"Run #{failed_run_id} (FAILED)" in recovery.text
        assert f"→ Run #{recovered_run_id}" in recovery.text

        driver.refresh()
        wait.until(
            lambda browser: browser.find_element(
                By.ID, "issue-driven-mode"
            ).is_displayed()
        )
        assert not driver.find_elements(By.CSS_SELECTOR, "#run-list .run-item.selected")
        assert (
            driver.find_element(By.ID, "developer-views").get_attribute("open") is None
        )
        assert not driver.find_element(By.ID, "runtime-panel").is_displayed()
        assert driver.find_element(By.ID, "new-run").get_attribute("aria-pressed") == "true"
        driver.find_element(
            By.CSS_SELECTOR, f'[data-run-id="{recovered_run_id}"]'
        ).click()
        wait.until(
            lambda browser: (
                exact_prompt
                in browser.find_element(By.ID, "agent-turns").get_attribute(
                    "textContent"
                )
            )
        )

        assert [item.run_id for item in runner.snapshots()] == [
            failed_run_id,
            recovered_run_id,
        ]
        for run_id, trace in authoritative_traces.items():
            snapshot = runner.snapshot(run_id)
            assert snapshot.state == "failed"
            assert snapshot.as_json()["agentTurns"] == trace
    finally:
        if driver is not None:
            driver.quit()
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join()
        runner.close()
