"""Starts widget.ps1 and talks to it: JSON on stdin (state) and stdout (commands)."""
import glob
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

DEFAULT_POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"


def find_powershell(configured=""):
    for candidate in (configured, shutil.which("powershell.exe"), DEFAULT_POWERSHELL):
        if candidate and os.path.exists(candidate):
            return candidate
    return None


class Widget:
    def __init__(self, script, powershell=""):
        self.script = Path(script)
        self.powershell = powershell
        self.process = None
        self.ready = False
        self.commands = queue.Queue()
        self.pending = []           # toasts waiting for the widget to be ready
        self.retry_at = 0.0
        self.delay = 2.0
        self.started = 0.0
        self.last_error = None

    def _log(self, text):
        if text != self.last_error:
            print(f"usage-guardd: {text}", file=sys.stderr, flush=True)
            self.last_error = text

    def ensure(self):
        """Starts the widget if it is not alive, with increasing delay between attempts."""
        now = time.monotonic()
        if self.process and self.process.poll() is None:
            if self.ready and now - self.started > 60:
                self.delay = 2.0
            elif not self.ready and now - self.started > 60:
                self._log("the widget does not respond; restarting it")
                self.stop()
            return
        if self.process:
            self._log(f"the widget exited (code {self.process.returncode}); starting it again")
            self._discard()
        if now < self.retry_at:
            return
        self.retry_at = now + self.delay
        self.delay = min(60.0, self.delay * 2)
        powershell = find_powershell(self.powershell)
        if not powershell:
            self._log("powershell.exe not found; continuing without the widget")
            return
        try:
            path = subprocess.run(["wslpath", "-w", str(self.script)], capture_output=True,
                                  text=True, timeout=10, check=True).stdout.strip()
            env = dict(os.environ)
            if not os.path.exists(env.get("WSL_INTEROP", "")):
                # It may be missing in a systemd service: the socket of init works.
                sockets = ["/run/WSL/1_interop", *sorted(glob.glob("/run/WSL/*_interop"))]
                found = next((s for s in sockets if os.path.exists(s)), None)
                if found:
                    env["WSL_INTEROP"] = found
            cwd = "/mnt/c/Windows" if os.path.isdir("/mnt/c/Windows") else "/"
            self.process = subprocess.Popen(
                [powershell, "-NoLogo", "-NoProfile", "-STA", "-WindowStyle", "Hidden",
                 "-ExecutionPolicy", "Bypass", "-File", path],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, cwd=cwd)
        except (OSError, subprocess.SubprocessError) as error:
            self._log(f"could not start the widget: {error}")
            return
        os.set_blocking(self.process.stdin.fileno(), False)
        self.started = now
        self.ready = False
        threading.Thread(target=self._read, args=(self.process,), daemon=True).start()

    def _read(self, process):
        for line in process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict):
                self.commands.put(message)

    def poll_commands(self):
        while True:
            try:
                message = self.commands.get_nowait()
            except queue.Empty:
                return
            if message.get("cmd") == "ready":
                self.ready = True
                self.last_error = None
            else:
                yield message

    def send(self, message):
        if not (self.ready and self.process and self.process.poll() is None):
            return False
        data = (json.dumps(message) + "\n").encode("ascii")
        try:
            written = os.write(self.process.stdin.fileno(), data)
        except OSError:
            written = 0
        if written != len(data):  # broken or full pipe: do not block, start it again
            self.stop()
            return False
        return True

    def toast(self, message):
        self.pending = (self.pending + [message])[-10:]
        self.flush()

    def flush(self):
        while self.pending and self.send(self.pending[0]):
            self.pending.pop(0)

    def stop(self):
        """Closes stdin: the widget closes itself. Otherwise it is killed."""
        if not self.process:
            return
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self._discard()

    def _discard(self):
        if self.process and not self.process.stdin.closed:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        self.process = None
        self.ready = False
