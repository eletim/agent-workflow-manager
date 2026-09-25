"""Run as an AWM Python Workflow; pass a registered target ID for external work."""

import sys
from urllib.error import URLError

from purplemux_client import (
    ExternalRunError,
    ExternalRunLaunchUnknown,
    emit_step,
    get_child_run_result,
    start_child_run,
    wait_child_run,
)

# No argument starts a local child; e.g. args=["remote"] selects an external AWM.
target_id = sys.argv[1] if len(sys.argv) > 1 else None
child_code = """
import sys
from purplemux_client import emit_step
emit_step("child", "completed")
print("hello", sys.argv[1], flush=True)
"""

try:
    child_id = start_child_run(child_code, args=["AWM"], target_id=target_id)
except (ExternalRunLaunchUnknown, URLError, TimeoutError):
    # The request may have launched a Run. Inspect both histories before recovery.
    emit_step("launch", "failed", error="Launch outcome unknown; inspect history")
    raise

print(f"Child Run ID: {child_id}; target: {target_id or 'local'}", flush=True)
try:
    result = get_child_run_result(child_id, target_id=target_id)
    if result is None:  # Confirmed running; Python decides to wait.
        result = wait_child_run(child_id, target_id=target_id, timeout=300)
except (TimeoutError, ExternalRunError, URLError):
    # This does not stop the child or establish its terminal outcome.
    emit_step("await", "failed", error="Result unavailable; inspect child Run")
    raise

print(result.state, result.exit_code, result.stdout, result.stderr, flush=True)
# Waiting returns failed/stopped results too. This workflow requires success.
if result.state != "success" or result.exit_code != 0:
    raise RuntimeError(f"Child Run {child_id} ended {result.state}")
emit_step("await", "completed")
