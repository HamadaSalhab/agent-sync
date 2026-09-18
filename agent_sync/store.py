"""Git transport, machine snapshots, and local recovery copies."""

import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import snapshot

from .files import (SyncError, allowed, atomic_write, collect, digest, encode,
                    safe_path)

FORMAT = {"format": "agent-sync", "version": 1}


def git(repo, *args, check=True):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_MERGE_AUTOEDIT="no")
    result = subprocess.run(["git", "-C", str(repo)] + list(args),
                            capture_output=True, text=True, env=env)
    if check and result.returncode:
        raise SyncError("git {} failed:\n{}".format(args[0], result.stderr.strip() or result.stdout.strip()))
    return result


@contextlib.contextmanager
def locked(state):
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state, 0o700)
    with (state / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError("Another agent-sync command is using {}".format(state))
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def crypt(repo, *args):
    if not shutil.which("git-crypt"):
        raise SyncError("Install git-crypt to use this encrypted sync repository.")
    p = subprocess.run(["git-crypt"] + list(args), cwd=str(repo), capture_output=True, text=True)
    if p.returncode:
        raise SyncError("git-crypt {} failed: {}".format(args[0], p.stderr.strip()))


def validate_repo(repo):
    if not (repo / ".git").is_dir():
        raise SyncError("Sync repository is missing. Run init in a new state directory.")
    marker = safe_path(repo, "agent-sync.json")
    try:
        data = json.loads(marker.read_text())
        if (not isinstance(data, dict) or data.get("format") != FORMAT["format"]
                or type(data.get("version")) is not int or data["version"] not in snapshot.VERSIONS):
            raise ValueError("unsupported format")
    except (OSError, ValueError) as exc:
        raise SyncError("Unsupported agent-sync data repository; update agent-sync: {}".format(exc))
    # A locked git-crypt checkout still contains the plaintext format marker.
    for path in (repo / "machines").glob("*/*/manifest.json"):
        safe_path(repo, path.relative_to(repo).as_posix())
        with path.open("rb") as f:
            if f.read(10).startswith(b"\x00GITCRYPT"):
                raise SyncError("Encrypted repository is locked. Run agent-sync unlock --key-file <path>.")
    return data


def require_clean(repo):
    if git(repo, "status", "--porcelain").stdout.strip():
        raise SyncError("Sync checkout has uncommitted changes: {}. Inspect and recover it before retrying.".format(repo))


def initialize(state, cfg, encrypt=False, key_file=None):
    repo = state / "repository"
    if repo.exists() or (state / "config.json").exists():
        raise SyncError("This state directory is already initialized; use push/pull or a new --state-dir.")
    if not cfg["remote"] or cfg["remote"].startswith("-"):
        raise SyncError("A Git remote URL or local bare repository path is required.")
    git(state, "check-ref-format", "--branch", cfg["branch"])
    temp = Path(tempfile.mkdtemp(prefix=".init-", dir=str(state)))
    try:
        result = subprocess.run(["git", "clone", "--no-checkout", "--", cfg["remote"], str(temp)],
                                capture_output=True, text=True,
                                env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
        if result.returncode:
            raise SyncError("Could not clone sync remote: {}".format(result.stderr.strip()))
        ref = "refs/remotes/origin/" + cfg["branch"]
        exists = git(temp, "show-ref", "--verify", "--quiet", ref, check=False).returncode == 0
        if exists:
            git(temp, "checkout", "-B", cfg["branch"], ref)
            marker = temp / "agent-sync.json"
            if not marker.is_file():
                raise SyncError("Remote has another layout. Use a new, empty repository for agent-sync.")
            data = json.loads(marker.read_text())
            if not isinstance(data, dict):
                raise SyncError("Remote data format marker must be a JSON object.")
            if data.get("encrypted"):
                if not key_file:
                    raise SyncError("Encrypted remote requires init --key-file <existing-key>.")
                crypt(temp, "unlock", str(key_file))
            elif encrypt:
                raise SyncError("Encryption must be enabled when creating an empty data repository.")
            validate_repo(temp)
        else:
            if git(temp, "for-each-ref", "--format=%(refname)", "refs/remotes").stdout.strip():
                raise SyncError("Requested branch is absent from a nonempty remote; check --branch.")
            git(temp, "symbolic-ref", "HEAD", "refs/heads/" + cfg["branch"])
            if encrypt:
                if key_file is None:
                    raise SyncError("--encrypt requires --key-file <new-key-path>.")
                if key_file.exists():
                    raise SyncError("New encryption key path already exists; choose an unused path.")
                if state == key_file or state in key_file.parents:
                    raise SyncError("Keep the encryption key outside the agent-sync state directory.")
                crypt(temp, "init")
                key_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                crypt(temp, "export-key", str(key_file))
                os.chmod(key_file, 0o600)
                atomic_write(temp / ".gitattributes", b"machines/** filter=git-crypt diff=git-crypt\n")
            atomic_write(temp / "agent-sync.json", encode(dict(FORMAT, encrypted=encrypt)))
            atomic_write(temp / "README.md", b"# agent-sync private conversation data\n\nManaged by agent-sync. Do not edit snapshots manually.\n")
            commit(temp, "Initialize agent-sync data repository")
            git(temp, "push", "origin", "HEAD:refs/heads/" + cfg["branch"])
        temp.rename(repo)
    finally:
        if temp.exists():
            shutil.rmtree(str(temp))


def refresh(repo, cfg):
    validate_repo(repo)
    require_clean(repo)
    branch = git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip()
    if branch != cfg["branch"]:
        raise SyncError("Sync checkout is on the wrong branch: {}".format(branch))
    git(repo, "fetch", "origin", "refs/heads/" + cfg["branch"])
    # Each machine owns a separate namespace, so concurrent commits can merge.
    # A genuine Git conflict stops the operation before local data is touched.
    git(repo, "-c", "user.name=agent-sync", "-c", "user.email=agent-sync@localhost",
        "merge", "--no-edit", "FETCH_HEAD")
    validate_repo(repo)


def commit(repo, message):
    git(repo, "add", "--all")
    if not git(repo, "diff", "--cached", "--quiet", check=False).returncode:
        return False
    git(repo, "-c", "user.name=agent-sync", "-c", "user.email=agent-sync@localhost",
        "commit", "-m", message)
    return True


def write_snapshot(repo, cfg, tool, files):
    root = safe_path(repo, "machines/{}/{}".format(cfg["machine_id"], tool))
    manifest = {"version": 1, "files": {}}
    for rel, (data, stamp) in sorted(files.items()):
        if not allowed(tool, rel):
            raise SyncError("Unsupported snapshot path: " + rel)
        manifest["files"][rel] = snapshot.write_file(root, rel, data, stamp)
    if any("chunks" in attrs for attrs in manifest["files"].values()):
        manifest["version"] = 2
        marker = validate_repo(repo)
        if marker["version"] == 1:
            marker["version"] = 2
            atomic_write(repo / "agent-sync.json", encode(marker))
            print("Large-file storage enabled. Update every machine to agent-sync 0.1.6 or newer before syncing.")
    target = safe_path(root, "manifest.json")
    data = encode(manifest)
    if not target.exists() or target.read_bytes() != data:
        atomic_write(target, data)


def read_snapshots(repo, tools):
    validate_repo(repo)
    result = {t: {} for t in tools}
    for machine in sorted((repo / "machines").glob("*")):
        safe_path(repo, machine.relative_to(repo).as_posix())
        try:
            if str(uuid.UUID(machine.name)) != machine.name:
                raise ValueError()
        except ValueError:
            raise SyncError("Invalid machine namespace: {}".format(machine.name))
        for tool in tools:
            root = safe_path(machine, tool)
            manifest = safe_path(root, "manifest.json")
            if not manifest.exists():
                continue
            try:
                meta = json.loads(manifest.read_text())
                if (not isinstance(meta, dict) or type(meta["version"]) is not int
                        or meta["version"] not in snapshot.VERSIONS or not isinstance(meta["files"], dict)):
                    raise ValueError("unsupported manifest")
                for rel, attrs in meta["files"].items():
                    if not allowed(tool, rel):
                        raise SyncError("Snapshot contains an unsupported file: {}/{}".format(tool, rel))
                    safe_path(root, "data/" + rel)
                    data, stamp = snapshot.read_file(root, rel, attrs, meta["version"])
                    result[tool].setdefault(rel, []).append((data, stamp))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise SyncError("Invalid snapshot {}: {}".format(manifest, exc))
    return result


GIT_BLOB_LIMIT = 100 * 1024 * 1024


def oversized_outgoing(repo, base):
    objects = git(repo, "rev-list", "--objects", "HEAD", "^" + base).stdout
    ids = [line.split(" ", 1)[0] for line in objects.splitlines()]
    if not ids:
        return []
    result = subprocess.run(["git", "-C", str(repo), "cat-file",
                             "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
                            input="\n".join(ids) + "\n", capture_output=True, text=True)
    if result.returncode:
        raise SyncError("Could not verify outgoing Git object sizes: " + result.stderr.strip())
    return [sha for sha, kind, size in (line.split() for line in result.stdout.splitlines())
            if kind == "blob" and int(size) > GIT_BLOB_LIMIT]


def check_push_size(repo):
    if oversized_outgoing(repo, "FETCH_HEAD"):
        raise SyncError("Unpublished Git history contains a file over 100 MiB. "
                        "Run agent-sync repair-large-files --push to preserve and repack a single "
                        "rejected snapshot. Adding another commit cannot remove an oversized historical blob.")


def repair_large_files(repo, cfg, push=False):
    """Repack exactly one unpublished own-machine snapshot; never force-push."""
    validate_repo(repo)
    require_clean(repo)
    if git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip() != cfg["branch"]:
        raise SyncError("Sync checkout is on the wrong branch")
    git(repo, "fetch", "origin", "refs/heads/" + cfg["branch"])
    base = git(repo, "rev-parse", "FETCH_HEAD").stdout.strip()
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    parents = git(repo, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
    if parents != [head, base]:
        raise SyncError("Repair requires exactly one unpublished snapshot directly after remote main. "
                        "No history was rewritten; retain your backup and review the pending commits.")
    namespace = "machines/" + cfg["machine_id"] + "/"
    changed = git(repo, "diff", "--name-only", base, head).stdout.splitlines()
    if any(not p.startswith(namespace) and p != "agent-sync.json" for p in changed):
        raise SyncError("Repair refuses a pending commit that changes another machine or unrelated files")
    if not oversized_outgoing(repo, base):
        raise SyncError("No oversized unpublished Git blobs need repair")
    # Validate every cached file before changing the representation. Do not read
    # live agent homes: the rejected snapshot is already a complete frozen input.
    snapshots = {}
    for tool in ("claude", "codex"):
        root = safe_path(repo, namespace + tool)
        manifest = safe_path(root, "manifest.json")
        if not manifest.exists():
            continue
        meta = json.loads(manifest.read_text())
        if not isinstance(meta, dict) or not isinstance(meta.get("files"), dict):
            raise SyncError("Invalid pending snapshot manifest")
        snapshots[tool] = {}
        for rel, attrs in meta["files"].items():
            if not allowed(tool, rel):
                raise SyncError("Unsupported pending snapshot path")
            safe_path(root, "data/" + rel)
            snapshots[tool][rel] = snapshot.read_file(root, rel, attrs, meta.get("version"))
    recovery = "refs/agent-sync/recovery/" + uuid.uuid4().hex
    git(repo, "update-ref", recovery, head)
    print("Original rejected snapshot preserved at {} ({})".format(recovery, head))
    for tool, files in snapshots.items():
        write_snapshot(repo, cfg, tool, files)
    git(repo, "add", "--", "agent-sync.json", namespace)
    git(repo, "-c", "user.name=agent-sync", "-c", "user.email=agent-sync@localhost",
        "commit", "--amend", "--no-edit")
    check_push_size(repo)
    if push:
        git(repo, "push", "origin", "HEAD:refs/heads/" + cfg["branch"])
        print("Push complete. Repaired the captured snapshot; live agent files were not read or changed.")
    else:
        print("Repair complete. The preserved snapshot has not been pushed.")
    return recovery


def backup(state, cfg, tools):
    # Read and validate the entire backup before creating any output.
    snapshots = {t: collect(Path(cfg[t + "_dir"]), t) for t in tools}
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex[:8]
    root = state / "backups" / name
    manifest = {"version": 1, "files": {}}
    for tool, files in snapshots.items():
        for rel, (data, stamp) in files.items():
            key = tool + "/" + rel
            atomic_write(safe_path(root, key), data, stamp)
            manifest["files"][key] = {"sha256": digest(data), "mtime_ns": stamp}
    atomic_write(root / "manifest.json", encode(manifest))
    return root


def backup_files(state, name, tools):
    if not re.fullmatch(r"[A-Za-z0-9.-]+", name) or name in (".", ".."):
        raise SyncError("Use a backup name from agent-sync backups.")
    root = safe_path(state, "backups/" + name)
    try:
        meta = json.loads(safe_path(root, "manifest.json").read_text())
        if not isinstance(meta, dict) or meta["version"] != 1 or not isinstance(meta["files"], dict):
            raise ValueError("unsupported backup")
        result = []
        for key, attrs in meta["files"].items():
            tool, rel = key.split("/", 1)
            if tool not in tools:
                continue
            if not allowed(tool, rel):
                raise ValueError("unsupported backup file")
            data = safe_path(root, key).read_bytes()
            if digest(data) != attrs["sha256"]:
                raise ValueError("checksum mismatch")
            stamp = attrs["mtime_ns"]
            if type(stamp) is not int or not 0 <= stamp <= 9223372036854775807:
                raise ValueError("invalid modification time")
            result.append((tool, rel, data, stamp))
        return result
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SyncError("Invalid backup {}: {}".format(name, exc))
