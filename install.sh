#!/usr/bin/env bash
# Installs usage-guard. Safe to run again.
# Asks which accounts to apply it to; the others are not touched.
#   --account DIR     applies to that account without asking (repeatable)
#   --all             applies to every detected account
#   --dry-run         shows what it would do and changes nothing
#   --no-statusline   touches no settings.json and does not ask for accounts
#   --no-service      does not install the systemd service
#   --windows-task    start from a Windows scheduled task instead of systemd
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
share=$HOME/.local/share/usage-guard
bin=$HOME/.local/bin
config=$HOME/.config/usage-guard/config.toml
unit_dir=$HOME/.config/systemd/user
with_statusline=1 with_service=1 windows_task=0
dry_run=0 selection=()
while [ $# -gt 0 ]; do
    case $1 in
        --account) [ -n "${2:-}" ] || { echo "--account needs a path" >&2; exit 2; }
                   selection+=("$2"); shift ;;
        --all) selection=(--all) ;;
        --dry-run) dry_run=1 ;;
        --no-statusline) with_statusline=0 ;;
        --no-service) with_service=0 ;;
        --windows-task) windows_task=1 with_service=0 ;;
        *) sed -n '2,9p' "$0"; exit 2 ;;
    esac
    shift
done

for need in python3 jq; do
    command -v "$need" >/dev/null || { echo "Missing $need: sudo apt install $need" >&2; exit 1; }
done
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || { echo "Python 3.9 or later is required" >&2; exit 1; }

tool=$repo/settings_tool.py
chosen=()
if [ $with_statusline = 1 ]; then
    list=$(python3 "$tool" choose "${selection[@]}")
    mapfile -t chosen <<<"$list"
    echo "== What will be done"
    # If an account has problems, stop here, before changing anything.
    python3 "$tool" check "${chosen[@]}" || { echo "Nothing was changed." >&2; exit 1; }
fi
[ $dry_run = 0 ] || { echo "Dry run: nothing was changed."; exit 0; }

echo "== Files"
mkdir -p "$share/usage_guard" "$bin" "$(dirname "$config")"
# install writes with unlink + create: it does not break a script that is running.
install -m 644 "$repo"/usage_guard/*.py "$share/usage_guard/"
install -m 755 "$repo/statusline.sh" "$repo/run.sh" "$repo/settings_tool.py" "$share/"
install -m 644 "$repo/widget.ps1" "$share/"
install -m 755 "$repo/bin/usage-guard" "$repo/bin/usage-guardd" "$bin/"
echo "  $share, $bin/usage-guard, $bin/usage-guardd"

echo "== Configuration"
if [ ! -e "$config" ]; then
    tmp=$(mktemp "$config.XXXXXX")
    sed '/^\[\[accounts\]\]/,$d' "$repo/config.example.toml" >"$tmp"
    mv "$tmp" "$config"
    echo "  created $config"
fi
if [ $with_statusline = 1 ]; then
    python3 "$tool" configure "${chosen[@]}"
    echo "== Statusline"
    python3 "$tool" add "${chosen[@]}"
fi

if [ $with_service = 1 ]; then
    echo "== Service"
    state=$(systemctl --user is-system-running 2>/dev/null || true)
    case $state in
        running|degraded|starting)
            mkdir -p "$unit_dir"
            install -m 644 "$repo/usage-guard.service" "$unit_dir/"
            systemctl --user daemon-reload
            systemctl --user enable usage-guard.service
            systemctl --user restart usage-guard.service
            sleep 2
            systemctl --user is-active --quiet usage-guard.service || {
                echo "ERROR: the service does not start. See: journalctl --user -u usage-guard" >&2; exit 1; }
            echo "  usage-guard.service active"
            ;;
        *)
            cat >&2 <<'MSG'
  systemctl --user does not work in this WSL. Choose:
   1) Enable systemd: add to /etc/wsl.conf (with sudo)
          [boot]
          systemd=true
      run `wsl --shutdown` in Windows, open WSL and repeat ./install.sh
   2) Use a Windows scheduled task:  ./install.sh --windows-task
MSG
            exit 1
            ;;
    esac
fi

if [ $windows_task = 1 ]; then
    echo "== Windows scheduled task"
    if systemctl --user is-active --quiet usage-guard.service 2>/dev/null; then
        systemctl --user disable --now usage-guard.service
    fi
    powershell=$(command -v powershell.exe || echo /mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe)
    (cd /mnt/c/Windows && "$powershell" -NoProfile -Command "
        \$ErrorActionPreference = 'Stop'
        \$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        \$action = New-ScheduledTaskAction -Execute 'conhost.exe' -Argument '--headless wsl.exe -d $WSL_DISTRO_NAME --exec $bin/usage-guardd'
        \$trigger = New-ScheduledTaskTrigger -AtLogOn -User \$user
        \$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName 'usage-guard' -Action \$action -Trigger \$trigger -Settings \$settings -Force | Out-Null
        Start-ScheduledTask -TaskName 'usage-guard'" </dev/null) || {
        echo "ERROR: Windows did not allow creating the task (it may need an administrator console)" >&2; exit 1; }
    echo "  task 'usage-guard' created and started"
fi

echo
echo "Done. Try: usage-guard status   |   usage-guardd --simulate"
