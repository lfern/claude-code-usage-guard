import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from usage_guard import budget, cli, storage  # noqa: E402
from usage_guard.engine import Engine  # noqa: E402
from usage_guard.storage import Account, Config  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
MADRID = ZoneInfo("Europe/Madrid")


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=MADRID).timestamp()


RESET = at("2026-11-29T10:00")


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.account = Account("A", self.root / "cfg")
        self.config = Config([self.account])
        self.engine = Engine(self.config, self.root / "data")

    def read(self, pct, when, resets=RESET, now=None):
        """Simulates a sensor reading and a daemon cycle."""
        when = at(when) if isinstance(when, str) else when
        storage.write_json_atomic(self.account.state_path, {
            "seven_day_pct": pct, "seven_day_resets_at": resets, "five_hour_pct": 5, "read_at": when})
        return self.tick(now or when)

    def tick(self, now):
        statuses, toasts = self.engine.tick(at(now) if isinstance(now, str) else now)
        return statuses[0], [t["level"] for t in toasts]


class BudgetTest(Base):
    def test_budget_from_baseline_and_days(self):
        self.read(40, "2026-11-25T10:00")
        status, _ = self.read(44, "2026-11-25T12:00")
        self.assertAlmostEqual(status.budget, 60 / 4)
        self.assertAlmostEqual(status.spent, 4)
        self.assertAlmostEqual(status.ratio, 4 / 15)

    def test_budget_stays_fixed_during_the_day(self):
        first, _ = self.read(40, "2026-11-25T10:00")
        later, _ = self.read(50, "2026-11-25T23:00")
        self.assertEqual(first.budget, later.budget)

    def test_minimum_one_day(self):
        status, _ = self.read(70, "2026-11-29T08:00")
        self.assertAlmostEqual(status.budget, 30)

    def test_day_change(self):
        self.read(40, "2026-11-25T23:50")
        self.read(55, "2026-11-25T23:58")
        status, _ = self.tick("2026-11-26T00:01")  # yesterday's reading, still recent
        self.assertFalse(status.fresh)
        status, alerts = self.read(56, "2026-11-26T09:00")
        self.assertEqual((status.spent, status.level, alerts), (0, budget.OK, []))

    def test_local_day_is_madrid_not_utc(self):
        self.assertEqual(budget.local_day(at("2026-11-26T00:30"), "Europe/Madrid"), "2026-11-26")
        start, end = budget.day_bounds(at("2026-10-25T12:00"), "Europe/Madrid")
        self.assertEqual(end - start, 25 * 3600)

    def test_week_change_same_day(self):
        self.read(90, "2026-11-29T09:00")
        status, _ = self.read(2, "2026-11-29T11:00", RESET + budget.WEEK)
        self.assertEqual(status.spent, 0)
        self.assertAlmostEqual(status.budget, 98 / ((RESET + budget.WEEK - at("2026-11-29T11:00")) / 86400))

    def test_reset_jitter_is_not_a_new_week(self):
        self.read(40, "2026-11-25T10:00")
        status, _ = self.read(44, "2026-11-25T12:00", RESET + 2)
        self.assertEqual(status.spent, 4)

    def test_stale(self):
        self.read(40, "2026-11-25T10:00")
        self.read(99, "2026-11-25T10:05")
        status, alerts = self.tick("2026-11-25T10:16")
        self.assertFalse(status.fresh)
        self.assertEqual(alerts, [])
        status, _ = self.tick("2026-11-25T10:15")
        self.assertTrue(status.fresh)

    def test_reading_in_the_future_is_not_trusted(self):
        self.assertFalse(self.read(40, "2026-11-25T12:00", now=at("2026-11-25T10:00"))[0].fresh)

    def test_day_without_readings(self):
        self.read(40, "2026-11-25T10:00")
        self.assertFalse(self.tick("2026-11-26T12:00")[0].fresh)
        status, _ = self.read(52, "2026-11-27T10:00")
        self.assertEqual(status.spent, 0)
        self.assertAlmostEqual(status.budget, 48 / 2)

    def test_reset_in_the_past(self):
        status, alerts = self.read(99, "2026-11-29T10:01")
        self.assertFalse(status.fresh)
        self.assertEqual(alerts, [])
        self.assertNotIn("baseline", self.engine.runtime["A"]["record"])

    def test_extend_only_today_and_accumulates(self):
        self.read(40, "2026-11-25T10:00")
        self.engine.manual.extend("A", 5, "2026-11-25")
        self.engine.manual.extend("A", 2, "2026-11-25")
        self.assertAlmostEqual(self.read(44, "2026-11-25T11:00")[0].budget, 22)
        self.assertAlmostEqual(self.read(44, "2026-11-26T10:00")[0].budget, 56 / 3)

    def test_spent_never_negative(self):
        self.read(40, "2026-11-25T10:00")
        self.assertEqual(self.read(38, "2026-11-25T11:00")[0].spent, 0)

    def test_exhausted_quota_is_never_green(self):
        status, _ = self.read(100, "2026-11-25T10:00")
        self.assertEqual(status.level, budget.LIMIT)

    def test_projection(self):
        self.read(40, "2026-11-25T00:00")
        status, _ = self.read(60, "2026-11-25T10:00")  # 2 %/h
        self.assertAlmostEqual(status.full_at, at("2026-11-26T06:00"))

    def test_levels(self):
        for ratio, level in ((.79, 0), (.8, 1), (.99, 1), (1.0, 2), (1.2, 2), (1.21, 3)):
            self.assertEqual(budget.level_of(ratio, self.config), level)

    def test_baseline_survives_restart(self):
        self.read(40, "2026-11-25T10:00")
        self.engine = Engine(self.config, self.root / "data")
        self.assertEqual(self.read(44, "2026-11-25T11:00")[0].spent, 4)


class AlertTest(Base):  # budget: 15 points
    def setUp(self):
        super().setUp()
        self.read(40, "2026-11-25T10:00")

    def test_once_per_crossing(self):
        self.assertEqual(self.read(52.5, "2026-11-25T10:10")[1], [1])
        self.assertEqual(self.read(53, "2026-11-25T10:11")[1], [])
        self.assertEqual(self.read(55.5, "2026-11-25T10:12")[1], [2])
        self.assertEqual(self.read(59, "2026-11-25T10:13")[1], [3])
        self.assertEqual(self.read(59, "2026-11-25T10:20")[1], [])
        self.assertEqual(self.read(59, "2026-11-25T10:28")[1], [3])

    def test_jump_gives_one_toast(self):
        self.assertEqual(self.read(60, "2026-11-25T10:10")[1], [3])

    def test_restart_does_not_repeat(self):
        self.read(53, "2026-11-25T10:10")
        self.engine = Engine(self.config, self.root / "data")
        self.assertEqual(self.read(53, "2026-11-25T10:11")[1], [])

    def test_extend_rearms(self):
        self.read(53, "2026-11-25T10:10")
        self.engine.manual.extend("A", 10, "2026-11-25")
        self.assertEqual(self.read(53, "2026-11-25T10:11")[1], [])
        self.assertEqual(self.read(61, "2026-11-25T10:12")[1], [1])

    def test_snooze_delays_alert(self):
        self.engine.manual.snooze(["A"], at("2026-11-25T11:00"))
        self.assertEqual(self.read(60, "2026-11-25T10:10")[1], [])
        self.assertEqual(self.read(60, "2026-11-25T11:00")[1], [3])

    def test_stale_does_not_rearm(self):
        self.read(53, "2026-11-25T10:10")
        self.tick("2026-11-25T11:00")
        self.assertEqual(self.read(53, "2026-11-25T11:05")[1], [])


class RobustnessTest(Base):
    def test_fixtures(self):
        self.account.state_path.parent.mkdir(parents=True)
        now = 1795593600
        for name, fresh in (("normal", True), ("empty_fields", False), ("corrupt", False), ("not_object", False)):
            self.account.state_path.write_bytes((FIXTURES / f"{name}.json").read_bytes())
            self.assertEqual(self.tick(now)[0].fresh, fresh, name)
        self.account.state_path.unlink()
        self.assertFalse(self.tick(now)[0].fresh)

    def test_hostile_values(self):
        for bad in ("x", [], {}, True, float("nan"), 1e300, -1e300, "1e999", None):
            for key in ("seven_day_pct", "seven_day_resets_at", "read_at", "five_hour_pct"):
                state = {"seven_day_pct": 40, "seven_day_resets_at": RESET, "read_at": at("2026-11-25T10:00")}
                state[key] = bad
                storage.write_json_atomic(self.account.state_path, state)
                with self.subTest(key=key, bad=bad):
                    status, _ = self.tick("2026-11-25T10:00")
                    json.dumps(self.engine.widget_state([status], at("2026-11-25T10:00")))

    def test_corrupt_runtime_and_manual(self):
        self.read(40, "2026-11-25T10:00")
        for text in ("{", "[]", '{"accounts":{"A":{"record":5,"alert":[],"sample":"x"}},"extend":3,"snooze":[]}',
                     '{"accounts":{"A":{"record":{"baseline":"x"},"alert":{"level":"z"}}},"extend":{"A":1}}'):
            self.engine.runtime_path.write_text(text)
            self.engine.manual.path.write_text(text)
            self.engine = Engine(self.config, self.root / "data")
            self.assertTrue(self.read(44, "2026-11-25T11:00")[0].fresh)
            self.engine.manual.extend("A", 1, "2026-11-25")

    def test_history_at_most_once_a_minute(self):
        start = at("2026-11-25T10:00")
        for second in range(0, 121, 10):
            self.read(40, start + second)
        self.tick(start + 300)  # no new reading, no sample
        self.assertEqual(len(self.engine.history_path.read_text().splitlines()), 1 + 3)

    def test_widget_commands(self):
        now = at("2026-11-25T10:00")
        for message in ({}, {"cmd": "extend", "account": "nope"}, {"cmd": [], "account": "A"}):
            self.engine.command(message, now)
        self.engine.command({"cmd": "extend", "account": "A"}, now)
        self.engine.command({"cmd": "snooze", "account": "A"}, now)
        self.assertEqual(self.engine.manual.read("A", "2026-11-25"), (5, now + 3600))

    def test_cli(self):
        path = self.root / "config.toml"
        path.write_text(f'[[accounts]]\nname = "A"\nconfig_dir = "{self.account.config_dir}"\n')
        with mock.patch.dict(os.environ, USAGE_GUARD_CONFIG=str(path), USAGE_GUARD_DATA=str(self.root / "data")), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            for args in (["status"], ["extend", "5"], ["snooze", "1h"], ["history"]):
                self.assertEqual(cli.main(args), 0)
            self.assertEqual(cli.main(["extend", "5", "-a", "nope"]), 1)


class ConfigTest(Base):
    def test_example(self):
        text = (ROOT / "config.example.toml").read_text()
        expected = storage.parse_toml(text)
        self.assertEqual(storage.parse_toml_subset(text), expected)
        config = storage.load_config(ROOT / "config.example.toml")
        self.assertEqual([a.name for a in config.accounts], ["personal", "other"])

    def test_bad_configs(self):
        path = self.root / "c.toml"
        for text in ("x", "[thresholds]\nwarn = 2", "[[accounts]]\nname='a'", "stale_minutes = 0", 'timezone = "Nope/X"', "timezone = 5", 'timezone = "../x"', "accounts = 3"):
            path.write_text(text + "\n")
            with self.assertRaises(ValueError, msg=text):
                storage.load_config(path)

    def test_snooze_duration(self):
        self.assertEqual(cli.duration("1h"), 3600)
        for text in ("x", "-1h", "99999999d"):
            with self.assertRaises(Exception):
                cli.duration(text)

    def test_hash_inside_string(self):
        self.assertEqual(storage.parse_toml_subset('a = "x#y" # z\nb = \'C:\\p#\' # w'), {"a": "x#y", "b": "C:\\p#"})


class StatuslineTest(Base):
    def run_line(self, data, *args):
        env = dict(os.environ, CLAUDE_CONFIG_DIR=str(self.root / "cfg"))
        result = subprocess.run(["bash", str(ROOT / "statusline.sh"), *args], input=data,
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0)
        return result.stdout

    def start_session(self):
        """When Claude Code opens, the line is painted before it talks to the API."""
        self.run_line('{"session_id":"abc-1","cost":{"total_api_duration_ms":0}}')

    def test_first_paint_of_idle_session_is_not_trusted(self):
        data = (FIXTURES / "statusline_input.json").read_text()
        self.run_line(data)
        self.assertFalse(self.account.state_path.exists())
        self.run_line(data.replace('_ms":100', '_ms":150'))
        self.assertTrue(self.account.state_path.exists())

    def test_writes_state(self):
        self.start_session()
        line = self.run_line((FIXTURES / "statusline_input.json").read_text())
        self.assertEqual(line, "5h:12% 7d:44% today:--/--%")
        state = storage.read_json(self.account.state_path)
        self.assertEqual((state["seven_day_pct"], state["five_hour_resets_at"]), (44, 1795600000))
        self.assertEqual([p.name for p in self.account.state_path.parent.iterdir() if "state.json." in p.name], [])

    def test_never_fails(self):
        for data in ("", "{", "[]", "null", '{"rate_limits":null}', '{"rate_limits":{}}', '{"rate_limits":{"seven_day":5}}'):
            self.run_line(data)
        self.assertFalse(self.account.state_path.exists())

    def test_idle_session_does_not_refresh(self):
        data = json.loads((FIXTURES / "statusline_input.json").read_text())
        self.start_session()
        self.run_line(json.dumps(data))
        self.account.state_path.write_text('{"mark":1}')
        self.run_line(json.dumps(data))
        self.assertIn("mark", storage.read_json(self.account.state_path))
        data["cost"]["total_api_duration_ms"] = 200
        self.run_line(json.dumps(data))
        self.assertNotIn("mark", storage.read_json(self.account.state_path))

    def test_old_percentages_from_another_session_are_ignored(self):
        data = json.loads((FIXTURES / "statusline_input.json").read_text())
        limits = data["rate_limits"]

        def paint(week, five, hours=0):
            data["cost"]["total_api_duration_ms"] += 1
            limits["seven_day"] = {"used_percentage": week, "resets_at": 1796029200 + hours * 3600}
            limits["five_hour"]["used_percentage"] = five
            self.run_line(json.dumps(data))
            state = storage.read_json(self.account.state_path)
            return state["seven_day_pct"], state["five_hour_pct"]

        self.start_session()
        self.assertEqual(paint(5, 12), (5, 12))
        self.assertEqual(paint(4, 9), (5, 12))
        self.assertEqual(paint(5, 11), (5, 12))
        self.assertEqual(paint(5, 12.5), (5, 12.5))
        self.assertEqual(paint(1, 13, hours=168), (1, 13))  # new week
        state = storage.read_json(self.account.state_path)
        state["read_at"] -= 700
        storage.write_json_atomic(self.account.state_path, state)
        self.assertEqual(paint(0, 1, hours=168), (0, 1))    # nothing recent to compare with

    def test_today_is_shown_until_it_expires(self):
        for expiry, text in ((9999999999, "today:4/15%"), (1, "today:--/--%")):
            storage.write_json_atomic(self.account.today_path, {"spent": 4.2, "budget": 15, "valid_until": expiry})
            self.assertIn(text, self.run_line("{}"))

    def test_modes(self):
        data = (FIXTURES / "statusline_input.json").read_text()
        self.start_session()
        self.run_line(data, "--line")
        self.assertFalse(self.account.state_path.exists())
        self.assertEqual(self.run_line(data, "--sensor"), "")
        self.assertTrue(self.account.state_path.exists())


class LauncherTest(Base):
    def test_failures_do_not_stop_the_rest(self):
        scripts = {"10-a.sh": "#!/bin/sh\ncat", "20-fails.sh": "#!/bin/sh\necho partial; echo boom >&2; exit 3",
                   "30-hangs.sh": "#!/bin/sh\nsleep 30", "40-bad.sh": "#!/no/such/shell\n",
                   "50-syntax.sh": "#!/bin/bash\nif (", "60-killed.sh": "#!/bin/sh\nkill -9 $$",
                   "70-group.sh": "#!/bin/sh\nkill 0", "90-z.sh": "#!/bin/sh\necho end"}
        for name, text in scripts.items():
            (self.root / name).write_text(text + "\n")
            (self.root / name).chmod(0o755)
        (self.root / "80-off.sh").write_text("#!/bin/sh\necho no\n")
        result = subprocess.run(["bash", str(ROOT / "run.sh"), str(self.root)], input="in", text=True, timeout=10,
                                capture_output=True, env=dict(os.environ, STATUSLINE_TIMEOUT="1"))
        self.assertEqual((result.returncode, result.stdout), (0, "in\npartial\nend\n"))
        log = (self.root / "last-error.log").read_text()
        for name in list(scripts)[1:-1]:
            self.assertIn(name, log)
        self.assertIn("boom", log)

    def test_missing_folder(self):
        self.assertEqual(subprocess.run(["bash", str(ROOT / "run.sh"), "/no/such"], input=b"").returncode, 0)


class InstallTest(Base):
    def setUp(self):
        super().setUp()
        fake = self.root / "fakebin"
        fake.mkdir()
        for name in ("powershell.exe", "systemctl"):  # the real service and Windows are not touched
            (fake / name).write_text("#!/bin/sh\nexit 0\n")
            (fake / name).chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.root), PATH=f"{fake}:/usr/bin:/bin")
        for key in ("CLAUDE_CONFIG_DIR", "USAGE_GUARD_DATA", "USAGE_GUARD_CONFIG"):
            self.env.pop(key, None)
        self.one, self.two = self.root / ".claude.personal", self.root / ".claude-other"
        self.line = {"type": "command", "command": "jq -r .session_id; echo ñ", "padding": 1}
        self.put(self.one, {"statusLine": self.line, "x": [1]})
        self.put(self.two, {"y": 2})
        self.scripts = self.root / ".config/claude-statusline/personal.d"

    def put(self, directory, value):
        storage.write_json_atomic(directory / "settings.json", value)

    def settings(self, directory):
        return storage.read_json(directory / "settings.json")

    def script(self, name, *args, code=0):
        result = subprocess.run(["bash", str(ROOT / name), *map(str, args)], env=self.env,
                                capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def install(self, *dirs, code=0):
        flags = [x for d in dirs for x in ("--account", d)]
        return self.script("install.sh", "--no-service", *flags, code=code)

    def paint(self, directory):
        return subprocess.run(["bash", "-c", self.settings(directory)["statusLine"]["command"]],
                              input=(FIXTURES / "statusline_input.json").read_text(), text=True,
                              capture_output=True, env=dict(self.env, CLAUDE_CONFIG_DIR=str(directory))).stdout

    def test_only_chosen_account(self):
        before = self.settings(self.two)
        self.install(self.one)
        self.assertEqual(self.settings(self.two), before)
        self.assertFalse((self.two / "usage-guard").exists())
        config = storage.load_config(self.root / ".config/usage-guard/config.toml")
        self.assertEqual([a.config_dir for a in config.accounts], [self.one])
        self.assertEqual(self.paint(self.one), "abc-1\nñ\n")
        self.assertTrue((self.one / "usage-guard/sessions/abc-1").exists())
        (self.one / "usage-guard/sessions/abc-1").write_text("before")
        self.paint(self.one)
        self.assertTrue((self.one / "usage-guard/state.json").exists())
        self.install(self.two)
        self.assertEqual(len(storage.load_config(self.root / ".config/usage-guard/config.toml").accounts), 2)
        self.assertIn("7d:44%", self.paint(self.two))

    def test_menu(self):
        import pty
        for answer, count in ((b"2\n", 1), (b"9\n\n", 0), (b"a\n", 2)):
            parent, child = pty.openpty()
            os.write(parent, answer)
            result = subprocess.run([sys.executable, str(ROOT / "settings_tool.py"), "choose"], env=self.env,
                                    stdin=child, capture_output=True, text=True)
            os.close(parent), os.close(child)
            self.assertEqual(len(result.stdout.split()), count, result.stderr)

    def test_no_terminal_and_dry_run_change_nothing(self):
        before = sorted(self.root.rglob("*"))
        self.install(code=1)
        self.script("install.sh", "--dry-run", "--all")
        self.assertEqual(sorted(self.root.rglob("*")), before)

    def test_bad_settings_change_nothing(self):
        for text in ("{", "[]", '{"statusLine": "x"}', '{"statusLine": {"type": "other", "command": "x"}}'):
            (self.two / "settings.json").write_text(text)
            before = sorted(self.root.rglob("*"))
            self.install(self.one, self.two, code=1)
            self.assertEqual(sorted(self.root.rglob("*")), before)

    def test_repeat_respects_user_changes(self):
        self.install(self.one)
        first = (self.one / "settings.json").read_bytes()
        (self.scripts / "10-usage-guard.sh").chmod(0o644)
        (self.scripts / "50-original.sh").write_text("#!/bin/sh\necho edited\n")
        (self.scripts / "70-mine.sh").write_text("#!/bin/sh\necho another\n")
        (self.scripts / "70-mine.sh").chmod(0o755)
        self.install(self.one)
        self.assertEqual((self.one / "settings.json").read_bytes(), first)
        self.assertFalse(os.access(self.scripts / "10-usage-guard.sh", os.X_OK))
        self.assertEqual(self.paint(self.one), "edited\nanother\n")
        self.script("uninstall.sh")
        self.assertEqual(self.paint(self.one), "edited\nanother\n")
        self.script("uninstall.sh", "--restore-statusline")
        self.assertEqual(self.settings(self.one)["statusLine"], self.line)
        self.assertTrue((self.scripts / "70-mine.sh").exists())

    def test_replaced_statusline(self):
        self.install(self.one)
        self.put(self.one, {"statusLine": {"type": "command", "command": "echo new"}})
        self.install(self.one)
        self.assertEqual(self.paint(self.one), "new\n")

    def test_restore_everything(self):
        one, two = self.settings(self.one), self.settings(self.two)
        self.install(self.one, self.two)
        self.script("uninstall.sh", "--restore-statusline", "--purge")
        self.assertEqual((self.settings(self.one), self.settings(self.two)), (one, two))
        self.assertFalse((self.root / ".config/claude-statusline").exists())
        self.assertFalse((self.root / ".local/bin/usage-guard").exists())

    def test_plain_uninstall_without_own_line(self):
        self.install(self.two)
        self.script("uninstall.sh")
        self.assertEqual(self.settings(self.two), {"y": 2})

    def test_uninstall_one_account(self):
        self.install(self.one, self.two)
        self.script("uninstall.sh", "--restore-statusline", "--account", self.one)
        self.assertEqual(self.settings(self.one)["statusLine"], self.line)
        self.assertIn("run.sh", self.settings(self.two)["statusLine"]["command"])
        self.assertTrue((self.root / ".local/bin/usage-guard").exists())

    def test_migrates_previous_version(self):
        storage.write_json_atomic(self.one / "usage-guard/original-statusline.json", self.line)
        self.put(self.one, {"statusLine": {"type": "command", "command": "bash '/h/usage-guard/statusline.sh' --compose"}})
        self.install(self.one)
        self.assertEqual(self.paint(self.one), "abc-1\nñ\n")

    def test_symlink_is_kept(self):
        real = self.root / "dotfiles.json"
        (self.one / "settings.json").rename(real)
        (self.one / "settings.json").symlink_to(real)
        self.install(self.one)
        self.assertTrue((self.one / "settings.json").is_symlink())
        self.assertIn("run.sh", storage.read_json(real)["statusLine"]["command"])


if __name__ == "__main__":
    unittest.main()
