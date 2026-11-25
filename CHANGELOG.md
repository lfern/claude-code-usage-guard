# Changelog

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.1] - 2026-11-25

### Fixed

- With several sessions open, one that had been idle could overwrite the current percentages with
  its old ones. A drop within the same window is now ignored if the last reading is less than
  10 minutes old.
- The panel shows today's spending in the same unit as the tray icon (`20%  (3 of 15.3)`), instead
  of points followed by a confusing `%`.

## [0.1.0] - 2026-11-25

First public version.

### Added

- Statusline sensor that saves the Claude Code usage limits, without calling any API.
- Neutral statusline launcher: one folder of scripts per account, run in parallel with a time limit.
- Daemon (`usage-guardd`, systemd user service) with daily budget, projected pace, history and
  several accounts, and a `--simulate` mode.
- CLI `usage-guard`: `status`, `extend`, `snooze` and `history`.
- Windows tray widget: icon, panel and toasts at 0.8, 1.0 and 1.2 of the daily budget.
- Installer that lets you choose the accounts, with backups and `--dry-run`, and uninstaller.

[Unreleased]: https://github.com/lfern/claude-code-usage-guard/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/lfern/claude-code-usage-guard/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/lfern/claude-code-usage-guard/releases/tag/v0.1.0
