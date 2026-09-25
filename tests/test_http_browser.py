from __future__ import annotations

import re
import shutil
import socket
import threading
import time
from collections.abc import Iterator

import pytest

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
        developer_views = driver.find_element(By.ID, "developer-views")
        if developer_views.get_attribute("open") is None:
            driver.find_element(By.CSS_SELECTOR, "#developer-views > summary").click()
        driver.find_element(By.ID, "runtime-view").click()
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
            "validate",
            "dry-run",
            "run",
            "stop",
            "cleanup",
            "settings-open",
        ):
            assert driver.find_element(By.ID, control_id).is_displayed()
        assert driver.find_element(By.ID, "diagnostics-panel").is_displayed()

        runtime_panel = driver.find_element(By.ID, "runtime-panel")
        assert runtime_panel.get_attribute("open") is None
        assert not runtime_panel.is_displayed()
        developer_views = driver.find_element(By.ID, "developer-views")
        if developer_views.get_attribute("open") is None:
            driver.find_element(By.CSS_SELECTOR, "#developer-views > summary").click()
        assert driver.find_element(By.ID, "prompt-mode").is_displayed()
        assert driver.find_element(By.ID, "workflow-mode").is_displayed()
        driver.find_element(By.ID, "runtime-view").click()
        wait.until(
            lambda browser: browser.find_element(By.ID, "run-list").is_displayed()
        )
        assert not driver.find_elements(By.CSS_SELECTOR, "#run-list .run-item.selected")
        assert runtime_panel.is_displayed()
        assert runtime_panel.get_attribute("open") is not None

        runs_panel = driver.find_element(By.ID, "runs-panel")
        runs_summary = driver.find_element(By.CSS_SELECTOR, "#runs-panel > summary")
        runs_summary.click()
        assert runs_panel.get_attribute("open") is None
        runs_summary.click()
        assert runs_panel.get_attribute("open") is not None

        driver.find_element(By.ID, "new-run").click()
        wait.until(
            lambda browser: (
                browser.find_element(By.ID, "code").get_attribute("readonly") is None
            )
        )
        driver.find_element(By.CSS_SELECTOR, "#run-list .run-item").click()
        wait.until(
            lambda browser: (
                browser.find_element(By.ID, "code").get_attribute("readonly")
                is not None
            )
        )
        driver.find_element(By.ID, "new-run").click()

        mobile_workflow = (
            "from purplemux_client import emit_step\n"
            'WORKFLOW_OUTLINE = ["mobile step"]\n'
            'emit_step("mobile step", "started")\n'
            'print("X" * 4000, flush=True)\n'
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
        wait.until(
            lambda browser: browser.find_element(By.ID, "outline-panel").is_displayed()
        )
        wait.until(
            lambda browser: len(browser.find_element(By.ID, "stdout").text) >= 4000
        )
        assert driver.find_element(By.ID, "progress").is_displayed()
        assert driver.find_element(By.ID, "stdout").is_displayed()
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
        developer_views = driver.find_element(By.ID, "developer-views")
        assert developer_views.get_attribute("open") is None
        driver.find_element(By.CSS_SELECTOR, "#developer-views > summary").click()
        driver.find_element(By.ID, "runtime-view").click()

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
        driver.find_element(By.CSS_SELECTOR, "#developer-views > summary").click()
        driver.find_element(By.ID, "runtime-view").click()
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
