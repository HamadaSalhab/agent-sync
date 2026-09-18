# agent-sync

Sync **Claude Code and Codex conversations across your computers** through one private Git repository. Includes local backups, optional git-crypt encryption, and recovery copies when conversations diverge.

> **Fork attribution:** agent-sync is a fork and evolution of [Claude Code Conversation Sync](https://github.com/porkchop/claude-code-sync), created by **[porkchop](https://github.com/porkchop)** and its contributors. It builds on [HamadaSalhab's modified fork](https://github.com/HamadaSalhab/claude-code-sync). The original project established the Git-based conversation sync, backup, and encryption workflow. This repository preserves its Git history and original MIT copyright notice. Thank you to the original author and contributors. See [NOTICE.md](NOTICE.md).

**Version 0.1.6:** a command-line first release for macOS, Linux, and Windows through WSL. Conversation sync works separately within each tool; it does not convert Claude conversations into Codex conversations or vice versa.

## Install

Requires **Python 3.8+** and **Git**. Use **Python 3.12+** for full nanosecond timestamp precision on macOS; older Python builds can round restored times down to microseconds. No third-party Python runtime packages, API key, or paid service is required by agent-sync. Optional encryption requires `git-crypt`. Restoring paginated Codex sessions or saved conversation names also requires the Codex CLI for its native history schema and metadata operations.

### Homebrew (macOS and Linux)

```bash
brew trust --formula hamadasalhab/tap/agent-sync
brew install HamadaSalhab/tap/agent-sync
agent-sync --version
```

The `brew trust` step is required by Homebrew 7 for third-party formulae. Omit that line on older Homebrew versions without a `trust` command.

This installs the experimental **0.1.6** release from the project's own [Homebrew tap](https://github.com/HamadaSalhab/homebrew-tap). It is not a Homebrew/core package. Homebrew manages the Python runtime; install the Codex CLI separately when syncing Codex history or titles.

To update later:

```bash
brew update
brew upgrade HamadaSalhab/tap/agent-sync
```

Installing, upgrading, or uninstalling the tool does not initialize sync, change your machine identity, or upload or restore conversations. Existing configuration under `~/.local/share/agent-sync` is reused; **do not run `init` again on an already configured machine**. Keep your conversation-data repository private and separate from this public source repository.

### From source

From this source checkout:

```bash
./agent-sync --version
./agent-sync --help
```

Add the checkout to your shell's `PATH`, or install the CLI into a virtual environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/agent-sync --help
```

The examples below assume `agent-sync` is on your `PATH`.

## First machine

Create an **empty private Git repository** for conversation data on your preferred Git host. Keep it separate from the tool's source-code repository. Then:

```bash
agent-sync init --remote git@github.com:YOUR_USERNAME/agent-conversations.git

# Close Claude Code and Codex before syncing.
agent-sync backup
agent-sync push --all
```

`init` connects to the remote and creates the data format commit if it is empty. `push` takes a snapshot of both tools and uploads it. A tool that has not been used on this machine is skipped.

### With encryption

Enable encryption **before the first conversation push**, when initializing an empty remote:

```bash
agent-sync init \
  --remote git@github.com:YOUR_USERNAME/agent-conversations.git \
  --encrypt --key-file "$HOME/agent-sync.key"
agent-sync push --all
```

Keep the exported key in a password manager. The command does not print key contents. Choose a new key path outside the sync state directory. Encryption cannot be retroactively enabled on an existing data repository by this version.

## Additional machines

Install the tool, then connect to the same conversation remote:

```bash
agent-sync init --remote git@github.com:YOUR_USERNAME/agent-conversations.git

# For an encrypted remote, add: --key-file "$HOME/agent-sync.key"

# Close the agents first. Existing local conversations are backed up and merged.
agent-sync pull --all
```

Start each agent to find the synced conversations. In Codex CLI, `codex resume --all` searches across project directories; `codex resume <session-id>` selects a specific session. Sign in to each agent separately on each machine.

For a one-way migration, push from the source machine and pull on the destination. The source machine does not need to pull. Push from the destination only when you want to share work created there with the other machines.

## Daily use

```bash
# Before starting the agents:
agent-sync pull

# After closing the agents, before switching computers:
agent-sync push

agent-sync status
```

Default commands use the tools selected during `init` (both by default). Choose a subset when needed:

```bash
agent-sync push --tool claude
agent-sync pull --tool codex
agent-sync push --all
```

### Preview changes

```bash
agent-sync pull --dry-run
agent-sync push --dry-run
```

Dry runs perform **no fetch, write, commit, or push**. Pull previews use the already-cached checkout, so they cannot show changes that have not been fetched yet. Push previews report the number of supported files that would be snapshotted, not a remote diff.

## What is synced

| Tool | Files | Policy |
| --- | --- | --- |
| Claude Code | `projects/**/*.jsonl` | Conversation records, including nested subagent logs |
| Claude Code | `file-history/**`, `todos/**/*.json` | Saved file edits and todo records |
| Claude Code | `history.jsonl` | Deduplicate complete records and order by timestamp |
| Codex | `sessions/**/*.jsonl`, `archived_sessions/**/*.jsonl` | Native session logs |
| Codex | `history.jsonl` | Deduplicate complete records and order by timestamp |
| Codex | `session_index.jsonl` | Merge by session ID, keeping the latest named entry; restore native picker names on pull/restore |
| Codex | Per-thread JSON exports from `thread_history_1.sqlite` | Preserve paginated conversation turns and items; bind each export to its session-log checksum |

Credentials, `auth.json`, SQLite database files and their WAL files, caches, logs, browser data, settings, plugins, skills, rules, memories, and project source files are outside this release's allowlist. The Codex adapter exports only selected conversation rows, not complete databases. A conversation or saved file edit can itself contain secrets; the allowlist does not redact message contents.

### Codex compatibility

Codex stores its local data under `CODEX_HOME`, normally `~/.codex`. Its native `thread/list` operation supports scanning session logs and repairing metadata (`useStateDbOnly: false`). See the official [state-location documentation](https://learn.chatgpt.com/docs/config-file/config-advanced#config-and-state-locations) and [App Server documentation](https://learn.chatgpt.com/docs/app-server).

For **paginated** sessions, logs alone are insufficient. agent-sync reads the history store in a consistent read-only transaction and exports each thread's turns, items, realtime items, and projection position as JSON under `.agent-sync-history/`. Each export references the exact log checksum. On pull, Codex initializes a new machine's schema, then agent-sync validates and imports only matching threads in one SQLite transaction. Other threads are retained. Unknown schemas, missing history, or mismatched exports stop the operation. This adapter depends on internal Codex schema version 1 and is experimental.

Native integration tests cover both legacy `thread/read` and paginated `thread/turns/list` using isolated Codex homes. They send no model prompt and require no credentials. Compatibility is tested with **Codex CLI 0.154.0**. Run these tests against your installed version before relying on a different session format.

Saved names are applied through Codex's `thread/name/set` metadata operation after restoring files and history. The merged index determines the name, so newer local renames win over older remote entries. Native rename timestamps are not allowed to replace the original index timestamps. Names for sessions without supported local logs are retained in the index but not applied. This also repairs names imported with 0.1.1: close the agents and run `agent-sync pull --tool codex` again, then reopen the resume picker. Native name restoration does not start a model turn.

Full desktop-app state synchronization is outside v0.1: pins, project organization, running tasks, attachments stored outside session logs, and sessions without supported local logs are not copied. A desktop build may have additional discovery requirements; desktop UI visibility and interactive continuation are not covered by the automated tests.

## How conflicts work

### Large conversations

Version 0.1.6 stores files larger than 32 MiB as ordered, checksum-verified chunks in the Git snapshot. Pull reconstructs the exact original bytes and modification time before applying normal merge rules. Live transcripts and local backups remain complete files. This also covers large Claude files and paginated Codex exports, and works with git-crypt without Git LFS.

The first chunked snapshot upgrades the data repository to format version 2. **Update agent-sync to 0.1.6 or newer on every machine before syncing that repository.** Older clients refuse the new format. Version 0.1.6 still reads existing version-1 repositories and snapshots; machine identities, local configuration, and backup formats do not change.

If 0.1.5 already created a local snapshot whose push was rejected for a file over GitHub's 100 MiB limit, update the tool and run:

```bash
agent-sync repair-large-files --push
```

This narrowly scoped recovery command requires a clean checkout with exactly one unpublished snapshot directly after the current remote commit, changing only this machine's namespace (and optionally the format marker). It preserves the original commit under a local `refs/agent-sync/recovery/` reference, repacks its complete files, and amends only that unpublished commit. It never force-pushes or reads/writes live agent data. The `--push` flag uploads the captured snapshot; omit it to repair locally without pushing. It refuses more complex histories for review. A normal `push` also checks outgoing Git history for oversized blobs before contacting the push endpoint.

### Snapshot layout

The data repository contains a separate snapshot for each machine:

```text
agent-sync.json
machines/
  <machine-id>/
    claude/
      manifest.json       # hashes and original modification times
      data/projects/...
      chunks/<sha256>    # large-file pieces, referenced by the manifest
    codex/
      manifest.json
      data/sessions/...
      chunks/<sha256>
```

Every `init` generates a new machine ID. **Initialize each computer independently; do not copy `config.json` between machines.** Separate namespaces allow Git to merge pushes from different machines. Git's history retains earlier snapshots, and identical blobs are deduplicated by Git.

On pull:

1. Fetch and merge the remote. A Git failure stops the command before local agent files are changed.
2. Validate supported paths and file hashes, then plan all merges.
3. Back up supported local data before applying updates.
4. If one conversation is an exact extension of another, use the longer history regardless of clock differences.
5. If a conversation has different continuations, keep the existing local version and save the alternatives under `AGENT_SYNC_HOME/conflicts/`. On a new machine, select a deterministic version and save all alternatives.

A conflict exits with **code 2** and prints its path. Each recovery folder contains the complete alternative in `content` and its original relative path in `path.txt`. Inspect these copies before manually choosing a version. Transcripts are never concatenated or sorted together to invent a conversation.

Pulling is additive: it does not delete local files. **Deletions and archive/unarchive actions do not propagate reliably across machines in this version.** An archived session can have a stale active copy on another machine; clean up its active/archive location manually after syncing. Close the agents before pull, push, or restore; agent-sync's lock coordinates its own commands, not running agent processes.

## Read-only conflict audit

```bash
agent-sync audit-conflicts --tool codex
agent-sync audit-conflicts --tool codex --json > conflict-report.json
```

The audit compares **already-preserved alternatives** with their current local counterparts, grouped by conversation ID. It reads cached snapshots and portable history exports; it does not fetch, push, resolve conflicts, restore names, or change configuration, sessions, backups, recovery copies, or machine identity. Reports go to stdout. Native reads use disposable homes without credentials and never start model turns. Temporary copies can contain conversation content and are removed when the command finishes normally.

Each comparison reports one of:

- `equivalent_content`: matching active dialogue and compared archival/context records. **This does not mean the files are interchangeable.**
- `local_additional_content` / `alternative_additional_content`: one side has additional ordered content or instruction context.
- `divergent_content`: the compared content differs on both sides or has a different order.
- `inconclusive`: required evidence is missing, unsupported, ambiguous, or the native readers disagree in ways the audit cannot reconcile.

Separate sections report active turns, all raw response records (including tool calls/results and material omitted by the active reader), rollback markers, instruction context, undo snapshots, other session metadata, and unrecognized records. Undo and other metadata differences remain prominent even when the dialogue is equivalent. Summary counts are per conversation; a conversation with different findings across multiple alternatives is marked inconclusive, with every comparison retained. No message bodies are printed; reports include IDs, paths, counts, hashes, and recovery locations.

Instruction coverage treats absent/null instructions as empty and recognizes the legacy `instructions` / `base_instructions.text` representation. Changed text and non-null context fields remain significant. An initial instruction can also be covered by an exact initial system/developer message or an explicit instruction-only user block; arbitrary substrings, quoted dialogue, and later messages do not qualify. `instruction_storage` retains the original representation comparison, while `instruction_context` retains the combined normalized comparison and session instruction character counts. `session_instructions` separately reports header-text coverage (including `covered_elsewhere`), and `turn_context` compares complete ordered per-turn context with field-level counts/hashes. A preserved initial instruction does not establish equivalent historical context: truncation policies and per-turn instruction fields still affect the combined classification. Field-level summaries are diagnostic; they do not replace the ordered comparison of complete records.

Supported initial user envelopes include single- or double-newline `<user_instructions>` blocks and the exact `# AGENTS.md instructions for <path>\n\n<INSTRUCTIONS>\n...\n</INSTRUCTIONS>` form. Only the known envelope bytes are removed; instruction whitespace is retained. Ambiguous boundaries can remain unproven. Within `sandbox_policy` only, `mode` and `type` normalize to the same key when their values agree or only one is present; conflicting aliases and other policy fields remain significant.

Migration `item_completed` events are recognized only when their thread/turn/item identity and content match an exact-hash-bound history export that the native reader has independently verified. Known message, reasoning, command, MCP, and web-search representations are translated explicitly. Legacy image blocks map `image_url` to `url` with null `detail`; URLs must match byte-for-byte, and image order, duplicates, non-null detail, and extra fields are not discarded. Cumulative reasoning snapshots must preserve order and repeated text. Command arguments, paths, outputs, statuses, errors, and tool results remain checked; unknown fields or unsupported conversions stay inconclusive. Reports include covered/uncovered event counts and reasons, plus unrecognized record types. `findings` retains verified archival/context/undo differences even on an inconclusive comparison; `known_content_classification` summarizes the portions compared successfully. Neither is a resolution recommendation.

For paginated sessions, exports must match the exact thread ID, log path, SHA-256, and complete projection byte offset. Live local history is read from a disposable DB/WAL copy, never by opening the source database in SQLite. The audit pages through full native turns and checks exported turn/item coverage, order, statuses, errors, and payloads. It compares ordered sequences with duplicates intact. Generated user/assistant/reasoning item IDs, turn IDs, and turn timing are excluded from dialogue comparison; command IDs, nested fields, statuses, errors, tool arguments, and results are retained. Missing or changing inputs fail closed.

**Limits:** format-aware comparison currently supports Codex rollout conflicts and requires the Codex CLI (tested with 0.154.0). Claude and other file conflicts are listed as inconclusive. Native legacy readers can omit command items exposed by paginated readers: even if raw response records match, this version reports that mismatch as inconclusive rather than guessing a mapping. Realtime history rows, incomplete projections, differing unknown records, and ambiguous exports also prevent equivalence. The audit cannot prove that external attachments or undo snapshot targets exist. It does not retrieve old exports from Git history or backups if the cached/stored export is unavailable. Close agents for a stable audit; observed source changes abort the report.

Exit status is `0` for a completed, conclusive report (including additional/divergent content), `2` when any comparison is inconclusive/unsupported, and `1` for an input or command error. **No classification authorizes replacing a session or deleting an alternative.** Existing v1 snapshots and conflict folders remain compatible.

## Paths and configuration

```bash
agent-sync init --remote git@github.com:YOUR_USERNAME/agent-conversations.git \
  --claude-dir "$HOME/.claude" \
  --codex-dir "$HOME/.codex" \
  --branch main
```

| Setting | Default | Override |
| --- | --- | --- |
| Sync state | `~/.local/share/agent-sync` | `AGENT_SYNC_HOME` or global `--state-dir` |
| Claude data | `~/.claude` | `init --claude-dir`, then `CLAUDE_DATA_DIR` |
| Codex data | `~/.codex` | `init --codex-dir`, then `CODEX_HOME` |
| Selected tools | Both | `init --tool claude` / `--tool codex`; per-command `--tool` or `--all` |

Configuration is stored as JSON in `<state>/config.json`; it is never executed as shell code. Environment data-directory overrides take precedence over saved paths. The state and agent directories must not overlap. The global `--state-dir` option goes **before** the subcommand:

```bash
agent-sync --state-dir /some/private/location status
```

Project paths inside conversations are preserved. Use matching absolute project paths across machines for the most predictable continuation. This version does not rewrite usernames, working directories, or paths inside messages, and it does not transfer your actual project checkout or worktrees.

## Backup and restore

```bash
agent-sync backup --all
agent-sync backups
agent-sync restore BACKUP_NAME --dry-run
agent-sync restore BACKUP_NAME
```

Backups contain supported conversation files, portable Codex history exports, and a checksum manifest. Restore makes another safety backup first, then replaces the backed-up files and matching history rows while retaining unrelated conversations. Backups and conflict copies are local and **unencrypted**, even when the Git remote uses git-crypt. They are stored under a private state directory with owner-only permissions. There is no automatic retention cleanup in v0.1; remove old backups manually after verifying your recovery copies.

## Migrating from claude-code-sync

Your existing `~/.claude` data remains the source for the new tool. Create a new empty conversation remote, initialize agent-sync, and push those local conversations. If some conversations exist only in the old sync repository, restore them using the old tool first. Keep that repository as a backup.

The original flat conversation-repository layout is not accepted by agent-sync. The new source checkout retains the original commit history, but its CLI replaces the old `claude-*` scripts.

## Troubleshooting

- **Push rejected after another machine pushed:** run `agent-sync push` again. It fetches and merges first, and retries any previously committed snapshot even if local files have not changed.
- **Uncommitted changes in the sync checkout:** inspect `<state>/repository` with Git. Recover or commit the intended snapshot changes before retrying; the CLI never resets them automatically.
- **Encrypted checkout is locked:** run `agent-sync unlock --key-file /path/to/key`.
- **Malformed or changing JSONL:** close the agent and retry. Partial conversation writes are rejected before snapshot publication.
- **Missing conversations:** restart the agent; in Codex try `codex resume --all`. Check project paths and the compatibility limitations above.
- **Codex shows opening messages instead of saved titles:** upgrade to 0.1.2+, close the agents, and pull again. Name repair runs even if the conversation files are already current. If the native metadata service fails, the command reports the error; completed file restoration and its safety backup are retained, and another pull retries name repair.
- **A new machine has no agent data directory:** pull creates directories for transferred files. Push skips missing directories.

Exit codes: `0` success, `1` error, `2` preserved conflicts, `130` interrupted. Argument parsing also uses `2` for invalid command-line arguments.

## Development

```bash
python3 -m unittest discover -s tests -v
```

Tests use temporary agent homes and local bare Git remotes. Native Codex and git-crypt tests run when their binaries are installed; otherwise they report a skip. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## License and attribution

[MIT](LICENSE). Original work by porkchop and the Claude Code Sync contributors; subsequent changes by HamadaSalhab and agent-sync contributors. The upstream license notice and history are retained. This is an independent community project and is not affiliated with Anthropic or OpenAI.
