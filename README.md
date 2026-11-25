# claude-code-usage-guard

Warns you when your daily spending of the Claude Code weekly quota goes over a daily budget.
**It only warns**: it blocks nothing, uses no hooks, reads no credentials and calls no API.
For WSL2 + Windows.

> Unofficial project, not affiliated with or endorsed by Anthropic. "Claude" is a trademark of
> Anthropic.

    Claude Code → run.sh → statusline.sh → state.json → usage-guardd → widget.ps1 (tray, panel, toasts)

The daemon is written in Python 3 (stdlib only): it checks files every 2 s, and Rust would only
add a build step.

## Install and uninstall

    git clone https://github.com/lfern/claude-code-usage-guard.git
    cd claude-code-usage-guard
    ./install.sh                     # asks which accounts to apply it to
    ./install.sh --account ~/.claude-other   # without asking (repeatable)
    ./install.sh --all               # every detected account
    ./install.sh --dry-run           # shows what it would do, changes nothing
    ./install.sh --no-statusline     # only updates the program and the service
    ./install.sh --windows-task      # without systemd: scheduled task at Windows logon
    ./uninstall.sh                   # removes usage-guard; the launcher and your scripts stay
    ./uninstall.sh --restore-statusline   # also restores your original statusLine
    ./uninstall.sh --account DIR     # only that account
    ./uninstall.sh --purge           # also deletes configuration, history and backups

Requirements: `python3` (3.9+) and `jq`. Backups of `settings.json` are kept in
`$CLAUDE_CONFIG_DIR/usage-guard/*.bak`.

## Statusline: you are in control

`settings.json` calls `~/.config/claude-statusline/run.sh`, a launcher that knows nothing about
usage-guard. It runs the executable `*.sh` files in the account's folder in parallel and prints
their output in name order:

    ~/.config/claude-statusline/personal.d/
        10-usage-guard.sh   saves the data, prints nothing
        50-original.sh      the statusline you had before
        60-usage-line.sh    "5h:12% 7d:44% today:4/15%" (off if you already had a statusline)

To turn one off: `chmod -x`. To add one: drop in an executable `.sh` file. If one fails or takes
longer than 5 s (`STATUSLINE_TIMEOUT`), the others still run; the reason is saved in
`last-error.log`.

Windows 11 hides new icons under the `^` arrow: drag the icon to the taskbar to always see it.

## CLI

    usage-guard status
    usage-guard extend 5 [-a other]   # +5 points of weekly quota for today's budget
    usage-guard snooze 1h [-a personal]  # 30m, 2d...; 0 removes it; without -a, all accounts
    usage-guard history [-n 50] [-a personal]
    usage-guardd --simulate [--seconds 60]   # an accelerated day, does not touch your data

Without `-a`, `extend` uses the account in `$CLAUDE_CONFIG_DIR`. Log:
`journalctl --user -u usage-guard`.

## Thresholds

In `~/.config/usage-guard/config.toml`; the daemon reloads it when you save.

    [thresholds]
    warn = 0.8     # amber
    limit = 1.0
    excess = 1.2   # red: blinks and repeats every repeat_minutes

## How it is calculated

- **Baseline**: weekly % in the first reading of the local day (or of the new week).
- **Budget** = (100 − baseline) / days until reset + `extend`. Days are fractional, at least 1,
  and fixed when the baseline is taken.
- **Pace**: today's spending spread from midnight (or from the reset, if it was today).
- **No data**: the last reading is more than 10 min old. This is normal when you are not using
  Claude Code: the sensor only refreshes after the session has talked to the API, so an idle
  session cannot make old data look new.
- A threshold crossed while snoozed is announced when the snooze ends, if it still applies.
- Usage from another machine before the first reading of the day does not count as today's.

## Tests

    python3 -m unittest discover -s tests

## Limitations

- The percentages come from the `rate_limits` field that Claude Code sends to the statusline. If
  your account does not get that field, there is no data.
- Project-level or managed `statusLine` settings take priority over the user one: no data arrives
  from sessions that use them.
- Only tested on Ubuntu 22.04 (WSL2) and Windows 11.
- Not tested yet: `--windows-task`, the Windows cleanup in `uninstall.sh`, `wsl --shutdown`, and
  sleep/resume.
- The widget is started with `powershell.exe -ExecutionPolicy Bypass`, for that process only, so
  that the unsigned `widget.ps1` can run. It also creates one registry key,
  `HKCU:\Software\Classes\AppUserModelId\UsageGuard.Widget`, so that toasts show the right name.

## License

[MIT](LICENSE)
