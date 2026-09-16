# Changelog

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
