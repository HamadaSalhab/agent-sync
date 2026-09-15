import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agent_sync import cli, config, store
from agent_sync.files import SyncError, collect, digest, encode, merge_file, safe_path

ROOT = Path(__file__).resolve().parents[1]


def lines(*rows):
    return b"".join((json.dumps(row) + "\n").encode() for row in rows)


class SyncIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-sync-test-")
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True)
        self.env = dict(os.environ)
        for key in ["CODEX_HOME", "CLAUDE_DATA_DIR", "AGENT_SYNC_HOME", "GIT_DIR", "GIT_WORK_TREE"]:
            self.env.pop(key, None)
        self.env["GIT_CONFIG_NOSYSTEM"] = "1"
        self.env["GIT_CONFIG_GLOBAL"] = os.devnull
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.a = self.machine("a")
        self.b = self.machine("b")

    def tearDown(self):
        self.temp.cleanup()

    def command(self, machine, *args, code=0):
        result = subprocess.run([sys.executable, str(ROOT / "agent-sync"), "--state-dir",
                                 str(machine / "state")] + list(args), env=self.env,
                                capture_output=True, text=True, cwd=str(self.root))
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def machine(self, name, **kwargs):
        machine = self.root / name
        self.command(machine, "init", "--remote", str(self.remote),
                     "--claude-dir", str(machine / "claude"), "--codex-dir", str(machine / "codex"),
                     *kwargs.get("extra", []))
        return machine

    def put(self, machine, tool, rel, data, stamp=1700000000000000000):
        path = machine / tool / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.utime(str(path), ns=(stamp, stamp))
        return path

    def test_two_tools_two_machines_round_trip_preserves_mtimes_and_secrets(self):
        claude = lines({"type": "user", "message": "hello"})
        codex = lines({"type": "session_meta", "payload": {"id": "test"}})
        self.put(self.a, "claude", "projects/my-project/session.jsonl", claude)
        self.put(self.a, "codex", "sessions/2026/09/16/rollout.jsonl", codex)
        self.put(self.a, "codex", "archived_sessions/old.jsonl", codex)
        self.put(self.a, "claude", "file-history/id/edited-file@v1", b"file contents")
        self.put(self.a, "claude", "todos/id.json", b"[]")
        self.put(self.a, "codex", "auth.json", b"do-not-sync-secret")
        self.put(self.a, "claude", ".credentials.json", b"do-not-sync-secret")
        self.put(self.a, "codex", "state_5.sqlite", b"do-not-sync-database")
        self.command(self.a, "push", "--all")
        self.command(self.b, "pull", "--all")
        self.assertEqual((self.b / "claude/projects/my-project/session.jsonl").read_bytes(), claude)
        restored = self.b / "codex/sessions/2026/09/16/rollout.jsonl"
        self.assertEqual(restored.read_bytes(), codex)
        self.assertEqual(restored.stat().st_mtime_ns, 1700000000000000000)
        self.assertTrue((self.b / "codex/archived_sessions/old.jsonl").exists())
        self.assertFalse((self.b / "codex/auth.json").exists())
        self.assertFalse((self.b / "codex/state_5.sqlite").exists())
        repo = self.a / "state/repository"
        tracked = store.git(repo, "ls-files").stdout
        self.assertNotIn("auth.json", tracked)
        self.assertNotIn("credentials", tracked)
        self.assertNotIn("sqlite", tracked)

    def test_divergent_git_commits_from_separate_machines_merge(self):
        self.put(self.a, "claude", "projects/p/a.jsonl", lines({"a": 1}))
        self.put(self.b, "codex", "sessions/b.jsonl", lines({"b": 1}))
        # Commit B locally before A pushes, reproducing concurrent snapshots.
        cfg_b = json.loads((self.b / "state/config.json").read_text())
        repo_b = self.b / "state/repository"
        store.write_snapshot(repo_b, cfg_b, "codex", collect(self.b / "codex", "codex"))
        store.commit(repo_b, "B snapshot before A push")
        self.command(self.a, "push")
        self.command(self.b, "push")
        self.command(self.a, "pull")
        self.assertTrue((self.a / "codex/sessions/b.jsonl").exists())
        self.command(self.b, "pull")
        self.assertTrue((self.b / "claude/projects/p/a.jsonl").exists())

    def test_history_union_on_both_push_pull_cycles_and_index_latest_name(self):
        self.put(self.a, "claude", "history.jsonl", lines({"timestamp": 20, "display": "A"}))
        self.put(self.b, "claude", "history.jsonl", lines({"timestamp": 10, "display": "B"}))
        self.put(self.a, "codex", "history.jsonl", lines({"ts": 20, "text": "A"}))
        self.put(self.b, "codex", "history.jsonl", lines({"ts": 10, "text": "B"}))
        self.put(self.a, "codex", "session_index.jsonl", lines({"id": "id", "thread_name": "old", "updated_at": "2026-01-01T00:00:00Z"}))
        self.put(self.b, "codex", "session_index.jsonl", lines({"id": "id", "thread_name": "new", "updated_at": "2026-02-01T00:00:00Z"}))
        self.command(self.a, "push")
        self.command(self.b, "push")
        self.command(self.a, "pull")
        self.command(self.a, "push")
        self.command(self.b, "pull")
        for tool in ("claude", "codex"):
            data = (self.a / tool / "history.jsonl").read_bytes()
            self.assertEqual(data, (self.b / tool / "history.jsonl").read_bytes())
            self.assertEqual(len(data.splitlines()), 2)
        index = json.loads((self.b / "codex/session_index.jsonl").read_text())
        self.assertEqual(index["thread_name"], "new")

    def test_prefix_extension_beats_stale_clock_and_divergence_keeps_local(self):
        rel = "projects/p/session.jsonl"
        first, extended, other = lines({"n": 1}), lines({"n": 1}, {"n": 2}), lines({"n": 1}, {"branch": "other"})
        self.put(self.a, "claude", rel, first, 1800000000000000000)
        self.put(self.b, "claude", rel, extended, 1700000000000000000)
        self.command(self.b, "push")
        self.command(self.a, "pull")
        self.assertEqual((self.a / "claude" / rel).read_bytes(), extended)
        self.put(self.a, "claude", rel, other)
        result = self.command(self.a, "pull", code=2)
        self.assertIn("CONFLICT", result.stdout)
        self.assertEqual((self.a / "claude" / rel).read_bytes(), other)
        conflicts = list((self.a / "state/conflicts").rglob("content"))
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].read_bytes(), extended)

    def test_dry_run_does_not_touch_files_or_fetch(self):
        self.put(self.a, "claude", "projects/p/a.jsonl", lines({"a": 1}))
        self.command(self.a, "push")
        before = {p.relative_to(self.b): (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in self.b.rglob("*") if p.is_file()}
        self.command(self.b, "pull", "--dry-run")
        self.command(self.b, "push", "--dry-run")
        after = {p.relative_to(self.b): (p.read_bytes(), p.stat().st_mtime_ns)
                 for p in self.b.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_backup_restore_retains_unrelated_files_and_creates_safety_backup(self):
        rel = "sessions/a.jsonl"
        self.put(self.a, "codex", rel, lines({"a": 1}))
        self.command(self.a, "backup")
        name = self.command(self.a, "backups").stdout.strip()
        self.put(self.a, "codex", rel, lines({"a": 2}))
        self.put(self.a, "codex", "auth.json", b"keep me")
        self.command(self.a, "restore", name, "--dry-run")
        self.assertEqual((self.a / "codex" / rel).read_bytes(), lines({"a": 2}))
        self.command(self.a, "restore", name)
        self.assertEqual((self.a / "codex" / rel).read_bytes(), lines({"a": 1}))
        self.assertEqual((self.a / "codex/auth.json").read_bytes(), b"keep me")
        self.assertEqual(len(list((self.a / "state/backups").glob("*/manifest.json"))), 2)
        self.command(self.a, "restore", "../config.json", code=1)

    def test_remote_failure_is_reported_without_modifying_local_data(self):
        self.put(self.a, "codex", "sessions/a.jsonl", lines({"a": 1}))
        self.command(self.a, "push")
        shutil.move(str(self.remote), str(self.root / "remote-moved.git"))
        result = self.command(self.b, "pull", code=1)
        self.assertNotIn("Pull complete", result.stdout)
        self.assertFalse((self.b / "codex").exists())
        result = self.command(self.a, "push", code=1)
        self.assertNotIn("Push complete", result.stdout)

    def test_failed_push_can_be_retried_with_no_new_local_changes(self):
        self.put(self.a, "codex", "sessions/a.jsonl", lines({"a": 1}))
        hook = self.remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o700)
        self.command(self.a, "push", code=1)
        hook.unlink()
        self.command(self.a, "push")
        self.command(self.b, "pull")
        self.assertTrue((self.b / "codex/sessions/a.jsonl").exists())

    def test_tool_selection_and_idempotent_push(self):
        self.put(self.a, "claude", "projects/p/a.jsonl", lines({"a": 1}))
        self.put(self.a, "codex", "sessions/a.jsonl", lines({"a": 1}))
        self.command(self.a, "push", "--tool", "claude")
        self.command(self.b, "pull")
        self.assertTrue((self.b / "claude/projects/p/a.jsonl").exists())
        self.assertFalse((self.b / "codex").exists())
        head = store.git(self.a / "state/repository", "rev-parse", "HEAD").stdout
        self.command(self.a, "push", "--tool", "claude")
        self.assertEqual(head, store.git(self.a / "state/repository", "rev-parse", "HEAD").stdout)

    def test_malformed_jsonl_fails_before_snapshot_changes(self):
        self.put(self.a, "codex", "sessions/a.jsonl", b'{"unfinished":')
        result = self.command(self.a, "push", code=1)
        self.assertIn("Invalid JSONL", result.stderr)
        self.assertFalse((self.a / "state/repository/machines").exists())

    def test_symlink_source_and_destination_rejected(self):
        target = self.put(self.a, "codex", "auth.json", b"secret")
        (self.a / "codex/sessions").mkdir()
        (self.a / "codex/sessions/leak.jsonl").symlink_to(target)
        self.command(self.a, "push", code=1)
        (self.a / "codex/sessions/leak.jsonl").unlink()
        self.put(self.a, "codex", "sessions/good.jsonl", lines({"a": 1}))
        self.command(self.a, "push")
        outside = self.root / "outside"
        outside.mkdir()
        (self.b / "codex").mkdir()
        (self.b / "codex/sessions").symlink_to(outside, target_is_directory=True)
        self.command(self.b, "pull", code=1)
        self.assertEqual(list(outside.iterdir()), [])

    def test_checksum_and_traversal_in_remote_snapshot_rejected(self):
        self.put(self.a, "codex", "sessions/a.jsonl", lines({"a": 1}))
        self.command(self.a, "push")
        repo = self.a / "state/repository"
        manifest = next(repo.glob("machines/*/codex/manifest.json"))
        meta = json.loads(manifest.read_text())
        meta["files"]["sessions/a.jsonl"]["sha256"] = "0" * 64
        manifest.write_bytes(encode(meta))
        store.commit(repo, "Corrupt test fixture")
        store.git(repo, "push", "origin", "HEAD:main")
        result = self.command(self.b, "pull", code=1)
        self.assertIn("checksum mismatch", result.stderr)
        self.assertFalse((self.b / "codex").exists())
        entry = next(iter(meta["files"].values()))
        meta["files"] = {"sessions/../../escape.jsonl": entry}
        manifest.write_bytes(encode(meta))
        store.commit(repo, "Traversal test fixture")
        store.git(repo, "push", "origin", "HEAD:main")
        self.command(self.b, "pull", code=1)
        self.assertFalse((self.b / "escape.jsonl").exists())

    def test_unknown_remote_layout_is_not_modified(self):
        other = self.root / "other"
        subprocess.run(["git", "clone", str(self.remote), str(other)], check=True, capture_output=True)
        # Clone may not select main because a bare repo's symbolic HEAD is master.
        store.git(other, "checkout", "main")
        (other / "agent-sync.json").unlink()
        store.commit(other, "Remove marker for test")
        store.git(other, "push", "origin", "HEAD:main")
        self.command(self.root / "c", "init", "--remote", str(self.remote), code=1)
        self.assertFalse((self.root / "c/state/config.json").exists())

    @unittest.skipUnless(shutil.which("codex"), "Codex CLI is not installed")
    def test_native_codex_discovers_and_reads_transferred_session(self):
        session_id = "11111111-1111-4111-8111-111111111111"
        data = lines(
            {"timestamp": "2026-09-16T00:00:00Z", "type": "session_meta", "payload": {
                "id": session_id, "timestamp": "2026-09-16T00:00:00Z",
                "cwd": str(self.root), "originator": "codex_cli_rs", "cli_version": "0.154.0",
                "source": "cli", "model_provider": "openai"}},
            {"timestamp": "2026-09-16T00:00:01Z", "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": "Synthetic agent-sync verification."}]}},
            {"timestamp": "2026-09-16T00:00:01Z", "type": "event_msg", "payload": {
                "type": "user_message", "message": "Synthetic agent-sync verification.",
                "images": [], "local_images": [], "text_elements": []}})
        rel = "sessions/2026/09/16/rollout-2026-09-16T00-00-00-{}.jsonl".format(session_id)
        self.put(self.a, "codex", rel, data)
        self.command(self.a, "push", "--tool", "codex")
        self.command(self.b, "pull", "--tool", "codex")
        env = dict(self.env, CODEX_HOME=str(self.b / "codex"), HOME=str(self.b))
        for key in list(env):
            if "API_KEY" in key or "TOKEN" in key:
                env.pop(key)
        proc = subprocess.Popen(["codex", "app-server"], cwd=str(self.root), env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, bufsize=0)
        pending = bytearray()

        def call(request_id, method, params):
            proc.stdin.write((json.dumps({"id": request_id, "method": method, "params": params}) + "\n").encode())
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                while b"\n" in pending:
                    line, _, rest = pending.partition(b"\n")
                    pending[:] = rest
                    response = json.loads(line)
                    if response.get("id") == request_id:
                        self.assertNotIn("error", response, response)
                        return response["result"]
                if select.select([proc.stdout], [], [], 0.5)[0]:
                    chunk = os.read(proc.stdout.fileno(), 65536)
                    self.assertTrue(chunk, "Codex app-server stopped unexpectedly")
                    pending.extend(chunk)
            self.fail("Codex app-server timed out on " + method)

        try:
            call(1, "initialize", {"clientInfo": {"name": "agent_sync_test", "version": "0.1.0"}})
            proc.stdin.write(b'{"method":"initialized"}\n')
            threads = call(2, "thread/list", {"limit": 100, "useStateDbOnly": False})
            self.assertIn(session_id, [t["id"] for t in threads["data"]])
            thread = call(3, "thread/read", {"threadId": session_id, "includeTurns": True})["thread"]
            self.assertIn("Synthetic agent-sync verification.", json.dumps(thread["turns"]))
            self.assertEqual(thread["id"], session_id)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            proc.stdin.close()
            proc.stdout.close()

    @unittest.skipUnless(shutil.which("git-crypt"), "git-crypt is not installed")
    def test_encryption_round_trip_and_locked_checkout(self):
        remote = self.root / "encrypted.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        key = self.root / "key"
        c, d = self.root / "c", self.root / "d"
        for m, extra in [(c, ["--encrypt"]), (d, [])]:
            self.command(m, "init", "--remote", str(remote), "--key-file", str(key),
                         "--claude-dir", str(m / "claude"), "--codex-dir", str(m / "codex"), *extra)
        self.put(c, "codex", "sessions/a.jsonl", lines({"secret": "synthetic"}))
        self.command(c, "push")
        self.command(d, "pull")
        self.assertEqual((d / "codex/sessions/a.jsonl").read_bytes(), lines({"secret": "synthetic"}))
        paths = store.git(c / "state/repository", "ls-files").stdout.splitlines()
        path = next(p for p in paths if p.endswith("a.jsonl"))
        raw = subprocess.check_output(["git", "--git-dir", str(remote), "show", "main:" + path])
        self.assertTrue(raw.startswith(b"\x00GITCRYPT"))
        store.crypt(d / "state/repository", "lock")
        self.command(d, "pull", code=1)
        self.command(d, "unlock", "--key-file", str(key))


class MergePolicyTests(unittest.TestCase):
    def test_divergent_fresh_machine_keeps_all_alternatives(self):
        a, b = lines({"a": 1}), lines({"b": 2})
        chosen, stamp, alternatives = merge_file("codex", "sessions/a.jsonl", [(a, 1), (b, 2)])
        self.assertEqual((chosen, stamp), (b, 2))
        self.assertEqual(alternatives, [(a, 1)])

    def test_paths_and_overlapping_roots_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for relative in ["../x", "/x", "a/../../x", "a//b", ".git/config", "a\\b"]:
                with self.assertRaises(SyncError):
                    safe_path(root, relative)
            with self.assertRaises(SyncError):
                config.create(root, "remote", "main", ["codex"], root / "claude", root / "codex")

    def test_file_history_with_jsonl_extension_is_treated_as_raw_data(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            p = root / "file-history/example.jsonl"
            p.parent.mkdir()
            p.write_bytes(b"a partial edited file, not a conversation")
            self.assertEqual(collect(root, "claude")["file-history/example.jsonl"][0], p.read_bytes())

    def test_single_malformed_remote_transcript_is_rejected(self):
        with self.assertRaises(SyncError):
            merge_file("codex", "sessions/a.jsonl", [(b'{"partial":', 1)])

    def test_state_lock_prevents_concurrent_sync(self):
        with tempfile.TemporaryDirectory() as temp:
            with store.locked(Path(temp)):
                with self.assertRaises(SyncError):
                    with store.locked(Path(temp)):
                        self.fail("Second sync acquired the lock")


if __name__ == "__main__":
    unittest.main()
