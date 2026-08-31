#!/usr/bin/env bash
set -euo pipefail

case ${BASH_SOURCE[0]} in
    */*) script_dir=${BASH_SOURCE[0]%/*} ;;
    *) script_dir=. ;;
esac
repo_root=$(cd -P -- "$script_dir" && pwd)

if [[ $PWD != "$repo_root" ]]; then
    printf 'ERROR: run start.sh from the repository root: %s\n' "$repo_root" >&2
    exit 1
fi

export PATH="$HOME/.local/bin:$PATH"
notify_config=${NOTIFY_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/notify/config}
notification_mode=${AGENT_WORKFLOW_MANAGER_NOTIFICATIONS:-auto}

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        printf 'ERROR: required command not found: %s\n' "$1" >&2
        exit 1
    fi
}

require_command uv
require_command purplemux

printf 'Syncing Python dependencies...\n'
uv sync --locked

if [[ $notification_mode != 0 && $notification_mode != disabled && $notification_mode != false ]]; then
    require_command curl
    if ! command -v notify >/dev/null 2>&1; then
        install_root=$(mktemp -d)
        cleanup_install() { rm -rf -- "$install_root"; }
        trap cleanup_install EXIT
        printf 'Installing the public notify CLI...\n'
        mkdir -p "$install_root/notify-server/bin"
        curl --fail --silent --show-error --location \
            --output "$install_root/notify-server/install-cli.sh" \
            https://raw.githubusercontent.com/eletim/notify-server/main/install-cli.sh
        curl --fail --silent --show-error --location \
            --output "$install_root/notify-server/bin/notify" \
            https://raw.githubusercontent.com/eletim/notify-server/main/bin/notify
        bash "$install_root/notify-server/install-cli.sh"
        trap - EXIT
        cleanup_install
    fi

    notify_config_dir=${notify_config%/*}
    if [[ $notify_config_dir == "$notify_config" ]]; then
        notify_config_dir=.
    fi
    mkdir -p -- "$notify_config_dir"
    if [[ ! -f $notify_config ]]; then
        {
            printf 'NOTIFY_SERVER=https://eletim.jp\n'
            printf 'NOTIFY_TOPIC=agents\n'
            printf '# NOTIFY_TOKEN=tk_...\n'
        } >"$notify_config"
        chmod 600 "$notify_config"
        printf 'Created notify configuration: %s\n' "$notify_config"
    fi

    token_configured=false
    if [[ -n ${NOTIFY_TOKEN:-} ]]; then
        token_configured=true
    elif (
        set +u
        # shellcheck disable=SC1090
        source "$notify_config" >/dev/null 2>&1
        [[ -n ${NOTIFY_TOKEN:-} ]]
    ); then
        token_configured=true
    fi

    if [[ $token_configured == false && -t 0 ]]; then
        printf 'Notify token (leave blank to disable notifications): ' >&2
        IFS= read -r -s notify_token
        printf '\n' >&2
        if [[ -n $notify_token ]]; then
            printf 'NOTIFY_TOKEN=%q\n' "$notify_token" >>"$notify_config"
            chmod 600 "$notify_config"
            unset notify_token
            token_configured=true
            printf 'Notify token saved to %s\n' "$notify_config"
        fi
    fi

    if [[ $token_configured == true ]]; then
        export AGENT_WORKFLOW_MANAGER_NOTIFICATIONS=1
        printf 'Terminal notifications enabled.\n'
    else
        export AGENT_WORKFLOW_MANAGER_NOTIFICATIONS=0
        printf 'Terminal notifications disabled; Runner startup will continue.\n'
    fi
else
    export AGENT_WORKFLOW_MANAGER_NOTIFICATIONS=0
    printf 'Terminal notifications disabled by configuration.\n'
fi

host=${AGENT_WORKFLOW_MANAGER_HOST:-127.0.0.1}
port=${AGENT_WORKFLOW_MANAGER_PORT:-8765}
printf 'Starting Agent Workflow Manager at http://%s:%s\n' "$host" "$port"
exec uv run python -m purplemux_client.web --host "$host" --port "$port"
