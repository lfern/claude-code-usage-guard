"""Paths, atomic writes, TOML configuration and manual adjustments."""
import fcntl
import json
import math
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo


def data_dir():
    return Path(os.environ.get("USAGE_GUARD_DATA") or Path.home() / ".local/share/usage-guard")


def config_path():
    return Path(os.environ.get("USAGE_GUARD_CONFIG") or Path.home() / ".config/usage-guard/config.toml")


def number(value):
    """Finite number or None (accepts numeric strings; rejects bool, NaN and infinity)."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def read_json(path):
    """JSON object in the file, or {} if missing, corrupt or not an object."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def write_text_atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json_atomic(path, value):
    write_text_atomic(path, json.dumps(value, ensure_ascii=False, indent=1) + "\n")


# ---------------------------------------------------------------- configuration

@dataclass
class Account:
    name: str
    config_dir: Path

    @property
    def state_path(self):
        return self.config_dir / "usage-guard" / "state.json"

    @property
    def today_path(self):
        return self.config_dir / "usage-guard" / "today.json"


@dataclass
class Config:
    accounts: list = field(default_factory=list)
    timezone: str = "Europe/Madrid"
    stale_minutes: float = 10
    repeat_minutes: float = 15
    warn: float = 0.8
    limit: float = 1.0
    excess: float = 1.2
    powershell: str = ""


def parse_toml(text):
    """tomllib on Python >= 3.11; otherwise the subset used by config.toml."""
    try:
        import tomllib
        return tomllib.loads(text)
    except ImportError:
        return parse_toml_subset(text)


def _strip_comment(line):
    quote = ""
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
        elif quote == '"' and char == "\\":
            escaped = True
        elif quote:
            quote = "" if char == quote else quote
        elif char in "\"'":
            quote = char
        elif char == "#":
            return line[:index]
    return line


def parse_toml_subset(text):
    """Tables, arrays of tables, strings, numbers and booleans; anything else is an error."""
    root = {}
    table = root
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw).strip()
        if not line:
            continue
        if match := re.fullmatch(r"\[\[\s*([\w-]+)\s*\]\]", line):
            table = {}
            root.setdefault(match[1], []).append(table)
            continue
        if match := re.fullmatch(r"\[\s*([\w-]+)\s*\]", line):
            table = root.setdefault(match[1], {})
            continue
        match = re.fullmatch(r"([\w-]+)\s*=\s*(.+)", line)
        if not match:
            raise ValueError(f"config.toml line {lineno}: cannot parse {raw!r}")
        key, value = match.groups()
        if len(value) >= 2 and value[0] == value[-1] == "'" and "'" not in value[1:-1]:
            table[key] = value[1:-1]
            continue
        try:
            table[key] = json.loads(value.replace("_", "") if value[0] in "+-.0123456789" else value)
        except ValueError:
            raise ValueError(f"config.toml line {lineno}: invalid value {value!r}") from None
    return root


def load_config(path=None):
    """Raises ValueError or OSError with a clear message if the configuration is unusable."""
    data = parse_toml(Path(path or config_path()).read_text(encoding="utf-8"))
    config = Config()
    thresholds = data.get("thresholds", {})
    widget = data.get("widget", {})
    if not isinstance(thresholds, dict) or not isinstance(widget, dict):
        raise ValueError("config.toml: [thresholds] and [widget] must be tables")
    for source, keys in ((data, ("stale_minutes", "repeat_minutes")),
                         (thresholds, ("warn", "limit", "excess"))):
        for key in keys:
            if key in source:
                value = number(source[key])
                if value is None or value <= 0:
                    raise ValueError(f"config.toml: {key} must be a number greater than 0")
                setattr(config, key, value)
    if not config.warn < config.limit < config.excess:
        raise ValueError("config.toml: thresholds must satisfy warn < limit < excess")
    config.timezone = str(data.get("timezone", config.timezone))
    try:
        ZoneInfo(config.timezone)
    except (KeyError, ValueError, OSError):
        raise ValueError(f"config.toml: unknown timezone {config.timezone!r}") from None
    config.powershell = str(widget.get("powershell", ""))
    entries = data.get("accounts", [])
    if not isinstance(entries, list):
        raise ValueError("config.toml: use [[accounts]] (double brackets)")
    names = set()
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("name") or not entry.get("config_dir"):
            raise ValueError("config.toml: each [[accounts]] needs name and config_dir")
        name = str(entry["name"])
        if name in names:
            raise ValueError(f"config.toml: duplicate account: {name}")
        names.add(name)
        config.accounts.append(Account(name, Path(os.path.expandvars(str(entry["config_dir"]))).expanduser()))
    return config


# -------------------------------------------- manual extensions and snoozes

class Manual:
    """manual.json: changed by both the CLI and the widget buttons."""

    def __init__(self, directory=None):
        directory = Path(directory or data_dir())
        self.path = directory / "manual.json"
        self.lock = directory / "manual.lock"

    @contextmanager
    def _edit(self):
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            data = read_json(self.path)
            for key in ("extend", "snooze"):
                if not isinstance(data.get(key), dict):
                    data[key] = {}
            yield data
            write_json_atomic(self.path, data)

    def extend(self, account, points, day):
        with self._edit() as data:
            old = self._extension(data, account, day)
            data["extend"][account] = {"day": day, "points": old + points}
        return old + points

    def snooze(self, accounts, until):
        with self._edit() as data:
            for account in accounts:
                data["snooze"][account] = until

    @staticmethod
    def _extension(data, account, day):
        table = data.get("extend")
        entry = table.get(account) if isinstance(table, dict) else None
        if not isinstance(entry, dict) or entry.get("day") != day:
            return 0.0
        return number(entry.get("points")) or 0.0

    def read(self, account, day):
        """(extra points today, epoch until which it is snoozed)."""
        data = read_json(self.path)
        table = data.get("snooze")
        until = number(table.get(account)) if isinstance(table, dict) else None
        return self._extension(data, account, day), until or 0.0
