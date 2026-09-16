"""Small native Codex client for local metadata operations, never model turns."""

import json
import os
import select
import shutil
import subprocess
import tempfile
import time

from . import __version__
from .files import SyncError


class CodexMetadata:
    methods = ("initialize", "thread/read", "thread/name/set", "thread/list")

    def __init__(self, root, isolated=False):
        self.root = root
        self.isolated = isolated
        self.pending = bytearray()
        self.request_id = 0

    def __enter__(self):
        if not shutil.which("codex"):
            raise SyncError("Install Codex CLI to restore saved conversation names, then retry pull/restore.")
        env = dict(os.environ, CODEX_HOME=str(self.root))
        if self.isolated:
            env.update(HOME=str(self.root), XDG_CONFIG_HOME=str(self.root / "config"),
                       XDG_DATA_HOME=str(self.root / "data"), XDG_CACHE_HOME=str(self.root / "cache"))
        for key in list(env):
            if "API_KEY" in key or "TOKEN" in key:
                env.pop(key)
        self.errors = tempfile.TemporaryFile()
        try:
            self.proc = subprocess.Popen(
                ["codex", "app-server", "-c", "analytics.enabled=false"],
                cwd=str(self.root), env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=self.errors, bufsize=0)
            self.call("initialize", {"clientInfo": {"name": "agent_sync", "version": __version__},
                                     "capabilities": {"experimentalApi": True}})
            self.proc.stdin.write(b'{"method":"initialized"}\n')
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def call(self, method, params):
        if method not in self.methods:
            raise ValueError("Unsupported metadata operation: " + method)
        self.request_id += 1
        try:
            self.proc.stdin.write((json.dumps({"id": self.request_id, "method": method,
                                               "params": params}) + "\n").encode())
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                while b"\n" in self.pending:
                    line, _, rest = self.pending.partition(b"\n")
                    self.pending[:] = rest
                    response = json.loads(line)
                    if response.get("id") == self.request_id:
                        if "error" in response:
                            raise SyncError("Codex {} failed: {}".format(method, response["error"]))
                        return response["result"]
                if select.select([self.proc.stdout], [], [], 0.25)[0]:
                    chunk = os.read(self.proc.stdout.fileno(), 65536)
                    if not chunk:
                        self.errors.seek(0)
                        raise SyncError("Codex metadata service stopped: " +
                                        self.errors.read(4096).decode(errors="replace").strip())
                    self.pending.extend(chunk)
            raise SyncError("Codex metadata operation timed out: " + method)
        except (OSError, ValueError, KeyError) as exc:
            raise SyncError("Cannot restore Codex names: {}".format(exc))

    def __exit__(self, *_):
        proc = getattr(self, "proc", None)
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            proc.stdin.close()
            proc.stdout.close()
        self.errors.close()


class CodexReader(CodexMetadata):
    """Audit client: deliberately cannot rename, resume, or start a turn."""

    methods = ("initialize", "thread/read", "thread/turns/list")
