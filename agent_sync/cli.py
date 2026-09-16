import argparse
import os
import shutil
import sys
from pathlib import Path

from . import __version__
from . import config, store, codex
from .files import (SyncError, atomic_write, collect, digest, merge_file,
                    read_stable, safe_path)


def parser():
    p = argparse.ArgumentParser(description="Sync Claude Code and Codex conversations through Git.")
    p.add_argument("--version", action="version", version="agent-sync " + __version__)
    p.add_argument("--state-dir", help="Local state directory (or AGENT_SYNC_HOME)")
    subs = p.add_subparsers(dest="command", required=True)
    init = subs.add_parser("init", help="Connect this machine to an empty or existing agent-sync data repo")
    init.add_argument("--remote", required=True, help="Private Git remote URL, or local bare Git repository")
    init.add_argument("--branch", default="main")
    init.add_argument("--claude-dir")
    init.add_argument("--codex-dir")
    init.add_argument("--encrypt", action="store_true", help="Enable git-crypt on a NEW data repository")
    init.add_argument("--key-file", type=config.absolute, help="New export path with --encrypt; existing key when cloning")
    for command, help_text in [
            ("push", "Snapshot local conversations and push to Git"),
            ("pull", "Merge remote conversations into local agent directories"),
            ("status", "Show configuration and cached snapshot counts"),
            ("backup", "Back up supported local conversation files"),
            ("restore", "Restore a named backup, first backing up current files")]:
        sub = subs.add_parser(command, help=help_text)
        selection(sub)
        if command in ("push", "pull", "restore"):
            sub.add_argument("--dry-run", action="store_true", help="Preview cached data without fetching or writing")
        if command == "restore":
            sub.add_argument("name", help="Backup name from the backups command")
    selection(init)
    subs.add_parser("backups", help="List local backups")
    audit = subs.add_parser("audit-conflicts", help="Read-only report of preserved conflicts using cached data")
    selection(audit)
    audit.add_argument("--json", action="store_true", help="Print a structured report to stdout")
    unlock = subs.add_parser("unlock", help="Unlock an encrypted sync checkout")
    unlock.add_argument("--key-file", type=config.absolute, required=True)
    return p


def selection(p):
    group = p.add_mutually_exclusive_group()
    group.add_argument("--tool", action="append", choices=config.TOOLS, help="Select one tool (repeatable)")
    group.add_argument("--all", action="store_true", help="Select both supported tools")


def selected(args, cfg=None):
    return sorted(set(config.TOOLS if getattr(args, "all", False) else
                      (getattr(args, "tool", None) or (cfg["tools"] if cfg else config.TOOLS))))


def plan_pull(repo, cfg, tools):
    incoming = store.read_snapshots(repo, tools)
    plan = []
    for tool in tools:
        root = Path(cfg[tool + "_dir"])
        local_exports = collect(root, tool) if tool == "codex" else {}
        for rel, versions in sorted(incoming[tool].items()):
            target = safe_path(root, rel)
            if tool == "codex" and rel.startswith(codex.EXPORT_DIR + "/"):
                local = local_exports.get(rel)
            else:
                local = read_stable(target) if target.exists() else None
            data, stamp, alternatives = merge_file(tool, rel, versions, local)
            changed = local is None or local[0] != data
            plan.append((tool, rel, data, stamp, alternatives, changed, local))
    return plan


def apply_pull(state, cfg, tools, plan, dry_run=False):
    changes = sum(item[5] for item in plan)
    conflicts = sum(bool(item[4]) for item in plan)
    future_codex = {rel: data for tool, rel, data, _, _, _, _ in plan if tool == "codex"}
    codex_root = Path(cfg["codex_dir"])
    exports = codex.matching_exports(codex_root, future_codex)
    print("{} files to update; {} files with divergent versions.".format(changes, conflicts))
    if dry_run:
        for tool, rel, _, _, alt, changed, _ in plan:
            if alt or changed:
                print("  {} {}/{}".format("CONFLICT" if alt else "UPDATE", tool, rel))
        return 2 if conflicts else 0
    if not changes and not conflicts:
        # A previous release may have copied the index without hydrating native
        # picker names. Retry metadata restoration even on a file-level no-op.
        names = codex.saved_names(codex_root) if "codex" in tools else {}
        if names:
            print("Backup: {}".format(store.backup(state, cfg, tools)))
            print("Restored Codex names for {} sessions.".format(codex.restore_names(codex_root, names)))
        return 0
    saved = store.backup(state, cfg, tools)
    print("Backup: {}".format(saved))
    codex.prepare_import(codex_root, exports)
    # Refuse to overwrite any file changed since the plan was built.
    current_exports = collect(codex_root, "codex") if exports else {}
    for tool, rel, _, _, _, _, local in plan:
        target = safe_path(Path(cfg[tool + "_dir"]), rel)
        if tool == "codex" and rel.startswith(codex.EXPORT_DIR + "/"):
            now = current_exports.get(rel)
        else:
            now = read_stable(target) if target.exists() else None
        if now != local:
            raise SyncError("Local data changed during pull; close the agent and retry: {}".format(target))
    for tool, rel, data, stamp, alternatives, changed, _ in plan:
        for other, other_stamp in alternatives:
            # Full alternatives remain outside agent data directories, so they
            # cannot be mistaken for duplicate sessions by either agent.
            rel_conflict = "conflicts/{}/{}/{}".format(tool, digest(rel.encode()), digest(other))
            folder = safe_path(state, rel_conflict)
            atomic_write(folder / "content", other, other_stamp)
            atomic_write(folder / "path.txt", (rel + "\n").encode())
        if changed:
            atomic_write(safe_path(Path(cfg[tool + "_dir"]), rel), data, stamp)
        if alternatives:
            print("CONFLICT: {}/{}; alternatives saved under {}".format(tool, rel, state / "conflicts"))
    codex.import_history(codex_root, exports)
    if exports:
        print("Restored paginated Codex history for {} sessions.".format(len(exports)))
    if "codex" in tools:
        print("Restored Codex names for {} sessions.".format(codex.restore_names(codex_root)))
    if conflicts:
        print("Review saved alternatives; your existing version was kept where present.")
    return 2 if conflicts else 0


def run(args, state):
    if args.command == "init":
        cfg = config.create(state, args.remote, args.branch, selected(args), args.claude_dir, args.codex_dir)
        store.initialize(state, cfg, args.encrypt, args.key_file)
        config.save(state, cfg)
        print("Initialized {} for {}.".format(state, ", ".join(cfg["tools"])))
        print("Close the agents, then run agent-sync push (or pull on an additional machine).")
        return 0
    cfg = config.load(state)
    repo = state / "repository"
    tools = selected(args, cfg)
    if args.command == "audit-conflicts":
        from .audit import build_report, print_report
        return print_report(build_report(state, cfg, tools), args.json)
    elif args.command == "unlock":
        store.crypt(repo, "unlock", str(args.key_file))
        store.validate_repo(repo)
        print("Sync checkout unlocked.")
    elif args.command == "status":
        snapshots = store.read_snapshots(repo, tools)
        print("agent-sync {}\nState: {}\nBranch: {}".format(__version__, state, cfg["branch"]))
        print("Encryption: {}".format("git-crypt" if store.validate_repo(repo).get("encrypted") else "off"))
        for tool in tools:
            root = Path(cfg[tool + "_dir"])
            print("{}: {} local files; {} cached paths; {}{}".format(
                tool, len(collect(root, tool)), len(snapshots[tool]), root,
                " (not present yet)" if not root.exists() else ""))
        print("Checkout: {}. Status uses cached data; it does not contact the remote.".format(
            "dirty" if store.git(repo, "status", "--porcelain").stdout.strip() else "clean"))
    elif args.command == "push":
        # Capture every source before modifying the checkout.
        snapshots = {}
        for tool in tools:
            root = Path(cfg[tool + "_dir"])
            if root.exists():
                snapshots[tool] = collect(root, tool)
            else:
                print("{}: skipped missing data directory {}.".format(tool, root))
        if args.dry_run:
            store.validate_repo(repo)
            for tool, files in snapshots.items():
                print("{}: would snapshot {} files (no fetch, writes, or push).".format(tool, len(files)))
            return 0
        store.refresh(repo, cfg)
        for tool, files in snapshots.items():
            store.write_snapshot(repo, cfg, tool, files)
            print("{}: snapshotted {} files.".format(tool, len(files)))
        store.commit(repo, "Sync {} from {}".format(", ".join(tools), cfg["machine_id"]))
        # Always push: a previous attempt may have committed successfully but
        # failed to reach the remote. Git rejects concurrent updates safely.
        store.git(repo, "push", "origin", "HEAD:refs/heads/" + cfg["branch"])
        print("Push complete.")
    elif args.command == "pull":
        if not args.dry_run:
            store.refresh(repo, cfg)
        else:
            print("Dry run: using cached snapshots; remote has not been fetched.")
        plan = plan_pull(repo, cfg, tools)
        result = apply_pull(state, cfg, tools, plan, args.dry_run)
        if not args.dry_run:
            print("Pull complete. Restart the agents to discover synced conversations.")
        return result
    elif args.command == "backup":
        print("Backup: {}".format(store.backup(state, cfg, tools)))
    elif args.command == "backups":
        backups = sorted((state / "backups").glob("*/manifest.json"))
        print("\n".join(p.parent.name for p in backups) or "No backups yet.")
    elif args.command == "restore":
        files = store.backup_files(state, args.name, tools)
        for tool, rel, _, _ in files:
            safe_path(Path(cfg[tool + "_dir"]), rel)
        exports = codex.matching_exports(Path(cfg["codex_dir"]), {rel: data for tool, rel, data, _ in files if tool == "codex"})
        print("{} files to restore; other files are retained.".format(len(files)))
        if args.dry_run or not files:
            return 0
        print("Safety backup: {}".format(store.backup(state, cfg, tools)))
        codex.prepare_import(Path(cfg["codex_dir"]), exports)
        for tool, rel, data, stamp in files:
            atomic_write(safe_path(Path(cfg[tool + "_dir"]), rel), data, stamp)
        codex.import_history(Path(cfg["codex_dir"]), exports)
        if "codex" in tools:
            print("Restored Codex names for {} sessions.".format(codex.restore_names(Path(cfg["codex_dir"]))))
        print("Restore complete.")
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    state = config.state_path(args.state_dir)
    os.umask(0o077)
    try:
        if not shutil.which("git"):
            raise SyncError("Git is required. Install Git and retry.")
        if getattr(args, "dry_run", False) or args.command in ("status", "backups", "audit-conflicts"):
            return run(args, state)
        with store.locked(state):
            return run(args, state)
    except (SyncError, OSError, ValueError) as exc:
        print("Error: {}".format(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. Any completed safety backup is retained.", file=sys.stderr)
        return 130
