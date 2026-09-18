# Changelog

## 0.1.6 — 2026-09-18

- Store files larger than 32 MiB in ordered, checksum-verified Git storage chunks and restore their exact bytes and modification times. Applies to both agents, portable Codex exports, and git-crypt repositories; no Git LFS dependency.
- Introduce format version 2 only when chunks are needed. Read existing version-1 snapshots; older clients refuse version 2 and must be upgraded on every machine. Local backup format, configuration, machine identity, and conflict policies are unchanged.
- Add `repair-large-files [--push]` to preserve and repack one rejected unpublished own-machine snapshot without touching live agent files or force-pushing. Reject complex/unrelated histories and check outgoing Git blobs against the 100 MiB hosting limit.
- Cover production chunk boundaries, corruption, missing/reordered chunks, path/symlink rejection, mixed snapshot versions, exact restoration, conflicts, encryption, native history, read-only audits, and rejected-push recovery with synthetic regression tests.

## 0.1.5 — 2026-09-17

- Recognize exact double-newline and AGENTS.md initial instruction envelopes without stripping instruction content. Normalize nonconflicting `sandbox_policy.mode`/`type` aliases only within sandbox policies.
- Verify legacy image-URL representations against native image items, retaining URL bytes, order, detail, and unknown fields. Unsupported events remain inconclusive.
- Separate session instruction-text coverage from ordered per-turn context evidence, including field-level counts for retained truncation policies and per-turn instructions. Preserve the combined classification and raw storage evidence.
- Add wrapper, policy, image, context-retention, and isolated native-image regressions. No sync policy, data format, configuration, or identity changes.

## 0.1.4 — 2026-09-17

- Compare instruction coverage across null fields, legacy instruction strings, text wrappers, and exact initial instruction carriers; retain raw storage evidence and meaningful context differences.
- Verify known migrated item-completion events against exact thread/turn/item identities and native-validated exports, including cumulative reasoning, command path representations, and tool results. Unknown records remain inconclusive with type/count diagnostics.
- Retain known-content classifications and archival/rollback/context/undo findings when another part of a comparison is inconclusive.
- Add synthetic regressions for the 32,374-character context and 120-versus-117 archival-record cases, covered native migration events, and altered/missing evidence. No sync policy, data format, configuration, or identity changes.

## 0.1.3 — 2026-09-16

- Add `audit-conflicts` with read-only text/JSON reports grouped by conversation ID, using preserved alternatives and cached snapshots. No source/data migration or automatic conflict resolution.
- Compare exact-hash-bound paginated exports and legacy sessions through isolated native readers; retain order, duplicate records, statuses, errors, and tool results. Check full exported turn/item coverage and detect incomplete or ambiguous evidence.
- Report archival response records, rollback markers, instruction context, undo metadata, and unknown records separately. Native representation mismatches remain inconclusive.
- Read live history through disposable DB/WAL copies and verify source bytes/timestamps remain stable; add synthetic and native regression coverage without model turns.

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
