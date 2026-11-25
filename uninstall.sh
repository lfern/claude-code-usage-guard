#!/usr/bin/env bash
# Uninstalls usage-guard. The statusline launcher and your scripts stay.
#   --restore-statusline   also restores the original statusLine in settings.json
#   --account DIR          only removes the sensor from that account; the rest stays installed
#   --purge                also deletes configuration, history and backups
set -uo pipefail

share=$HOME/.local/share/usage-guard
bin=$HOME/.local/bin
config_dir=$HOME/.config/usage-guard
unit=$HOME/.config/systemd/user/usage-guard.service
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
tool=$repo/settings_tool.py
purge=0 action=remove only=()
while [ $# -gt 0 ]; do
    case $1 in
        --purge) purge=1 ;;
        --restore-statusline) action=restore ;;
        --account) [ -n "${2:-}" ] || { echo "--account needs a path" >&2; exit 2; }
                   only+=("$2"); shift ;;
        *) sed -n '2,5p' "$0"; exit 2 ;;
    esac
    shift
done
[ $purge = 1 ] && [ ${#only[@]} -gt 0 ] && { echo "--purge cannot be combined with --account" >&2; exit 2; }

echo "== Statusline"
if [ ${#only[@]} -gt 0 ]; then
    python3 "$tool" "$action" "${only[@]}" || exit 1
    echo "Delete its [[accounts]] block from $config_dir/config.toml so that it does not show as grey."
    exit 0
fi
accounts=$(python3 "$tool" installed) || { echo "Nothing was deleted." >&2; exit 1; }
if [ -n "$accounts" ]; then
    mapfile -t dirs <<<"$accounts"
    python3 "$tool" "$action" "${dirs[@]}" || { echo "Stopping here: the program is still installed." >&2; exit 1; }
fi

echo "== Service, task and files"
if [ -e "$unit" ]; then
    systemctl --user disable --now usage-guard.service 2>/dev/null
    rm -f "$unit"
    systemctl --user daemon-reload 2>/dev/null
fi
powershell=$(command -v powershell.exe || echo /mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe)
if [ -x "$powershell" ]; then
    (cd /mnt/c/Windows && "$powershell" -NoProfile -Command "
        Stop-ScheduledTask -TaskName 'usage-guard' -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName 'usage-guard' -Confirm:\$false -ErrorAction SilentlyContinue
        Remove-Item 'HKCU:\Software\Classes\AppUserModelId\UsageGuard.Widget' -Recurse -ErrorAction SilentlyContinue
        exit 0" </dev/null)
fi
pkill -u "$(id -u)" -f "$bin/usage-guardd" 2>/dev/null
rm -f "$bin/usage-guard" "$bin/usage-guardd"
rm -rf "$share/usage_guard" "$share/__pycache__"
rm -f "$share/statusline.sh" "$share/run.sh" "$share/settings_tool.py" "$share/widget.ps1" "$share/daemon.lock" "$share/manual.lock"
while read -r dir; do
    [ -n "$dir" ] || continue
    rm -rf "$dir/usage-guard/sessions"
    rm -f "$dir/usage-guard/state.json" "$dir/usage-guard/today.json" "$dir/usage-guard/original-statusline.json"
    [ $purge = 1 ] && rm -rf "$dir/usage-guard"
done <<<"$accounts"

if [ $purge = 1 ]; then
    rm -rf "$share" "$config_dir"
    echo "Completely uninstalled."
else
    echo "Uninstalled. Kept: config.toml, history.csv and the settings.json backups (--purge deletes them)."
fi
