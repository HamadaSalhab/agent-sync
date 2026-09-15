# Contributing

agent-sync is a community fork of [porkchop/claude-code-sync](https://github.com/porkchop/claude-code-sync). Preserve the attribution in README.md, NOTICE.md, LICENSE, and the Git history.

## Development

Requires Python 3.8+ and Git. No third-party runtime dependencies.

```bash
python3 -m unittest discover -s tests -v
./agent-sync --help
```

Install `git-crypt` to exercise encryption tests. Install `codex` to exercise native session discovery and reading. Tests must use temporary homes, synthetic messages, and local remotes; never use a contributor's real agent state or credentials.

## Architecture

- `agent_sync/cli.py`: commands, planning, and applying local updates.
- `agent_sync/config.py`: JSON configuration, machine identity, data roots.
- `agent_sync/files.py`: tool allowlists, safe paths, stable reads, merge rules.
- `agent_sync/store.py`: Git snapshots, encryption, backups, and validation.
- `tests/`: two-machine integration tests and merge-policy tests.

When adding an adapter, explicitly define its transferable files, history merge rules, native discovery requirements, and exclusions. Include a round-trip test. Do not add whole-home directory copies or merge SQLite files using Git.

Before changing sync behavior, cover meaningful failure cases: independently extended sessions, timestamp differences, remote rejection, partial files, traversal, symlinks, and retries. Do not silently discard either side of a divergent conversation. A failed remote operation must not be reported as successful.

Update README.md and CHANGELOG.md for user-facing changes. Use conventional commit messages. Match the version in `agent_sync/__init__.py` and `pyproject.toml` when releasing. A source release and a conversation-data push are separate operations.
