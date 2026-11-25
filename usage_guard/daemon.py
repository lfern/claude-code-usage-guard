"""usage-guardd: main loop and --simulate mode."""
import argparse
import fcntl
import signal
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import budget, report
from .engine import Engine
from .storage import Account, Config, config_path, data_dir, load_config, write_json_atomic
from .widget_process import Widget

TICK = 2.0
HEARTBEAT = 5.0  # the widget closes after 20 s without news
WIDGET_SCRIPT = Path(__file__).resolve().parent.parent / "widget.ps1"


class Stop(Exception):
    pass


def _stop(*_):
    raise Stop


def run_cycle(engine, widget, now, sent):
    """One full cycle. `sent` = [last state, instant]. Returns (statuses, toasts)."""
    if widget:
        widget.ensure()
        for message in widget.poll_commands():
            engine.command(message, now)
    statuses, toasts = engine.tick(now)
    if widget:
        state = engine.widget_state(statuses, now)
        if state != sent[0] or time.monotonic() - sent[1] >= HEARTBEAT:
            sent[:] = [state, time.monotonic()] if widget.send(state) else [None, 0.0]
        for toast in toasts:
            widget.toast(toast)
        widget.flush()
    return statuses, toasts


def serve(use_widget):
    directory = data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    lock = open(directory / "daemon.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("usage-guardd: another daemon is already running", file=sys.stderr)
        return 1
    engine = Engine(load_config(), directory)
    widget = Widget(WIDGET_SCRIPT, engine.config.powershell) if use_widget else None
    print(f"usage-guardd: watching {', '.join(a.name for a in engine.config.accounts) or 'no accounts'}",
          flush=True)
    sent, mtime = [None, 0.0], None
    try:
        while True:
            # Reloads config.toml when it changes; if it is wrong, keeps the previous one.
            try:
                current = config_path().stat().st_mtime_ns
            except OSError:
                current = mtime
            if mtime is not None and current != mtime:
                try:
                    engine.config = load_config()
                    print("usage-guardd: configuration reloaded", flush=True)
                except (ValueError, OSError) as error:
                    print(f"usage-guardd: config.toml ignored: {error}", file=sys.stderr, flush=True)
            mtime = current
            try:
                run_cycle(engine, widget, time.time(), sent)
            except Stop:
                raise
            except Exception as error:  # one bad cycle must not bring the daemon down
                print(f"usage-guardd: error in cycle: {error!r}", file=sys.stderr, flush=True)
            time.sleep(TICK)
    finally:
        if widget:
            widget.stop()


def simulate(seconds, use_widget):
    """An accelerated day (08:00 to 24:00): 0% to 130% of the budget, without touching real data."""
    with tempfile.TemporaryDirectory(prefix="usage-guard-sim-") as temp:
        root = Path(temp)
        config = Config([Account("SIM rising", root / "a"), Account("SIM calm", root / "b")])
        try:
            config.powershell = load_config().powershell
        except (ValueError, OSError):
            pass
        zone = ZoneInfo(config.timezone)
        start = datetime.now(zone).replace(hour=8, minute=0, second=0, microsecond=0).timestamp()
        length = 16 * 3600 - 1
        reset = start + 4 * budget.DAY
        engine = Engine(config, root / "data")
        widget = Widget(WIDGET_SCRIPT, config.powershell) if use_widget else None
        sent = [None, 0.0]

        def feed(progress, now):
            for account, base, top in ((config.accounts[0], 40, 1.3), (config.accounts[1], 10, 0.4)):
                planned = (100 - base) / 4
                write_json_atomic(account.state_path, {
                    "seven_day_pct": base + planned * top * progress, "seven_day_resets_at": reset,
                    "five_hour_pct": 60 * progress, "five_hour_resets_at": now + 3600, "read_at": now})

        def step(progress, shown):
            now = start + length * progress
            feed(progress, now)
            statuses, toasts = run_cycle(engine, widget, now, sent)
            status = statuses[0]
            if toasts or status.level != shown:
                print(f"{datetime.fromtimestamp(now, zone):%H:%M}  ratio {status.ratio:4.2f}  "
                      f"{report.color(status):5}  " + "  ".join("TOAST " + t["title"] for t in toasts),
                      flush=True)
            return status.level

        print(f"Simulation: one day in {seconds:.0f} s (Ctrl+C to quit)", flush=True)
        try:
            if widget:
                deadline = time.monotonic() + 30
                while not widget.ready and time.monotonic() < deadline:
                    widget.ensure()
                    list(widget.poll_commands())
                    time.sleep(0.2)
                if not widget.ready:
                    print("WARNING: the widget did not respond; continuing on the console only", file=sys.stderr)
            begin, shown = time.monotonic(), budget.OK
            while (elapsed := time.monotonic() - begin) < seconds:
                shown = step(elapsed / seconds, shown)
                time.sleep(0.25)
            hold = time.monotonic() + 15 if widget else 0
            if widget:
                print("End of the day. 15 s more with the icon blinking...", flush=True)
            step(1.0, shown)
            while time.monotonic() < hold:
                step(1.0, shown)
                time.sleep(0.25)
        finally:
            if widget:
                widget.stop()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="usage-guardd", description="usage-guard daemon (alerts only)")
    parser.add_argument("--simulate", action="store_true", help="replays an accelerated day")
    parser.add_argument("--seconds", type=float, default=60, help="length of the simulation (60)")
    parser.add_argument("--no-widget", action="store_true", help="without the Windows widget")
    args = parser.parse_args(argv)
    if args.seconds <= 0:
        parser.error("--seconds must be greater than 0")
    signal.signal(signal.SIGTERM, _stop)
    try:
        if args.simulate:
            return simulate(args.seconds, not args.no_widget)
        return serve(not args.no_widget)
    except (Stop, KeyboardInterrupt):
        return 0
    except (ValueError, OSError) as error:
        print(f"usage-guardd: {error}", file=sys.stderr)
        return 1
