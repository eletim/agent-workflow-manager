from __future__ import annotations

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
    runner = PythonRunner(stop_timeout=0.5)
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
            lambda browser: browser.find_element(By.ID, "stdout").text == "HTTP_STDOUT"
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

        driver.find_element(By.ID, "output-copy").click()
        assert driver.find_element(By.ID, "output-copy").text == "Copied"
        editor.click()
        editor.send_keys(Keys.CONTROL, "a")
        editor.send_keys(Keys.CONTROL, "v")
        assert editor.get_attribute("value") == (
            "stdout:\nHTTP_STDOUT\n\nstderr:\nHTTP_STDERR\n"
        )

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
