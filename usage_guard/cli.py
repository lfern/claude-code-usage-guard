"""usage-guard CLI: status, extend, snooze, history."""
import argparse
import csv
import fcntl
import os
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import budget, report
from .engine import Engine
from .storage import data_dir, load_config, number


def duration(text):
    match = re.fullmatch(r"(\d+)\s*([smhd]?)", text.strip().lower())
    if not match:
        raise argparse.ArgumentTypeError("use for example 30m, 1h, 2d (0 removes it)")
    seconds = int(match[1]) * {"s": 1, "m": 60, "": 60, "h": 3600, "d": 86400}[match[2]]
    if seconds > 30 * 86400:
        raise argparse.ArgumentTypeError("30d at most")
    return seconds


def points(text):
    value = number(text)
    if value is None or not -100 <= value <= 100:
        raise argparse.ArgumentTypeError("must be a number between -100 and 100")
    return value


def daemon_running():
    try:
        with open(data_dir() / "daemon.lock") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True
    except OSError:
        return False


def pick_accounts(config, wanted, command):
    """The requested account; else the one in $CLAUDE_CONFIG_DIR; else the only one there is."""
    if wanted:
        found = [a for a in config.accounts if a.name.lower() == wanted.lower()]
    elif command == "snooze":
        found = config.accounts
    else:
        current = os.environ.get("CLAUDE_CONFIG_DIR")
        found = [a for a in config.accounts
                 if current and os.path.realpath(current) == os.path.realpath(a.config_dir)]
        if not found and len(config.accounts) == 1:
            found = config.accounts
    if not found:
        names = ", ".join(a.name for a in config.accounts) or "none"
        raise ValueError(f"choose the account with --account (configured: {names})")
    return found


def bar(value, total, width=24):
    share = 0 if total <= 0 else max(0.0, min(1.0, value / total))
    return "█" * round(share * width) + "░" * (width - round(share * width))


def show_status(engine, now):
    for status in engine.statuses(now):
        print(f"{status.account}   [{report.color(status)}]   read {report.age_text(status.age)}")
        if status.week_pct is not None:
            print(f"  week   {bar(status.week_pct, 100)} {status.week_pct:.1f}%")
        if status.fresh:
            extra = f" (includes {status.extension:+g} from extend)" if status.extension else ""
            print(f"  today  {bar(status.spent, status.budget)} {status.spent:.1f} of "
                  f"{status.budget:.1f} points · {status.ratio * 100:.0f}%{extra}")
        print(f"  {report.pace_text(status, engine.config.timezone, now)}")
        if now < status.snoozed_until:
            print(f"  snoozed until {report.when(status.snoozed_until, engine.config.timezone, now)}")
    if not daemon_running():
        print("\nWARNING: the daemon is not running (systemctl --user start usage-guard)", file=sys.stderr)


def show_history(engine, accounts, limit):
    zone = ZoneInfo(engine.config.timezone)
    rows = []
    try:
        with open(engine.history_path, newline="", encoding="utf-8", errors="replace") as handle:
            for row in csv.reader(handle):
                if len(row) == 6 and budget.epoch(row[0]) and (not accounts or row[1] in accounts):
                    rows.append(row)
    except OSError:
        pass
    print(f"{'date':16}  {'account':14} {'week':>7} {'5h':>6} {'today':>6} {'budget':>8}")
    for stamp, account, week, five, spent, plan in rows[-limit:] if limit > 0 else rows:
        moment = datetime.fromtimestamp(float(stamp), zone)
        print(f"{moment:%Y-%m-%d %H:%M}  {account:14} {week:>7} {five:>6} {spent:>6} {plan:>8}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="usage-guard", description="Claude Code usage against the daily budget")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="status of each account")
    extend = commands.add_parser("extend", help="add N points of weekly quota to today's budget")
    extend.add_argument("points", type=points, metavar="N")
    snooze = commands.add_parser("snooze", help="snooze the alerts (all accounts by default)")
    snooze.add_argument("time", type=duration, metavar="TIME", help="30m, 1h, 2d; 0 removes it")
    history = commands.add_parser("history", help="latest samples")
    history.add_argument("-n", type=int, default=30, help="how many (30; 0 = all)")
    for sub in (extend, snooze, history):
        sub.add_argument("--account", "-a", help="account name in config.toml")
    args = parser.parse_args(argv)
    try:
        engine = Engine(load_config(), data_dir())
        now = time.time()
        if args.command == "status":
            show_status(engine, now)
        elif args.command == "history":
            names = [a.name for a in pick_accounts(engine.config, args.account, "history")] if args.account else []
            show_history(engine, names, args.n)
        else:
            names = [a.name for a in pick_accounts(engine.config, args.account, args.command)]
            if args.command == "extend":
                total = engine.manual.extend(names[0], args.points, budget.local_day(now, engine.config.timezone))
                print(f"{names[0]}: today's budget extended ({total:+g} points in total)")
            else:
                engine.manual.snooze(names, now + args.time)
                print(f"{', '.join(names)}: " + (
                    f"alerts snoozed until {report.when(now + args.time, engine.config.timezone, now)}"
                    if args.time else "alerts on"))
    except (ValueError, OSError) as error:
        print(f"usage-guard: {error}", file=sys.stderr)
        return 1
    return 0
