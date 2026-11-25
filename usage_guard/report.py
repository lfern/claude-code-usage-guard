"""Human-readable texts, shared by the CLI, the toasts and the widget."""
from datetime import datetime
from zoneinfo import ZoneInfo

from . import budget

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def when(timestamp, timezone, now):
    moment = datetime.fromtimestamp(timestamp, ZoneInfo(timezone))
    if timestamp - now > 6 * budget.DAY:
        return moment.strftime("%d %b %H:%M")
    return f"{WEEKDAYS[moment.weekday()]} {moment:%H:%M}"


def age_text(age):
    if age is None:
        return "never read"
    if age < 60:
        return f"{age:.0f} s ago"
    if age < 3600:
        return f"{age // 60:.0f} min ago"
    if age < 2 * budget.DAY:
        return f"{age // 3600:.0f} h ago"
    return f"{age // budget.DAY:.0f} days ago"


def pace_text(status, timezone, now):
    if not status.fresh:
        return "no data: " + status.reason
    reset = when(status.resets_at, timezone, now)
    if status.week_pct >= 100:
        return f"weekly quota exhausted (reset {reset})"
    if status.full_at is not None and status.full_at < status.resets_at:
        return f"at this pace: 100% on {when(status.full_at, timezone, now)} (reset {reset})"
    return f"at this pace: {status.pct_at_reset:.0f}% at reset ({reset})"


def toast_text(status, level, config, now):
    titles = {budget.WARN: "close to today's budget",
              budget.LIMIT: "today's budget used up",
              budget.EXCESS: "over today's budget"}
    body = (f"Today {status.spent:.1f} of {status.budget:.1f} points ({status.ratio * 100:.0f}%) · "
            f"week {status.week_pct:.0f}%\n{pace_text(status, config.timezone, now)}")
    return f"{status.account}: {titles[level]}", body


def color(status):
    if not status.fresh:
        return "gray"
    return ("green", "amber", "amber", "red")[status.level]
