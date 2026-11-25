"""Joins files and logic: one cycle per account. The clock always comes from outside."""
import csv
import io
import os
import sys
from pathlib import Path

from . import budget, report
from .storage import Manual, read_json, write_json_atomic

HISTORY_HEADER = ["timestamp", "account", "week_pct", "five_hour_pct", "spent_today", "budget"]


class Engine:
    def __init__(self, config, directory):
        self.config = config
        self.dir = Path(directory)
        self.manual = Manual(self.dir)
        self.runtime_path = self.dir / "runtime.json"
        self.history_path = self.dir / "history.csv"
        saved = read_json(self.runtime_path).get("accounts")
        self.runtime = {k: v for k, v in saved.items() if isinstance(v, dict)} \
            if isinstance(saved, dict) else {}
        self._saved_today = {}

    def evaluate(self, account, now):
        """Read only: (Status, record)."""
        memory = self.runtime.get(account.name, {})
        record = memory.get("record")
        day = budget.local_day(now, self.config.timezone)
        extension, snooze = self.manual.read(account.name, day)
        return budget.evaluate(account.name, read_json(account.state_path),
                               record if isinstance(record, dict) else {},
                               extension, snooze, self.config, now)

    def statuses(self, now):
        return [self.evaluate(account, now)[0] for account in self.config.accounts]

    def tick(self, now):
        """One daemon cycle: (statuses, toasts). A disk failure does not bring it down."""
        statuses, toasts = [], []
        before = repr(self.runtime)
        for account in self.config.accounts:
            status, record = self.evaluate(account, now)
            memory = dict(self.runtime.get(account.name, {}))
            alert = memory.get("alert")
            fire, alert = budget.next_alert(status, alert if isinstance(alert, dict) else {},
                                            self.config, now)
            memory.update(record=record, alert=alert)
            if fire is not None:
                title, body = report.toast_text(status, fire, self.config, now)
                toasts.append({"type": "toast", "account": account.name, "level": fire,
                               "title": title, "body": body})
            if status.fresh:
                self._guarded(self._sample, status, memory, now)
                self._guarded(self._publish_today, account, status, now)
            self.runtime[account.name] = memory
            statuses.append(status)
        if repr(self.runtime) != before or not self.runtime_path.exists():
            self._guarded(write_json_atomic, self.runtime_path, {"accounts": self.runtime})
        return statuses, toasts

    @staticmethod
    def _guarded(action, *args):
        try:
            action(*args)
        except OSError as error:
            print(f"usage-guardd: could not write: {error}", file=sys.stderr, flush=True)

    def _sample(self, status, memory, now):
        """At most one sample per minute and account, and only from new readings."""
        read_at = now - status.age
        last = memory.get("sample")
        if isinstance(last, (int, float)) and (0 <= now - last < 60 or read_at <= last):
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        row = [int(now), status.account, status.week_pct,
               "" if status.five_hour_pct is None else status.five_hour_pct,
               round(status.spent, 3), round(status.budget, 3)]
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        # A single write with O_APPEND: either the whole line goes in or none of it.
        fd = os.open(self.history_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            if os.fstat(fd).st_size == 0:
                writer.writerow(HISTORY_HEADER)
            writer.writerow(row)
            os.write(fd, buffer.getvalue().encode("utf-8"))
        finally:
            os.close(fd)
        memory["sample"] = now

    def _publish_today(self, account, status, now):
        """For the "today:xx/yy%" of statusline.sh; expires at midnight."""
        values = (round(status.spent, 2), round(status.budget, 2))
        _, end = budget.day_bounds(now, self.config.timezone)
        valid_until = int(min(end, status.resets_at))
        key = (*values, valid_until)
        if self._saved_today.get(account.name) != key:
            write_json_atomic(account.today_path,
                              {"spent": values[0], "budget": values[1], "valid_until": valid_until})
            self._saved_today[account.name] = key

    def widget_state(self, statuses, now):
        rows = []
        for status in statuses:
            rows.append({
                "account": status.account, "fresh": status.fresh, "color": report.color(status),
                "week": status.week_pct, "spent": status.spent, "budget": status.budget,
                "ratio": status.ratio, "pace": report.pace_text(status, self.config.timezone, now),
                "age": report.age_text(status.age), "snoozed": now < status.snoozed_until})
        top = budget.worst(statuses)
        blink = any(s.fresh and s.level == budget.EXCESS and now >= s.snoozed_until for s in statuses)
        return {"type": "state", "color": report.color(top) if top else "gray",
                "ratio": top.ratio if top else None, "account": top.account if top else None,
                "blink": blink, "accounts": rows}

    def command(self, message, now):
        """Command from the widget. Anything not understood is ignored."""
        names = [a.name for a in self.config.accounts]
        account = message.get("account")
        if account not in names:
            return
        if message.get("cmd") == "extend":
            self._guarded(self.manual.extend, account, 5.0,
                          budget.local_day(now, self.config.timezone))
        elif message.get("cmd") == "snooze":
            self._guarded(self.manual.snooze, [account], now + 3600)
