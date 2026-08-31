# Agent Workflow Manager runtime/startup configuration.
# Copying is normally handled automatically by: bash start.sh
# Notification credentials belong in NOTIFY_CONFIG, never in this file.

AGENT_WORKFLOW_MANAGER_HOST="127.0.0.1"
AGENT_WORKFLOW_MANAGER_PORT="8765"
AGENT_WORKFLOW_MANAGER_NOTIFICATIONS="auto"
AGENT_WORKFLOW_MANAGER_NOTIFY_SUCCESS="true"
AGENT_WORKFLOW_MANAGER_NOTIFY_FAILURE="true"
AGENT_WORKFLOW_MANAGER_NOTIFY_STOPPED="false"
NOTIFY_CONFIG="$HOME/.config/notify/config"
