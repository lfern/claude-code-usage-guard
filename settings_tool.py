#!/usr/bin/env python3
"""Accounts and statuslines. Used by install.sh and uninstall.sh.

  settings_tool.py choose [--all | DIR...]   chooses accounts (menu if none given) and prints them
  settings_tool.py check DIR...              checks and says what it would do; changes nothing
  settings_tool.py add DIR...                installs the launcher and the sensor
  settings_tool.py configure DIR...          adds missing accounts to config.toml
  settings_tool.py installed                 accounts with the sensor or in config.toml
  settings_tool.py remove DIR...             removes the sensor; the launcher and your scripts stay
  settings_tool.py restore DIR...            also restores the original statusLine
"""
import json
import os
import re
import shlex
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from usage_guard.storage import (  # noqa: E402
    config_path, load_config, read_json, write_json_atomic, write_text_atomic)

HOME = Path.home()
LAUNCHER = HOME / ".config/claude-statusline"
SHARE = HOME / ".local/share/usage-guard"
SENSOR, ORIGINAL, LINE = "10-usage-guard.sh", "50-original.sh", "60-usage-line.sh"


def absolute(text):
    return Path(os.path.abspath(os.path.expanduser(str(text))))


def account_name(directory):
    name = re.sub(r"^\.claude[-.]?", "", directory.name)
    name = re.sub(r"[^\w-]+", "-", name).strip("-") or "main"
    return name


def scripts_dir(directory):
    """Scripts folder of the account; .account keeps apart two with the same name."""
    base = LAUNCHER / f"{account_name(directory).lower()}.d"
    number = 2
    while (base / ".account").exists() and (base / ".account").read_text().strip() != str(directory):
        base = LAUNCHER / f"{account_name(directory).lower()}-{number}.d"
        number += 1
    return base


def configured():
    return [absolute(a.config_dir) for a in load_config().accounts] if config_path().exists() else []


def detect():
    candidates = [HOME / ".claude", *sorted(HOME.glob(".claude-*")), *sorted(HOME.glob(".claude.*"))]
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        candidates.append(absolute(os.environ["CLAUDE_CONFIG_DIR"]))
    found = [d for d in candidates if d.is_dir() and (
        (d / "settings.json").exists() or (d / ".claude.json").exists() or (d / "projects").is_dir())]
    return list(dict.fromkeys([*found, *[d for d in configured() if d.is_dir()]]))


def settings_path(directory):
    return Path(os.path.realpath(directory / "settings.json"))


def load_settings(directory):
    path = settings_path(directory)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise ValueError(f"{path} is not valid JSON: {error}") from None
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def save_settings(directory, value):
    path = settings_path(directory)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    temp = path.with_name(path.name + ".usage-guard-tmp")
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp, mode)
    os.replace(temp, path)


def classify(directory):
    """(state, statusLine): launcher | legacy | none | command. ValueError if not understood."""
    if not directory.is_dir():
        raise ValueError(f"{directory} does not exist")
    line = load_settings(directory).get("statusLine")
    if line is None:
        return "none", None
    command = line.get("command") if isinstance(line, dict) else None
    if not isinstance(command, str) or not command.strip() or line.get("type", "command") != "command":
        raise ValueError(f"{settings_path(directory)}: statusLine has an unknown format, leaving it alone")
    if "claude-statusline/run.sh" in command:
        return "launcher", line
    if "usage-guard/statusline.sh" in command:
        return "legacy", line
    return "command", line


DESCRIPTIONS = {"launcher": "already has the launcher: only the sensor is updated",
                "legacy": "previous version of usage-guard: will be migrated",
                "none": "no statusline: the usage-guard one will be used",
                "command": "your statusline moves to a script and stays visible"}


def write_script(path, text, executable=True):
    write_text_atomic(path, text)
    path.chmod(0o755 if executable else 0o644)


def our_script(flag):
    return f"#!/bin/sh\nexec {shlex.quote(str(SHARE / 'statusline.sh'))} {flag}\n"


def add(directory):
    state, line = classify(directory)
    target = scripts_dir(directory)
    target.mkdir(parents=True, exist_ok=True)
    (target / ".account").write_text(f"{directory}\n")
    LAUNCHER.mkdir(parents=True, exist_ok=True)
    run = LAUNCHER / "run.sh"
    source = Path(__file__).resolve().parent / "run.sh"
    if source.read_bytes() != (run.read_bytes() if run.exists() else b""):
        write_script(run, source.read_text(encoding="utf-8"))

    original = read_json(directory / "usage-guard/original-statusline.json") if state == "legacy" else (line or {})
    if state != "launcher":
        backups = directory / "usage-guard"
        backups.mkdir(exist_ok=True)
        if settings_path(directory).exists():
            backup = backups / f"settings.json.{time.strftime('%Y%m%d-%H%M%S')}.bak"
            shutil.copy2(settings_path(directory), backup)
            print(f"  backup: {backup}")
        write_json_atomic(target / ".original.json", original)
        if original.get("command"):
            script = target / ORIGINAL
            if script.exists():  # from a previous install: kept, turned off
                old = target / f"{ORIGINAL}.{time.time_ns()}.old"
                script.rename(old)
                old.chmod(0o644)
            write_script(script, f"#!/usr/bin/env bash\n{original['command']}\n")
    # Existing scripts keep their state: if you turned one off, it stays off.
    for name, flag, active in ((SENSOR, "--sensor", True), (LINE, "--line", not original.get("command"))):
        script = target / name
        if script.exists():
            active = os.access(script, os.X_OK)
        elif state == "launcher" and name == LINE:
            active = False
        write_script(script, our_script(flag), active)

    # settings.json goes last.
    if state != "launcher":
        settings = load_settings(directory)
        command = f"bash {shlex.quote(str(run))} {shlex.quote(str(target))}"
        settings["statusLine"] = dict(original, type="command", command=command)
        save_settings(directory, settings)
    print(f"  {directory}: {DESCRIPTIONS[state]}\n    scripts in {target}")


def remove(directory, restore):
    state, _ = classify(directory)
    target = scripts_dir(directory)
    original = read_json(target / ".original.json")
    known = (target / ".original.json").exists()
    if state == "legacy":
        legacy = directory / "usage-guard/original-statusline.json"
        original, known, restore = read_json(legacy), legacy.exists(), True
    mine = {SENSOR, LINE}
    others = [p for p in target.glob("*.sh") if p.name not in mine and os.access(p, os.X_OK)]
    if state == "launcher" and not others:
        restore = True  # nothing left to run
    if restore and state in ("launcher", "legacy"):
        if not known:
            raise ValueError(f"original statusLine not found; see {directory}/usage-guard/*.bak")
        settings = load_settings(directory)
        if original:
            settings["statusLine"] = original
        else:
            settings.pop("statusLine", None)
        save_settings(directory, settings)
        print(f"  {directory}: original statusLine restored")
        state = "none"
    for name in mine:
        (target / name).unlink(missing_ok=True)
    if state == "launcher":
        print(f"  {directory}: sensor removed; your scripts stay in {target}")
    elif target.is_dir():
        script = target / ORIGINAL
        if script.exists() and script.read_text() == f"#!/usr/bin/env bash\n{original.get('command')}\n":
            script.unlink()
        for name in (".original.json", ".account", "last-error.log"):
            (target / name).unlink(missing_ok=True)
        if any(target.iterdir()):
            print(f"  keeping your scripts in {target}")
        else:
            target.rmdir()
        if not any(LAUNCHER.glob("*.d")):
            (LAUNCHER / "run.sh").unlink(missing_ok=True)
            if not any(LAUNCHER.iterdir()):
                LAUNCHER.rmdir()


def installed():
    found = []
    for mark in sorted(LAUNCHER.glob("*.d/.account")):
        if (mark.parent / SENSOR).exists() or (mark.parent / ".original.json").exists():
            found.append(Path(mark.read_text().strip()))
    return [d for d in dict.fromkeys([*found, *configured()]) if d.is_dir()]


def choose(args):
    if args == ["--all"]:
        return detect()
    if args:
        return list(dict.fromkeys(absolute(a) for a in args))
    found = detect()
    if not sys.stdin.isatty():
        raise ValueError("cannot ask without a terminal: use --account DIR (repeatable) or --all. "
                         "Detected: " + (", ".join(map(str, found)) or "none"))
    say = lambda text: print(text, file=sys.stderr)  # noqa: E731  (stdout carries the result)
    say("Claude Code accounts:")
    for index, directory in enumerate(found, 1):
        try:
            text = DESCRIPTIONS[classify(directory)[0]]
        except (OSError, ValueError) as error:
            text = f"not possible: {error}"
        say(f"  {index}) {directory}\n       {text}")
    while True:
        say("Where should it be applied? Numbers (1 2), paths, 'a' = all, empty = cancel:")
        try:
            answer = shlex.split(sys.stdin.readline())
        except ValueError:
            continue
        if not answer:
            raise ValueError("cancelled, nothing was changed")
        if answer == ["a"]:
            return found
        chosen = []
        for item in answer:
            if item.isdigit() and 1 <= int(item) <= len(found):
                chosen.append(found[int(item) - 1])
            elif not item.isdigit() and absolute(item).is_dir():
                chosen.append(absolute(item))
            else:
                say(f"  not understood: {item!r}")
                break
        else:
            return list(dict.fromkeys(chosen))


def configure(directories):
    path = config_path()
    text = path.read_text(encoding="utf-8")
    known = {os.path.realpath(d) for d in configured()}
    names = {a.name for a in load_config().accounts}
    for directory in directories:
        if os.path.realpath(directory) in known:
            continue
        base = name = account_name(directory)
        number = 2
        while name in names:
            name, number = f"{base}-{number}", number + 1
        names.add(name)
        text = text.rstrip("\n") + f"\n\n[[accounts]]\nname = {json.dumps(name)}\nconfig_dir = {json.dumps(str(directory))}\n"
        print(f"  {name}: {directory}")
    if text != path.read_text(encoding="utf-8"):
        write_text_atomic(path, text)
    for directory in configured():
        if not (scripts_dir(directory) / SENSOR).exists() and directory not in directories:
            print(f"  note: {directory} is in config.toml without the sensor; it will show as grey")


def main(args):
    if not args or args[0] not in ("choose", "check", "add", "configure", "installed", "remove", "restore"):
        print(__doc__, file=sys.stderr)
        return 2
    action, rest = args[0], args[1:]
    directories = [absolute(a) for a in rest] if action != "choose" else []
    failed = False
    try:
        if action == "choose":
            chosen = choose(rest)
            if not chosen:
                raise ValueError("no accounts: use --account DIR")
            print("\n".join(map(str, chosen)))
        elif action == "installed":
            print("\n".join(map(str, installed())))
        elif action == "configure":
            configure(directories)
        for directory in directories if action in ("check", "add", "remove", "restore") else []:
            try:
                if action == "check":
                    print(f"  {directory}: {DESCRIPTIONS[classify(directory)[0]]}")
                elif action == "add":
                    add(directory)
                else:
                    remove(directory, action == "restore")
            except (OSError, ValueError) as error:
                print(f"ERROR: {error}", file=sys.stderr)
                failed = True
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return int(failed)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
