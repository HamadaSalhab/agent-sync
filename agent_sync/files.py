"""Allowlisted file access and loss-preserving merge policies."""

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


class SyncError(Exception):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encode(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def safe_path(root, relative):
    parts = PurePosixPath(relative).parts
    if (not parts or relative != PurePosixPath(relative).as_posix()
            or PurePosixPath(relative).is_absolute()
            or any(p in (".", "..", ".git") for p in parts)
            or "\\" in relative or "\x00" in relative):
        raise SyncError("Unsafe relative path: {!r}".format(relative))
    target = root
    if root.is_symlink():
        raise SyncError("Symlink directory is not allowed: {}".format(root))
    for part in parts:
        target = target / part
        if target.is_symlink():
            raise SyncError("Symlinks are not synced: {}".format(target))
    if root.resolve() not in target.resolve().parents:
        raise SyncError("Path escapes its data directory: {}".format(relative))
    return target


def atomic_write(path, data, mtime_ns=None):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".agent-sync-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mtime_ns is not None:
            os.utime(name, ns=(mtime_ns, mtime_ns))
        os.replace(name, str(path))
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_stable(path):
    before = path.stat()
    data = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
            after.st_size, after.st_mtime_ns, after.st_ino):
        raise SyncError("File changed while reading; close the agent and retry: {}".format(path))
    return data, after.st_mtime_ns


def allowed(tool, relative):
    p = PurePosixPath(relative)
    parts = p.parts
    if not parts:
        return False
    if relative == "history.jsonl":
        return True
    if tool == "claude":
        return len(parts) >= 2 and (
            (parts[0] == "projects" and p.suffix == ".jsonl")
            or parts[0] == "file-history"
            or (parts[0] == "todos" and p.suffix == ".json"))
    if tool == "codex":
        return (len(parts) == 3 and parts[0] == ".agent-sync-history" and p.suffix == ".json") or relative == "session_index.jsonl" or (
            len(parts) >= 2 and parts[0] in ("sessions", "archived_sessions")
            and p.suffix == ".jsonl")
    return False


def transcript(tool, rel):
    first = PurePosixPath(rel).parts[0]
    return rel.endswith(".jsonl") and (
        (tool == "claude" and first == "projects")
        or (tool == "codex" and first in ("sessions", "archived_sessions")))


def json_rows(data, label):
    try:
        rows = [json.loads(line) for line in data.splitlines() if line.strip()]
        if not all(isinstance(row, dict) for row in rows):
            raise ValueError("expected JSON objects")
        return rows
    except (ValueError, UnicodeError) as exc:
        raise SyncError("Invalid JSONL in {}: {}. Close the agent and retry.".format(label, exc))


def collect(root, tool):
    """Snapshot only explicitly supported files. Never follow directory symlinks."""
    result = {}
    if not root.exists():
        return result
    starts = ["history.jsonl"]
    starts += (["projects", "file-history", "todos"] if tool == "claude" else
               ["sessions", "archived_sessions", "session_index.jsonl"])
    for start in starts:
        top = safe_path(root, start)
        if not top.exists():
            continue
        candidates = [top] if top.is_file() else sorted(top.rglob("*"))
        for path in candidates:
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                raise SyncError("Symlinks are not synced: {}".format(path))
            if not path.is_file() or not allowed(tool, rel):
                continue
            safe_path(root, rel)
            data, stamp = read_stable(path)
            if transcript(tool, rel) or rel in ("history.jsonl", "session_index.jsonl"):
                json_rows(data, path)
            result[rel] = (data, stamp)
    if tool == "codex":
        from .codex import export_history
        export_history(root, result)
    return result


def row_time(row):
    value = row.get("updated_at", row.get("timestamp", row.get("ts", 0)))
    if isinstance(value, (int, float)):
        return float(value) / (1000 if value > 100000000000 else 1)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp()
        except ValueError:
            pass
    return 0


def merge_file(tool, rel, versions, local=None):
    """Return (chosen bytes, original mtime, divergent alternatives).

    A conversation can only be merged automatically when one version extends
    another. Divergent transcripts are never concatenated or line-sorted.
    """
    unique = {}
    for data, stamp in versions + ([local] if local is not None else []):
        key = digest(data)
        unique[key] = (data, max(stamp, unique.get(key, (None, 0))[1]))
    values = list(unique.values())
    if not values:
        raise SyncError("No versions for {}".format(rel))
    stamp = max(v[1] for v in values)
    parsed = None
    if transcript(tool, rel):
        parsed = [(v, json_rows(v[0], rel)) for v in values]
    if rel in ("history.jsonl", "session_index.jsonl"):
        rows = {}
        for data, _ in values:
            for row in json_rows(data, rel):
                canonical = json.dumps(row, sort_keys=True, separators=(",", ":"))
                key = canonical
                if rel == "session_index.jsonl":
                    if not isinstance(row.get("id"), str):
                        raise SyncError("Codex session index entry has no string id")
                    key = row["id"]
                old = rows.get(key)
                if old is None or (row_time(row), canonical) > (row_time(old), json.dumps(old, sort_keys=True, separators=(",", ":"))):
                    rows[key] = row
        ordered = sorted(rows.values(), key=lambda r: (row_time(r), json.dumps(r, sort_keys=True)))
        merged = b"".join((json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n").encode() for r in ordered)
        return merged, stamp, []
    if len(values) == 1:
        return values[0][0], values[0][1], []
    if parsed is not None:
        longest, rows = max(parsed, key=lambda pair: len(pair[1]))
        if all(rows[:len(other)] == other for _, other in parsed):
            return longest[0], longest[1], []
    # Keep the machine's own version on a real conflict. On a fresh machine use
    # a deterministic winner, but save every other candidate for recovery.
    chosen = local if local is not None else max(values, key=lambda v: (v[1], digest(v[0])))
    return chosen[0], chosen[1], [v for v in values if v[0] != chosen[0]]
