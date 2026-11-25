"""Budget logic: pure functions, no clock and no files."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from .storage import number

DAY = 86400
WEEK = 7 * DAY
RESET_JITTER = 3600      # resets_at may shift by a few seconds within the same week
MIN_PACE_WINDOW = 3600   # no pace is extrapolated from less than one hour
RATIO_CAP = 9.99
EPS = 1e-9
MAX_EPOCH = 4102444800   # year 2100; anything later is garbage

OK, WARN, LIMIT, EXCESS = range(4)


@dataclass
class Status:
    account: str
    fresh: bool = False
    reason: str = ""                 # why there is no data
    age: Optional[float] = None      # age of the reading (s)
    week_pct: Optional[float] = None
    five_hour_pct: Optional[float] = None
    resets_at: Optional[float] = None
    spent: Optional[float] = None
    budget: Optional[float] = None
    extension: float = 0.0
    ratio: Optional[float] = None
    level: int = OK
    full_at: Optional[float] = None  # when it would reach 100% of the week at this pace
    pct_at_reset: Optional[float] = None
    snoozed_until: float = 0.0


def local_day(timestamp, timezone):
    return datetime.fromtimestamp(timestamp, ZoneInfo(timezone)).date().isoformat()


def day_bounds(timestamp, timezone):
    """(start, end) of the local day; also correct across DST changes."""
    zone = ZoneInfo(timezone)
    date = datetime.fromtimestamp(timestamp, zone).date()
    start = datetime(date.year, date.month, date.day, tzinfo=zone)
    after = date + timedelta(days=1)
    return start.timestamp(), datetime(after.year, after.month, after.day, tzinfo=zone).timestamp()


def epoch(value):
    """Epoch seconds from a number or an ISO 8601 date; None if invalid."""
    result = number(value)
    if result is None and isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError):
            return None
    return result if result is not None and 0 < result < MAX_EPOCH else None


def level_of(ratio, config):
    if ratio > config.excess + EPS:
        return EXCESS
    if ratio >= config.limit - EPS:
        return LIMIT
    if ratio >= config.warn - EPS:
        return WARN
    return OK


def evaluate(name, state, record, extension, snoozed_until, config, now):
    """Returns (Status, new record). `record` is the memory of the day: day, baseline,
    baseline_at, resets_at, days. If the reading is unusable, record does not change."""
    status = Status(name, extension=extension, snoozed_until=snoozed_until)
    status.week_pct = number(state.get("seven_day_pct"))
    status.five_hour_pct = number(state.get("five_hour_pct"))
    read_at = epoch(state.get("read_at"))
    resets_at = epoch(state.get("seven_day_resets_at"))
    if resets_at is None:
        resets_at = epoch(record.get("resets_at"))
    status.resets_at = resets_at
    if read_at is not None:
        status.age = max(0.0, now - read_at)

    if status.week_pct is None or read_at is None:
        status.reason = "no readings"
    elif abs(now - read_at) > config.stale_minutes * 60:
        status.reason = "old data"
    elif resets_at is None:
        status.reason = "no reset date"
    elif resets_at <= now:
        status.reason = "week ended, waiting for a new reading"
    elif local_day(read_at, config.timezone) != local_day(now, config.timezone):
        status.reason = "no readings today"
    if status.reason:
        return status, record

    today = local_day(now, config.timezone)
    baseline = number(record.get("baseline"))
    baseline_at = epoch(record.get("baseline_at"))
    days = number(record.get("days"))
    known_reset = epoch(record.get("resets_at"))
    same_week = known_reset is not None and abs(known_reset - resets_at) <= RESET_JITTER
    if (record.get("day") != today or not same_week
            or baseline is None or baseline_at is None or days is None):
        baseline, baseline_at = status.week_pct, read_at
        days = max(1.0, (resets_at - read_at) / DAY)
        record = {"day": today, "baseline": baseline, "baseline_at": baseline_at,
                  "resets_at": resets_at, "days": days}

    status.fresh = True
    status.spent = max(0.0, status.week_pct - baseline)
    status.budget = max(0.0, (100 - baseline) / days + extension)
    if status.budget > EPS:
        status.ratio = min(RATIO_CAP, status.spent / status.budget)
    else:
        status.ratio = RATIO_CAP if status.spent > 0 else 0.0
    if status.week_pct >= 100:  # quota exhausted: never green
        status.ratio = max(status.ratio, config.limit)
    status.level = level_of(status.ratio, config)

    # Pace: today's spending spread from midnight (or from the start of the new week).
    start = max(day_bounds(now, config.timezone)[0], resets_at - WEEK)
    elapsed = max(MIN_PACE_WINDOW, read_at - start)
    rate = status.spent / elapsed
    status.pct_at_reset = status.week_pct + rate * (resets_at - read_at)
    if status.week_pct >= 100:
        status.full_at = read_at
    elif rate > 0:
        full_at = read_at + (100 - status.week_pct) / rate
        status.full_at = full_at if full_at < MAX_EPOCH else None
    return status, record


def next_alert(status, alert, config, now):
    """Decides whether to alert. `alert` = {level, at}.
    Returns (level to notify or None, new alert).

    - One alert per crossing; when the ratio drops (e.g. after extend) the threshold rearms.
    - Without data there are no alerts, and past ones are not forgotten.
    - Snoozed: the crossing stays pending until the snooze ends."""
    if not status.fresh:
        return None, alert
    notified = int(number(alert.get("level")) or 0)
    if notified > status.level:
        notified = status.level
    last = number(alert.get("at")) or 0.0
    fire = None
    if now >= status.snoozed_until:
        if status.level > notified:
            fire = status.level
        elif status.level == EXCESS and now - last >= config.repeat_minutes * 60:
            fire = EXCESS
    if fire is not None:
        notified, last = fire, now
    return fire, {"level": notified, "at": last}


def worst(statuses):
    """The account with data and the highest ratio, or None."""
    fresh = [s for s in statuses if s.fresh]
    return max(fresh, key=lambda s: s.ratio) if fresh else None
