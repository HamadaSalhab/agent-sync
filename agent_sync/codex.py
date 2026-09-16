"""Portable, per-thread exports for Codex's paginated history store.

Only known history tables are read. No database files, migrations, configuration,
or credentials travel through Git. The receiving Codex binary creates its own
schema; imports replace rows for matching restored threads in one transaction.
"""

import json
import os
import select
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .files import SyncError, atomic_write, digest, encode, json_rows, read_stable, row_time, safe_path

DB_NAME = "thread_history_1.sqlite"
EXPORT_DIR = ".agent-sync-history"
TABLES = {
    "thread_turns": ("thread_id", "turn_id", "rollout_ordinal", "status", "error_json",
                     "started_at", "completed_at", "duration_ms", "first_user_item_id",
                     "final_agent_item_id", "rollout_byte_offset", "rollout_end_ordinal",
                     "rollout_end_byte_offset"),
    "thread_items": ("thread_id", "turn_id", "item_id", "rollout_ordinal", "created_at_ms",
                     "item_json", "item_type", "updated_at_ordinal"),
    "thread_history_projection_state": ("thread_id", "next_rollout_byte_offset", "next_rollout_ordinal"),
    "thread_realtime_items": ("thread_id", "item_id", "rollout_ordinal", "created_at_ms", "item_type", "item_json"),
}


def saved_names(root):
    """Latest indexed name for each session with a supported local rollout."""
    index = safe_path(root, "session_index.jsonl")
    if not index.is_file():
        return {}
    names = {}
    for row in json_rows(index.read_bytes(), index):
        thread_id, name = row.get("id"), row.get("thread_name")
        if not isinstance(thread_id, str) or not isinstance(name, str) or not name.strip():
            continue
        previous = names.get(thread_id)
        if previous is None or (row_time(row), json.dumps(row, sort_keys=True)) > (
                row_time(previous), json.dumps(previous, sort_keys=True)):
            names[thread_id] = row
    if not names:
        return {}
    present = set()
    for directory in ("sessions", "archived_sessions"):
        folder = safe_path(root, directory)
        for path in folder.rglob("*.jsonl"):
            path = safe_path(root, path.relative_to(root).as_posix())
            with path.open("rb") as stream:
                meta = json.loads(stream.readline()).get("payload", {})
            if meta.get("id") in names:
                present.add(meta["id"])
    return {thread_id: names[thread_id]["thread_name"] for thread_id in sorted(present)}


def restore_names(root, names=None):
    """Hydrate native picker names from the merged index without re-dating it.

    Pull has already selected the latest index entries across both machines.
    Native rename also appends to that index with today's timestamp, so retain
    the original bytes/time to avoid turning an old imported name into a new
    rename that could win a subsequent merge. Call only with agents closed.
    """
    names = saved_names(root) if names is None else names
    if not names:
        return 0
    from .native import CodexMetadata
    index = safe_path(root, "session_index.jsonl")
    original, stamp = read_stable(index)
    changed = 0
    try:
        with CodexMetadata(root) as native:
            for thread_id, name in names.items():
                thread = native.call("thread/read", {"threadId": thread_id, "includeTurns": False})["thread"]
                if thread.get("name") == name:
                    continue
                native.call("thread/name/set", {"threadId": thread_id, "name": name})
                updated = native.call("thread/read", {"threadId": thread_id, "includeTurns": False})["thread"]
                if updated.get("name") != name:
                    raise SyncError("Codex did not restore the saved name for " + thread_id)
                changed += 1
    finally:
        if read_stable(index) != (original, stamp):
            atomic_write(index, original, stamp)
    return changed


def check_schema(connection):
    for table, fields in TABLES.items():
        actual = {row[1] for row in connection.execute('PRAGMA table_info("{}")'.format(table))}
        if actual != set(fields):
            raise SyncError("Unsupported Codex history schema in {}. No history rows were imported.".format(table))


def export_history(root, files):
    sessions = []
    for rel, (data, stamp) in list(files.items()):
        if not rel.startswith(("sessions/", "archived_sessions/")):
            continue
        first = next((line for line in data.split(b"\n", 1) if line.strip()), b"{}")
        meta = json.loads(first).get("payload", {})
        if meta.get("history_mode") != "paginated":
            continue
        try:
            thread_id = str(uuid.UUID(meta.get("id", "")))
        except (ValueError, AttributeError):
            raise SyncError("Paginated Codex session has no valid UUID: {}".format(rel))
        sessions.append((rel, data, stamp, thread_id))
    if not sessions:
        return
    path = safe_path(root, DB_NAME)
    if not path.is_file():
        raise SyncError("Paginated Codex sessions require {} for a complete backup.".format(DB_NAME))
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN")  # Consistent read, including committed WAL records.
            check_schema(connection)
            for rel, data, stamp, thread_id in sessions:
                payload = {"version": 1, "thread_id": thread_id, "rollout_path": rel,
                           "rollout_sha256": digest(data), "tables": {}}
                for table, fields in TABLES.items():
                    rows = connection.execute('SELECT {} FROM "{}" WHERE thread_id=?'.format(
                        ",".join('"{}"'.format(f) for f in fields), table), (thread_id,))
                    payload["tables"][table] = sorted([dict(row) for row in rows], key=lambda row: json.dumps(row, sort_keys=True))
                projection = payload["tables"]["thread_history_projection_state"]
                if len(projection) != 1:
                    raise SyncError("Codex history is not indexed for {}. Open it in Codex, close Codex, and retry.".format(thread_id))
                if projection[0]["next_rollout_byte_offset"] > len(data):
                    raise SyncError("Codex history changed while snapshotting. Close Codex and retry.")
                export_rel = "{}/{}/{}.json".format(EXPORT_DIR, thread_id, payload["rollout_sha256"])
                files[export_rel] = (encode(payload), stamp)
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise SyncError("Cannot read Codex history: {}".format(exc))


def validate_export(relative, data):
    try:
        obj = json.loads(data)
        if not isinstance(obj, dict) or obj["version"] != 1:
            raise ValueError("unknown export format")
        thread_id = str(uuid.UUID(obj["thread_id"]))
        sha = obj["rollout_sha256"]
        if not isinstance(obj["rollout_path"], str) or not isinstance(sha, str):
            raise ValueError("invalid rollout reference")
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("invalid rollout hash")
        if relative != "{}/{}/{}.json".format(EXPORT_DIR, thread_id, sha):
            raise ValueError("export identity does not match filename")
        if not isinstance(obj["tables"], dict) or set(obj["tables"]) != set(TABLES):
            raise ValueError("unsupported tables")
        for table, fields in TABLES.items():
            rows = obj["tables"][table]
            if not isinstance(rows, list):
                raise ValueError("table rows must be a list")
            for row in rows:
                if not isinstance(row, dict) or set(row) != set(fields) or row["thread_id"] != thread_id:
                    raise ValueError("invalid or unrelated history row")
                if any(v is not None and type(v) not in (str, int) for v in row.values()):
                    raise ValueError("invalid history value")
                for field, value in row.items():
                    if field in ("rollout_ordinal", "created_at_ms", "updated_at_ordinal", "next_rollout_byte_offset", "next_rollout_ordinal"):
                        if type(value) is not int or value < 0:
                            raise ValueError("invalid history offset")
        if len(obj["tables"]["thread_history_projection_state"]) != 1:
            raise ValueError("missing projection state")
        return obj
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise SyncError("Invalid portable Codex history {}: {}".format(relative, exc))


def matching_exports(root, future_files):
    exports = []
    for rel, data in future_files.items():
        if not rel.startswith(EXPORT_DIR + "/"):
            continue
        obj = validate_export(rel, data)
        rollout = obj["rollout_path"]
        if not rollout.startswith(("sessions/", "archived_sessions/")) or not rollout.endswith(".jsonl"):
            raise SyncError("Invalid rollout path in Codex history export")
        target = safe_path(root, rollout)
        content = future_files.get(rollout)
        if content is None and target.is_file():
            content = target.read_bytes()
        if content is None or digest(content) != obj["rollout_sha256"]:
            continue  # An older or divergent snapshot must never replace this thread's database rows.
        meta = json.loads(content.split(b"\n", 1)[0]).get("payload", {})
        if meta.get("id") != obj["thread_id"] or meta.get("history_mode") != "paginated":
            raise SyncError("Codex history export does not match the restored session identity")
        if obj["tables"]["thread_history_projection_state"][0]["next_rollout_byte_offset"] > len(content):
            raise SyncError("Codex history projection exceeds its session log")
        exports.append(obj)
    covered = {obj["rollout_path"] for obj in exports}
    for rel, data in future_files.items():
        if rel.startswith(("sessions/", "archived_sessions/")) and rel.endswith(".jsonl"):
            meta = json.loads(data.split(b"\n", 1)[0]).get("payload", {})
            if meta.get("history_mode") == "paginated" and rel not in covered:
                # A newer local transcript may correctly win against an older
                # incoming snapshot. Its own existing database remains intact.
                current = safe_path(root, rel)
                if current.is_file() and digest(current.read_bytes()) == digest(data) and safe_path(root, DB_NAME).is_file():
                    continue
                raise SyncError("Paginated Codex history is missing for {}. Push again from the source with agent-sync 0.1.1+.".format(rel))
    by_id = {}
    for obj in exports:
        previous = by_id.get(obj["thread_id"])
        if previous is not None and previous["tables"] != obj["tables"]:
            raise SyncError("Divergent history exports share a Codex thread ID; resolve their active/archive locations first.")
        # Codex can retain several rollout files for the same thread. Keep all
        # logs, but import identical database rows only once.
        if previous is None or obj["rollout_path"] > previous["rollout_path"]:
            by_id[obj["thread_id"]] = obj
    return list(by_id.values())


def bootstrap(root):
    """Let the installed Codex version create/migrate its own database schema."""
    path = safe_path(root, DB_NAME)
    if path.exists():
        return
    if not shutil.which("codex"):
        raise SyncError("Install Codex CLI before restoring paginated Codex history.")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Codex lazily opens the history database on a paginated-thread read. Use a
    # disposable synthetic home to create its native schema without loading the
    # user's config or leaving a synthetic thread in their real session list.
    temporary = tempfile.TemporaryDirectory(prefix="agent-sync-codex-schema-")
    native_home = Path(temporary.name)
    seed_id = str(uuid.uuid4())
    seed = native_home / "sessions/2026/09/16" / ("rollout-2026-09-16T00-00-00-" + seed_id + ".jsonl")
    seed.parent.mkdir(parents=True)
    seed.write_text(json.dumps({"timestamp": "2026-09-16T00:00:00Z", "ordinal": 0, "type": "session_meta",
        "payload": {"id": seed_id, "timestamp": "2026-09-16T00:00:00Z", "cwd": str(native_home),
                    "source": "cli", "originator": "codex_cli_rs", "cli_version": "0.154.0",
                    "model_provider": "openai", "history_mode": "paginated"}}) + "\n")
    env = dict(os.environ, CODEX_HOME=str(native_home), HOME=str(native_home),
               XDG_CONFIG_HOME=str(native_home / "config"), XDG_DATA_HOME=str(native_home / "data"),
               XDG_CACHE_HOME=str(native_home / "cache"))
    for key in list(env):
        if "API_KEY" in key or "TOKEN" in key:
            env.pop(key)
    proc = subprocess.Popen(["codex", "app-server"], cwd=str(native_home), env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    try:
        request = {"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "agent_sync", "version": "0.1.1"}, "capabilities": {"experimentalApi": True}}}
        proc.stdin.write((json.dumps(request) + "\n").encode())
        deadline = time.monotonic() + 20
        pending = bytearray()
        ready = False
        while time.monotonic() < deadline:
            if select.select([proc.stdout], [], [], 0.25)[0]:
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                pending.extend(chunk)
                while b"\n" in pending:
                    line, _, rest = pending.partition(b"\n")
                    pending[:] = rest
                    response = json.loads(line)
                    if response.get("id") == 1:
                        if "error" in response:
                            raise SyncError("Codex could not initialize its local history store")
                        proc.stdin.write(b'{"method":"initialized"}\n')
                        proc.stdin.write((json.dumps({"id": 2, "method": "thread/read", "params": {
                            "threadId": seed_id, "includeTurns": True}}) + "\n").encode())
                    elif response.get("id") == 2:
                        if "error" in response:
                            raise SyncError("Installed Codex cannot initialize paginated history")
                        ready = True
            if ready and (native_home / DB_NAME).is_file():
                break
        if not ready or not (native_home / DB_NAME).is_file():
            raise SyncError("Codex did not create a supported history store within 20 seconds")
        with sqlite3.connect((native_home / DB_NAME).as_uri() + "?mode=ro", uri=True) as native:
            check_schema(native)
            with sqlite3.connect(str(path)) as destination:
                native.backup(destination)
                for table in TABLES:
                    destination.execute('DELETE FROM "{}" WHERE thread_id=?'.format(table), (seed_id,))
    except sqlite3.Error as exc:
        raise SyncError("Cannot initialize Codex history database: {}".format(exc))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        proc.stdin.close()
        proc.stdout.close()
        temporary.cleanup()


def prepare_import(root, exports):
    if not exports:
        return
    bootstrap(root)
    try:
        with sqlite3.connect(str(safe_path(root, DB_NAME))) as connection:
            check_schema(connection)
            # Validate constraints without touching local rows: every candidate
            # is inserted into an in-memory schema copied from known local DDL.
            with sqlite3.connect(":memory:") as scratch:
                for table in TABLES:
                    ddl = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
                    scratch.execute(ddl)
                write_rows(scratch, exports)
    except sqlite3.Error as exc:
        raise SyncError("Codex history import preflight failed: {}".format(exc))


def write_rows(connection, exports):
    for obj in exports:
        for table, fields in TABLES.items():
            connection.execute('DELETE FROM "{}" WHERE thread_id=?'.format(table), (obj["thread_id"],))
            sql = 'INSERT INTO "{}" ({}) VALUES ({})'.format(table,
                ",".join('"{}"'.format(f) for f in fields), ",".join("?" for _ in fields))
            connection.executemany(sql, [[row[f] for f in fields] for row in obj["tables"][table]])


def import_history(root, exports):
    if not exports:
        return
    try:
        with sqlite3.connect(str(safe_path(root, DB_NAME))) as connection:
            connection.execute("BEGIN IMMEDIATE")
            check_schema(connection)
            write_rows(connection, exports)
    except sqlite3.Error as exc:
        raise SyncError("Codex history import failed; safety backup is retained: {}".format(exc))
