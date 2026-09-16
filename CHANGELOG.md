# Changelog

## 0.1.2 — 2026-09-16

- Restore saved Codex conversation names through its native metadata API after pull and backup restore, so the resume picker shows transferred titles instead of opening messages.
- Repair names on repeated pulls even when conversation files are already current. Use the latest merged index entry, preserving newer local names and original rename timestamps; skip index entries without a supported local session log.
- Add native tests for picker names, older/newer remote names, metadata repair retries, dry-run behavior, backup restoration, archived legacy sessions, and missing Codex CLI errors. These tests run locally without model turns.

## 0.1.1 — 2026-09-16

- Real-data round-trip testing exposed the need to include Codex paginated history. Added per-thread JSON export of known conversation tables and checksum-bound transactional imports, preserving unrelated local threads.
- Added native paginated-history, mixed-machine history, missing-store, and thread-identity validation tests.
- Documented Python 3.12+ for exact nanosecond timestamps on macOS; older runtimes can round to microseconds.

## 0.1.0 — 2026-09-16

- Forked from porkchop's Claude Code Conversation Sync through HamadaSalhab's modified fork, preserving attribution, MIT license, and Git history.
- Added a unified `agent-sync` CLI for Claude Code and Codex.
- Added independent machine snapshots, original file times, strict allowlists, and checksum validation.
- Added history/index merging, append-only conversation extension detection, and preserved alternatives for divergent conversations.
- Added backups, restore, dry runs, status, optional git-crypt setup/unlock, and explicit remote failure reporting.
- Added isolated two-machine tests and optional native Codex/git-crypt integration tests.

Earlier Claude Code Sync changes remain available in the preserved Git history.
